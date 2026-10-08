"""
Databento API functions -- tick-level (cmbp-1 top-of-book) historical S&P 500
options quotes, via the OPRA.PILLAR dataset. Used to compare against Kalshi
KXINX range-market BBO in kalshi_latency_arbs (does Kalshi's quote lag the
options-implied probability, or vice versa?).

Schema is "cmbp-1", not the plain "mbp-1" used on single-venue datasets like
GLBX.MDP3 -- confirmed directly (a live 422 error) that OPRA.PILLAR only
supports ['cmbp-1', 'cbbo-1s', 'cbbo-1m', 'tcbbo', 'trades', 'ohlcv-*',
'statistics', 'status', 'definition']. Makes sense in hindsight: OPRA is a
SIP feed consolidating quotes across 17 exchanges, so its top-of-book schema
is the *consolidated* MBP-1 variant. Same bid_px_00/ask_px_00/bid_sz_00/
ask_sz_00 field names as plain mbp-1 though (confirmed against
databento_dbn's CMBP1Msg/ConsolidatedBidAskPair).

Unlike Predexon (flat-rate subscription, see predexon_functions.py),
Databento bills per GB of historical data pulled -- every fetch below is
gated by a metadata.get_cost() quote checked against max_cost_usd *before*
spending, and every result is cached to parquet so re-running the same
window is a free local read, same immutable-once-fetched convention as
predexon_functions.py.

Historical prices/strikes come back from databento's to_df() already as
floats in dollars (price_type="float" is its default) -- no manual 1e-9
fixed-point scaling needed here, confirmed directly against the installed
databento_dbn package's InstrumentDefMsg/CMBP1Msg field docs and
DBNStore.to_df's docstring (C:\\Python312\\Lib\\site-packages\\databento_dbn\\_lib.pyi,
C:\\Python312\\Lib\\site-packages\\databento\\common\\dbnstore.py).
"""
import datetime
import os
import re
from zoneinfo import ZoneInfo

import databento as db
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from keys import DATABENTO_API_KEY as API_KEY

DATASET = "OPRA.PILLAR"
ET = ZoneInfo("America/New_York")

_DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
DATABENTO_ROOT = os.path.join(_DATA_ROOT, "databento")

# Safety default for fetch_option_quotes/load_option_quotes_long_df -- an
# uncached pull that quotes above this raises DatabentoCostGuardTripped
# instead of spending silently. Callers doing a deliberate large backfill
# should pass a higher value explicitly.
_MAX_AUTO_COST_USD = 5.0


def _definitions_dir(underlying: str) -> str:
    return os.path.join(DATABENTO_ROOT, underlying, "definitions")


def _quotes_dir(underlying: str) -> str:
    return os.path.join(DATABENTO_ROOT, underlying, "quotes")


class DatabentoCostGuardTripped(Exception):
    """Raised when an uncached historical pull's estimated cost exceeds max_cost_usd."""


_client = None


def _get_client() -> db.Historical:
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "DATABENTO_API_KEY is not set in keys.py -- sign up at databento.com "
                "(new accounts get $125 in free credit) and paste your key in keys.py"
            )
        _client = db.Historical(key=API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Instrument definitions (which strikes/expiries exist)
# ---------------------------------------------------------------------------

def get_option_definitions(underlying: str, date: datetime.date, cache_dir: str = None,
                            dataset: str = DATASET) -> pd.DataFrame:
    """
    Every option instrument definition listed under `underlying` as of
    `date` (e.g. underlying="SPXW" for the daily-expiry, PM-settled series
    that KXINX's hourly-close markets actually match -- plain "SPX" only
    has monthly/quarterly expiries), via Databento's definition schema
    (stype_in='parent', symbols='{underlying}.OPT', matching the
    dataset='GLBX.MDP3', symbols='ES.FUT', stype_in='parent' convention
    from databento's own README).

    Cached to data/databento/{underlying}/definitions/{date}.parquet,
    treated as immutable once fetched -- same convention as
    predexon_functions.get_event_markets, since a past date's listed chain
    never changes in hindsight.

    Returns columns: raw_symbol, strike_price (float dollars), expiration
    (tz-aware UTC timestamp -- the contract's last eligible trade time, i.e.
    its expiry), instrument_class ('C'/'P'/etc, see
    databento_dbn.InstrumentClass; confirmed directly InstrumentClass.CALL.value
    == 'C', .PUT.value == 'P').
    """
    if cache_dir is None:
        cache_dir = _definitions_dir(underlying)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{date.isoformat()}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)

    client = _get_client()
    start = datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.timezone.utc)
    # Clamped to "now" (minus a safety buffer) for `date` == today (or,
    # degenerately, any date whose UTC day hasn't finished yet): the
    # dataset has no data past the current moment, and requesting past it
    # 422s outright (confirmed directly: "data_end_after_available_end"
    # querying SPXW definitions for the same day, ~30min after that day's
    # KXINX close but still ~3.5h before the naive end-of-UTC-day
    # `start + 1 day` this used to send unconditionally). The buffer is
    # needed on top of a bare `now` clamp too -- confirmed directly
    # Databento's own historical ingestion runs ~20min behind true "now"
    # (a retry using wall-clock `now` as `end` still 422'd, with the error
    # itself reporting real availability as ~20min earlier). Defs are
    # static once listed for the day, so trading off "how current" for
    # "won't 422" costs nothing here -- a no-op for any date that's
    # actually finished, since `now - buffer` is already past
    # `start + 1 day` then.
    _INGESTION_LAG_BUFFER = datetime.timedelta(hours=1)
    end = min(start + datetime.timedelta(days=1),
              datetime.datetime.now(datetime.timezone.utc) - _INGESTION_LAG_BUFFER)
    end = max(end, start)  # never invert the range querying `date` in the hour right after UTC midnight
    data = client.timeseries.get_range(
        dataset=dataset,
        symbols=[f"{underlying}.OPT"],
        stype_in="parent",
        schema="definition",
        start=start,
        end=end,
    )
    df = data.to_df()
    cols = ["raw_symbol", "strike_price", "expiration", "instrument_class"]
    df = df[[c for c in cols if c in df.columns]].drop_duplicates(subset=["raw_symbol"]).reset_index(drop=True)
    df.to_parquet(cache_path, index=False)
    print(f"  [databento] {len(df)} instrument definitions for {underlying} @ {date} -> {cache_path}")
    return df


