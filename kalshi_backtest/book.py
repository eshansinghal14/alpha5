"""
Order-book state: L2 reconstruction from raw kalshi_ws ticks, and a unified
BookView the engine/strategy can query regardless of which underlying data
source produced it (kalshi_ws L2, predexon top-of-book+depth, or Kalshi's
own 1-min candles with no depth at all).

Convention (matches kalshi_infra/api_functions.compute_midpoint and
predexon_functions.load_predexon_long_df): everything is normalized to the
YES side. A resting order on the "no" side of the book at price q is the
same physical liquidity as a YES ask at price (1 - q) -- so a "no" ladder
converts to a YES ask ladder by mapping price -> 1 - price.
"""
import dataclasses
import json

import pandas as pd

from kalshi_backtest.core import Quote


class L2Book:
    """Live per-ticker L2 book reconstructed from raw kalshi_ws snapshot/delta
    rows. yes_bids/no_bids are {price: qty} ladders; a "no" resting order at
    price q is a YES ask at (1 - q) with the same qty (see module docstring)."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes_bids: dict[float, float] = {}
        self.no_bids: dict[float, float] = {}
        # When each side was last fully resynced from a snapshot (not just
        # incrementally updated via deltas) -- see to_quote's own docstring
        # on why this matters: confirmed directly on real 2026-08-26 data
        # that yes_bids/no_bids can silently DRIFT away from the true book
        # over a long delta-only stretch between snapshots (44+ minutes
        # observed on a real ticker), producing an impossible bid>ask
        # touch that Kalshi's own next snapshot then immediately corrected
        # -- i.e. genuinely wrong reconstructed state, not a real exchange
        # condition (Kalshi does enforce yes+no=1 matching). The exact
        # dropped/misapplied delta causing any one drift wasn't isolated
        # (seq numbers here are a single per-connection counter shared
        # across hundreds of tickers, not a clean per-ticker sequence), so
        # this guards the SYMPTOM (an ever-more-stale, unverified ladder)
        # rather than the specific root cause.
        self.last_snapshot_time: dict[str, pd.Timestamp | None] = {"yes": None, "no": None}

    def apply_snapshot(self, side: str, levels: list[tuple[float, float]], time: pd.Timestamp | None = None):
        book = self.yes_bids if side == "yes" else self.no_bids
        book.clear()
        for price, qty in levels:
            if qty > 0:
                book[round(float(price), 2)] = float(qty)
        if time is not None:
            self.last_snapshot_time[side] = time

    def apply_delta(self, side: str, price: float, delta: float):
        book = self.yes_bids if side == "yes" else self.no_bids
        price = round(float(price), 2)
        new_qty = book.get(price, 0.0) + delta
        if new_qty <= 1e-9:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def best_bid(self) -> float | None:
        return max(self.yes_bids) if self.yes_bids else None

    def best_ask(self) -> float | None:
        # best (cheapest) YES ask == the highest-priced resting NO order, converted
        return round(1.0 - max(self.no_bids), 2) if self.no_bids else None

    def depth_at(self, side: str, price: float) -> float:
        """Resting size at an exact price, in that SIDE's own price terms
        (e.g. side='no', price=0.99 means the no-bid queue at 99c on the no
        side -- no conversion). 0.0 if nothing rests there (a known, not
        missing, answer -- this is full L2 data)."""
        price = round(float(price), 2)
        book = self.yes_bids if side == "yes" else self.no_bids
        return book.get(price, 0.0)

    def snapshot_age_s(self, now: pd.Timestamp) -> float | None:
        """Seconds since the OLDER of the two sides' last snapshot -- None
        if either side has never been snapshotted yet (a book this young
        hasn't had a chance to drift, but also hasn't been verified
        against ground truth even once; callers treat None as "don't
        know" rather than "trust it")."""
        ages = [t for t in self.last_snapshot_time.values() if t is not None]
        if len(ages) < 2:
            return None
        return max((now - t).total_seconds() for t in ages)

    def to_quote(self, time: pd.Timestamp, max_snapshot_age_s: float | None = None) -> Quote:
        """max_snapshot_age_s, if given, distrusts (reports as unknown --
        bid=ask=None, same as "no data yet") a ladder that hasn't been
        fully resynced from a snapshot recently enough -- see
        snapshot_age_s/this class's own docstring on the real drift this
        guards against. A bid>ask reading is ALWAYS distrusted regardless
        of this setting -- that's a physically impossible state on a
        venue that matches yes+no=1 (confirmed directly), so it's never
        the true book, only ever reconstruction drift."""
        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None and bid > ask:
            bid = ask = None
        elif max_snapshot_age_s is not None:
            age = self.snapshot_age_s(time)
            if age is None or age > max_snapshot_age_s:
                bid = ask = None
        return Quote(
            ticker=self.ticker, time=time, bid=bid, ask=ask,
            bid_depth=self.yes_bids.get(bid) if bid is not None else None,
            ask_depth=self.no_bids.get(round(1.0 - ask, 2)) if ask is not None else None,
            source="kalshi_ws", fidelity="l2",
        )


def replay_ws_ticks(raw_df: pd.DataFrame, max_snapshot_age_s: float | None = None) -> dict[str, list[Quote]]:
    """
    Reconstruct a full L2 book per ticker from raw kalshi_ws rows (columns:
    ticker, msg_type, seq, time, side, price, delta, levels_json -- see
    ws_raw_ob_collector.py's Collector.handle_message for exact field
    meaning) and emit one Quote per row that actually changes the touch
    (bid/ask/bid_depth/ask_depth) -- consecutive duplicate quotes are
    dropped to keep the event stream a manageable size without losing any
    price-relevant information.

    max_snapshot_age_s is passed straight through to each row's
    L2Book.to_quote() call (see that method's own docstring) -- an
    impossible bid>ask reading is always distrusted (reported as
    bid=ask=None) regardless of this setting; a ladder that hasn't been
    resynced from a snapshot recently enough is ALSO distrusted if this is
    given. Confirmed directly this matters here specifically, not just in
    engine.py's own separate true-book ladder: this function is what
    produces the touch stream a strategy's ctx.book()/debug_log actually
    sees, so an uncorrected drifted touch here shows up directly as
    impossible bid/ask values in, e.g., viz's own quote_df.

    Returns {ticker: [Quote, ...]}, each list sorted by time. This is a
    throwaway L2Book per ticker used only to derive the touch stream -- it is
    NOT the live ladder the engine queries during simulation for off-touch
    depth (that would leak future state into decisions made before it, see
    engine.py's caution about this in its wakeup handler). The engine
    rebuilds its own live L2Book per ticker from EventData.raw_ws instead.
    """
    out: dict[str, list[Quote]] = {}
    for ticker, sub in raw_df.groupby("ticker", sort=False):
        sub = sub.sort_values(["time", "seq"])
        book = L2Book(ticker)
        quotes = []
        last_key = None
        for row in sub.itertuples(index=False):
            if row.msg_type == "snapshot":
                levels = json.loads(row.levels_json) if row.levels_json else []
                book.apply_snapshot(row.side, levels, time=row.time)
            else:
                book.apply_delta(row.side, row.price, row.delta)
            q = book.to_quote(row.time, max_snapshot_age_s=max_snapshot_age_s)
            key = (q.bid, q.ask)
            if key == last_key:
                continue
            last_key = key
            quotes.append(q)
        out[ticker] = quotes
    return out


@dataclasses.dataclass
class BookView:
    """Point-in-time snapshot the strategy/engine reads. `ladder` is set only
    for l2-fidelity tickers (a live reference to that ticker's L2Book), so
    depth_at() can answer arbitrary prices there; for top_of_book/candle
    fidelity it only knows depth at the current touch."""
    ticker: str
    time: pd.Timestamp
    bid: float | None
    ask: float | None
    bid_depth: float | None
    ask_depth: float | None
    source: str
    fidelity: str
    ladder: L2Book | None = None

    @classmethod
    def from_quote(cls, q: Quote, ladder: L2Book | None = None) -> "BookView":
        return cls(ticker=q.ticker, time=q.time, bid=q.bid, ask=q.ask,
                    bid_depth=q.bid_depth, ask_depth=q.ask_depth,
                    source=q.source, fidelity=q.fidelity, ladder=ladder)

    def touch_price(self, side: str) -> float | None:
        """The price you'd have to pay to buy `side` ('yes'/'no') immediately
        (crosses the book) -- yes buys against the yes ask, no buys against
        the yes bid converted to no terms."""
        if side == "yes":
            return self.ask
        return round(1.0 - self.bid, 2) if self.bid is not None else None

    def is_marketable(self, side: str, price: float) -> bool:
        touch = self.touch_price(side)
        return touch is not None and price >= touch

    def _resting_queue_touch(self, side: str) -> tuple[float | None, float | None]:
        """(price, depth) at the top of `side`'s own RESTING bid queue --
        NOT the crossing price from touch_price. A resting yes-buy order
        joins the yes bid queue (top = current best bid, depth = bid_depth);
        a resting no-buy order joins the no bid queue, whose top price in no
        terms is (1 - current yes ask) and whose known depth is ask_depth
        (same physical resting order, just expressed from the other side)."""
        if side == "yes":
            return self.bid, self.bid_depth
        no_touch = round(1.0 - self.ask, 2) if self.ask is not None else None
        return no_touch, self.ask_depth

    def depth_at(self, side: str, price: float) -> float | None:
        """Resting queue depth at an exact price, in `side`'s own price
        terms (i.e. what you'd pass as an Order's price for that side).
        None = unknown at this data source's fidelity -- NOT the same as
        0.0, which means known-empty."""
        if self.ladder is not None:
            return self.ladder.depth_at(side, price)
        touch, known_depth = self._resting_queue_touch(side)
        if touch is not None and abs(price - touch) < 1e-9:
            return known_depth
        return None
