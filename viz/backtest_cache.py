"""
Disk cache for one day's backtest (DayData), keyed so a STALE result can
never be served.

The dashboard's whole value is that the numbers on screen are the numbers the
strategy would produce right now. A cache that can go stale is worse than no
cache at all -- it turns "I changed the edge and the chart didn't move" from
an obvious bug into a silent wrong answer. So the key covers everything that
can change a result:

  * the QuotingConfig, field by field;
  * the CONTENT of every module that participates in producing the result
    (strategy, signal, pricing, engine, conflation, loader) -- hashed rather
    than mtime'd, so `touch` does not invalidate and a real edit always does;
  * a signature of every input data file for the event.

Anything outside that set is assumed not to affect the numbers. If that stops
being true -- a new module joins the pricing path, say -- it has to be added
to _CODE_GLOBS or the cache will happily serve the old answer.

Data files are fingerprinted by (size, mtime) rather than content: the
Databento quote parquets run to gigabytes and hashing them would cost more
than the backtest it is meant to save. The tradeoff is that a file rewritten
byte-identically with a preserved mtime would not invalidate -- which is not
a thing that happens to an immutable-once-fetched cache, but is the reason
this is a signature and not a proof.
"""
import dataclasses
import glob
import hashlib
import os
import pickle

_VIZ_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_VIZ_DIR, ".."))
CACHE_DIR = os.path.join(_ROOT, "data", "backtest_cache")

# Every module whose behaviour can change a backtest result.
_CODE_GLOBS = (
    "kalshi_backtest/*.py",
    "kalshi_latency_arbs/*.py",
    "kalshi_latency_arbs/spxw_kxinx/*.py",
    "kalshi_latency_arbs/spxw_kxinx/analysis/*.py",
    "kalshi_latency_arbs/spxw_kxinxu/*.py",
    "kalshi_infra/databento_functions.py",
    "kalshi_infra/black_scholes.py",
    "kalshi_infra/predexon_functions.py",
    "kalshi_infra/api_functions.py",
    "viz/data_loader.py",
)

# Input data for one event. Globbed rather than listed because which of these
# exist is exactly what decides the run's `source`/`fidelity`, so their
# presence or absence is itself part of the key.
_DATA_PATTERNS = (
    "data/kalshi_ws/{series}/raw_ob_ticks/{event}.parquet",
    "data/kalshi_ws/{series}/trades/{event}.parquet",
    "data/predexon/{series}/orderbooks/{event}.parquet",
    "data/kalshi_1min_historical/{series}/{event}.parquet",
    "data/databento/SPXW/quotes/{event}.parquet",
)

_code_cache: str | None = None


def code_fingerprint(refresh: bool = False) -> str:
    """SHA of the content of every module in _CODE_GLOBS.

    Memoised per process: a Streamlit rerun would otherwise re-read ~30 files
    on every widget change. A long-lived `streamlit run` therefore keeps the
    fingerprint it started with -- which is correct for the dashboard (you
    restart it to pick up code changes anyway, same rule as run_live) but is
    why the cache-warming script runs as a fresh process per event.
    """
    global _code_cache
    if _code_cache is not None and not refresh:
        return _code_cache
    h = hashlib.sha256()
    for pattern in _CODE_GLOBS:
        for path in sorted(glob.glob(os.path.join(_ROOT, pattern))):
            h.update(os.path.relpath(path, _ROOT).replace("\\", "/").encode())
            with open(path, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
    _code_cache = h.hexdigest()
    return _code_cache


def _data_fingerprint(event_ticker: str, series: str) -> str:
    h = hashlib.sha256()
    for pattern in _DATA_PATTERNS:
        path = os.path.join(_ROOT, pattern.format(series=series, event=event_ticker))
        if os.path.exists(path):
            st = os.stat(path)
            h.update(f"{pattern}:{st.st_size}:{int(st.st_mtime)}".encode())
        else:
            h.update(f"{pattern}:absent".encode())
    return h.hexdigest()


def cache_key(event_ticker: str, series: str, config) -> str:
    h = hashlib.sha256()
    h.update(f"{series}|{event_ticker}|".encode())
    cfg = dataclasses.asdict(config) if dataclasses.is_dataclass(config) else dict(vars(config))
    for k in sorted(cfg):
        h.update(f"{k}={cfg[k]!r};".encode())
    h.update(code_fingerprint().encode())
    h.update(_data_fingerprint(event_ticker, series).encode())
    return h.hexdigest()[:32]


def _path(event_ticker: str, series: str, key: str) -> str:
    return os.path.join(CACHE_DIR, series, f"{event_ticker}-{key}.pkl")


def load(event_ticker: str, series: str, config):
    """The cached DayData for this exact (event, config, code, data), or None."""
    p = _path(event_ticker, series, cache_key(event_ticker, series, config))
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        # A truncated or unreadable entry is a cache miss, never an error --
        # the only cost of being wrong here is recomputing.
        return None


def save(day, event_ticker: str, series: str, config) -> str:
    key = cache_key(event_ticker, series, config)
    p = _path(event_ticker, series, key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".partial"
    with open(tmp, "wb") as fh:
        pickle.dump(day, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, p)          # atomic: a reader never sees a half-written entry
    _prune(event_ticker, series, keep_key=key)
    return p


def _prune(event_ticker: str, series: str, keep_key: str) -> int:
    """Drop this event's entries under other keys. They are unreachable the
    moment the code or config changes, so keeping them just grows the
    directory without ever being read again."""
    n = 0
    for p in glob.glob(os.path.join(CACHE_DIR, series, f"{event_ticker}-*.pkl")):
        if not p.endswith(f"{keep_key}.pkl"):
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    return n