# ---------------------------------------------------------------------------
# Tick-level quotes
# ---------------------------------------------------------------------------

_MAX_BATCH_BILLABLE_BYTES = 2_000_000_000  # ~2.0GB raw DBN per get_range call -- see fetch_option_quotes.
# Raised from an earlier, more conservative 1.3GB once fetch_option_quotes
# started streaming each batch straight to a ParquetWriter instead of
# accumulating trimmed DataFrames for one big pd.concat at the end --
# confirmed directly *that* accumulation, not any single batch's spike, was
# what pushed a 5-batch pull over the edge even though each individual
# batch's spike alone had recovered fine. With streaming, each batch's
# spike is independent of every other one, so there's no accumulation risk
# left to budget extra margin against.
#
# Confirmed directly a 1.2-1.3GB raw batch's to_df() spikes resident memory
# to ~4-5GB (roughly a 3.5-4x inflation from raw DBN bytes to pandas'
# in-memory representation) -- this cap leaves real headroom on an
# 8GB-free machine instead of running right up against the edge.


def fetch_option_quotes(
    underlying: str,
    raw_symbols: list[str],
    start: datetime.datetime,
    end: datetime.datetime,
    cache_key: str,
    cache_dir: str = None,
    max_cost_usd: float = _MAX_AUTO_COST_USD,
    columns: list[str] = None,
    symbol_batch_size: int = 65,
    dataset: str = DATASET,
    schema: str = "cmbp-1",
    symbols: list[str] = None,
) -> pd.DataFrame:
    """
    Fetch tick-level top-of-book quotes+trades (cmbp-1) for specific raw
    symbols over [start, end] and cache to
    data/databento/{underlying}/quotes/{cache_key}.parquet.

    `dataset`/`schema` default to OPRA.PILLAR/cmbp-1 (SPXW/SPX, a
    consolidated-SIP schema).

    cache_key is caller-chosen (e.g. a Kalshi event_ticker like
    "KXINX-26AUG17H1600") so one fetch caches as one file, mirroring
    predexon_functions.fetch_event_orderbooks's per-event_ticker cache.

    Cost safety: an uncached pull is quoted via metadata.get_cost() first
    and raises DatabentoCostGuardTripped if it exceeds max_cost_usd instead
    of spending silently -- pass a higher max_cost_usd to proceed anyway.

    Memory safety: fetched in batches of `symbol_batch_size` raw_symbols
    (one get_range + to_df() call per batch, not one call for everything),
    each checked against metadata.get_billable_size() before the pull. This
    is a *different* number from the dollar cost -- confirmed directly they
    don't move together: a 284-symbol pull quoted at $0.75 (barely more
    than a 280-symbol pull, also $0.74) turned out to carry an actual raw
    DBN size of 5.0GB (vs. 1.94GB for a 114-symbol pull, $0.29) and OOM-ing
    -killed the whole process converting that to a DataFrame in one shot on
    a machine with only ~5-8GB free at the time. Batching bounds peak memory
    to roughly one batch's worth regardless of how many symbols/how wide a
    strike_pad the caller asks for, which is what Databento's own
    BentoWarning on that oversized single request recommended doing.
    Raises DatabentoCostGuardTripped (reused for this too) if a single
    batch's billable size exceeds _MAX_BATCH_BILLABLE_BYTES -- shrink
    symbol_batch_size if that fires.

    symbol_batch_size defaults large (140), not small -- confirmed directly
    that get_range's per-call latency is dominated by a large FIXED cost
    (~120s, observed for batches ranging 0.17GB-0.24GB raw) almost
    independent of batch size in this range, not by bytes transferred. A
    batch_size of 30 turned 1 slow call into 10 *equally* slow calls
    (~20-25 minutes total instead of ~2), for no memory benefit beyond what
    a few hundred more symbols per batch already gets you. Bigger batches
    (up to _MAX_BATCH_BILLABLE_BYTES) trade a little memory headroom for a
    lot less wall-clock time -- shrink symbol_batch_size for a memory-
    constrained machine, not to make the fetch faster.

    Returns databento's raw to_df() columns (symbol, bid_px_00, ask_px_00,
    bid_sz_00, ask_sz_00, ...) plus a 'time' column (renamed from the
    ts_recv index). Use load_option_quotes_long_df for the trimmed,
    predexon-shaped long-form version.

    `columns`, if given, restricts the on-disk parquet (both write, since
    each batch is trimmed before being appended -- this is what actually
    keeps the batching's memory win, not just the per-batch fetch -- and
    read) and the in-memory frame to just those column names, plus 'symbol'
    is always cast to category -- for a wide event this cache file is tens
    of millions of rows, confirmed directly that loading every raw databento
    column (with 'symbol' as a plain string/object dtype repeated per row)
    got the whole process OOM-killed on a real ~24M-row/114-symbol pull.
    Pass only the columns the caller actually needs.
    """
    if cache_dir is None:
        cache_dir = _quotes_dir(underlying)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{cache_key}.parquet")
    if os.path.exists(cache_path):
        # `symbols` restricts the READ to those contracts via a pyarrow
        # predicate. The cache files have ~174 row groups with per-group
        # statistics, so this is predicate pushdown -- non-matching groups are
        # never decoded -- which is what lets a caller process one symbol
        # batch at a time instead of materialising the whole event. Does NOT
        # change the cache key: this is a partial read of the same full pull,
        # not a differently-scoped fetch.
        read_kwargs = {"columns": columns}
        if symbols:
            read_kwargs["filters"] = [("symbol", "in", list(symbols))]
        df = pd.read_parquet(cache_path, **read_kwargs)
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype("category")
        return df
    if not raw_symbols:
        return pd.DataFrame()

    client = _get_client()
    cost = client.metadata.get_cost(
        dataset=dataset, symbols=raw_symbols, schema=schema,
        stype_in="raw_symbol", start=start, end=end,
    )
    billable = client.metadata.get_billable_size(
        dataset=dataset, symbols=raw_symbols, schema=schema,
        stype_in="raw_symbol", start=start, end=end,
    )
    print(f"  [databento] estimated cost for {cache_key}: ${cost:.4f}, raw size {billable/1e9:.2f}GB "
          f"({len(raw_symbols)} symbols, {start} -> {end})")
    if cost > max_cost_usd:
        raise DatabentoCostGuardTripped(
            f"${cost:.2f} > max_cost_usd=${max_cost_usd:.2f} for {cache_key} "
            f"({len(raw_symbols)} symbols, {start} to {end}); pass a higher max_cost_usd to proceed"
        )

    # Batch size is capped adaptively from the *already-measured* overall
    # density (billable / symbol count), not just the caller's requested
    # symbol_batch_size -- confirmed directly this matters a lot day to
    # day: three other KXINX sessions came in at 10.6-16.2GB raw for a
    # similar symbol count to a fourth day's 5.0GB, so a fixed
    # symbol_batch_size tuned against one day's density immediately blew
    # the per-batch cap on the denser ones. 0.7x, not 1.0x, leaves some
    # margin for striding not perfectly evening out density within a batch
    # -- still less conservative than an earlier 0.5x, since the per-batch
    # accumulation risk that factor was budgeting against no longer exists
    # now that batches stream straight to disk (see _MAX_BATCH_BILLABLE_BYTES).
    avg_bytes_per_symbol = billable / max(1, len(raw_symbols))
    adaptive_batch_size = max(1, int((_MAX_BATCH_BILLABLE_BYTES * 0.7) / avg_bytes_per_symbol))
    symbol_batch_size = min(symbol_batch_size, adaptive_batch_size)

    # Strided, not contiguous: raw_symbols is sorted (by strike, since the
    # OCC-style symbol string sorts that way), and quote density is heavily
    # concentrated near the money, not uniform -- confirmed directly a
    # contiguous 65-symbol slice landed entirely in a dense region and blew
    # the per-batch size cap right after an adjacent slice came in at 1/5th
    # the size. Striding every n_batches-th symbol into each batch mixes
    # dense and sparse strikes into every batch instead of concentrating
    # them, which is what actually keeps batch sizes comparable to each
    # other -- shrinking symbol_batch_size alone doesn't fix that, it just
    # produces more batches that can still each land in the dense region.
    n_batches = max(1, -(-len(raw_symbols) // symbol_batch_size))  # ceil division
    batches = [raw_symbols[i::n_batches] for i in range(n_batches)]

    # Streamed straight to disk, one batch at a time -- never holding more
    # than one batch's DataFrame in memory, unlike the earlier
    # accumulate-then-pd.concat version. Confirmed directly that version
    # still got OOM-killed even with per-batch memory bounded: 5 batches
    # each individually safe (~1.2-2GB raw -> ~4-5GB resident during
    # to_df()) still summed, once trimmed and concatenated together, to
    # more than this machine's free memory by the final batch. A
    # ParquetWriter appends each trimmed batch directly to a temp file and
    # is closed after the last one; the temp file is only renamed onto
    # cache_path (an atomic move) once every batch has succeeded, so a
    # failure partway through never leaves a partial/corrupt cache file for
    # a later call to silently treat as a real cache hit.
    tmp_path = cache_path + ".partial"
    writer = None
    total_rows = 0
    try:
        for bi, batch in enumerate(batches):
            batch_billable = client.metadata.get_billable_size(
                dataset=dataset, symbols=batch, schema=schema, stype_in="raw_symbol", start=start, end=end,
            )
            if batch_billable > _MAX_BATCH_BILLABLE_BYTES:
                raise DatabentoCostGuardTripped(
                    f"batch {bi + 1}/{len(batches)} for {cache_key} is {batch_billable/1e9:.2f}GB raw > "
                    f"{_MAX_BATCH_BILLABLE_BYTES/1e9:.2f}GB limit; lower symbol_batch_size and retry"
                )
            data = client.timeseries.get_range(
                dataset=dataset, symbols=batch, stype_in="raw_symbol", schema=schema, start=start, end=end,
            )
            bdf = data.to_df()
            idx_name = bdf.index.name or "ts_recv"
            bdf = bdf.reset_index().rename(columns={idx_name: "time"})
            if columns is not None:
                bdf = bdf[[c for c in columns if c in bdf.columns]]
            table = pa.Table.from_pandas(bdf, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(tmp_path, table.schema)
            writer.write_table(table)
            total_rows += len(bdf)
            print(f"  [databento] batch {bi + 1}/{len(batches)}: {len(batch)} symbols, {len(bdf):,} rows, "
                  f"{batch_billable/1e9:.2f}GB raw")
            del bdf, table
    except BaseException:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    else:
        writer.close()
        os.replace(tmp_path, cache_path)

    print(f"  [databento] saved {total_rows:,} quote ticks -> {cache_path}")
    df = pd.read_parquet(cache_path, columns=columns)
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype("category")
    return df


_LONG_DF_COLS = ["raw_symbol", "strike_price", "right", "time", "bid", "ask", "bid_size", "ask_size"]
_RAW_COLS = ["symbol", "time", "bid_px_00", "ask_px_00", "bid_sz_00", "ask_sz_00"]


def _raw_to_long_df(raw: pd.DataFrame, definitions: pd.DataFrame = None) -> pd.DataFrame:
    """Shared by load_option_quotes_long_df and
    load_cached_option_quotes_for_symbols -- turns fetch_option_quotes's raw
    columns (symbol, time, bid_px_00, ask_px_00, bid_sz_00, ask_sz_00) into
    the long-form shape (raw_symbol, strike_price, right, time, bid, ask,
    bid_size, ask_size)."""
    if raw.empty:
        return pd.DataFrame(columns=_LONG_DF_COLS)

    out = pd.DataFrame({
        # already category dtype from fetch_option_quotes's cache-hit path
        # (or the fresh-fetch path, both cast "symbol" before returning) --
        # no need to re-cast here, and definitely don't cast to str.
        "raw_symbol": raw["symbol"],
        "time": pd.to_datetime(raw["time"], utc=True),
        "bid": raw["bid_px_00"],
        "ask": raw["ask_px_00"],
        "bid_size": raw["bid_sz_00"],
        "ask_size": raw["ask_sz_00"],
    })
    del raw  # drop the reference before the merge below, which briefly needs its own working memory
    if definitions is not None and not definitions.empty:
        meta = definitions.drop_duplicates(subset=["raw_symbol"]).set_index("raw_symbol")[["strike_price", "instrument_class"]]
        # Match meta's index to out['raw_symbol']'s *category* dtype before
        # merging, not after -- confirmed directly that merging a
        # categorical key against meta's plain-string index (the shape it's
        # naturally in coming out of get_option_definitions, which never
        # categorizes it) makes pandas silently materialize the key column
        # as plain Python strings for the merge, and that this isn't just a
        # cosmetic dtype difference in the *result*: on a ~62M-row event
        # the transient blow-up (out's pre-merge frame + raw, both still
        # referenced, alongside the ~4.37GB uncategorized merge key) was
        # enough on its own to OOM-kill the whole process, well before
        # anything downstream (build_mids, notebook cells, etc.) ever ran.
        # Matching dtype *before* the merge avoids ever materializing the
        # full-string version at all, rather than paying that transient
        # cost and recategorizing after.
        meta.index = meta.index.astype(out["raw_symbol"].dtype)
        out = out.merge(meta, left_on="raw_symbol", right_index=True, how="left")
        out = out.rename(columns={"instrument_class": "right"})
        out["right"] = out["right"].astype("category")
        # float32, not float64 -- a strike is a single value repeated across
        # every tick of a given contract (there are only ~len(raw_symbols)
        # distinct values, e.g. ~30-60 here), and float32's ~7 significant
        # digits is far more precision than a strike like 7787.5 needs.
        # Confirmed directly this alone is ~200-400MB on a 51M-row pull --
        # a real, avoidable cost from carrying a redundant full-precision
        # float64 on every single row rather than just the raw_symbol
        # category code that already implies it.
        out["strike_price"] = out["strike_price"].astype("float32")
    else:
        out["strike_price"] = pd.NA
        out["right"] = pd.NA

    # No sort here (an earlier version did .sort_values(["raw_symbol",
    # "time"]), which -- confirmed directly -- materializes a full extra
    # copy of the whole frame to sort, transiently doubling memory right at
    # the point this is already largest). Nothing downstream needs a
    # particular row order: build_mids re-sorts by "time" within each
    # symbol-chunk it processes regardless of input order, and
    # _fetched_meta only ever calls .unique() on raw_symbol.
    return out.reset_index(drop=True)[_LONG_DF_COLS]


def load_option_quotes_long_df(
    underlying: str,
    raw_symbols: list[str],
    start: datetime.datetime,
    end: datetime.datetime,
    cache_key: str,
    definitions: pd.DataFrame = None,
    cache_dir: str = None,
    max_cost_usd: float = _MAX_AUTO_COST_USD,
    symbol_batch_size: int = 65,
    dataset: str = DATASET,
    schema: str = "cmbp-1",
) -> pd.DataFrame:
    """
    Long-form (raw_symbol, strike_price, right, time, bid, ask, bid_size,
    ask_size) built from cached cmbp-1 top-of-book quotes -- same shape
    convention as predexon_functions.load_predexon_long_df, so it drops
    into the same downstream analysis pattern. Pass `definitions` (from
    get_option_definitions) to fill in strike_price/right; without it
    those two columns are left null. See fetch_option_quotes's docstring
    for symbol_batch_size and dataset/schema.
    """
    raw = fetch_option_quotes(
        underlying, raw_symbols, start, end, cache_key, cache_dir=cache_dir, max_cost_usd=max_cost_usd,
        columns=_RAW_COLS, symbol_batch_size=symbol_batch_size, dataset=dataset, schema=schema,
    )
    return _raw_to_long_df(raw, definitions)



def load_option_quotes_long_df_chunked(
    underlying: str,
    raw_symbols: list[str],
    start: datetime.datetime,
    end: datetime.datetime,
    cache_key: str,
    definitions: pd.DataFrame = None,
    cache_dir: str = None,
    max_cost_usd: float = _MAX_AUTO_COST_USD,
    symbol_batch_size: int = 65,
    dataset: str = DATASET,
    schema: str = "cmbp-1",
    transform=None,
    read_batch_symbols: int = 20,
) -> pd.DataFrame:
    """Same result as load_option_quotes_long_df, built one symbol batch at a
    time so peak memory is a batch rather than the whole event.

    Why this exists: a near-complete session is BIGGER than a gappy one, so
    the most valuable days are the ones that OOM. KXINXU-26AUG28H1600 is
    175.9M ticks -- roughly 3x any other cached event because it is the only
    session with no collector gaps -- and _raw_to_long_df's merge leaves three
    float64 columns (bid, ask, strike_price) that pandas consolidates into one
    (3, 175877327) block: a single 3.93 GiB contiguous allocation that fails
    on a 16GB machine regardless of how much is free.

    `transform` is applied to EACH batch before it is accumulated. Passing the
    IB conflation here rather than after the full load is the whole point --
    conflation keeps ~8% of rows, so applying it per batch makes peak memory
    a batch's raw size instead of the event's, and the accumulated result is
    an order of magnitude smaller than the input. Per-symbol batching is safe
    for it because conflation never looks across contracts.

    Falls back to a single unbatched pass when the cache is cold: the fetch
    path has to run once for the whole symbol set anyway to write the cache.
    """
    cache_dir_resolved = cache_dir or _quotes_dir(underlying)
    cache_path = os.path.join(cache_dir_resolved, f"{cache_key}.parquet")
    if not os.path.exists(cache_path):
        # Cold cache: fetch (which writes the parquet), then fall through to
        # the batched read so the big frame is never held in full.
        fetch_option_quotes(
            underlying, raw_symbols, start, end, cache_key, cache_dir=cache_dir,
            max_cost_usd=max_cost_usd, columns=_RAW_COLS,
            symbol_batch_size=symbol_batch_size, dataset=dataset, schema=schema,
        )

    out = []
    total_raw = 0
    for i in range(0, len(raw_symbols), read_batch_symbols):
        batch = raw_symbols[i:i + read_batch_symbols]
        raw = fetch_option_quotes(
            underlying, batch, start, end, cache_key, cache_dir=cache_dir,
            max_cost_usd=max_cost_usd, columns=_RAW_COLS,
            symbol_batch_size=symbol_batch_size, dataset=dataset, schema=schema,
            symbols=batch,
        )
        if raw.empty:
            continue
        total_raw += len(raw)
        part = _raw_to_long_df(raw, definitions)
        del raw
        if transform is not None:
            part = transform(part)
        out.append(part)
    if not out:
        return pd.DataFrame(columns=_LONG_DF_COLS)
    df = pd.concat(out, ignore_index=True)
    del out
    print(f"  [databento] {cache_key}: {total_raw:,} raw ticks -> {len(df):,} rows "
          f"in {max(1, (len(raw_symbols) + read_batch_symbols - 1) // read_batch_symbols)} batch(es)")
    return df.sort_values("time", kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# KXINX-specific convenience: bucket boundaries -> bracketing strikes -> quotes
# ---------------------------------------------------------------------------

def _kxinx_event_markets(event_ticker: str) -> list[dict]:
    """Full Kalshi market objects (not just the trimmed columns
    api_functions.get_event_markets returns) -- need close_time for the
    option-expiry window. KXINX populates floor_strike/cap_strike directly
    on its own (confirmed via a live API check on 2026-08-15), unlike
    KXHIGHLAX, so no ticker-suffix parsing is needed here the way
    predexon_functions._parse_strike_from_ticker needed it."""
    import api_functions
    data = api_functions.kalshi_get("/trade-api/v2/markets", params={"event_ticker": event_ticker, "limit": 200})
    return data.get("markets", [])


def bracket_listed_strikes(boundaries: set[float], listed_strikes: list[float]) -> set[float]:
    """For each boundary, the nearest listed strike *and* its immediate
    listed-strike neighbors on both sides (typically $5 apart near the
    money) -- not just the nearest strike alone. Two adjacent Kalshi
    buckets share a boundary (one's cap is the next one's floor), so
    naively using each bucket's own nearest strike as the *other* side of
    a finite difference ends up differencing across the ~$25 gap between
    Kalshi boundaries instead of the ~$5 gap between actually-listed
    strikes -- a much coarser, noisier derivative estimate.
    bucket_probabilities' finite difference (pricing.prob_above_finite_diff)
    picks whichever strikes are immediately adjacent, by index, to the one
    nearest its target level -- so it automatically uses these tight
    neighbors instead, as long as they're actually present in what's
    fetched here. `listed_strikes` must already be sorted ascending.
    Shared by load_kxinx_event_option_quotes (historical SPXW) and by
    the live feed's own subscription-symbol selection
    (kalshi_latency_arbs/live/live_ib_feed.py) -- identical
    bracket-selection logic regardless of whether the caller then fetches
    historical ticks or subscribes live."""
    wanted = set()
    for b in boundaries:
        idx0 = min(range(len(listed_strikes)), key=lambda i: abs(listed_strikes[i] - b))
        for i in (idx0 - 1, idx0, idx0 + 1):
            if 0 <= i < len(listed_strikes):
                wanted.add(listed_strikes[i])
    return wanted


# IB's default market-data allowance is ~100 concurrent L1 lines, and
# reqMktData spends one per OPTION CONTRACT -- not per Kalshi bucket. A full
# event blows straight through it: KXINX-26AUG28H1600's 30 buckets bracket
# out to 174 contracts, KXINXU's 60 to 124.
#
# Measured 2026-08-29 against that event's recorded session: 5 ATM KXINX
# buckets cost 36 contracts and 15 ATM KXINXU buckets cost 34, so 70 total
# with 30 lines spare -- no Quote Booster packs ($30 per extra 100) needed.
# ATM buckets are the CHEAP case despite covering the busiest strikes: they
# are contiguous, so their bracketed strikes overlap heavily (KXINXU's 15
# floors span 70 points at $5 spacing and collapse to ~17 strikes). A
# scattered selection of the same size costs roughly double.
IB_MARKET_DATA_LINES = 100


def select_event_buckets(event_ticker: str, n: int, by: str = "atm") -> list[str]:
    """The `n` bucket tickers in `event_ticker` worth actually subscribing to.

    by="atm": closest to 50/50 on Kalshi's own touch -- where the option
    signal has any chance of disagreeing usefully. Markets without a
    two-sided quote are ranked LAST rather than treated as 0.5: an absent
    quote is not an at-the-money one, and scoring it as such silently fills
    the whole selection with untraded buckets (confirmed directly -- it was
    picking dead markets first).

    Used for both series: 5 ATM buckets for KXINX, 15 for KXINXU. In a
    single-sided ladder P(S>K) is monotonic in K, so the buckets nearest
    50/50 are contiguous thresholds -- which is why 15 of them still fit the
    line budget (see IB_MARKET_DATA_LINES).

    by="liquid": most open interest + 24h volume. Kept as an alternative
    ranking; it does NOT track where the option signal is informative, only
    where volume already went.

    Ties and unquoted markets fall back to open interest, so this always
    returns min(n, len(markets)) tickers rather than a short list.
    """
    markets = _kxinx_event_markets(event_ticker)
    if not markets:
        return []

    def _f(m, key):
        try:
            return float(m.get(key))
        except (TypeError, ValueError):
            return None

    def _oi(m):
        return _f(m, "open_interest_fp") or 0.0

    if by == "liquid":
        ranked = sorted(markets, key=lambda m: _oi(m) + (_f(m, "volume_24h_fp") or 0.0), reverse=True)
    elif by == "atm":
        def _key(m):
            bid, ask = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
            if bid is None or ask is None or bid <= 0 or ask >= 1.0 or ask <= bid:
                return (1, 0.0, -_oi(m))     # unquoted -> after every quoted market
            return (0, abs((bid + ask) / 2.0 - 0.5), -_oi(m))
        ranked = sorted(markets, key=_key)
    else:
        raise ValueError(f"unknown ranking {by!r} -- expected 'atm' or 'liquid'")
    return [m["ticker"] for m in ranked[:n]]


def count_subscription_contracts(event_ticker: str, tickers: list[str] = None,
                                  underlying: str = "SPXW") -> int:
    """How many option contracts subscribing to `tickers` would cost, i.e.
    how many IB market-data lines. Check this against IB_MARKET_DATA_LINES
    before a live run -- exceeding the allowance doesn't error, it silently
    starves whichever subscriptions came last."""
    markets = _kxinx_event_markets(event_ticker)
    if tickers is not None:
        markets = [m for m in markets if m["ticker"] in set(tickers)]
    if not markets:
        return 0
    close_time = datetime.datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    date = close_time.astimezone(ET).date()
    defs = get_option_definitions(underlying, date)
    same = defs[(defs["expiration"].dt.date == date) & (defs["instrument_class"].isin(["C", "P"]))]
    if same.empty:
        return 0
    boundaries = set()
    for m in markets:
        for k in ("floor_strike", "cap_strike"):
            if m.get(k) is not None:
                boundaries.add(float(m[k]))
    wanted = bracket_listed_strikes(boundaries, sorted(same["strike_price"].unique()))
    return int(same[same["strike_price"].isin(wanted)]["raw_symbol"].nunique())


def load_kxinx_event_option_quotes(
    event_ticker: str,
    underlying: str = "SPXW",
    max_cost_usd: float = _MAX_AUTO_COST_USD,
    symbol_batch_size: int = 65,
    tickers: list[str] = None,
    transform=None,
) -> pd.DataFrame:
    """
    For a KXINX event (e.g. "KXINX-26AUG17H1600"), fetch the matching
    same-day SPXW 0DTE option definitions, pick the single listed strike
    nearest each Kalshi bucket boundary in the event, and return their
    cmbp-1 quotes long-form (see load_option_quotes_long_df) for the
    regular trading session on that date, through the market's own
    close_time.

    The nearest strike plus its immediate listed-strike neighbors on both
    sides (typically $5 apart) -- pricing (latency_arbs_functions.
    bucket_probabilities) is model-free, a discrete finite difference of
    the quoted price-vs-strike curve, so it needs tight, actually-adjacent
    strikes to differentiate against, not a coarser bracket 2-3 boundaries
    away.

    KXINX's rules_primary resolves off "the end-of-day S&P 500 index
    value" at the market's close_time (confirmed directly: close_time is
    always 20:00Z / 4pm ET, the regular session close) -- so the matching
    option series is that same calendar date's daily-expiry SPXW chain,
    not any later-dated expiry. Only call this once that date's 0DTE chain
    is actually listed (Databento's definitions for a date will be empty/
    missing same-day strikes otherwise) -- Kalshi opens KXINX events for
    trading up to ~a week ahead of their close_time, well before SPXW lists
    that far out, so don't expect data for events beyond a day or two out.

    Pass `tickers` (a subset of this event's own Kalshi market tickers) to
    only pull strikes bracketing *those* buckets' boundaries, not every
    boundary in the event -- e.g. just the 5 most-active/most-ATM buckets,
    found cheaply from already-cached Kalshi data (predexon ticks or
    trades) before spending anything on Databento. A wide event has ~56
    boundaries -> ~280-290 contracts -> tens of millions of ticks, most of
    which sit in deep-OTM buckets that rarely trade and don't need this
    level of modeling; a handful of ATM buckets is a small fraction of
    that (confirmed on a real event: 5 contiguous ATM buckets vs. 56 total
    boundaries), with a proportionally cheap Databento pull and no
    downstream memory pressure -- use this instead of pulling the full
    chain and filtering afterward, since filtering *after* the fact still
    pays the full pull's cost and (per real experience building this
    pipeline) its memory risk.
    """
    markets = _kxinx_event_markets(event_ticker)
    if not markets:
        return pd.DataFrame()
    if tickers is not None:
        markets = [m for m in markets if m["ticker"] in set(tickers)]
        if not markets:
            return pd.DataFrame()

    close_time = datetime.datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    date = close_time.astimezone(ET).date()
    session_open = datetime.datetime.combine(date, datetime.time(9, 30), tzinfo=ET)

    boundaries = set()
    for m in markets:
        if m.get("floor_strike") is not None:
            boundaries.add(float(m["floor_strike"]))
        if m.get("cap_strike") is not None:
            boundaries.add(float(m["cap_strike"]))
    if not boundaries:
        return pd.DataFrame()

    defs = get_option_definitions(underlying, date)
    if defs.empty:
        return pd.DataFrame()
    # expiration is date-only (confirmed directly: every value is exactly
    # midnight UTC, e.g. 2026-08-14T00:00:00Z for the Aug-14 0DTE chain) --
    # compare its UTC date as-is. Converting to ET first (as an earlier
    # version of this did) shifts midnight UTC back to the *previous*
    # calendar day in ET and silently zeroes out every same-day match.
    same_day = defs[defs["expiration"].dt.date == date]
    if same_day.empty:
        print(f"  [warn] no {underlying} definitions expiring {date} (0DTE chain may not be listed yet)")
        return pd.DataFrame()

    # For each boundary, the nearest listed strike *and* its immediate
    # listed-strike neighbors on both sides (typically $5 apart near the
    # money) -- not just the nearest strike alone. Two adjacent Kalshi
    # buckets share a boundary (one's cap is the next one's floor), so
    # naively using each bucket's own nearest strike as the *other* side of
    # a finite difference (as an earlier version of this did) ends up
    # differencing across the ~$25 gap between Kalshi boundaries instead of
    # the ~$5 gap between actually-listed strikes -- a much coarser,
    # noisier derivative estimate. bucket_probabilities' finite difference
    # (pricing.prob_above_finite_diff) picks whichever strikes are
    # immediately adjacent, by index, to the one nearest its target level --
    # so it automatically uses these tight neighbors instead, as long as
    # they're actually present in what's fetched here.
    listed_strikes = sorted(same_day["strike_price"].unique())
    wanted_strikes = bracket_listed_strikes(boundaries, listed_strikes)

    picked = same_day[same_day["strike_price"].isin(wanted_strikes)]
    raw_symbols = sorted(picked["raw_symbol"].unique().tolist())
    print(f"  [databento] {event_ticker}: {len(boundaries)} bucket boundaries -> "
          f"{len(wanted_strikes)} strikes -> {len(raw_symbols)} contracts (C+P)")

    # A `tickers`-scoped fetch caches under its own key -- never event_ticker
    # itself -- so it can't collide with (overwrite, or be silently served
    # stale by) a wider, all-boundaries cache already on disk for the same
    # event_ticker, or vice versa.
    cache_key = event_ticker if tickers is None else f"{event_ticker}-{len(markets)}tickers-nearest"
    # Always the chunked path: identical result, but peak memory is one symbol
    # batch rather than the whole event, and `transform` (the IB conflation)
    # is applied per batch so its ~12x reduction bounds the peak too. See
    # load_option_quotes_long_df_chunked for the event that forced this.
    return load_option_quotes_long_df_chunked(
        underlying, raw_symbols, session_open, close_time, cache_key=cache_key,
        definitions=same_day, max_cost_usd=max_cost_usd, symbol_batch_size=symbol_batch_size,
        transform=transform,
    )



if __name__ == "__main__":
    import sys
    event_tickers = sys.argv[1:] or ["KXINX-26AUG17H1600"]
    for et in event_tickers:
        print(f"=== {et} ===")
        df = load_kxinx_event_option_quotes(et)
        print(df.head())
