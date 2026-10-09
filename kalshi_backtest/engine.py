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

Cancellation (ctx.cancel(order_id), for a strategy that requotes -- e.g.
kalshi_latency_arbs/spxw_kxinx/sp_options_lag_quoting.py) reuses the exact same
send_s + measured-processing-time latency and arrival-time formula as a new
order (see BacktestContext.cancel/'cancel_arrival' below): a cancel is just
another message to the exchange, equally subject to the round-trip. A
resting QueueTracker marked cancelled is simply excluded from future fills
(see execution.QueueTracker.done); this is a maker-side-only order book
(only our own resting orders are tracked at all), so there's no other
participant's queue position to reshuffle.
"""
import dataclasses
import heapq
import itertools
import json
import time as _walltime
from typing import Callable

import pandas as pd

from kalshi_backtest.core import Order, Portfolio
from kalshi_backtest.book import BookView, L2Book
from kalshi_backtest.data import EventData, STALE_THRESHOLD_S, MAX_SNAPSHOT_AGE_S
from kalshi_backtest.execution import LatencyModel, QueueTracker, resolve_marketable, resolve_resting


@dataclasses.dataclass
class BacktestContext:
    time: pd.Timestamp          # current visible (delayed) sim time
    last_event: object           # the Quote/Trade/external payload that triggered this wakeup
    event_ticker: str
    portfolio: Portfolio
    _visible_books: dict
    _engine: "Backtest"
    pending_orders: list = dataclasses.field(default_factory=list)
    pending_cancels: list = dataclasses.field(default_factory=list)

    def book(self, ticker: str) -> BookView | None:
        """The strategy's own (delayed) view of `ticker`'s book -- reflects
        only data that has actually become visible by `self.time`."""
        return self._visible_books.get(ticker)

    def position(self, ticker: str):
        return self.portfolio.position(ticker)

    def buy(self, ticker: str, side: str, size: float, price: float | None = None,
            order_type: str = "limit", intent: str = "take", role: str = "edge"):
        """Queue a buy order. Not resolved immediately -- lands at the
        exchange after send_s + (this wakeup's own measured processing
        time), resolved there against the book as it stands at arrival, not
        against what's visible right now. order_type='market' ignores
        `price`. Only buys are supported in v1 -- closing/reducing a
        position before settlement isn't modeled; see engine module notes
        and the README-style caveats in the top-level summary.

        `intent` ("take"/"quote") is recorded on the Order and ignored here:
        this engine already rests anything that isn't marketable on arrival.
        It exists so the same strategy code drives the live path, which does
        have to choose between an IOC and a resting order -- see core.Order.
        """
        order = Order(order_id=self._engine._next_order_id(), ticker=ticker, side=side,
                       order_type=order_type, price=price, size=size, decision_time=self.time,
                       intent=intent)
        order.role = role
        self.pending_orders.append(order)
        return order

    def amend(self, order_id: int, new_size: float) -> bool:
        """Resting-size change. The backtest has no amend message, so this is
        modelled as unsupported: the caller falls back to cancel + new order,
        which reaches the same back-of-queue state."""
        return False

    def cancel(self, order_id: int, force: bool = False):
        """Queue a cancel for a previously-placed order (by the id returned
        from buy()). Same send_s + measured-processing-time latency as a new
        order, resolved at the SAME arrival-time formula -- see engine.py's
        run() for exactly how a cancel_arrival interacts with an order that
        may or may not have reached the exchange yet. A cancel against an
        order that's already fully filled (or already cancelled) is a
        harmless no-op."""
        self.pending_cancels.append(order_id)


@dataclasses.dataclass
class BacktestResult:
    event_ticker: str
    source: str
    fidelity: str
    portfolio: Portfolio
    orders: list[Order]
    queue_trackers: list[QueueTracker]
    unresolved_orders: list[Order]  # arrived but couldn't be resolved -- see unresolved_reasons for why
    unresolved_reasons: dict = dataclasses.field(default_factory=dict)  # reason -> count. 'no_l2_depth' means a quote could not be simulated at this fidelity, NOT that it failed to fill.
    position_breaches: dict = dataclasses.field(default_factory=dict)  # ticker -> worst |net| seen, for tickers that exceeded max_position. Empty is the healthy case -- see run()'s own breach check.
    source_by_ticker: dict[str, str] = dataclasses.field(default_factory=dict)
    fidelity_by_ticker: dict[str, str] = dataclasses.field(default_factory=dict)
    tickers_with_no_data: set = dataclasses.field(default_factory=set)
    cancelled_order_ids: set = dataclasses.field(default_factory=set)


class Backtest:
    def __init__(self, latency: LatencyModel | None = None, fee_multiplier: float = 0.07,
                 maker_fee_multiplier: float | None = None, max_position: float | None = None,
                 stale_threshold_s: float = STALE_THRESHOLD_S,
                 max_snapshot_age_s: float = MAX_SNAPSHOT_AGE_S):
        """stale_threshold_s: an order never fills or rests against a book
        whose true_books[ticker].time (the true time of the LAST REAL tick
        applied to it -- see book.py's BookView) is more than this many
        seconds behind the order's own arrival time. Motivated directly by
        a real Kalshi WS collector outage found in this project (a whole
        event's book silently froze at its pre-market state and never
        updated again for the rest of the session -- see
        kalshi_backtest/data.py's empirical_wire_s docstring for the
        related wire-latency finding on the same incident): without this,
        the engine had no way to distinguish "the market genuinely didn't
        move" from "we stopped receiving updates", and would happily keep
        filling hypothetical orders against a frozen, hours-stale snapshot
        for the rest of the day. The strategy still gets woken up and may
        still attempt to place orders during a stale stretch -- only
        execution against the (unreliable) book is refused, same as the
        "true_book is None" case just above it.

        max_snapshot_age_s: a DIFFERENT staleness guard, passed through to
        every L2Book.to_quote() call (see book.py) -- distrusts (reports
        as unknown, bid=ask=None) a ladder that hasn't been fully
        resynced from a snapshot on BOTH sides within this many seconds,
        and unconditionally distrusts an impossible bid>ask reading
        regardless of this setting. Confirmed directly on real
        2026-08-26 L2 data that yes_bids/no_bids reconstruction can
        silently drift wrong over a long (44+ minute observed)
        delta-only stretch between snapshots -- Kalshi's own next
        snapshot immediately corrected it, proving the drift was our
        own reconstruction, not a real exchange condition (this venue
        matches yes+no=1). stale_threshold_s above catches "we stopped
        hearing from the feed entirely"; this catches "the feed kept
        talking but our own ladder quietly diverged from truth" --
        a different failure mode needing a different guard."""
        self.latency = latency or LatencyModel()
        self.fee_multiplier = fee_multiplier
        self.maker_fee_multiplier = maker_fee_multiplier
        # Only used to DETECT a breach after the fact (see run()'s
        # _note_breach) -- the engine never refuses a fill, because a venue
        # would not. Pass the strategy's own cap so the two agree.
        self.max_position = max_position
        self.stale_threshold_s = stale_threshold_s
        self.max_snapshot_age_s = max_snapshot_age_s
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
        on any external_events you pass in (delayed by signal_s instead --
        a separate feed with its own latency plus pricing cost, e.g.
        crossing_arb's station-temperature crossing signal or the SPXW/ES
        option-implied probability). Call ctx.buy(...) to act; positions/
        PnL/fees are tracked automatically, and settlement is applied at
        the end from event_data.results.

        external_events: optional list of (true_time, payload) -- anything
        that should also wake the strategy but isn't itself Kalshi market
        data. Not fed into true_book (only Quote/Trade events affect it).
        """
        if event_data is None:
            from kalshi_backtest.data import load_event_data
            event_data = load_event_data(event_ticker, source=source)

        # A single pd.NaT anywhere in the heap corrupts ordering for OTHER,
        # unrelated elements too -- NaT's comparison is non-transitive
        # (`NaT < x` and `x < NaT` are both False for every x), which
        # breaks the total order heapq's sift algorithm requires. Confirmed
        # directly this is a real failure mode, not theoretical: Kalshi's
        # own orderbook_snapshot WS messages carry no timestamp at all (see
        # data.py's _fill_missing_ws_timestamps, which is the real fix --
        # this is a last-resort guard in case some OTHER future data path
        # ever feeds NaT in here again without going through that
        # cleaning). Fail loudly rather than silently misorder a whole run.
        for label, per_ticker in (("quotes", event_data.quotes), ("trades", event_data.trades)):
            for ticker, items in per_ticker.items():
                if any(pd.isna(it.time) for it in items):
                    raise ValueError(
                        f"{event_ticker}: {label}[{ticker}] contains a NaT timestamp -- this would "
                        f"corrupt the event heap's ordering for unrelated tickers too, not just this "
                        f"one; fix it at the data-loading layer (see data.py's _fill_missing_ws_timestamps "
                        f"for the pattern) rather than passing it through.")

        self._heap = []
        self._counter = itertools.count()
        portfolio = Portfolio(fee_multiplier=self.fee_multiplier, maker_fee_multiplier=self.maker_fee_multiplier)

        true_books: dict[str, BookView] = {}
        visible_books: dict[str, BookView] = {}
        ladders: dict[str, L2Book] = {t: L2Book(t) for t in event_data.raw_ws}
        queue_trackers: list[QueueTracker] = []
        all_orders: list[Order] = []
        unresolved_orders: list[Order] = []
        unresolved_reasons: dict[str, int] = {}
        position_breaches: dict[str, float] = {}

        def _note_breach(ticker: str):
            """Record any REALISED position past max_position.

            The strategy enforces its cap predictively -- position + in-flight
            + what it believes is resting, all checked before sending. That is
            blind to any divergence between its own books and what the venue
            actually holds: on KXINXU-26AUG28H1600 a single print swept 89 of
            our resting orders at one price while the strategy's `resting_no`
            read 1, and the run finished reporting a -85 position against a cap
            of 10 without a word. A predictive check cannot catch that by
            construction, so this is the independent one -- measured after the
            fact, on fills that actually happened.
            """
            cap = getattr(self, "max_position", None)
            if cap is None:
                return
            pos = portfolio.positions.get(ticker)
            if pos is None:
                return
            net = abs(pos.yes_qty - pos.no_qty)
            if net > cap + 1e-9 and net > position_breaches.get(ticker, 0.0):
                position_breaches[ticker] = net

        def _unresolved(order, reason):
            unresolved_orders.append(order)
            unresolved_reasons[reason] = unresolved_reasons.get(reason, 0) + 1
        trackers_by_id: dict[int, QueueTracker] = {}
        cancelled_ids: set = set()

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
        # signal_visible_time, NOT visible_time: an external signal comes off
        # the OPTION feed (Databento) and has to be priced before it can be
        # acted on, which is a different delay from Kalshi's own market-data
        # wire_s -- see LatencyModel.signal_visible_time / MEASURED_SIGNAL_S.
        for true_time, ext_payload in (external_events or []):
            self._push(self.latency.signal_visible_time(true_time), "wakeup", ("external", None, ext_payload))

        while self._heap:
            time, _, kind, payload = heapq.heappop(self._heap)

            if kind == "raw_ws_row":
                ticker, row = payload
                book = ladders[ticker]
                if row.msg_type == "snapshot":
                    levels = json.loads(row.levels_json) if row.levels_json else []
                    book.apply_snapshot(row.side, levels, time=time)
                else:
                    book.apply_delta(row.side, row.price, row.delta)
                true_books[ticker] = BookView.from_quote(
                    book.to_quote(time, max_snapshot_age_s=self.max_snapshot_age_s), ladder=book)

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
                        _note_breach(tracker.order.ticker)

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
                for order_id in ctx.pending_cancels:
                    arrival = self.latency.arrival_time(time, processing_s)
                    self._push(arrival, "cancel_arrival", order_id)

            elif kind == "order_arrival":
                order = payload
                if order.order_id in cancelled_ids:
                    # cancel for this order reached the exchange first (only
                    # possible in a pathological latency edge case -- see
                    # ctx.cancel's docstring) -- never resolve or rest it.
                    continue
                true_book = true_books.get(order.ticker)
                if true_book is None:
                    _unresolved(order, "no_book")
                    continue
                if (time - true_book.time).total_seconds() > self.stale_threshold_s:
                    # Book hasn't had a REAL tick in too long (see
                    # __init__'s stale_threshold_s docstring) -- refuse to
                    # fill or rest against it, same treatment as an
                    # entirely-missing book just above.
                    _unresolved(order, "stale_book")
                    continue
                fill = resolve_marketable(order, true_book)
                if fill is not None:
                    portfolio.record_fill(order, fill.price, fill.size, time, fill.liquidity)
                    _note_breach(order.ticker)
                    continue
                if order.order_type == "market":
                    # marketable check failed (empty book) and market orders can't rest
                    _unresolved(order, "market_order_empty_book")
                    continue
                if order.intent == "take":
                    # A take that is no longer marketable on arrival EXPIRES --
                    # it does not rest. Live sends every take
                    # time_in_force="immediate_or_cancel" (see
                    # live_orders.place_ioc), so an unfilled take simply never
                    # exists there; resting it here invented an order live
                    # could not have.
                    #
                    # This was not a modelling nicety. A missed take rested
                    # untracked forever: the strategy records takes only in
                    # `_sent` for in-flight accounting, which expires after
                    # inflight_timeout_s, and `_resting()` reads `_quotes`,
                    # which takes never enter. So they were invisible to the
                    # position cap. On KXINXU-26AUG28H1600 that accumulated
                    # 92 uncancelled orders at one price on T7734.9999; a
                    # single print swept 89 of them at once and the position
                    # went to -85 against a cap of 10, while the strategy's
                    # own `resting_no` read 1. They were also booked as MAKER
                    # fills, which is why days with "maker fills" were exactly
                    # the days that breached.
                    _unresolved(order, "take_expired_unfilled")
                    continue
                tracker = resolve_resting(order, true_book)
                if not tracker.known:
                    # No depth known at this exact (side, price): the source
                    # has no L2 ladder, so there is no queue to sit in and
                    # this quote can never fill. Counted under its own reason
                    # and warned about after the run -- it used to be silently
                    # indistinguishable from an order that rested and simply
                    # wasn't hit, which reads as "quoting doesn't work here"
                    # rather than "quoting was never simulated here".
                    _unresolved(order, "no_l2_depth")
                else:
                    queue_trackers.append(tracker)
                    trackers_by_id[order.order_id] = tracker

            elif kind == "cancel_arrival":
                # Marks the matching resting order (if it has one by now) as
                # cancelled -- on_trade/'done' then ignore it going forward.
                # An order that hasn't reached order_arrival yet at this
                # point is normally impossible (see ctx.cancel's docstring
                # for why cancel_arrival can't outrace its own order's
                # arrival under a shared latency model); recording the id in
                # cancelled_ids either way makes order_arrival's own check
                # above a correct guard regardless.
                order_id = payload
                cancelled_ids.add(order_id)
                tracker = trackers_by_id.get(order_id)
                if tracker is not None:
                    tracker.cancelled = True

        if position_breaches:
            worst = max(position_breaches.values())
            print(f"  [backtest] *** POSITION LIMIT BREACHED *** {len(position_breaches)} market(s) "
                  f"exceeded max_position={self.max_position:g}; worst |net| {worst:g} "
                  f"({max(position_breaches, key=position_breaches.get)}). Realised fills went "
                  f"past the cap, which means the strategy's own view of its resting/in-flight "
                  f"exposure diverged from what actually filled. PnL for this run is not a "
                  f"result to act on until that is explained.")

        n_no_l2 = unresolved_reasons.get("no_l2_depth", 0)
        if n_no_l2:
            print(f"  [backtest] WARNING: {n_no_l2} resting order(s) could not be simulated -- "
                  f"{event_data.fidelity} fidelity has no depth at their price, so no queue "
                  f"position exists. Quoting results for {event_data.event_ticker} are NOT "
                  f"meaningful; use an l2 day.")

        portfolio.settle(event_data.results)
        return BacktestResult(event_ticker=event_data.event_ticker, source=event_data.source,
                               fidelity=event_data.fidelity, portfolio=portfolio, orders=all_orders,
                               queue_trackers=queue_trackers, unresolved_orders=unresolved_orders,
                               unresolved_reasons=unresolved_reasons,
                               position_breaches=position_breaches,
                               source_by_ticker=dict(event_data.source_by_ticker),
                               fidelity_by_ticker=dict(event_data.fidelity_by_ticker),
                               tickers_with_no_data=set(event_data.tickers_with_no_data),
                               cancelled_order_ids=cancelled_ids)
