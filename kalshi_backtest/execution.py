"""
Latency model and fill simulation.

Two separate delays, per the user's spec:
  - wire_s: time for a market data event that truly happened at exchange
    time T to become visible to the strategy (visible_time = T + wire_s).
  - send_s + processing_s: time from the strategy's decision to when an
    order it submits actually lands at the exchange. processing_s is NOT a
    constant -- it's measured as the real wall-clock time the strategy
    callback itself took to run (instrumented by the engine), so a strategy
    that does more computation to decide on a trade realistically arrives
    later. arrival_time = decision_time + processing_s + send_s.

Both delays are one-directional and don't compound with each other except
through decision_time already including wire_s (decision_time is itself a
visible_time, i.e. already offset by wire_s from the true event that
triggered it) -- see engine.py's event loop for exactly how these compose.
"""
import dataclasses

import pandas as pd

from core import Order, Trade
from book import BookView


@dataclasses.dataclass
class LatencyModel:
    wire_s: float = 1.0
    send_s: float = 1.0

    def visible_time(self, true_time: pd.Timestamp) -> pd.Timestamp:
        return true_time + pd.Timedelta(seconds=self.wire_s)

    def arrival_time(self, decision_time: pd.Timestamp, processing_s: float) -> pd.Timestamp:
        return decision_time + pd.Timedelta(seconds=self.send_s + processing_s)


@dataclasses.dataclass
class ResolvedFill:
    price: float
    size: float
    liquidity: str  # "taker" | "maker"


def resolve_marketable(order: Order, true_book: BookView) -> ResolvedFill | None:
    """An order that crosses the book at arrival time: filled immediately at
    the touch price, up to whatever depth is known there. If depth at the
    touch isn't known (top_of_book/candle fidelity gives no ask/bid_depth,
    or it's simply not populated), the order is assumed to fill in full at
    the touch price -- an optimistic simplification (real depth beyond what
    we can see could be thinner), flagged here rather than silently assumed
    elsewhere. Returns None if the order isn't actually marketable given
    true_book (caller should route it to resolve_resting instead)."""
    price = order.price if order.order_type == "limit" else true_book.touch_price(order.side)
    if price is None or not true_book.is_marketable(order.side, price):
        return None
    touch = true_book.touch_price(order.side)
    known_depth = true_book.ask_depth if order.side == "yes" else true_book.bid_depth
    fill_size = order.size if known_depth is None else min(order.size, known_depth)
    return ResolvedFill(price=touch, size=fill_size, liquidity="taker")


class QueueTracker:
    """Tracks a single resting (non-marketable) limit order's position in the
    book, FIFO price-time priority: depth_ahead contracts must trade at this
    exact (side, price) before our own order starts filling.

    Deliberately ignores cancellations by orders ahead of us in the queue --
    we only see PRINTED TRADES (real executed volume), not the private
    cancel/resize stream of other participants' resting orders. A
    cancellation ahead of us would let us fill *sooner* in reality, so
    ignoring it makes this a conservative (slower-than-real) fill-time
    estimate, matching kalshi_quoting_arbs/fill_backtest.ipynb's approach.
    """

    def __init__(self, order: Order, price: float, depth_ahead: float | None):
        self.order = order
        self.price = price
        self.depth_ahead = depth_ahead  # None = unknown (no depth data at this price)
        self.filled_qty = 0.0
        self.fills: list[ResolvedFill] = []

    @property
    def known(self) -> bool:
        return self.depth_ahead is not None

    @property
    def remaining(self) -> float:
        return self.order.size - self.filled_qty

    @property
    def done(self) -> bool:
        return self.remaining <= 1e-9

    def on_trade(self, trade: Trade) -> ResolvedFill | None:
        """Feed one trade print (must already be filtered to this order's
        ticker) through the queue. Returns a ResolvedFill for however much of
        OUR order that trade fills, or None if it fills none of it (either a
        different price, or depth_ahead not yet exhausted, or unknown depth)."""
        if not self.known or self.done:
            return None
        if abs(trade.price - self.price) > 1e-9:
            return None
        available = trade.size
        consume_ahead = min(self.depth_ahead, available)
        self.depth_ahead -= consume_ahead
        available -= consume_ahead
        if available <= 1e-9:
            return None
        consume_self = min(self.remaining, available)
        self.filled_qty += consume_self
        fill = ResolvedFill(price=self.price, size=consume_self, liquidity="maker")
        self.fills.append(fill)
        return fill


def resolve_resting(order: Order, true_book: BookView) -> QueueTracker:
    """Register a non-marketable limit order in the book: depth_ahead is
    read from true_book.depth_at() as of THIS instant (arrival time), not
    the (possibly stale) view the strategy decided on -- the whole point of
    resolving against true_book rather than the visible/delayed book."""
    depth_ahead = true_book.depth_at(order.side, order.price)
    return QueueTracker(order, order.price, depth_ahead)
