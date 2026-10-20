"""Strategy performance from the day journals: every real fill, its markout
vs Kalshi mid, and settled PnL.

Incremental. Journal parts are immutable, so a refresh reads only new fills
parts and the bbo parts around fills that just became markable, a half hour
of book at a time and only for the traded tickers. A full day of bbo is ~3M
rows; reading it whole on the live box (3.8 GB, shared with trading) is what
OOM-killed the review queries on 2026-10-12.

    python -m kalshi_viz.perf --series KXCRYPTOLEAD15M      # print the metrics
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import glob
import json
import math
import os
import pickle
import re
import threading
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from kalshi_infra.config import atomic_write_json
from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_infra.trading.lip import payable_usd

# Markout horizons, seconds after the fill. "mo0" is the mid 1ms BEFORE the
# fill: the spread we captured.
HORIZONS = {"mo0": -0.001, "mo1": 1.0, "mo10": 10.0, "mo60": 60.0}
MAX_H = max(HORIZONS.values())
# bbo only records touch changes, so the mid in force at a fill can be this
# old in a quiet market.
LOOKBACK = pd.Timedelta("10min")
CHUNK = pd.Timedelta("30min")
# Crypto results were final ~3 min after close (2026-10-11).
SETTLE_WAIT = pd.Timedelta("4min")
SETTLE_RETRY_S = 60
SETTLE_PER_REFRESH = 25
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILL_KEY = ["exchange_order_id", "trade_id", "time", "price", "size"]
# A decision row is only written when something it records changes, so the
# last one before a print can be old and still current. This caps how old,
# so a print after a restart or gap isn't priced off the pre-gap theo. A
# safety bound, not a measurement.
THEO_TOL = pd.Timedelta("5min")
# Bump when Perf's accumulated state changes shape: a pickle from another
# version is discarded and the journals are rescanned.
STATE_VERSION = 1


@functools.lru_cache(maxsize=4096)
def event_close(ticker: str) -> pd.Timestamp | None:
    """Close from the ticker, which is stamped in New York time:
    KXCRYPTOLEAD15M-26OCT120300-BTC, KXINX-26OCT09H1600-B7662."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    try:
        t = pd.to_datetime(parts[1].replace("H", ""), format="%y%b%d%H%M")
    except ValueError:
        return None
    return pd.Timestamp(t).tz_localize("America/New_York").tz_convert("UTC")


def _days(series: str) -> list[str]:
    root = os.path.join(LIVE_ROOT, series)
    return sorted(d for d in os.listdir(root) if _DAY.match(d)) if os.path.isdir(root) else []


