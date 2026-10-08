"""
Unified data loading: turns whichever of the three raw sources (kalshi_ws L2
ticks, predexon top-of-book+depth snapshots, kalshi 1-min candles) is
available for a given event into a common Quote/Trade stream, plus
settlement results. Trade prints (for queue-position fill simulation) always
come from kalshi_trades regardless of which quote source is used, since
that's the only source with real executed-trade prints.

Source resilience: coverage does NOT overlap cleanly across time --
kalshi_ws only exists for ~the last few days (collector just started),
predexon covers Apr-May 2026 in real depth, kalshi_1min candles are the
widest-covering but depth-free fallback. A backtest spanning dates with
different available fidelity will silently mix apples and oranges (a
strategy that looks great on L2 depth data may look different -- not
necessarily worse, but *differently biased* -- once run only against
candle-fidelity days) unless you check `EventData.fidelity` per event and
account for it. See select_source()'s docstring for the exact priority and
how to override it, and its `allow_predexon` flag to exclude predexon
entirely (both as a quote source and as a per-ticker backfill) for a run
that wants single-fidelity data. Settlement (load_settlement) and each
event's own market listing always come from Kalshi's own live endpoint,
never predexon -- see load_settlement's docstring for why.
"""
import glob
import os
import sys

import pandas as pd

from kalshi_infra import databento_functions as dbf
from kalshi_infra import predexon_functions as pf
from kalshi_backtest.core import Quote, Trade
from kalshi_backtest.book import replay_ws_ticks

_DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
KALSHI_WS_ROOT = os.path.join(_DATA_ROOT, "kalshi_ws")

SOURCE_PRIORITY = ["kalshi_ws", "predexon", "kalshi_1min"]

# Shared by engine.Backtest (refuses to fill/rest an order against a book
# this stale) and viz/data_loader.py (masks the Kalshi bid/ask line out
# during a stretch this stale, rather than drawing a misleading flat
# line) -- one definition, so the two can't silently drift apart. See
# engine.Backtest.__init__'s own docstring for the real incident (a
# collector outage that froze a book for hours) this exists to guard
# against.
STALE_THRESHOLD_S = 60.0

# Shared by engine.Backtest and book.L2Book.to_quote -- a DIFFERENT guard
# from STALE_THRESHOLD_S above: that one catches "the feed stopped
# talking entirely"; this one catches "the feed kept talking but our own
# yes_bids/no_bids reconstruction quietly drifted wrong over a long
# delta-only stretch between snapshots" -- confirmed directly on real
# 2026-08-26 L2 data (a 44+ minute gap between snapshots produced an
# impossible bid>ask touch that Kalshi's own next snapshot immediately
# corrected -- see engine.Backtest.__init__'s own docstring). 30 minutes
# is a heuristic pick from that data: short enough to catch the observed
# 44+ minute bad gap, generous enough not to distrust the more common
# 10-25 minute gaps this project's actual snapshot cadence produces.
MAX_SNAPSHOT_AGE_S = 1800.0


def _kalshi_ws_path(event_ticker: str) -> str | None:
    pattern = os.path.join(KALSHI_WS_ROOT, "*", "raw_ob_ticks", f"{event_ticker}.parquet")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def empirical_wire_s(event_ticker: str, default: float = 1.0) -> float:
    """Median observed (receive_tx - exchange_tx) across this event's own
    real Kalshi WS ticks -- a direct, per-event measurement of how long it
    actually took our own collector to receive a message after Kalshi
    stamped it, used in place of LatencyModel's assumed wire_s=1.0 default
    (see execution.py). Median, not mean -- confirmed directly on real
    data the distribution is heavily right-skewed (occasional multi-second
    outliers, presumably reconnect/backlog gaps, next to a typical latency
    of single-digit milliseconds: KXINX-26AUG25H1600 measured mean=2.30s
    but median=0.007s), so the mean badly overstates "typical" latency.

    Falls back to `default` for old-schema files (pre-2026-08-25 collector
    redesign) that don't carry exchange_tx at all, or if no row has both
    timestamps -- there's nothing to measure from in either case, and 1.0s
    remains a deliberately conservative placeholder, not a real estimate.
    """
    path = _kalshi_ws_path(event_ticker)
    if path is None:
        return default
    raw = pd.read_parquet(path)
    if "exchange_tx" not in raw.columns or "receive_tx" not in raw.columns:
        return default
    have_both = raw["exchange_tx"].notna() & raw["receive_tx"].notna()
    if not have_both.any():
        return default
    lat = (raw.loc[have_both, "receive_tx"] - raw.loc[have_both, "exchange_tx"]).dt.total_seconds()
    return float(lat.median())


