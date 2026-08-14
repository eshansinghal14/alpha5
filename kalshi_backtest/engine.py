"""
The backtest engine: a discrete-event simulation over one event's market
data (all tickers/markets within it), with realistic latency and fill
simulation, calling a plain strategy function whenever there's something new
for it to react to.

Why discrete-event (a time-ordered heap) rather than a simple for-loop over
quotes: three different "clocks" need to interleave correctly --
  1. true exchange time (when things actually happened -- drives book state
     and resting-order fills, which are real exchange-side facts unaffected
     by our own latency),
  2. visible time = true time + wire_s (when the strategy finds out and can
     react), and
  3. order arrival time = decision time + send_s + processing_s (when an
     order the strategy just placed actually reaches the exchange, to be
     resolved against the book as it stands AT ARRIVAL, not as it stood when
     the strategy decided -- this is what makes latency actually cost
     something instead of being cosmetic).
A single time-ordered event heap is the simplest structure that keeps all
three correctly interleaved, including order arrivals that get scheduled
dynamically mid-run (only known once the strategy actually calls .buy()).

Trades are pushed into the heap TWICE, deliberately, at two different times:
once at their true time (to deplete resting queue trackers -- a real
exchange-side fact, not something our own latency affects) and once at
their visible time (to wake the strategy, since finding out a trade printed
is itself delayed by wire_s like any other market data).
"""
import dataclasses
import heapq
import itertools
import json
import time as _walltime
from typing import Callable

import pandas as pd

from core import Order, Portfolio
from book import BookView, L2Book
from data import EventData
from execution import LatencyModel, QueueTracker, resolve_marketable, resolve_resting


@dataclasses.dataclass
class BacktestContext:
    time: pd.Timestamp          # current visible (delayed) sim time
    last_event: object           # the Quote/Trade/external payload that triggered this wakeup
    event_ticker: str
    portfolio: Portfolio
    _visible_books: dict
    _engine: "Backtest"
    pending_orders: list = dataclasses.field(default_factory=list)

    def book(self, ticker: str) -> BookView | None:
        """The strategy's own (delayed) view of `ticker`'s book -- reflects
        only data that has actually become visible by `self.time`."""
        return self._visible_books.get(ticker)

    def position(self, ticker: str):
        return self.portfolio.position(ticker)

    def buy(self, ticker: str, side: str, size: float, price: float | None = None,
            order_type: str = "limit"):
        """Queue a buy order. Not resolved immediately -- lands at the
        exchange after send_s + (this wakeup's own measured processing
        time), resolved there against the book as it stands at arrival, not
        against what's visible right now. order_type='market' ignores
        `price`. Only buys are supported in v1 -- closing/reducing a
        position before settlement isn't modeled; see engine module notes
        and the README-style caveats in the top-level summary."""
        order = Order(order_id=self._engine._next_order_id(), ticker=ticker, side=side,
                       order_type=order_type, price=price, size=size, decision_time=self.time)
        self.pending_orders.append(order)
        return order


@dataclasses.dataclass
class BacktestResult:
    event_ticker: str
    source: str
    fidelity: str
    portfolio: Portfolio
    orders: list[Order]
    queue_trackers: list[QueueTracker]
    unresolved_orders: list[Order]  # arrived but couldn't be resolved (no depth data at all)
    source_by_ticker: dict[str, str] = dataclasses.field(default_factory=dict)
    fidelity_by_ticker: dict[str, str] = dataclasses.field(default_factory=dict)
    tickers_with_no_data: set = dataclasses.field(default_factory=set)


