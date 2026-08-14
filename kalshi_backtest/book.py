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

from core import Quote


class L2Book:
    """Live per-ticker L2 book reconstructed from raw kalshi_ws snapshot/delta
    rows. yes_bids/no_bids are {price: qty} ladders; a "no" resting order at
    price q is a YES ask at (1 - q) with the same qty (see module docstring)."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes_bids: dict[float, float] = {}
        self.no_bids: dict[float, float] = {}

    def apply_snapshot(self, side: str, levels: list[tuple[float, float]]):
        book = self.yes_bids if side == "yes" else self.no_bids
        book.clear()
        for price, qty in levels:
            if qty > 0:
                book[round(float(price), 2)] = float(qty)

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

    def to_quote(self, time: pd.Timestamp) -> Quote:
        bid, ask = self.best_bid(), self.best_ask()
        return Quote(
            ticker=self.ticker, time=time, bid=bid, ask=ask,
            bid_depth=self.yes_bids.get(bid) if bid is not None else None,
            ask_depth=self.no_bids.get(round(1.0 - ask, 2)) if ask is not None else None,
            source="kalshi_ws", fidelity="l2",
        )


def replay_ws_ticks(raw_df: pd.DataFrame) -> dict[str, list[Quote]]:
    """
    Reconstruct a full L2 book per ticker from raw kalshi_ws rows (columns:
    ticker, msg_type, seq, time, side, price, delta, levels_json -- see
    ws_raw_ob_collector.py's Collector.handle_message for exact field
    meaning) and emit one Quote per row that actually changes the touch
    (bid/ask/bid_depth/ask_depth) -- consecutive duplicate quotes are
    dropped to keep the event stream a manageable size without losing any
    price-relevant information.

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
                book.apply_snapshot(row.side, levels)
            else:
                book.apply_delta(row.side, row.price, row.delta)
            key = (book.best_bid(), book.best_ask())
            if key == last_key:
                continue
            last_key = key
            quotes.append(book.to_quote(row.time))
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