def available_sources(event_ticker: str) -> dict[str, bool]:
    """Which of the three quote sources actually have cached data on disk
    for this event -- checked directly (real cache-file existence), never
    assumed from a date range."""
    return {
        "kalshi_ws": _kalshi_ws_path(event_ticker) is not None,
        "predexon": event_ticker in set(pf.all_cached_predexon_event_tickers()),
        "kalshi_1min": event_ticker in set(pf.all_cached_kalshi_1min_event_tickers()),
    }


def select_source(event_ticker: str, prefer: str | None = None, allow_predexon: bool = True) -> str:
    """
    Pick which quote source to use for this event.

    prefer=None (default, "auto"): highest-fidelity source that's actually
    cached, in order kalshi_ws (real L2 depth, but only ~last few days) >
    predexon (long history, top-of-book depth) > kalshi_1min (widest
    coverage, NO depth -- can price marketable fills but can't simulate a
    resting order's queue position at all).

    prefer="kalshi_ws"/"predexon"/"kalshi_1min": force that source; raises
    if it isn't cached for this event rather than silently falling back --
    a silent fallback would let a backtest run "successfully" on a
    different fidelity than you asked for and you'd never notice from the
    output alone.

    allow_predexon=False: treat predexon as uncached regardless of what's
    actually on disk -- for a run that wants ONLY kalshi_ws (or kalshi_1min)
    fidelity and would rather fail/skip an event than silently mix in
    predexon's top-of-book data. Independent of `prefer` (predexon can
    still be forced via prefer="predexon" even with allow_predexon=False,
    since an explicit prefer is an explicit request, not a fallback).
    """
    avail = available_sources(event_ticker)
    if not allow_predexon and prefer != "predexon":
        avail = {**avail, "predexon": False}
    if prefer is not None:
        if not avail.get(prefer):
            raise ValueError(f"{event_ticker}: source {prefer!r} requested but not cached ({avail})")
        return prefer
    for src in SOURCE_PRIORITY:
        if avail[src]:
            return src
    raise ValueError(f"{event_ticker}: no cached quote source available at all ({avail})")


class EventData:
    """Everything the engine needs for one event_ticker: quotes (per ticker,
    deduped to touch-changes -- drives when the strategy wakes up), trades
    (per ticker), settlement results, and metadata about which source/
    fidelity was actually used.

    raw_ws (l2 fidelity only): the full, UNDEDUPED raw tick rows per ticker,
    sorted by time. The engine replays these directly into a live per-ticker
    L2Book as it advances through true simulation time, so depth_at() away
    from the touch is always answered from the ladder AS IT STOOD AT THAT
    MOMENT -- not a final/future state. quotes[ticker] (deduped, touch-only)
    is deliberately a *separate* stream used only to decide when to wake the
    strategy; it is not itself replayed into the ladder (that would apply
    each row twice)."""

    def __init__(self, event_ticker: str, source: str, fidelity: str,
                 quotes: dict[str, list[Quote]], trades: dict[str, list[Trade]],
                 results: dict[str, str | None], raw_ws: dict[str, list] | None = None):
        self.event_ticker = event_ticker
        self.source = source
        self.fidelity = fidelity
        self.quotes = quotes
        self.trades = trades
        self.results = results
        self.raw_ws = raw_ws or {}
        # Populated by load_event_data after per-ticker fallback -- see its
        # docstring. Defaults keep a manually-constructed EventData (e.g. in
        # tests) usable without callers having to know about backfilling.
        self.source_by_ticker: dict[str, str] = {t: source for t in quotes}
        self.fidelity_by_ticker: dict[str, str] = {t: fidelity for t in quotes}
        self.tickers_with_no_data: set[str] = set()

    @property
    def tickers(self) -> list[str]:
        return sorted(set(self.quotes) | set(self.trades))