def _time_range(path: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """From the parquet row-group stats; falls back to reading the column.
    None for a part that can't be read yet (the newest can be mid-write)."""
    try:
        md = pq.ParquetFile(path).metadata
        i = md.schema.to_arrow_schema().get_field_index("time")
        lo = hi = None
        for g in range(md.num_row_groups):
            s = md.row_group(g).column(i).statistics
            if s is None or not s.has_min_max:
                raise ValueError("no stats")
            lo = s.min if lo is None else min(lo, s.min)
            hi = s.max if hi is None else max(hi, s.max)
        if lo is None:
            return None
        lo, hi = pd.Timestamp(lo), pd.Timestamp(hi)
    except Exception:
        try:
            t = pq.read_table(path, columns=["time"]).column("time").to_pandas()
        except Exception:
            return None
        if t.empty:
            return None
        lo, hi = pd.Timestamp(t.min()), pd.Timestamp(t.max())
    if lo.tzinfo is None:
        lo, hi = lo.tz_localize("UTC"), hi.tz_localize("UTC")
    return lo, hi


class Perf:
    """Per-series accumulator. `refresh()` is safe to call from several
    Streamlit sessions at once."""

    def __init__(self, series: str):
        self.series = series
        self.root = os.path.join(LIVE_ROOT, series)
        self._lock = threading.Lock()
        self._fill_parts: set[str] = set()
        self._keys: set[tuple] = set()
        self._pending: list[pd.DataFrame] = []
        self._done = pd.DataFrame()
        self._bbo: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
        self._last_mid: dict[str, float] = {}
        self._lip_parts: set[str] = set()
        self._lip: dict[str, tuple[pd.Timestamp, float]] = {}   # ticker -> (last time, accrued $)
        self._dec: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
        self._trade_parts: set[str] = set()
        self._prints_pending: list[pd.DataFrame] = []
        self._prints = pd.DataFrame(columns=["time", "ticker", "trade_id", "yes_price", "size", "theo"])
        self._settle_path = os.path.join(self.root, "settlements.json")
        self._settle: dict[str, str] = self._load_settlements()
        self._settle_tried: dict[str, float] = {}
        # Decisions part -> its live-row theo (time, ticker, theo). Parts are
        # written once, so each is read once; entries older than the window
        # being marked are dropped. Re-reading 5+ min of parts per 30s refresh
        # was ~31% of the dashboard's CPU (10-20).
        self._theo_cache: dict[str, pd.DataFrame] = {}

    # -- persistence ----------------------------------------------------------
    # A cold pass rescans every journal part: 64s on KXINXU and >11 min on
    # KXCRYPTOLEAD15M (25k bbo + 25k decisions parts, 10 days; 10-20). Keeping
    # the state across restarts makes a restart only catch up.

    _TRANSIENT = ("_lock", "_theo_cache", "_settle_tried")

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k not in self._TRANSIENT}

    def __setstate__(self, state):
        self.__dict__.update(state)
        # States saved before _price_prints dropped unpriced prints.
        if len(self._prints) and self._prints.theo.isna().any():
            self._prints = self._prints[self._prints.theo.notna()].reset_index(drop=True)
        self._lock = threading.Lock()
        self._theo_cache = {}
        self._settle_tried = {}

    def save(self, path: str) -> None:
        with self._lock:
            blob = pickle.dumps((STATE_VERSION, self), protocol=pickle.HIGHEST_PROTOCOL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)

    @classmethod
    def load(cls, series: str, path: str) -> "Perf":
        """The saved state if it is this version and this series, else fresh."""
        try:
            with open(path, "rb") as fh:
                version, p = pickle.load(fh)
            if version == STATE_VERSION and isinstance(p, cls) and p.series == series:
                p.root = os.path.join(LIVE_ROOT, series)
                p._settle_path = os.path.join(p.root, "settlements.json")
                return p
        except Exception:
            pass
        return cls(series)

    # -- settlement ---------------------------------------------------------

    def _load_settlements(self) -> dict[str, str]:
        try:
            with open(self._settle_path) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _update_settlements(self, tickers, limit: int | None = SETTLE_PER_REFRESH) -> None:
        from kalshi_infra.api_functions import kalshi_get
        now_t, now_m = pd.Timestamp.now(tz="UTC"), time.monotonic()
        due = [t for t in tickers if t not in self._settle and (c := event_close(t)) is not None
               and c + SETTLE_WAIT < now_t and now_m - self._settle_tried.get(t, -1e18) > SETTLE_RETRY_S]
        changed = False
        for t in due[:limit]:
            self._settle_tried[t] = now_m
            try:
                m = kalshi_get(f"/trade-api/v2/markets/{t}", max_retries=2).get("market", {})
            except Exception:
                continue
            if m.get("status") in ("finalized", "settled", "determined"):
                # "" marks a market that settled without a yes/no result, so it isn't re-asked.
                self._settle[t] = m.get("result") if m.get("result") in ("yes", "no") else ""
                changed = True
        if changed:
            try:
                tmp = self._settle_path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(self._settle, fh)
                os.replace(tmp, self._settle_path)
            except OSError:
                pass          # read-only: kept in memory, re-fetched after a restart

    # -- journal ------------------------------------------------------------

    def _scan(self) -> None:
        for d in _days(self.series):
            j = os.path.join(self.root, d, "journal")
            for p in sorted(glob.glob(os.path.join(j, "bbo", "part-*.parquet"))):
                if p not in self._bbo:
                    r = _time_range(p)
                    if r is not None:
                        self._bbo[p] = r
            for p in sorted(glob.glob(os.path.join(j, "fills", "part-*.parquet"))):
                if p in self._fill_parts:
                    continue
                try:
                    f = pd.read_parquet(p)
                except Exception:
                    continue          # mid-write; next refresh
                self._fill_parts.add(p)
                if f.empty or "mode" not in f.columns:
                    continue
                seeded = f["seeded"].fillna(False).astype(bool) if "seeded" in f.columns else False
                f = f[(f["mode"] == "real") & ~seeded]
                if f.empty:
                    continue
                for c in _FILL_KEY:
                    if c not in f.columns:
                        f[c] = None
                keys = list(f[_FILL_KEY].astype(str).itertuples(index=False, name=None))
                new = [k not in self._keys for k in keys]
                self._keys.update(keys)
                f = f[new]
                if "role" not in f.columns:
                    f["role"] = None
                if len(f):
                    self._pending.append(f[["time", "ticker", "side", "price", "size", "fee", "liquidity", "role"]])
            # Theo is only used for edge share, which needs the tape: days
            # before it was journaled would cost a stat read per decisions
            # part (7k parts, ~30s) for nothing.
            has_tape = os.path.isdir(os.path.join(j, "trades"))
            for p in sorted(glob.glob(os.path.join(j, "decisions", "part-*.parquet"))) if has_tape else ():
                if p not in self._dec:
                    r = _time_range(p)
                    if r is not None:
                        self._dec[p] = r
            for p in sorted(glob.glob(os.path.join(j, "trades", "part-*.parquet"))):
                if p in self._trade_parts:
                    continue
                try:
                    x = pd.read_parquet(p, columns=["time", "ticker", "trade_id", "yes_price", "size"])
                except Exception:
                    continue          # mid-write; next refresh
                self._trade_parts.add(p)
                if len(x):
                    self._prints_pending.append(x)
            self._scan_lip(j)

    def _scan_lip(self, journal: str) -> None:
        """Per-ticker LIP accrued while armed: the last real-mode decision
        row's lip_accrued_usd. The quoter's tracker is per event and resets
        at a re-arm (reset_session), so per ticker the max over the day's real
        rows is that event's accrual. Rows written before the "live" column
        existed are skipped (no way to tell shadow from real)."""
        for p in sorted(glob.glob(os.path.join(journal, "decisions", "part-*.parquet"))):
            if p in self._lip_parts:
                continue
            try:
                names = pq.read_schema(p).names
                if "lip_accrued_usd" not in names or "live" not in names:
                    self._lip_parts.add(p)
                    continue
                x = pq.read_table(p, columns=["time", "ticker", "lip_accrued_usd", "live"]).to_pandas()
            except Exception:
                continue              # mid-write; next refresh
            self._lip_parts.add(p)
            x = x[x["live"].fillna(False).astype(bool) & x["lip_accrued_usd"].notna()]
            for t, g in x.groupby("ticker"):
                v = float(g["lip_accrued_usd"].max())
                prev = self._lip.get(t)
                if prev is None or v > prev[1]:
                    self._lip[t] = (pd.Timestamp(g["time"].max()), v)

    def _book(self, lo, hi, tickers) -> pd.DataFrame:
        parts = [p for p, (a, b) in self._bbo.items() if b >= lo and a <= hi]
        out = []
        for p in parts:
            try:
                x = pq.read_table(p, columns=["time", "ticker", "bid", "ask"],
                                  filters=[("ticker", "in", list(tickers))]).to_pandas()
            except Exception:
                continue
            if len(x):
                out.append(x)
        out = [x.dropna(axis=1, how="all") for x in out]
        if not out:
            return pd.DataFrame(columns=["time", "ticker", "mid"])
        b = pd.concat(out, ignore_index=True)
        for c in ("bid", "ask"):
            if c not in b:
                b[c] = np.nan
        b = b[(b.time >= lo) & (b.time <= hi)].sort_values("time")
        b["mid"] = (b.bid + b.ask) / 2
        return b[["time", "ticker", "mid"]]

    def _theo(self, lo, hi, tickers) -> pd.DataFrame:
        """(time, ticker, theo) from the decisions journal; theo is NaN on
        shadow rows and rows with no usable fair (blocked, or parts written
        before the live column existed)."""
        empty = pd.DataFrame({"time": pd.Series(dtype="datetime64[ns, UTC]"),
                              "ticker": pd.Series(dtype=object), "theo": pd.Series(dtype=float)})
        for p in [p for p in self._theo_cache if self._dec.get(p, (lo, lo))[1] < lo]:
            del self._theo_cache[p]
        out = []
        for p, (a, b) in self._dec.items():
            if b < lo or a > hi:
                continue
            x = self._theo_cache.get(p)
            if x is None:
                try:
                    if not {"prob", "live"} <= set(pq.read_schema(p).names):
                        x = empty
                    else:
                        x = pq.read_table(p, columns=["time", "ticker", "prob", "live"]).to_pandas()
                        # Shadow rows stay, as NaN: they end the armed theo before them.
                        live = x["live"].fillna(False).astype(bool)
                        x = x.assign(theo=pd.to_numeric(x["prob"], errors="coerce").where(live))[
                            ["time", "ticker", "theo"]]
                except Exception:
                    continue          # mid-write; next refresh
                self._theo_cache[p] = x
            if len(x):
                out.append(x[x.ticker.isin(tickers)])
        out = [x for x in out if len(x)]
        if not out:
            return empty
        x = pd.concat(out, ignore_index=True)
        return x[(x.time >= lo) & (x.time <= hi)].sort_values("time")

    def _with_theo(self, f: pd.DataFrame) -> pd.Series:
        """Our theo in force at each row's time (aligned to f's index)."""
        t = self._theo(f.time.min() - THEO_TOL, f.time.max(), set(f.ticker))
        m = pd.merge_asof(f[["time", "ticker"]].reset_index().sort_values("time"), t, on="time", by="ticker",
                          direction="backward", tolerance=THEO_TOL)
        return m.set_index("index").sort_index()["theo"]

    def _mark(self, f: pd.DataFrame) -> pd.DataFrame:
        f = f.sort_values("time").reset_index(drop=True)
        f["yes_px"] = np.where(f.side == "yes", f.price, 1.0 - f.price)
        f["dir"] = np.where(f.side == "yes", 1.0, -1.0)
        b = self._book(f.time.min() - LOOKBACK, f.time.max() + pd.Timedelta(seconds=MAX_H + 1),
                       set(f.ticker))
        for name, h in HORIZONS.items():
            q = f[["time", "ticker"]].copy()
            q["time"] = q.time + pd.Timedelta(seconds=h)
            m = pd.merge_asof(q.reset_index().sort_values("time"), b, on="time", by="ticker",
                              direction="backward").set_index("index").sort_index()
            f[name] = (f.dir * (m.mid - f.yes_px) - f.fee / f["size"]) * 100
        f["theo"] = self._with_theo(f)
        if len(b):
            self._last_mid.update(b.groupby("ticker").mid.last().dropna().to_dict())
        return f

    def refresh(self, settle_all: bool = False) -> pd.DataFrame:
        """Every real fill so far, one row each: time, ticker, event, close,
        side, liquidity, size, yes_px, fee, mo0/mo1/mo10/mo60 and settled
        (cents per contract, fee included), pnl_c (settled, else marked
        at the latest mid) and theo (our armed fair at the fill, NaN if
        none). Settlements are fetched SETTLE_PER_REFRESH at a
        time unless `settle_all`."""
        with self._lock:
            self._scan()
            newest = max((b for _, b in self._bbo.values()), default=None)
            if self._pending and newest is not None:
                p = pd.concat(self._pending, ignore_index=True)
                ready = p.time + pd.Timedelta(seconds=MAX_H + 1) <= newest
                done = []
                for _, g in p[ready].groupby(p[ready].time.dt.floor(CHUNK)):
                    done.append(self._mark(g))
                if done:
                    self._done = pd.concat([self._done] + done, ignore_index=True)
                self._pending = [p[~ready]] if (~ready).any() else []
            self._price_prints()
            if newest is not None:
                recent = [pp for pp, (_, b) in self._bbo.items() if b >= newest - pd.Timedelta("2min")]
                for pp in recent:
                    try:
                        x = pq.read_table(pp, columns=["time", "ticker", "bid", "ask"]).to_pandas()
                    except Exception:
                        continue
                    x = x.sort_values("time")
                    self._last_mid.update(((x.bid + x.ask) / 2).groupby(x.ticker).last().dropna().to_dict())
            d = self._done
            if d.empty:
                return d.assign(**{"role": pd.Series(dtype=object)}) if len(d.columns) else d
            self._update_settlements(sorted(d.ticker.unique()), None if settle_all else SETTLE_PER_REFRESH)
            d = d.copy()
            d["event"] = d.ticker.str.rsplit("-", n=1).str[0]
            d["close"] = d.ticker.map(event_close)
            pay = d.ticker.map(self._settle).map({"yes": 1.0, "no": 0.0})
            d["settled"] = (d.dir * (pay - d.yes_px) - d.fee / d["size"]) * 100
            last = d.ticker.map(self._last_mid)
            d["open_c"] = (d.dir * (last - d.yes_px) - d.fee / d["size"]) * 100
            d["pnl_c"] = d.settled.where(d.settled.notna(), d.open_c)
            return d

    def _price_prints(self) -> None:
        """Attach our theo to every journaled public print once the decisions
        journal covers its time."""
        newest = max((b for _, b in self._dec.values()), default=None)
        if not self._prints_pending or newest is None:
            return
        p = pd.concat(self._prints_pending, ignore_index=True)
        ready = p.time <= newest
        done = []
        for _, g in p[ready].groupby(p[ready].time.dt.floor(CHUNK)):
            g = g.sort_values("time").reset_index(drop=True)
            done.append(g.assign(theo=self._with_theo(g)))
        # Only priced prints are kept: edge share skips the rest, and keeping
        # them grew KXBTC15M's tape to 2.3M rows (435 MB, 211k priced) and
        # OOM-killed alpha5-perf (10-20).
        done = [g[g.theo.notna()] for g in done]
        if any(len(g) for g in done):
            x = pd.concat(([self._prints] if len(self._prints) else []) + [g for g in done if len(g)],
                          ignore_index=True)
            # A print is journaled once per process; a restart mid-event can't
            # replay it, but two parts of one day can still overlap a reconnect.
            has_id = x.trade_id.notna()
            self._prints = pd.concat([x[has_id].drop_duplicates(["ticker", "trade_id"]), x[~has_id]],
                                     ignore_index=True).sort_values("time", ignore_index=True)
        self._prints_pending = [p[~ready]] if (~ready).any() else []

    def prints_frame(self) -> pd.DataFrame:
        """(time, ticker, trade_id, yes_price, size, theo): every public print
        on this series' markets that we had an armed theo for."""
        with self._lock:
            return self._prints.copy()

    def lip_frame(self) -> pd.DataFrame:
        """(time, ticker, event, usd): LIP payable per market while armed,
        stamped at the last armed decision row. Markets whose accrual is under
        Kalshi's $1.00 minimum are left out (they pay nothing), including ones
        still running, which appear once they cross it."""
        with self._lock:
            rows = [(t, tk, payable_usd(v)) for tk, (t, v) in self._lip.items() if payable_usd(v) > 0]
        if not rows:
            return pd.DataFrame(columns=["time", "ticker", "event", "usd"])
        df = pd.DataFrame(rows, columns=["time", "ticker", "usd"]).sort_values("time")
        df["event"] = df.ticker.str.rsplit("-", n=1).str[0]
        return df