class Backtest:
    def __init__(self, latency: LatencyModel | None = None, fee_multiplier: float = 0.07,
                 maker_fee_multiplier: float | None = None):
        self.latency = latency or LatencyModel()
        self.fee_multiplier = fee_multiplier
        self.maker_fee_multiplier = maker_fee_multiplier
        self._heap: list[tuple] = []
        self._counter = itertools.count()
        self._order_seq = itertools.count()

    def _next_order_id(self) -> int:
        return next(self._order_seq)

    def _push(self, time: pd.Timestamp, kind: str, payload):
        heapq.heappush(self._heap, (time, next(self._counter), kind, payload))

    def run(self, strategy_fn: Callable[[BacktestContext], None], event_ticker: str,
            source: str | None = None, event_data: EventData | None = None,
            external_events: list[tuple] | None = None) -> BacktestResult:
        """
        strategy_fn(ctx): called on every visible market update (a
        touch-changing quote or a trade print, once delayed by wire_s) and
        on any external_events you pass in (also delayed by wire_s -- e.g.
        crossing_arb's station-temperature crossing signal). Call
        ctx.buy(...) to act; positions/PnL/fees are tracked automatically,
        and settlement is applied at the end from event_data.results.

        external_events: optional list of (true_time, payload) -- anything
        that should also wake the strategy but isn't itself Kalshi market
        data. Not fed into true_book (only Quote/Trade events affect it).
        """
        if event_data is None:
            from data import load_event_data
            event_data = load_event_data(event_ticker, source=source)

        self._heap = []
        self._counter = itertools.count()
        portfolio = Portfolio(fee_multiplier=self.fee_multiplier, maker_fee_multiplier=self.maker_fee_multiplier)

        true_books: dict[str, BookView] = {}
        visible_books: dict[str, BookView] = {}
        ladders: dict[str, L2Book] = {t: L2Book(t) for t in event_data.raw_ws}
        queue_trackers: list[QueueTracker] = []
        all_orders: list[Order] = []
        unresolved_orders: list[Order] = []

        # -- book-maintenance events: update true_books/ladders, silently (no wakeup) --
        for ticker, rows in event_data.raw_ws.items():
            for row in rows:
                self._push(row.time, "raw_ws_row", (ticker, row))
        for ticker, quotes in event_data.quotes.items():
            if ticker in event_data.raw_ws:
                continue  # l2 fidelity: true_books already maintained via raw_ws_row
            for q in quotes:
                self._push(q.time, "true_quote", q)

        # -- true-time trade events: deplete resting queues as real fills happen --
        for ticker, trades in event_data.trades.items():
            for t in trades:
                self._push(t.time, "trade_true", (ticker, t))

        # -- strategy wakeups (visible-time delayed): touch-changing quotes, trade
        #    prints becoming known, and any caller-supplied external signals --
        for ticker, quotes in event_data.quotes.items():
            for q in quotes:
                self._push(self.latency.visible_time(q.time), "wakeup", ("quote", ticker, q))
        for ticker, trades in event_data.trades.items():
            for t in trades:
                self._push(self.latency.visible_time(t.time), "wakeup", ("trade", ticker, t))
        for true_time, ext_payload in (external_events or []):
            self._push(self.latency.visible_time(true_time), "wakeup", ("external", None, ext_payload))

        while self._heap:
            time, _, kind, payload = heapq.heappop(self._heap)

            if kind == "raw_ws_row":
                ticker, row = payload
                book = ladders[ticker]
                if row.msg_type == "snapshot":
                    levels = json.loads(row.levels_json) if row.levels_json else []
                    book.apply_snapshot(row.side, levels)
                else:
                    book.apply_delta(row.side, row.price, row.delta)
                true_books[ticker] = BookView.from_quote(book.to_quote(time), ladder=book)

            elif kind == "true_quote":
                q = payload
                true_books[q.ticker] = BookView.from_quote(q)

            elif kind == "trade_true":
                ticker, trade = payload
                for tracker in queue_trackers:
                    if tracker.done or tracker.order.ticker != ticker:
                        continue
                    fill = tracker.on_trade(trade)
                    if fill is not None:
                        portfolio.record_fill(tracker.order, fill.price, fill.size, time, fill.liquidity)

            elif kind == "wakeup":
                evt_kind, ticker, evt_payload = payload
                if evt_kind == "quote":
                    # Deliberately touch-only (no ladder) even for l2-fidelity
                    # tickers: `ladders[ticker]` is a live object the engine
                    # keeps mutating up to the current TRUE time, which by
                    # wakeup time (true_time + wire_s later) has already
                    # advanced past what this quote represents. Handing the
                    # strategy that reference would leak up-to-wire_s-seconds
                    # of future depth data it hasn't actually seen yet. The
                    # static Quote object's own bid/ask/bid_depth/ask_depth
                    # (captured at its true time, immutable) is exactly what
                    # should be visible -- see BookView.depth_at's touch-only
                    # fallback path for what that limits a strategy to.
                    visible_books[evt_payload.ticker] = BookView.from_quote(evt_payload, ladder=None)
                ctx = BacktestContext(time=time, last_event=evt_payload, event_ticker=event_ticker,
                                       portfolio=portfolio, _visible_books=visible_books, _engine=self)
                t0 = _walltime.perf_counter()
                strategy_fn(ctx)
                processing_s = _walltime.perf_counter() - t0
                for order in ctx.pending_orders:
                    all_orders.append(order)
                    arrival = self.latency.arrival_time(time, processing_s)
                    self._push(arrival, "order_arrival", order)

            elif kind == "order_arrival":
                order = payload
                true_book = true_books.get(order.ticker)
                if true_book is None:
                    unresolved_orders.append(order)
                    continue
                fill = resolve_marketable(order, true_book)
                if fill is not None:
                    portfolio.record_fill(order, fill.price, fill.size, time, fill.liquidity)
                    continue
                if order.order_type == "market":
                    # marketable check failed (empty book) and market orders can't rest
                    unresolved_orders.append(order)
                    continue
                tracker = resolve_resting(order, true_book)
                if not tracker.known:
                    unresolved_orders.append(order)
                else:
                    queue_trackers.append(tracker)

        portfolio.settle(event_data.results)
        return BacktestResult(event_ticker=event_data.event_ticker, source=event_data.source,
                               fidelity=event_data.fidelity, portfolio=portfolio, orders=all_orders,
                               queue_trackers=queue_trackers, unresolved_orders=unresolved_orders,
                               source_by_ticker=dict(event_data.source_by_ticker),
                               fidelity_by_ticker=dict(event_data.fidelity_by_ticker),
                               tickers_with_no_data=set(event_data.tickers_with_no_data))