def _normalize_ws_schema(raw: pd.DataFrame) -> pd.DataFrame:
    """Two on-disk raw_ob_ticks schemas exist. Files from before the
    collector's 2026-08-25 redesign have a single `time`/`ts_ms` pair (NaT
    for orderbook_snapshot rows -- see _fill_missing_ws_timestamps below).
    Files from that redesign onward instead have `exchange_tx` (Kalshi's
    own timestamp -- still NaT for snapshots, Kalshi doesn't stamp those)
    and `receive_tx` (when WE received it -- populated on EVERY row,
    including snapshots, and guaranteed non-decreasing within the
    collector's own single-threaded processing/merge order -- see
    ws_raw_ob_collector.py's module docstring on why it sorts merges by
    receive_tx specifically). Maps receive_tx to `time` when present so
    the rest of this module doesn't need to know which schema a given file
    uses -- and so _fill_missing_ws_timestamps below becomes a real no-op
    for new files (zero NaNs to fill) rather than needing a parallel
    code path."""
    if "receive_tx" in raw.columns:
        raw = raw.copy()
        raw["time"] = raw["receive_tx"]
    return raw


def _fill_missing_ws_timestamps(raw: pd.DataFrame) -> pd.DataFrame:
    """Kalshi's own `orderbook_snapshot` WS messages carry NEITHER a `ts`
    nor a `ts_ms` field -- confirmed directly against real captured data
    (KXINX-26AUG18H1600: all 21,346 snapshot rows have time=NaT AND
    ts_ms=NaN; all 4,882 delta rows have valid times for both) -- so
    ws_raw_ob_collector.py's `"time": msg.get("ts")` silently becomes NaT
    for every snapshot row, not a rare parsing edge case.

    Only relevant to the OLD schema (see _normalize_ws_schema) -- files
    with a real receive_tx already have zero missing timestamps by the
    time this runs, so this is a no-op for anything collected after the
    2026-08-25 redesign.

    This matters far beyond "snapshots have an unknown timestamp": a
    single pd.NaT anywhere in the combined discrete-event heap
    (kalshi_backtest/engine.py) corrupts ordering for OTHER, unrelated
    elements too, not just the NaT rows themselves -- NaT's comparison is
    famously non-transitive (`NaT < x` and `x < NaT` are BOTH False for
    every x), which violates the total order heapq's sift algorithm
    requires. Confirmed directly this is exactly what was happening: a
    strategy's own position-cap logic looked mathematically airtight yet
    still saw net position overshoot 50-150% past its configured limit,
    traced across many hours of investigation to real fills landing wildly
    out of chronological order relative to when the strategy's own
    decisions were made -- NOT a bug in the strategy's cap math at all.

    Fixes it by filling each ticker's NaT rows from its OWN neighboring
    rows (ffill then bfill, ordered by Kalshi's own reliable per-connection
    `seq` counter, not the broken `time` column) -- a snapshot's true time
    is realistically very close to its neighboring deltas (both happen
    around the same reconnect/resubscribe), so borrowing a nearby real
    timestamp is far better than either dropping the row (losing the fresh
    book state it carries) or leaving it NaT (silently corrupting the
    entire simulation's event ordering). Rows where an ENTIRE ticker has no
    valid timestamp anywhere to borrow from (nothing to ffill/bfill from)
    are dropped -- there's no reasonable time left to assign them, and
    keeping a NaT is worse than losing that data point.
    """
    n_missing = raw["time"].isna().sum()
    if n_missing == 0:
        return raw
    raw = raw.sort_values(["ticker", "seq"], kind="mergesort").copy()
    raw["time"] = raw.groupby("ticker", sort=False)["time"].transform(lambda s: s.ffill().bfill())
    still_missing = raw["time"].isna().sum()
    raw = raw.dropna(subset=["time"])
    print(f"  [data] filled {n_missing - still_missing} missing kalshi_ws timestamps from "
          f"neighboring rows (dropped {still_missing} with no valid neighbor at all)")
    return raw