_STORE: dict[str, Perf] = {}
_STORE_LOCK = threading.Lock()


def get(series: str) -> Perf:
    with _STORE_LOCK:
        if series not in _STORE:
            _STORE[series] = Perf(series)
        return _STORE[series]


# -- worker output ------------------------------------------------------------
# kalshi_viz.perf_worker refreshes every series in its own process and writes
# these; the dashboard only reads them. In-process refresh threads held the
# GIL long enough to stall Streamlit's websockets (10-20: 33% CPU and 2 GB
# RSS in the dashboard, browser reconnecting).

def out_dir(series: str) -> str:
    return os.path.join(LIVE_ROOT, series, "perf")


@dataclasses.dataclass
class Result:
    fills: pd.DataFrame
    lip: pd.DataFrame
    prints: pd.DataFrame
    updated: pd.Timestamp | None
    error: str | None


def write_result(p: Perf, fills: pd.DataFrame, error: str | None = None) -> None:
    d = out_dir(p.series)
    os.makedirs(d, exist_ok=True)
    for name, df in (("fills", fills), ("lip", p.lip_frame()), ("prints", p.prints_frame())):
        tmp = os.path.join(d, f"{name}.parquet.tmp-{os.getpid()}")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, os.path.join(d, f"{name}.parquet"))
    atomic_write_json(os.path.join(d, "meta.json"),
                      {"updated": pd.Timestamp.now(tz="UTC").isoformat(), "error": error})


