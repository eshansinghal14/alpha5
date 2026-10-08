"""
Latency model and fill simulation.

Three separate delays:
  - wire_s: time for a KALSHI market data event that truly happened at
    exchange time T to become visible to the strategy
    (visible_time = T + wire_s).
  - signal_s: the same idea for an EXTERNAL signal feed (the SPXW/ES
    option-implied probability), which arrives over Databento rather than
    Kalshi's wire and additionally has to be priced before it's actionable
    (signal_visible_time = T + signal_s). Kept distinct from wire_s because
    they are different feeds with different latencies -- see
    MEASURED_SIGNAL_S.
  - send_s + processing_s: time from the strategy's decision to when an
    order it submits actually lands at the exchange. processing_s is NOT a
    constant -- it's measured as the real wall-clock time the strategy
    callback itself took to run (instrumented by the engine), so a strategy
    that does more computation to decide on a trade realistically arrives
    later. arrival_time = decision_time + processing_s + send_s.

send_s and signal_s both default to REAL MEASURED values (see the constants
below and kalshi_infra/measure_order_latency.py), not the round 1.0s
placeholders they used to carry.

Both delays are one-directional and don't compound with each other except
through decision_time already including wire_s (decision_time is itself a
visible_time, i.e. already offset by wire_s from the true event that
triggered it) -- see engine.py's event loop for exactly how these compose.
"""
import dataclasses

import pandas as pd

from kalshi_backtest.core import Order, Trade
from kalshi_backtest.book import BookView


# --- measured latency constants (see kalshi_infra/measure_order_latency.py) ---
#
# MEASURED_SEND_S: one-way decision -> Kalshi matching engine, measured
# 2026-08-28 against the live API from a home connection. Taken from Kalshi's
# own `ts_ms` matching-engine timestamp on the v2 create-order response,
# corrected for a local clock offset independently confirmed against three
# NTP servers (the machine's clock was 2.67s fast, which made the raw
# ts_ms - send_time difference come out NEGATIVE until corrected).
#
# This is the PERSISTENT-CONNECTION number. Measured directly, the same probe
# over a fresh TCP+TLS connection per order came out at 105ms vs 34ms
# warm -- i.e. ~70ms of the naive measurement was handshake, not order
# latency. 34ms is the right figure to model because a live trader holds one
# connection open; but note api_functions.kalshi_get currently uses
# module-level requests.get (no Session), so it pays the cold cost on every
# call -- anything latency-sensitive must reuse a Session to actually realize
# this number.
#
# NOT a lower bound assumption: one-way here is ~82% of the round trip, not
# the 50% a symmetric-path assumption would give, because Kalshi's inbound +
# matching leg dominates and the response leg is comparatively fast.
MEASURED_SEND_S = 0.034

# MEASURED_SIGNAL_S: delay on the OPTION-SIDE signal path, i.e. how long
# after an ES-daily option quote truly changes before the strategy could
# possibly act on it. Two components:
#   ~0.0245s  Databento live gateway -> us (half of a measured 48.9ms TCP RTT
#             to glbx-mdp3.lsg.databento.com). The exchange -> Databento
#             capture leg is NOT added on top: cached quote times are already
#             Databento's own ts_recv (see databento_functions), so it's
#             baked into the grid timestamps this delay is applied to.
#   ~0.0075s  our own pricing computation (median, measured over 300 runs of
#             live_signal.compute_live_bucket_probs + pick_atm_signal on a
#             real 29-strike / 30-bucket event; p95 9.5ms).
# The Databento leg is a network-RTT estimate rather than an end-to-end
# feed measurement -- refine it with a live-feed timestamp comparison if the
# strategy ever turns out to be sensitive at the ~10ms level.
MEASURED_SIGNAL_S = 0.032


@dataclasses.dataclass
class LatencyModel:
    wire_s: float = 1.0
    send_s: float = MEASURED_SEND_S
    # Applied to caller-supplied external_events (the SPXW/ES option-implied
    # signal) instead of wire_s. These are two genuinely different feeds:
    # wire_s is Kalshi's own market-data delay, whereas the option signal
    # arrives over Databento and then has to be priced before it's actionable.
    # An earlier version delayed external events by wire_s, which modeled the
    # option side as if it arrived over Kalshi's wire -- wrong feed, wrong
    # number, and it silently omitted pricing cost entirely.
    signal_s: float = MEASURED_SIGNAL_S

    def visible_time(self, true_time: pd.Timestamp) -> pd.Timestamp:
        return true_time + pd.Timedelta(seconds=self.wire_s)

    def signal_visible_time(self, true_time: pd.Timestamp) -> pd.Timestamp:
        """When an option-side signal computed off a quote that truly
        changed at `true_time` first becomes actionable -- feed latency plus
        our own pricing time. See MEASURED_SIGNAL_S."""
        return true_time + pd.Timedelta(seconds=self.signal_s)

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
        self.cancelled = False  # set by the engine on a matching cancel_arrival -- see engine.py

    @property
    def known(self) -> bool:
        return self.depth_ahead is not None

    @property
    def remaining(self) -> float:
        return self.order.size - self.filled_qty

    @property
    def done(self) -> bool:
        return self.cancelled or self.remaining <= 1e-9

    def on_trade(self, trade: Trade) -> ResolvedFill | None:
        """Feed one trade print (must already be filtered to this order's
        ticker) through the queue. Returns a ResolvedFill for however much of
        OUR order that trade fills, or None if it fills none of it (either a
        different price, or depth_ahead not yet exhausted, unknown depth, or
        the order has been cancelled -- `done` covers all of these)."""
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