def _load_quotes_kalshi_ws(event_ticker: str) -> tuple[dict[str, list[Quote]], dict[str, list]]:
    path = _kalshi_ws_path(event_ticker)
    raw = pd.read_parquet(path)
    raw = _normalize_ws_schema(raw)
    raw = _fill_missing_ws_timestamps(raw)
    # deduped touch-change stream, for wakeup scheduling -- max_snapshot_age_s
    # guards against reconstruction drift between snapshots (see book.py's
    # own docstrings), same default engine.Backtest uses for its separate
    # true-book ladder.
    quotes = replay_ws_ticks(raw, max_snapshot_age_s=MAX_SNAPSHOT_AGE_S)
    raw_ws: dict[str, list] = {}
    for ticker, sub in raw.groupby("ticker", sort=False):
        raw_ws[ticker] = list(sub.sort_values(["time", "seq"]).itertuples(index=False))
    return quotes, raw_ws


def _load_quotes_predexon(event_ticker: str) -> dict[str, list[Quote]]:
    long_df = pf.load_predexon_long_df(event_ticker)
    out: dict[str, list[Quote]] = {}
    if long_df.empty:
        return out
    for ticker, sub in long_df.groupby("ticker", sort=False):
        sub = sub.sort_values("time")
        out[ticker] = [
            Quote(ticker=ticker, time=row.time, bid=row.bid, ask=row.ask,
                  bid_depth=row.bid_depth, ask_depth=row.ask_depth,
                  source="predexon", fidelity="top_of_book")
            for row in sub.itertuples(index=False)
        ]
    return out


def _load_quotes_kalshi_1min(event_ticker: str) -> dict[str, list[Quote]]:
    long_df = pf.load_kalshi_backup_long_df(event_ticker)
    out: dict[str, list[Quote]] = {}
    if long_df.empty:
        return out
    for ticker, sub in long_df.groupby("ticker", sort=False):
        sub = sub.sort_values("time")
        out[ticker] = [
            Quote(ticker=ticker, time=row.time, bid=row.bid, ask=row.ask,
                  bid_depth=None, ask_depth=None,
                  source="kalshi_1min", fidelity="candle")
            for row in sub.itertuples(index=False)
        ]
    return out


def load_trades(event_ticker: str) -> dict[str, list[Trade]]:
    """Trade prints, always from kalshi_trades (the only source with real
    executed prints) regardless of which quote source is in use. Empty for
    events outside Kalshi's ~2.5-month trade retention window -- see
    predexon_functions.load_kalshi_trades_long_df's docstring; that's
    indistinguishable here from "confirmed zero trades", same caveat.

    Trade.price is recorded in the price terms of whichever side was
    actually RESTING (the maker), not the taker -- a trade with
    taker_side='yes' means an aggressive yes buy crossed into a resting NO
    order, so the queue that actually depleted was the no-side one, at
    no_price, not yes_price (they aren't the same number). This is what
    lets execution.QueueTracker match a resting order's own (side, price)
    against these prints with a plain price equality check -- see its
    docstring. taker_side=None (rare/malformed rows) can't be resolved to a
    maker side, so both yes_price and no_price are emitted, conservatively
    over-depleting whichever queue happens to exist at either price rather
    than silently dropping the print.
    """
    long_df = pf.load_kalshi_trades_long_df(event_ticker)
    out: dict[str, list[Trade]] = {}
    if long_df.empty:
        return out
    for ticker, sub in long_df.groupby("ticker", sort=False):
        sub = sub.sort_values("time")
        rows = []
        for row in sub.itertuples(index=False):
            if row.taker_side == "yes":
                maker_prices = [row.no_price]
            elif row.taker_side == "no":
                maker_prices = [row.yes_price]
            else:
                maker_prices = [row.yes_price, row.no_price]
            for price in maker_prices:
                rows.append(Trade(ticker=ticker, time=row.time, price=price,
                                   size=row.count, taker_side=row.taker_side, source="kalshi_trades"))
        out[ticker] = rows
    return out


def load_settlement(event_ticker: str) -> dict[str, str | None]:
    """{ticker: 'yes'/'no'/None} from Kalshi's own LIVE markets endpoint
    (databento_functions._kxinx_event_markets, uncached -- always current)
    -- None means not yet settled (still open) or unknown.

    Deliberately NOT Predexon's cached event_markets listing
    (predexon_functions.get_event_markets): confirmed directly that
    Predexon's own `result` field lags real Kalshi settlement by at least
    several days for a recently-closed event -- KXINX-26AUG21H1600 (closed
    2026-08-21) still showed result=None for all 30 markets via Predexon on
    2026-08-24, while Kalshi's own live endpoint already had every market
    `status="finalized"` with a real result. Silently treating that lag as
    "still unsettled" zeroed out an entire day's realized PnL (5,716 real
    fills, $111 in real fees) even though the outcome was long since known
    -- not a "the strategy broke even that day" result, a data-staleness
    bug. Live means no caching layer to go stale here again."""
    markets = dbf._kxinx_event_markets(event_ticker)
    return {m["ticker"]: m.get("result") or None for m in markets}