def write_error(series: str, error: str) -> None:
    path = os.path.join(out_dir(series), "meta.json")
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError):
        meta = {"updated": None}
    atomic_write_json(path, {**meta, "error": error})


_RESULT_CACHE: dict[str, tuple[tuple, Result]] = {}


def read_result(series: str) -> Result | None:
    """The worker's latest output, re-read only when meta.json changes
    (written last). None until the worker's first pass for this series."""
    d = out_dir(series)
    meta_path = os.path.join(d, "meta.json")
    try:
        st_ = os.stat(meta_path)
        key = (st_.st_mtime_ns, st_.st_size)
        hit = _RESULT_CACHE.get(series)
        if hit is not None and hit[0] == key:
            return hit[1]
        with open(meta_path) as fh:
            meta = json.load(fh)
        frames = {n: pd.read_parquet(os.path.join(d, f"{n}.parquet")) for n in ("fills", "lip", "prints")}
    except (OSError, ValueError):
        hit = _RESULT_CACHE.get(series)
        return hit[1] if hit else None
    r = Result(updated=pd.Timestamp(meta["updated"]) if meta.get("updated") else None,
               error=meta.get("error"), **frames)
    _RESULT_CACHE[series] = (key, r)
    return r


def window(df: pd.DataFrame, since: pd.Timestamp | None) -> pd.DataFrame:
    return df if since is None or df.empty else df[df.time >= since]


