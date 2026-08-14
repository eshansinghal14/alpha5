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
how to override it.
"""
import glob
import os
import sys

import pandas as pd

_INFRA_DIR = os.path.join(os.path.dirname(__file__), "..", "kalshi_infra")
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)

import predexon_functions as pf

from core import Quote, Trade
from book import replay_ws_ticks

_DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
KALSHI_WS_ROOT = os.path.join(_DATA_ROOT, "kalshi_ws")

SOURCE_PRIORITY = ["kalshi_ws", "predexon", "kalshi_1min"]


def _kalshi_ws_path(event_ticker: str) -> str | None:
    pattern = os.path.join(KALSHI_WS_ROOT, "*", "raw_ob_ticks", f"{event_ticker}.parquet")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def available_sources(event_ticker: str) -> dict[str, bool]:
    """Which of the three quote sources actually have cached data on disk
    for this event -- checked directly (real cache-file existence), never
    assumed from a date range."""
    return {
        "kalshi_ws": _kalshi_ws_path(event_ticker) is not None,
        "predexon": event_ticker in set(pf.all_cached_predexon_event_tickers()),
        "kalshi_1min": event_ticker in set(pf.all_cached_kalshi_1min_event_tickers()),
    }


def select_source(event_ticker: str, prefer: str | None = None) -> str:
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
    """
    avail = available_sources(event_ticker)
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


def _load_quotes_kalshi_ws(event_ticker: str) -> tuple[dict[str, list[Quote]], dict[str, list]]:
    path = _kalshi_ws_path(event_ticker)
    raw = pd.read_parquet(path)
    quotes = replay_ws_ticks(raw)  # deduped touch-change stream, for wakeup scheduling
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
    """{ticker: 'yes'/'no'/None} from Predexon's cached event_markets listing
    (which mirrors Kalshi's own `result` field) -- None means not yet
    settled (still open) or unknown."""
    markets = pf.get_event_markets(event_ticker)
    return {m["ticker"]: m.get("result") or None for m in markets}


_FIDELITY_OF = {"kalshi_ws": "l2", "predexon": "top_of_book", "kalshi_1min": "candle"}


def _load_quotes_for_source(source: str, event_ticker: str) -> tuple[dict[str, list[Quote]], dict[str, list]]:
    if source == "kalshi_ws":
        return _load_quotes_kalshi_ws(event_ticker)
    if source == "predexon":
        return _load_quotes_predexon(event_ticker), {}
    return _load_quotes_kalshi_1min(event_ticker), {}


def load_event_data(event_ticker: str, source: str | None = None) -> EventData:
    """
    Load everything needed to backtest one event (all markets within it),
    auto-selecting the best cached quote source unless `source` overrides it
    (see select_source()'s docstring for priority/override semantics).

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
    resolved = select_source(event_ticker, prefer=source)
    quotes, raw_ws = _load_quotes_for_source(resolved, event_ticker)
    source_by_ticker = {t: resolved for t, qs in quotes.items() if qs}
    fidelity_by_ticker = {t: _FIDELITY_OF[resolved] for t in source_by_ticker}

    all_tickers = {m["ticker"] for m in pf.get_event_markets(event_ticker)}
    missing = all_tickers - set(source_by_ticker)
    if missing and source is None:  # only auto-backfill when the caller didn't force a specific source
        fallback_order = [s for s in SOURCE_PRIORITY if s != resolved and available_sources(event_ticker)[s]]
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