_FIDELITY_OF = {"kalshi_ws": "l2", "predexon": "top_of_book", "kalshi_1min": "candle"}


def _load_quotes_for_source(source: str, event_ticker: str) -> tuple[dict[str, list[Quote]], dict[str, list]]:
    if source == "kalshi_ws":
        return _load_quotes_kalshi_ws(event_ticker)
    if source == "predexon":
        return _load_quotes_predexon(event_ticker), {}
    return _load_quotes_kalshi_1min(event_ticker), {}


def load_event_data(event_ticker: str, source: str | None = None, allow_predexon: bool = True) -> EventData:
    """
    Load everything needed to backtest one event (all markets within it),
    auto-selecting the best cached quote source unless `source` overrides it
    (see select_source()'s docstring for priority/override semantics).

    allow_predexon=False: never use predexon, neither as the primary quote
    source nor as a per-ticker backfill (see select_source()'s docstring) --
    for a run that wants a clean, single-fidelity (kalshi_ws, or kalshi_ws
    falling back only to kalshi_1min) dataset. Settlement and the event's
    own market listing (`all_tickers` below) never depended on predexon in
    the first place -- both come from Kalshi's own live endpoint (see
    load_settlement's docstring) -- so this flag only affects *quotes*.

    Per-ticker fallback: the event-level source choice does NOT guarantee
    every ticker within the event actually has data there -- confirmed
    directly (KXHIGHLAX-26AUG03-T76 has zero kalshi_ws rows even though four
    sibling markets in the same event captured thousands, presumably never
    registered by the ws collector at market open or never had book
    activity while it was connected) even though the event overall resolves
    to kalshi_ws. Any ticker with zero quotes from the primary source is
    individually backfilled from the next source down the priority list
    that's cached and has data for it -- so a single event's tickers can end
    up on genuinely different fidelities. EventData.source_by_ticker /
    fidelity_by_ticker records the per-ticker truth; .source/.fidelity
    remain the primary (highest-priority) source actually used, for the
    common case where nothing needed backfilling.
    """
    resolved = select_source(event_ticker, prefer=source, allow_predexon=allow_predexon)
    quotes, raw_ws = _load_quotes_for_source(resolved, event_ticker)
    source_by_ticker = {t: resolved for t, qs in quotes.items() if qs}
    fidelity_by_ticker = {t: _FIDELITY_OF[resolved] for t in source_by_ticker}

    all_tickers = {m["ticker"] for m in dbf._kxinx_event_markets(event_ticker)}
    missing = all_tickers - set(source_by_ticker)
    if missing and source is None:  # only auto-backfill when the caller didn't force a specific source
        fallback_order = [s for s in SOURCE_PRIORITY if s != resolved and available_sources(event_ticker)[s]]
        if not allow_predexon:
            fallback_order = [s for s in fallback_order if s != "predexon"]
        for fallback in fallback_order:
            if not missing:
                break
            fb_quotes, fb_raw_ws = _load_quotes_for_source(fallback, event_ticker)
            for ticker in list(missing):
                if fb_quotes.get(ticker):
                    quotes[ticker] = fb_quotes[ticker]
                    if ticker in fb_raw_ws:
                        raw_ws[ticker] = fb_raw_ws[ticker]
                    source_by_ticker[ticker] = fallback
                    fidelity_by_ticker[ticker] = _FIDELITY_OF[fallback]
                    missing.discard(ticker)

    trades = load_trades(event_ticker)
    results = load_settlement(event_ticker)
    ed = EventData(event_ticker, resolved, _FIDELITY_OF[resolved], quotes, trades, results, raw_ws)
    ed.source_by_ticker = source_by_ticker
    ed.fidelity_by_ticker = fidelity_by_ticker
    ed.tickers_with_no_data = missing
    return ed