def _wavg(x: pd.Series, w: pd.Series) -> float:
    ok = x.notna()
    return float(np.average(x[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else math.nan


def summarize(df: pd.DataFrame, since: pd.Timestamp | None = None) -> dict:
    """Window metrics. $ figures are sums; markouts are cents per contract."""
    now = pd.Timestamp.now(tz="UTC")
    d = window(df, since)
    if d.empty:
        return {"fills": 0}
    usd = d.pnl_c * d["size"] / 100
    ev = d.assign(usd=usd).groupby("event").usd.sum()
    cum = usd.fillna(0).cumsum()
    start = since if since is not None else d.time.min()
    hours = max((now - start).total_seconds() / 3600, 1e-9)
    out = {
        "fills": len(d), "taker": int((d.liquidity == "taker").sum()), "contracts": float(d["size"].sum()),
        "pnl": float(usd.sum()), "per_h": float(usd.sum() / hours), "hours": hours,
        "settled_usd": float((d.settled * d["size"] / 100).sum()),
        "open_usd": float((d.open_c * d["size"] / 100).where(d.settled.isna()).sum()),
        "unsettled": int(d.settled.isna().sum()), "fees": float(d.fee.sum()),
        "events": len(ev), "won": float((ev > 0).mean()) if len(ev) else math.nan,
        "t_event": float(ev.mean() / (ev.std(ddof=1) / math.sqrt(len(ev)))) if len(ev) > 2 and ev.std() > 0 else math.nan,
        "max_dd": float((cum - cum.cummax()).min()) if len(cum) else 0.0,
    }
    rows = {}
    role = d["role"] if "role" in d.columns else pd.Series(None, index=d.index)
    groups = [("all", d), ("maker", d[d.liquidity == "maker"]), ("taker", d[d.liquidity == "taker"])]
    if (role == "lip").any():
        groups += [("LIP quote", d[role == "lip"]), ("edge quote", d[(role != "lip") & (d.liquidity == "maker")])]
    for lab, g in groups:
        if len(g):
            rows[lab] = {"fills": len(g), **{c: _wavg(g[c], g["size"]) for c in (*HORIZONS, "pnl_c")}}
    out["markouts"] = rows
    return out


def _group(d: pd.DataFrame, hours: float) -> dict:
    if d.empty:
        return {"fills": 0, "contracts": 0.0, "pnl": 0.0, "per_h": 0.0, "taker": 0,
                **{c: math.nan for c in HORIZONS}, "mo10_usd": 0.0, "mo60_usd": 0.0}
    usd = float((d.pnl_c * d["size"] / 100).sum())
    return {"fills": len(d), "contracts": float(d["size"].sum()), "pnl": usd, "per_h": usd / hours,
            "taker": int((d.liquidity == "taker").sum()), **{c: _wavg(d[c], d["size"]) for c in HORIZONS},
            **{f"{c}_usd": float((d[c].fillna(0) * d["size"] / 100).sum()) for c in ("mo10", "mo60")}}


def summarize_split(df: pd.DataFrame, lip: pd.DataFrame | None = None,
                    since: pd.Timestamp | None = None) -> dict:
    """{"all", "regular", "lip"}: fills, contracts, pnl ($, settled else open
    at mid), per_h, taker and markouts (c/contract, fee incl.) per group.

    regular is every fill not on the LIP quote, takes included, so regular +
    lip = all. Fills from before the role column existed can't be told apart
    and count as regular. With `lip` (accrual frame), "lip" also gets reward
    (an estimate from the scoring replica, not money received), net = reward
    + LIP-fill PnL, net_mo10 / net_mo60 = reward + the fills' 10s / 60s
    markout $, and reward_c per LIP lot filled; "all" gets pnl_incl_lip."""
    now = pd.Timestamp.now(tz="UTC")
    d = window(df, since)
    L = window(lip, since) if lip is not None and len(lip) else None
    times = [x.time.min() for x in (d, L) if x is not None and len(x)]
    start = since if since is not None else (min(times) if times else now)
    hours = max((now - start).total_seconds() / 3600, 1e-9)
    is_lip = (d["role"] == "lip") if len(d) and "role" in d.columns else pd.Series(False, index=d.index)
    out = {"all": _group(d, hours), "regular": _group(d[~is_lip], hours), "lip": _group(d[is_lip], hours)}
    if lip is not None:
        reward = float(L.usd.sum()) if L is not None else 0.0
        g = out["lip"]
        g.update(reward=reward, reward_per_h=reward / hours, net=reward + g["pnl"],
                 net_mo10=reward + g["mo10_usd"], net_mo60=reward + g["mo60_usd"],
                 markets=len(L) if L is not None else 0,
                 reward_c=100 * reward / g["contracts"] if g["contracts"] else math.nan)
        out["all"]["pnl_incl_lip"] = out["all"]["pnl"] + reward
    return out


def summarize_edge(df: pd.DataFrame, prints: pd.DataFrame, since: pd.Timestamp | None = None) -> dict:
    """Our edge vs theo against all the edge vs theo that traded.

    Every print has a buyer of YES at `yes_price` and a buyer of NO at
    1 - yes_price; whichever side bought below our theo took
    size x |theo - yes_price|, so that sum is the market's total edge.
    Ours is our fills' signed edge (negative when we bought above theo). Both
    are before fees, counted only while armed with a usable theo, and our
    fills are on the tape so they sit inside the market total.

    share: our edge / market edge (can go negative). vol_share: our
    contracts / market contracts over the same priced prints."""
    P = window(prints, since)
    P = P[P.theo.notna()] if len(P) else P
    d = window(df, since)
    d = d[d.theo.notna()] if len(d) and "theo" in d.columns else d.iloc[0:0]
    mkt = float((P["size"] * (P.theo - P.yes_price).abs()).sum()) if len(P) else 0.0
    mkt_lots = float(P["size"].sum()) if len(P) else 0.0
    edge = d.dir * (d.theo - d.yes_px) * d["size"] if len(d) else pd.Series(dtype=float)
    ours = float(edge.sum()) if len(d) else 0.0
    lots = float(d["size"].sum()) if len(d) else 0.0
    out = {"prints": len(P), "market_usd": mkt, "market_lots": mkt_lots, "ours_usd": ours, "our_lots": lots,
           "share": ours / mkt if mkt > 0 else math.nan,
           "vol_share": lots / mkt_lots if mkt_lots > 0 else math.nan,
           "market_c": 100 * mkt / mkt_lots if mkt_lots else math.nan,
           "ours_c": 100 * ours / lots if lots else math.nan}
    if len(d):
        for lab, g in (("maker", d.liquidity == "maker"), ("taker", d.liquidity == "taker")):
            out[f"{lab}_usd"] = float(edge[g].sum())
    return out


def curves(df: pd.DataFrame, since: pd.Timestamp | None = None, lip: pd.DataFrame | None = None) -> pd.DataFrame:
    """Long frame (time, line, usd): cumulative PnL and 1s/10s markout $ by
    fill time; with `lip`, also cumulative LIP reward and PnL incl. LIP."""
    d = window(df, since)
    L = window(lip, since) if lip is not None and len(lip) else None
    # A series with no real fills has a column-less empty frame.
    if d.empty and L is None:
        return pd.DataFrame(columns=["time", "line", "usd"])
    times = [x.time.min() for x in (d, L) if x is not None and len(x)]
    start = since if since is not None else min(times)
    out = []
    if len(d):
        d = d.sort_values("time")
        lines = {"PnL (settled; open at mid)": d.pnl_c, "1s markout": d.mo1, "10s markout": d.mo10}
        for name, c in lines.items():
            v = (c.fillna(0) * d["size"] / 100).cumsum()
            out.append(pd.DataFrame({"time": [start] + list(d.time), "line": name, "usd": [0.0] + list(v)}))
    if L is not None:
        L = L.sort_values("time")
        out.append(pd.DataFrame({"time": [start] + list(L.time), "line": "LIP reward",
                                 "usd": [0.0] + list(L.usd.cumsum())}))
        ev = pd.concat([d[["time"]].assign(v=d.pnl_c.fillna(0) * d["size"] / 100) if len(d) else None,
                        L[["time"]].assign(v=L.usd)]).sort_values("time")
        out.append(pd.DataFrame({"time": [start] + list(ev.time), "line": "PnL incl. LIP",
                                 "usd": [0.0] + list(ev.v.cumsum())}))
    return pd.concat(out, ignore_index=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXCRYPTOLEAD15M")
    args = ap.parse_args()
    t0 = time.time()
    df = get(args.series).refresh(settle_all=True)
    print(f"{len(df)} real fills marked in {time.time() - t0:.1f}s")
    now = pd.Timestamp.now(tz="UTC")
    for lab, since in (("1h", now - pd.Timedelta(hours=1)), ("6h", now - pd.Timedelta(hours=6)),
                       ("24h", now - pd.Timedelta(hours=24)), ("all", None)):
        s = summarize(df, since)
        if not s.get("fills"):
            print(f"\n{lab}: no fills")
            continue
        print(f"\n{lab}: PnL ${s['pnl']:+.2f} (${s['per_h']:+.2f}/h; settled ${s['settled_usd']:+.2f}, "
              f"open ${s['open_usd']:+.2f} on {s['unsettled']} fills), {s['events']} events "
              f"({s['won']:.0%} won, t={s['t_event']:+.2f}), {s['fills']} fills ({s['taker']} taker), "
              f"{s['contracts']:.0f} contracts, fees ${s['fees']:.2f}, max drawdown ${s['max_dd']:.2f}")
        print(pd.DataFrame(s["markouts"]).T.round(2).to_string())


if __name__ == "__main__":
    main()
