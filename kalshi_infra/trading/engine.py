"""
LiveEngine + LiveContext: the live counterpart of kalshi_backtest.engine's
BacktestContext and its order_arrival/cancel_arrival/trade_true handlers,
driven by a real feed instead of a time-ordered heap of known events.

execution.py's fill primitives (resolve_marketable, resolve_resting,
QueueTracker) and core.py's Order/Portfolio are reused unmodified -- they
take an Order plus a BookView/Trade and assume nothing about historical
replay.

What engine.py's heap loop adds is event ordering and latency scheduling.
The ordering has no live analog (the feed delivers in real time), but the
latency does, and live keeps it: an order placed by the strategy is
resolved after send_s + that dispatch's measured processing time, against
whatever the book says at THAT moment. Resolving on submission instead
would model zero send latency and make the live sim strictly more
optimistic than the backtest it exists to validate -- any order placed on
a touch that vanished within the round trip would fill live but not in the
backtest.

The strategy's required `ctx` surface is `.time`, `.last_event`,
`.portfolio`, `.position(ticker)`, `.book(ticker)`, `.buy(...)`,
`.cancel(order_id)` -- LiveContext implements exactly that, as does
kalshi_backtest's BacktestContext.

LiveEngine runs in one of two execution modes (see set_live_trading). In
shadow mode, the default, nothing is ever sent to Kalshi. In real mode the
same orders, produced by the same strategy code from the same books, are
sent through kalshi_infra.kalshi.orders -- only the resolution step differs, so a
session can be compared against its own shadow behaviour.

Real mode sends two order shapes, keyed off Order.intent: a take goes out
as an IOC and is finished the moment its response comes back, while a quote
goes out as a resting limit order and is then something this engine OWNS
until it is filled or cancelled. That ownership is the only genuinely new
state here:

  * it has to be cancelled explicitly (cancel_order, pull_quotes), and a
    cancel that fails leaves the order live, so it is retried rather than
    forgotten;
  * it fills with nothing to respond to, so fills are polled off the fills
    endpoint and matched by exchange order id (apply_real_fills) -- matching
    on anything looser would double-count every take, which books its own
    fill off the create response;
  * it outlives the process, so disarming and shutting down both sweep the
    event for anything still resting (cancel_resting).
"""
import asyncio
import dataclasses
import itertools
import time
import traceback

import pandas as pd

from kalshi_backtest.book import BookView
from kalshi_backtest.core import Order, Portfolio, Trade
from kalshi_backtest.data import STALE_THRESHOLD_S as MAX_BOOK_AGE_S
from kalshi_infra.kalshi.orders import WRITE_HEADROOM, http_status
from kalshi_infra.trading import order_manager as om
from kalshi_backtest.execution import (MEASURED_SEND_S, QueueTracker, resolve_marketable,
                                       resolve_resting)

# Tunable guards read the strategy's RiskParams on each check, so dashboard
# edits apply immediately. The constants below are invariants, not tunables.

# Shadow fills resolve after the measured one-way send latency plus the
# dispatch's own processing time, so paper is never more optimistic than real.
SHADOW_SEND_S = MEASURED_SEND_S

# Consecutive REAL order errors before halting. Five in a row is auth,
# balance or market state, not a moved touch.
MAX_CONSECUTIVE_ORDER_ERRORS = 5
# Consecutive position audits (5s apart) a market must disagree on before the
# audit halts. A real accounting gap persists; a fill racing the read doesn't.
AUDIT_STRIKES = 2
# Shadow's modelled write bucket when there is no order client to read the
# account's limit from: Basic tier, 100 tokens/s (GET /account/limits, 2026-10-10).
SHADOW_WRITE_TOKENS_PER_S = 100.0 * 0.8

# Slack allowed when checking a fill against its own limit price. One tick
# covers ordinary venue rounding; anything beyond it is a units or
# side-convention error in how the response was read, not a better fill.
PRICE_SANITY_TOL = 0.01

# cancel_resting: pause between list-and-cancel passes, and the overall cap.
# The cap exceeds the configured order timeout (5s) so any POST in flight at
# shutdown has landed or timed out before the sweep can give up.
SWEEP_PASS_S = 0.5
SWEEP_MAX_S = 15.0


@dataclasses.dataclass
class LiveContext:
    """One instance per strategy dispatch (mirrors BacktestContext's own
    "fresh ctx every wakeup" convention) -- cheap, stateless beyond what's
    passed in."""
    time: pd.Timestamp
    last_event: object
    portfolio: Portfolio
    _books: dict
    _engine: "LiveEngine"

    def book(self, ticker: str) -> BookView | None:
        return self._books.get(ticker)

    def position(self, ticker: str):
        return self.portfolio.position(ticker)

    def buy(self, ticker: str, side: str, size: float, price: float | None = None,
            order_type: str = "limit", intent: str = "take", role: str = "edge") -> Order:
        """`intent="quote"` sends a RESTING order in real mode instead of an
        IOC (see core.Order and LiveEngine._resolve_arrival). Shadow mode
        resolves both the same way -- marketable fills, otherwise rests --
        which is what makes a shadow session a prediction of the real one."""
        order = Order(order_id=self._engine.next_order_id(), ticker=ticker, side=side,
                      order_type=order_type, price=price, size=size, decision_time=self.time,
                      intent=intent)
        order.role = role
        self._engine.submit_order(order, self.time)
        return order

    def cancel(self, order_id: int, force: bool = False, reason: str = "strategy"):
        """Every real cancel bypasses the write limiter (cancel_resting and
        _cancel_blocking claim with force), so `force` is the contract, not a
        switch here. `reason` only labels the write-budget accounting."""
        self._engine.cancel_order(order_id, reason=reason)

    def set_quote_ladder(self, ticker: str, side: str, desired):
        """Publish the edge quotes wanted on one side (order_manager.Desired,
        or None for none). Cancels it implies go out at once; creates are sent
        by the manager's pump, nearest the touch first."""
        self._engine.om.set_desired(ticker, side, desired)

    def set_lip_quote(self, ticker: str, side: str, target):
        """Publish the LIP quote wanted on one side (order_manager.LipTarget,
        or None). Lowest priority after the edge ladder."""
        self._engine.om.set_lip(ticker, side, target)

    def lip_quote(self, ticker: str, side: str):
        """Our live LIP quote on one side as (price, remaining), or None."""
        q = self._engine.om.lip_quote(ticker, side)
        return None if q is None else (q.price, q.remaining)

    def lip_queue(self, ticker: str, side: str):
        """(contracts ahead, behind) of our LIP quote at the last queue poll, or None."""
        if self._engine.om.lip_quote(ticker, side) is None:
            return None
        return self._engine.lip_queue.get((ticker, side))

    def quote_ladder(self, ticker: str, side: str) -> list[tuple[float, float]]:
        """[(price, remaining)] of our live quotes on one side."""
        return self._engine.om.ladder(ticker, side)

    def quotes_on_book(self, ticker: str, side: str) -> list[tuple[float, float]]:
        """The subset of our quotes Kalshi's book shows, at the price it shows."""
        return self._engine.om.on_book(ticker, side)

    @property
    def own_orders_in_book(self) -> bool:
        """Kalshi's book includes our resting orders only when they're real."""
        return self._engine.live_trading


class LiveEngine:
    """Owns the live book cache, the Portfolio, and every outstanding
    order's resolution state (queue_trackers/trackers_by_id, cancelled_ids,
    unresolved_orders) -- the live analog of the handful of dicts/lists
    engine.py's Backtest.run keeps as local variables across its heap loop.

    Two execution modes, chosen by `live_trading` and identical in every
    other respect (same strategy, same books, same failsafe, same
    accounting):

      shadow (default) -- orders resolve against execution.py's fill
        primitives in memory. Nothing is sent anywhere.
      real            -- orders are sent to Kalshi as IOC takes via
        kalshi.orders.KalshiOrderClient, and the fills recorded are the
        exchange's own prices, sizes and fees.

    `live_trading` can only be on if an order_client was supplied, and it
    is flipped exclusively through set_live_trading (which reconciles
    positions first) -- never by assignment.
    """

    def __init__(self, strategy, *, risk, quoting, fees, order_client=None,
                 event_ticker: str | None = None, close_time: pd.Timestamp | None = None):
        """`risk`/`quoting`: the strategy's config sections, held by reference
        so live edits apply on the next check. `fees`: the event's
        kalshi.fees.FeeSchedule. `strategy.fairs` (ticker -> Fair) is read for
        fair-value marking."""
        self.strategy = strategy
        self.risk = risk
        self.quoting = quoting
        self.fees = fees
        self._pending_submissions: list[Order] = []
        self.n_scheduled = 0      # placed, delay elapsed not yet -- see _flush_pending
        self.resolution_errors = 0
        self.unresolved_reasons: dict[str, int] = {}
        self.portfolio = Portfolio(fee_multiplier=fees.taker_rate, maker_fee_multiplier=fees.maker_rate)
        self.books: dict[str, BookView] = {}
        self.queue_trackers: list[QueueTracker] = []
        self.trackers_by_id: dict[int, QueueTracker] = {}
        self.cancelled_ids: set[int] = set()
        self.unresolved_orders: list[Order] = []
        self.orders: list[Order] = []
        self._order_id_seq = itertools.count(1)
        # Failsafe: once mark-to-market PnL drops to -max_loss_usd, stop
        # placing NEW orders. Resting orders are pulled by the session's flush
        # loop (blocked_reason), positions are never flattened.
        self.halted = False
        self.halt_reason: str | None = None
        # "loss" when the PnL failsafe tripped while sending real orders --
        # the runtime stops every series on that one (see runtime.session).
        self.halt_kind: str | None = None
        # Real PnL already booked by earlier events of this process; set by
        # the session so max_loss_usd bounds the whole run, not one event.
        self.loss_offset = 0.0

        # --- trading guards (see blocked_reason) ---
        self.close_time = close_time
        self.consecutive_order_errors = 0
        # Set by the session via set_clocks; until then staleness is unknown
        # and the guard is inert, so tests / synchronous callers aren't
        # silently blocked.
        self._signal_age_fn = None
        self._feed_age_fn = None
        self._applied_age_fn = None
        self.last_audit: str | None = None
        self.last_audit_diffs: dict[str, float] = {}
        # ticker -> consecutive audits that disagreed (see audit_positions).
        self._audit_strikes: dict[str, int] = {}
        self.over_limit: dict[str, float] = {}
        self._last_abs_position: dict[str, float] = {}
        self.blocked_counts: dict[str, int] = {}
        # (ticker, side, touch price) -> (Kalshi depth we saw, how much of it
        # our shadow takes have used). See _net_of_shadow_takes.
        self._shadow_taken: dict[tuple, tuple[float, float]] = {}

        # Session identity. Arming real trading starts a NEW session (see
        # reset_session), so everything downstream -- the dashboard's charts,
        # its cached market ordering, the PnL curve -- can tell the shadow
        # run apart from the real one instead of splicing them together.
        self.session_started = pd.Timestamp.now(tz="UTC").isoformat()
        self._session_reset_hooks: list = []
        # Orders decided before the current session are dropped on arrival
        # rather than filled into it. Ids come off a monotonic counter, so
        # one threshold covers every order still in flight at reset.
        self._min_valid_order_id = 0

        # --- real-order placement (see set_live_trading) ---
        self.order_client = order_client
        self.event_ticker = event_ticker
        self.live_trading = False
        self.n_real_orders = 0
        self.n_real_fills = 0
        self.real_order_errors = 0
        # Real orders are in flight on an executor thread, so they're in
        # neither _pending_submissions nor n_scheduled (n_scheduled is already
        # decremented by the time the POST goes out). Tracked separately so
        # flush_snapshot's n_unaccounted invariant still holds.
        self.n_real_in_flight = 0
        self.last_reconcile: str | None = None
        self.balance_at_arm: float | None = None
        self.reconcile_mismatches: list[str] = []

        # --- quotes: owned by the OrderManager (see order_manager.py) ---
        # The manager is the single record of what's live; these two maps are
        # only for booking fills, and are NOT cleared when a quote is cancelled
        # (a fill that raced the cancel must still find its order).
        self._quote_orders: dict[int, Order] = {}
        self._exchange_to_quote: dict[str, int] = {}
        # (ticker, side) -> (contracts ahead, behind) of our LIP quote at the
        # last queue-position poll; logged on decision rows.
        self.lip_queue: dict[tuple[str, str], tuple[float, float]] = {}
        # Shadow models the account write bucket at the real refill rate
        # (with our headroom), so it sees the same throttling a real session
        # would; real mode uses the order client's own bucket instead.
        rate = getattr(order_client, "tokens_per_s", None)
        sim_rate = rate * WRITE_HEADROOM if rate else SHADOW_WRITE_TOKENS_PER_S
        self.om = om.OrderManager(_Executor(self), cancels_token=quoting.cancels_token,
                                  sim_bucket_tokens_per_s=sim_rate)
        self.om.cancels_token_fn = lambda: self.quoting.cancels_token
        self.om.keep_inside_fn = lambda: getattr(self.quoting, "quote_keep_inside", 0.0) or 0.0
        self.om.ahead_grace_fn = lambda: getattr(self.quoting, "quote_ahead_grace_s", 1.0)
        lip_cfg = getattr(getattr(strategy, "cfg", None), "lip", None)
        self.om.lip_hold_fn = lambda: getattr(lip_cfg, "lip_hold_s", 1.0)
        self.n_real_quotes = 0
        self.n_real_cancelled = 0
        self.n_real_amends = 0
        self.failed_amends = 0
        self.failed_cancels = 0
        # (exchange_order_id, trade_id) already booked, so a poll window that
        # deliberately overlaps the previous one can't double-count.
        self._seen_real_fills: set = set()
        self._fill_poll_since: int | None = None
        # exchange id -> size booked off a create response (no trade id) that
        # the fill feed has not yet reported. Not cleared on reset, like
        # _exchange_to_quote: the feed's copy can arrive after one.
        self._ack_booked: dict[str, float] = {}

    def next_order_id(self) -> int:
        return next(self._order_id_seq)

    # -----------------------------------------------------------------
    # Trading guards
    # -----------------------------------------------------------------

    def set_clocks(self, signal_age_fn=None, feed_age_fn=None, applied_age_fn=None):
        """Wire the staleness clocks, which together localise a stall:

          feed_age    -- since the pricing feed delivered anything (callback path)
          applied_age -- since an update was folded into pricing state (our loop)
          signal_age  -- age of the newest input under the current fairs. Gates trading.

        Reading only loop-thread clocks makes a saturated loop look identical
        to a quiet feed; the triple tells them apart on the dashboard.
        """
        self._signal_age_fn = signal_age_fn
        self._feed_age_fn = feed_age_fn
        self._applied_age_fn = applied_age_fn

    def signal_age_s(self) -> float | None:
        return None if self._signal_age_fn is None else self._signal_age_fn()

    def feed_age_s(self) -> float | None:
        return None if self._feed_age_fn is None else self._feed_age_fn()

    def applied_age_s(self) -> float | None:
        return None if self._applied_age_fn is None else self._applied_age_fn()

    def position_limit(self) -> float:
        """The larger of the two caps: a LIP fill may legitimately take a
        position past max_position, up to the largest LIP size. Only while LIP
        quoting is on: otherwise a 400-lot LIP setting would silently loosen
        the guard on the edge quotes."""
        lip = getattr(getattr(self.strategy, "cfg", None), "lip", None)
        lip_max = (lip.max_lots() if lip is not None and hasattr(lip, "max_lots")
                   and getattr(lip, "enable_lip_quoting", False) else 0.0)
        return max(self.quoting.max_position, lip_max)

    def signal_age_limit(self) -> float:
        return self.risk.max_signal_age_s

    def _fair_probs(self) -> dict[str, float]:
        fairs = getattr(self.strategy, "fairs", None) or {}
        return {t: f.prob for t, f in fairs.items() if f is not None and f.prob is not None}

    def check_position_limit(self, positions: dict[str, float] | None = None) -> bool:
        """Halt only if a market that is ALREADY at/over the cap grows
        FURTHER from flat. Being over the cap on its own is reported, not
        halted.

        Sitting over the cap is a recoverable state the strategy handles
        correctly on its own, and halting there removes the only thing that
        fixes it. Verified against the live class: at -38 against a cap of
        10 the lean ratio clamps to -1, cap_allows_no goes False so it cannot
        grow, cap_allows_yes stays True, and lean on the REDUCING side is
        exactly 0 -- so it takes only unwinding orders, at no edge penalty,
        and walks the position back toward the cap. It is also a state you
        can enter benignly: lowering max_position from the dashboard doesn't
        unwind anything (by design), and a position seeded from Kalshi at arm
        may already exceed it.

        Growth while over the cap is different -- that is the cap logic
        itself failing, which is what a runaway looks like, so it halts.

        `positions` maps ticker -> signed net contracts; defaults to our own
        ledger. audit_positions passes Kalshi's numbers instead.
        """
        limit = self.position_limit()
        if self.halted:
            return self.halted
        if positions is None:
            positions = {t: p.yes_qty - p.no_qty for t, p in self.portfolio.positions.items()}
        self.over_limit = {t: q for t, q in positions.items() if abs(q) > limit + 1e-9}
        for ticker, qty in self.over_limit.items():
            prev = self._last_abs_position.get(ticker, 0.0)
            if abs(qty) > prev + 1e-9 and prev > limit + 1e-9:
                self.halted = True
                self.halt_kind = "position"
                self.halt_reason = (f"{ticker} grew to {qty:+.4g} while already past the cap "
                                    f"{limit:g} -- the position cap is not holding")
                print(f"[live] HALT: {self.halt_reason}. No further orders will be sent.")
                break
        for ticker, qty in positions.items():
            self._last_abs_position[ticker] = abs(qty)
        return self.halted

    def ledger_positions(self) -> dict[str, float]:
        """Our signed YES position per ticker, for an audit's before-snapshot."""
        return {t: p.yes_qty - p.no_qty for t, p in self.portfolio.positions.items()}

    def audit_positions(self, real: dict | None = None, before: dict[str, float] | None = None) -> dict:
        """Compare our ledger against Kalshi's own position record and halt
        on any material disagreement.

        `before` is our ledger as of just before Kalshi's positions were
        requested. A market agrees if Kalshi matches our ledger at EITHER end
        of the request: fills booked during the round trip are in one snapshot
        and not the other, and a quote that filled fully is no longer resting,
        so the resting-size tolerance doesn't cover it. All four audit halts
        2026-10-11..13 were that race -- Kalshi's fills and our journal matched
        execution for execution every time. A market must also disagree on
        AUDIT_STRIKES consecutive audits before we halt.

        This is the guard that does NOT depend on our own bookkeeping being
        right, which is the whole reason it exists: every other check reads
        numbers that a fill-accounting bug would have already corrupted. Run
        periodically while armed (see runtime.session's audit loop).

        Tolerance scales with orders in flight -- our ledger legitimately
        lags Kalshi by whatever hasn't come back yet, so a fill landing
        between the two reads is not a discrepancy. It scales with that
        MARKET's resting quote size too, for the same reason and no further:
        a quote fills without any response to book it against, so Kalshi
        knows about it up to one poll interval before we do (see
        apply_real_fills). Per market, not summed, so a ladder full of quotes
        doesn't blind the check that caught a 4x overshoot.
        """
        if self.order_client is None or self.event_ticker is None:
            return {}
        if real is None:
            real = self.order_client.positions(self.event_ticker)
        kalshi = {t: v["position"] for t, v in real.items()}
        ours = self.ledger_positions()
        snaps = [ours] if before is None else [before, ours]
        resting_by_ticker: dict[str, float] = {}
        for q in self.om.live():
            resting_by_ticker[q.ticker] = resting_by_ticker.get(q.ticker, 0.0) + q.remaining
        tol_base = float(self.n_real_in_flight) + 1.0
        diffs = {}
        for ticker in set(kalshi).union(*snaps):
            ds = [kalshi.get(ticker, 0.0) - s.get(ticker, 0.0) for s in snaps]
            d = min(ds, key=abs)
            if abs(d) > tol_base + resting_by_ticker.get(ticker, 0.0):
                diffs[ticker] = d
        self._audit_strikes = {t: self._audit_strikes.get(t, 0) + 1 for t in diffs}
        self.last_audit = pd.Timestamp.now(tz="UTC").isoformat()
        self.last_audit_diffs = {t: round(d, 4) for t, d in diffs.items()}
        # Kalshi's numbers, not ours, are what the growth check watches.
        self.check_position_limit(kalshi)
        confirmed = {t: d for t, d in diffs.items() if self._audit_strikes[t] >= AUDIT_STRIKES}
        if diffs and not confirmed:
            print("[live] WARN: position audit mismatch, re-checking next audit: "
                  + ", ".join(f"{t.split('-')[-1]} {d:+.4g}" for t, d in diffs.items()))
        diffs = confirmed
        if diffs and not self.halted:
            self.halted = True
            self.halt_kind = "audit"
            self.halt_reason = ("ledger disagrees with Kalshi on "
                                + ", ".join(f"{t.split('-')[-1]} {d:+.4g}" for t, d in list(diffs.items())[:4]))
            print(f"[live] HALT: {self.halt_reason} (tolerance {tol_base:g} + that market's "
                  f"resting size). Our fill accounting is not tracking reality -- no further "
                  f"orders will be sent.")
        return self.last_audit_diffs

    def check_failsafe(self) -> bool:
        """Trip the PnL halt if either marking convention has breached
        -max_loss_usd. Returns whether we are halted.

        WORSE OF THE TWO, not one or the other. They answer different
        questions and diverge by a lot (measured at $131 on one KXINXU
        ladder): touch marking is the liquidation value, prob marking is
        what markout_analysis reports and what the backtest is compared
        against. A risk limit should trip on whichever says we are down,
        and in particular must not be talked out of tripping by our own
        model -- that is why touch is kept in the test rather than replaced
        by the markout number.

        While live, loss_offset (earlier events' real PnL) is added, so the
        limit is on the whole process's real PnL. Shadow PnL never counts
        toward it. Safe to call repeatedly; halting is one-way for the session.
        """
        if self.halted:
            return True
        touch, prob = self.current_pnl(mark="touch"), self.current_pnl(mark="prob")
        offset = self.loss_offset if self.live_trading else 0.0
        worst = min(touch, prob) + offset
        if worst <= -self.risk.max_loss_usd:
            self.halted = True
            self.halt_kind = "loss" if self.live_trading else "shadow_loss"
            self.halt_reason = (f"PnL {worst:.2f} <= -{self.risk.max_loss_usd:.2f} "
                                f"(this event: touch {touch:.2f}, markout {prob:.2f}; "
                                f"earlier events {offset:+.2f})")
            print(f"[live] FAILSAFE: {self.halt_reason} -- halting new orders. Existing resting "
                  f"orders are left to resolve normally; restart the process to resume trading.")
        return self.halted

    def blocked_reason(self) -> str | None:
        """Why we must not send an order right now, or None if we may.

        Applies in BOTH shadow and real mode on purpose: a shadow session
        exists to predict what the real one would do, so a guard that only
        fires with real money on would make shadow the more optimistic of
        the two -- the same reasoning that keeps send latency in the shadow
        path (see this module's docstring).
        """
        if self.check_failsafe():
            return "halted"
        age = self.signal_age_s()
        if age is not None and age > self.signal_age_limit():
            # A frozen fair against a live book is not edge -- it crosses in
            # exactly the direction that just moved against us.
            return "stale_signal"
        if self.consecutive_order_errors >= MAX_CONSECUTIVE_ORDER_ERRORS:
            return "order_error_breaker"
        return None

    # -----------------------------------------------------------------
    # Real-order mode
    # -----------------------------------------------------------------

    def set_live_trading(self, enabled: bool) -> bool:
        """Turn real order placement on or off. Returns the state actually
        in effect, which is NOT always what was asked for: enabling without
        an order_client, or with a reconcile that failed, leaves it off.

        Enabling reconciles positions against Kalshi first and refuses to
        arm if that fails. Trading real money against an unverified position
        is the one failure here with no cheap recovery -- the strategy sizes
        every decision off ctx.position(ticker).

        Blocking (it makes REST calls). Called from the config-watch loop on
        a human toggle, not from the hot path, and deliberately kept inline
        rather than run on an executor: reconcile rewrites the Portfolio,
        which the strategy reads on every dispatch, so a few hundred
        milliseconds of event-loop stall is the cheaper hazard.
        """
        if not enabled:
            was_live = self.live_trading
            if was_live:
                print("[live] REAL TRADING DISABLED -- pulling every resting quote off Kalshi. "
                      "Any real POSITION already on the book stays there; nothing is flattened.")
                # Pull BEFORE flipping the flag: the manager's cancels go to
                # Kalshi only while live_trading is on. pull_all clears the
                # desired sets, so the pump has nothing to create meanwhile.
                self.pull_quotes()
            self.live_trading = False
            if was_live:
                # Blocking, and a full sweep rather than just our own map: an
                # order left over from a crashed process is invisible to the
                # map and is exactly what "stop trading" is supposed to mean.
                self.cancel_resting()
            return False
        if self.live_trading:
            return True
        if self.order_client is None:
            print("[live] WARN: cannot enable real trading -- no order client configured.")
            return False
        if self.halted:
            print(f"[live] REFUSING to enable real trading -- session is halted ({self.halt_reason}). "
                  f"Restart the process to resume.")
            return False
        # Both reads happen BEFORE the reset, so a dead auth path or an empty
        # account refuses the arm without having thrown away the shadow
        # session's record first.
        try:
            shard = self.order_client.exchange_index(self.event_ticker)
            balance = self.order_client.balance(exchange_index=shard)
            real_positions = self.order_client.positions(self.event_ticker)
            past_fills = self.order_client.fills(self.event_ticker)
        except Exception:
            print("[live] REFUSING to enable real trading -- could not read balance/positions/fills from Kalshi:")
            traceback.print_exc()
            return False
        if balance <= 0:
            print(f"[live] REFUSING to enable real trading -- balance on exchange shard {shard} "
                  f"(where {self.event_ticker} trades) is ${balance:.2f}. Move funds to that shard "
                  f"(kalshi.com/account/exchange-indexes, or POST /portfolio/intra_exchange_instance_transfer).")
            return False
        # An over-cap position does NOT block arming. It's the one state where
        # you most want the strategy running: it can only trade the reducing
        # side there, at zero lean, so arming is what walks it back down.
        over = {t: v["position"] for t, v in real_positions.items()
                if abs(v["position"]) > self.position_limit() + 1e-9}
        if over:
            print(f"[live] NOTE: {len(over)} market(s) already past the cap "
                  f"({', '.join(f'{t.split()[-1].split(chr(45))[-1]} {q:+.4g}' for t, q in over.items())}). "
                  f"Trading is armed anyway -- the strategy can only reduce these, and takes no lean "
                  f"penalty doing so. It halts if any of them grows further.")
        self.reset_session()
        self.seed_fills_from_kalshi(past_fills)
        self.reconcile_positions(real_positions)
        # Populates over_limit and seeds the growth baseline off the real
        # position. Cannot halt here: the baseline is empty, so nothing can
        # have "grown" yet -- which is what makes arming over the cap legal.
        self.check_position_limit()
        self.balance_at_arm = balance
        self.live_trading = True
        print(f"[live] *** REAL TRADING ENABLED -- orders are now being sent to Kalshi. "
              f"Balance ${balance:.2f}, halt at -${self.risk.max_loss_usd:.0f} real PnL this run "
              f"({self.loss_offset:+.2f} so far). ***")
        return True

    def add_session_reset_hook(self, fn):
        """Register a callback to clear state this engine doesn't own --
        the session's PnL/fair histories and the dashboard's snapshot
        files. Called by reset_session."""
        self._session_reset_hooks.append(fn)

    def reset_session(self):
        """Start a fresh tracking session: clear every fill, order and
        counter, and stamp a new session_started.

        Run when arming real trading. Up to that point the ledger is
        simulated -- shadow fills at shadow prices with modelled fees -- and
        carrying it into a real session would splice the two into one PnL
        curve, one fill list and one set of metrics that mean neither thing.
        A real session's numbers have to be answerable to the account
        statement, which only works if it starts empty.

        Market data (books, signals) is NOT cleared: it's the current state
        of the world, not a record of what this process did.
        """
        self.portfolio = Portfolio(fee_multiplier=self.portfolio.fee_multiplier,
                                   maker_fee_multiplier=self.portfolio.maker_fee_multiplier)
        self.orders = []
        self.unresolved_orders = []
        self.unresolved_reasons = {}
        self.blocked_counts = {}
        self.over_limit = {}
        self._last_abs_position = {}
        self._audit_strikes = {}
        # Anything resting belongs to the session being discarded, and its
        # order ids are about to become invalid -- pull it before the ledger
        # that knows about it is thrown away.
        self.pull_quotes()
        self.queue_trackers = []
        self.trackers_by_id = {}
        self.cancelled_ids = set()
        self.resolution_errors = 0
        self.n_real_orders = 0
        self.n_real_fills = 0
        self.real_order_errors = 0
        self.consecutive_order_errors = 0
        self.n_real_quotes = 0
        self.n_real_cancelled = 0
        self.failed_cancels = 0
        self._seen_real_fills = set()
        self._fill_poll_since = None
        # Shadow quotes were only modelled -- nothing to wait for. Real ones
        # were cancelled above and their acks are cancelled on arrival via the
        # discarded-session branch, so the manager can start empty.
        self.om.forget_all()
        self._shadow_taken = {}
        # Orders already decided but not yet resolved belong to the session
        # being discarded. Their in-flight counters are zeroed here and their
        # arrival callbacks bail out before touching anything -- see
        # _min_valid_order_id's uses in _resolve_arrival/_resolve_scheduled.
        self._pending_submissions = []
        self.n_scheduled = 0
        self.n_real_in_flight = 0
        # Not cleared: those POSTs are still coming back, and each one is
        # cancelled on arrival via the discarded-session branch.
        self._min_valid_order_id = self._peek_next_order_id()
        # The dashboard's decision log. Public and read by the snapshot; the
        # strategy's own signal cache is left alone (market data, see above).
        debug_log = getattr(self.strategy, "debug_log", None)
        if debug_log is not None:
            debug_log.clear()
        self.session_started = pd.Timestamp.now(tz="UTC").isoformat()
        for hook in self._session_reset_hooks:
            try:
                hook()
            except Exception:
                # A hook that fails must not leave trading armed against a
                # half-cleared session, but it also isn't worth aborting the
                # arm over -- it only ever clears display state.
                print("[live] WARN: session reset hook failed:")
                traceback.print_exc()

    def _peek_next_order_id(self) -> int:
        """The id the next order WILL get, without consuming it."""
        nxt = next(self._order_id_seq)
        self._order_id_seq = itertools.chain([nxt], self._order_id_seq)
        return nxt

    def seed_fills_from_kalshi(self, fills: list[dict]):
        """Replay this event's earlier fills into the fresh session, so the
        dashboard shows WHERE we already traded instead of a blank chart
        under a position that appeared from nowhere.

        Applied to positions as well as recorded, so cost basis comes from
        the prices actually paid. reconcile_positions runs immediately after
        and has the last word on quantities -- if the replay disagrees with
        Kalshi's own position record, that mismatch is logged and Kalshi
        wins. That check is the point: it turns the fills endpoint's
        `outcome_side` convention from an assumption into something verified
        on every arm.
        """
        for f in fills:
            order = Order(order_id=self.next_order_id(), ticker=f["ticker"], side=f["side"],
                          order_type="limit", price=f["price"], size=f["size"],
                          decision_time=pd.Timestamp(f["time"]))
            self.portfolio.record_fill(order, f["price"], f["size"], pd.Timestamp(f["time"]),
                                       f["liquidity"], fee=f["fee"], exchange_order_id=f.get("order_id"),
                                       trade_id=f.get("trade_id"), seeded=True)
        # Seeded orders must not be resolvable -- they already executed.
        self._min_valid_order_id = self._peek_next_order_id()
        if fills:
            tickers = len({f["ticker"] for f in fills})
            print(f"[live] seeded {len(fills)} earlier fill(s) across {tickers} market(s) from Kalshi")

    def reconcile_positions(self, real: dict | None = None):
        """Overwrite the in-memory positions with Kalshi's own record for
        this event. Fetches them unless `real` is passed (set_live_trading
        pre-fetches, so a network failure refuses the arm before anything
        has been cleared).

        Required when arming real trading: a freshly reset session starts
        with an empty ledger, but the account may already hold a position
        from earlier in the day, and the strategy reads ctx.position(ticker)
        for both its per-market cap and its lean. Starting from a fictional
        zero would let it buy straight through the cap.

        Quantities and cost basis come from Kalshi (`market_exposure` is
        that position's cost), so the seeded position has no corresponding
        fills -- the account statement, not this process, is what it is
        answerable to.
        """
        if real is None:
            if self.order_client is None or self.event_ticker is None:
                raise RuntimeError("reconcile_positions needs an order_client and an event_ticker")
            real = self.order_client.positions(self.event_ticker)
        changed = []
        tickers = set(real) | set(self.portfolio.positions)
        for ticker in tickers:
            pos = self.portfolio.position(ticker)
            before = pos.yes_qty - pos.no_qty
            info = real.get(ticker, {"position": 0.0, "market_exposure": 0.0, "fees_paid": None})
            qty = info["position"]
            cost = info["market_exposure"]
            pos.yes_qty = max(qty, 0.0)
            pos.no_qty = max(-qty, 0.0)
            # Both legs can't be non-zero: Kalshi nets a market to one signed
            # position, so whichever leg is flat has no cost either.
            pos.yes_cost = cost if qty > 0 and cost is not None else 0.0
            pos.no_cost = cost if qty < 0 and cost is not None else 0.0
            if info["fees_paid"] is not None:
                pos.fees_paid = info["fees_paid"]
            if abs(before - qty) > 1e-9:
                changed.append(f"{ticker} {before:+.4g} -> {qty:+.4g}")
        self.last_reconcile = pd.Timestamp.now(tz="UTC").isoformat()
        if changed:
            # After seed_fills_from_kalshi this is a real disagreement between
            # replaying the fills and Kalshi's own position record -- i.e. the
            # fills endpoint's side convention didn't mean what we read it to
            # mean. Kalshi wins, but it's worth knowing it happened.
            self.reconcile_mismatches = changed
            print(f"[live] reconcile OVERRODE {len(changed)} position(s) from Kalshi: {'; '.join(changed)}")
        else:
            self.reconcile_mismatches = []
            print("[live] reconciled against Kalshi: replayed fills match the position record")

    def update_book(self, book: BookView):
        self.books[book.ticker] = book

    def current_pnl(self, mark: str = "touch") -> float:
        """Mark-to-market PnL across every ticker this engine has touched.

        Two conventions, because they answer different questions and the
        gap between them is large -- measured live at $131 on one KXINXU
        ladder, enough to flip the session from -$58 to +$74:

        mark="touch" (default): yes at the bid, no at 1-ask, i.e. what the
        inventory could actually be liquidated for right now. Books the full
        spread as a loss the instant a fill happens. Conservative, and the
        right basis for the failsafe -- a risk limit should not be talked out
        of tripping by our own model.

        mark="prob": marks at the option-implied probability, matching
        markout_analysis.compute_pnl_over_day so the live curve is directly
        comparable to a backtest of the same day. Note this marks to our OWN
        model: if the model is wrong, this number flatters us, which is
        exactly why it doesn't gate the failsafe. Falls back to touch marking
        for any ticker with no current signal.
        """
        return float(sum(self.pnl_by_ticker(mark).values()))

    def pnl_by_ticker(self, mark: str = "touch") -> dict[str, float]:
        """current_pnl split per market, same marking rules."""
        out = {}
        fair_probs = self._fair_probs() if mark == "prob" else {}
        for ticker, pos in self.portfolio.positions.items():
            book = self.books.get(ticker)
            yes_bid = book.bid if book is not None else None
            yes_ask = book.ask if book is not None else None
            p = fair_probs.get(ticker)
            if p is not None:
                out[ticker] = pos.unrealized_pnl(p, p)   # both legs marked at the model's fair value
            else:
                out[ticker] = pos.unrealized_pnl(yes_bid, yes_ask)
        return out

    def submit_order(self, order: Order, now: pd.Timestamp):
        """Live analog of engine.py's order_arrival handler.

        The order is NOT resolved here. It's resolved after
        send_s + processing_s against whatever the book says at that later
        moment, mirroring the backtest's arrival_time exactly -- see
        _resolve_arrival. Resolving on submission instead modelled zero send
        latency, which made the live sim strictly more optimistic than the
        backtest it's meant to validate: any order placed on a touch that
        vanished within the round trip filled live but not in the backtest.

        Queued here rather than scheduled immediately: the arrival delay
        includes THIS dispatch's own processing time, which isn't known until
        the strategy callback returns. dispatch() measures it and flushes the
        queue -- the same order of operations engine.py uses (measure the
        callback, then schedule its pending orders with that processing_s)."""
        self.orders.append(order)
        self._pending_submissions.append(order)

    def _resolve_arrival(self, order: Order, delay_s: float):
        """Resolve `order` against the book as it stands NOW (i.e. at
        arrival), not as it stood when the strategy decided."""
        now = pd.Timestamp.now(tz="UTC")
        if order.order_id < self._min_valid_order_id:
            return   # decided in a session that has since been reset
        blocked = self.blocked_reason()
        if blocked is not None:
            self.blocked_counts[blocked] = self.blocked_counts.get(blocked, 0) + 1
            self._unresolved(order, blocked)
            return
        if order.order_id in self.cancelled_ids:
            return
        book = self.books.get(order.ticker)
        if book is None:
            self._unresolved(order, "no_book")
            return
        if (now - book.time).total_seconds() > MAX_BOOK_AGE_S:
            self._unresolved(order, "stale_book")
            return
        if self.live_trading:
            # Every guard above still applies -- halt, cancel, stale book are
            # exactly the conditions under which a real order is worst. Past
            # them, Kalshi's matching engine replaces execution.py: an IOC
            # either crosses on arrival or is cancelled, the outcome set
            # resolve_marketable models. (Quotes never come through here --
            # see _om_create.)
            self._send_real_order(order)
            return
        book = self._net_of_shadow_takes(order, book)
        if book is None:
            self._unresolved(order, "shadow_depth_consumed")
            return
        fill = resolve_marketable(order, book)
        if fill is not None:
            self._record_shadow_take(order, book, fill.size)
            self.portfolio.record_fill(order, fill.price, fill.size, now, fill.liquidity)
            self.check_position_limit()
            return
        # A take is an IOC: nothing rests in shadow either.
        self._unresolved(order, "ioc_no_fill")

    # A simulated take doesn't remove anything from Kalshi's real book, so
    # without this the same touch level is "available" to every later order
    # too. Observed 2026-10-08: a 0.01-contract ask on KXINX B7737 was taken
    # 779 times in 4 minutes of shadow. Keyed on the depth we saw, so the
    # ledger resets the moment Kalshi's own depth at that price changes.
    def _net_of_shadow_takes(self, order: Order, book: BookView) -> BookView | None:
        """`book` with our own earlier shadow takes at the touch subtracted,
        or None if they have used up all of it."""
        touch = book.touch_price(order.side)
        depth = book.ask_depth if order.side == "yes" else book.bid_depth
        if touch is None or depth is None:
            return book
        key = (order.ticker, order.side, round(touch, 2))
        seen = self._shadow_taken.get(key)
        if seen is None or abs(seen[0] - depth) > 1e-9:
            self._shadow_taken.pop(key, None)
            return book
        left = depth - seen[1]
        if left <= 1e-9:
            return None
        field = "ask_depth" if order.side == "yes" else "bid_depth"
        return dataclasses.replace(book, **{field: left})

    def _record_shadow_take(self, order: Order, book: BookView, size: float):
        touch = book.touch_price(order.side)
        if touch is None:
            return
        raw = self.books.get(order.ticker)
        depth = (raw.ask_depth if order.side == "yes" else raw.bid_depth) if raw is not None else None
        if depth is None:
            return
        key = (order.ticker, order.side, round(touch, 2))
        prev = self._shadow_taken.get(key, (depth, 0.0))
        self._shadow_taken[key] = (depth, prev[1] + size)

    def _send_real_order(self, order: Order):
        """Fire `order` at Kalshi as an IOC take, off the event loop.

        The POST is ~40ms of blocking I/O and this is called from a
        call_later callback on the loop thread, so it goes to the default
        executor; the response is recorded back on the loop thread (asyncio
        future callbacks run there), which is what keeps Portfolio mutation
        single-threaded against the strategy's own reads."""
        if not self.order_client.allow_now("order", reason="take"):
            self._unresolved(order, "rate_limited")
            return
        self.n_real_orders += 1
        self.n_real_in_flight += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._on_real_result(order, self._place_blocking(order))
            return
        fut = loop.run_in_executor(None, self._place_blocking, order)
        fut.add_done_callback(lambda f: self._on_real_result(order, f.result()))

    def _place_blocking(self, order: Order) -> tuple:
        """Runs on a worker thread -- must touch no engine state. Returns
        (fill, exception); raising here instead would surface only as a
        swallowed future exception."""
        try:
            return self.order_client.place_ioc(order), None
        except Exception as exc:
            return None, exc

    def _on_real_result(self, order: Order, result: tuple):
        """Back on the event loop thread: record what Kalshi actually did."""
        if order.order_id < self._min_valid_order_id:
            # Discarded session -- same reasoning as _resolve_scheduled. The
            # order really did reach Kalshi, so any fill it got is picked up
            # by the reconcile that follows the reset, not booked here.
            return
        self.n_real_in_flight -= 1
        fill, exc = result
        if exc is not None and http_status(exc) == 429:
            self.order_client.note_rate_limited("take")
            self._unresolved(order, "rate_limited_429")
            return
        if exc is not None:
            self.real_order_errors += 1
            self.consecutive_order_errors += 1
            self._unresolved(order, "real_order_error")
            print(f"[live] ERROR placing REAL order {order.order_id} "
                  f"({order.ticker} {order.side} {order.size}@{order.price}): {exc!r}")
            if self.consecutive_order_errors >= MAX_CONSECUTIVE_ORDER_ERRORS:
                print(f"[live] CIRCUIT BREAKER: {self.consecutive_order_errors} consecutive real-order "
                      f"errors -- no further orders will be sent. Fix the cause and restart.")
            return
        # A clean response, filled or not, proves the API path is healthy --
        # the breaker is for a broken path, not for a book that moved.
        self.consecutive_order_errors = 0
        if fill is None:
            # IOC took nothing -- the touch moved inside the round trip.
            # Nothing rests at the exchange, so there's no cleanup.
            self._unresolved(order, "ioc_no_fill")
            return
        # An IOC fills at or better than its limit -- never worse. A fill
        # outside that band means we cannot account for what we just did, so
        # the ledger (and every PnL number the failsafe reads off it) is not
        # trustworthy: halt rather than book it and carry on. This is the
        # check that would have caught the 100x unit bug on its first fill
        # instead of after 48 of them.
        if fill.price > order.price + PRICE_SANITY_TOL or fill.price < 0.0:
            self.halted = True
            self.halt_reason = (f"fill price {fill.price:.4f} outside limit {order.price:.4f} on "
                                f"{order.ticker} {order.side} -- cannot account for this fill")
            print(f"[live] HALT: {self.halt_reason}. Booking it anyway so the ledger still reflects "
                  f"the trade, but NO further orders will be sent -- check kalshi.orders._parse_fill "
                  f"against Kalshi's current response shape.")
        self.n_real_fills += 1
        self.portfolio.record_fill(order, fill.price, fill.size, pd.Timestamp.now(tz="UTC"),
                                   "taker", fee=fill.fee, exchange_order_id=fill.exchange_order_id)
        print(f"[live] REAL FILL {order.ticker} {order.side} {fill.size:g} @ {fill.price:.4f} "
              f"(fee ${fill.fee:.4f})")
        self.check_position_limit()

    # -----------------------------------------------------------------
    # Quotes: executor for the OrderManager
    # -----------------------------------------------------------------
    #
    # The quoter publishes a desired ladder per (ticker, side) through
    # LiveContext.set_quote_ladder; the OrderManager diffs it against what is
    # live and calls back here to create / cancel. Real mode sends to Kalshi;
    # shadow mode registers a QueueTracker after the modelled send latency.
    # Both go through the same manager, budget and priorities.

    def _om_bucket_tokens(self) -> float | None:
        """Tokens left in the account-wide write bucket, or None in shadow
        (where the manager meters only against the quote budget)."""
        if not self.live_trading or self.order_client is None:
            return None
        return self.order_client.tokens_now()

    def _om_create(self, q) -> bool:
        """Called by OrderManager.pump for one create (already registered in
        the manager). Returns False if it may not be sent now."""
        if self.blocked_reason() is not None:
            return False
        book = self.books.get(q.ticker)
        if book is None or (pd.Timestamp.now(tz="UTC") - book.time).total_seconds() > MAX_BOOK_AGE_S:
            return False
        if self.live_trading and not self.order_client.allow_now("order", reason=f"new_quote/{q.role}"):
            return False
        order = Order(order_id=q.order_id, ticker=q.ticker, side=q.side, order_type="limit",
                      price=q.price, size=q.size, decision_time=pd.Timestamp.now(tz="UTC"), intent="quote")
        order.role = q.role
        self.orders.append(order)
        self._quote_orders[order.order_id] = order
        if self.live_trading:
            self.n_real_orders += 1
            self.n_real_quotes += 1
            self.n_real_in_flight += 1
            self._run_blocking(lambda: self._rest_blocking(order),
                               lambda res: self._on_real_quote_result(order, res))
            return True
        # Shadow: lands after the modelled send latency, against the book then.
        self._schedule_shadow(order)
        return True

    def _om_cancel(self, q, reason: str):
        """Called by the OrderManager for one cancel (it has already checked
        the bucket). Tokens are debited HERE, on the loop thread, so the
        manager's very next bucket read sees them."""
        order = self._quote_orders.get(q.order_id)
        if not self.live_trading or q.exchange_id is None:
            # Shadow (or a real order whose create never acked into a real
            # session): stop its queue model and confirm at once.
            tracker = self.trackers_by_id.get(q.order_id)
            if tracker is not None:
                tracker.cancelled = True
            self.cancelled_ids.add(q.order_id)
            self.om.on_cancel_done(q.order_id)
            return
        tag = f"{reason}/{q.role}"
        self.order_client.allow_now("cancel", force=True, reason=tag)
        exchange_id, ticker = q.exchange_id, (order.ticker if order is not None else q.ticker)
        self._run_blocking(lambda: self._cancel_send(exchange_id, ticker),
                           lambda res: self._on_cancel_result(q.order_id, res, reason))

    def _om_amend(self, q, price: float, remaining: float) -> bool:
        """Move a resting quote to `price` (own-side) and `remaining` resting
        contracts with one amend. Shadow: the queue model has no amend, so a
        price change retires the old tracker and starts a new one at the back
        of the new price's queue -- what Kalshi does; a pure size cut keeps
        its place. Returns False if it may not be sent now."""
        if self.blocked_reason() is not None:
            return False
        order = self._quote_orders.get(q.order_id)
        if order is None:
            return False
        moved = abs(price - order.price) > 1e-9
        if not self.live_trading:
            old = self.trackers_by_id.get(q.order_id)
            filled = old.filled_qty if old is not None else q.filled
            order.price = price
            order.size = filled + remaining
            if old is not None and not moved:
                old.order.size = order.size          # size change in place
            else:
                if old is not None:
                    old.cancelled = True
                book = self.books.get(q.ticker)
                if book is not None:
                    tr = resolve_resting(order, book)
                    if not tr.known:
                        tr = QueueTracker(order, price, 0.0)
                    tr.filled_qty = filled
                    self.queue_trackers.append(tr)
                    self.trackers_by_id[q.order_id] = tr
            self.om.on_amended(q.order_id, ok=True)
            return True
        if not self.order_client.allow_now("amend", reason=f"move/{q.role}"):
            return False
        filled = q.filled
        self.n_real_amends += 1

        def work():
            try:
                return self.order_client.amend(q.exchange_id, order, remaining, filled, price=price), None
            except Exception as exc:
                return None, exc

        def done(res):
            resp, exc = res
            if exc is None:
                order.price = price
                order.size = filled + remaining
                self.om.on_amended(q.order_id, ok=True)
                return
            st = http_status(exc)
            if st == 404:
                self.om.on_amended(q.order_id, ok=False, gone=True)   # filled/cancelled first
                return
            if st == 429:
                self.order_client.note_rate_limited("move")
            else:
                self.failed_amends += 1
                print(f"[live] WARN: move of quote {q.order_id} to {price} failed ({exc!r}); left at old price")
            self.om.on_amended(q.order_id, ok=False)

        self._run_blocking(work, done)
        return True

    def _run_blocking(self, work, done):
        """Run `work` on the default executor and `done(result)` back on the
        loop; inline when there is no running loop (tests)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            done(work())
            return
        loop.run_in_executor(None, work).add_done_callback(
            lambda f: loop.call_soon_threadsafe(done, f.result()) if not loop.is_closed() else None)

    def _schedule_shadow(self, order: Order):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._shadow_quote_lands(order)
            return
        loop.call_later(SHADOW_SEND_S, self._shadow_quote_lands, order)

    def _shadow_quote_lands(self, order: Order):
        """Shadow analog of a create ack: cross what's marketable at arrival
        (a quote priced into a book that moved), rest the remainder behind
        the displayed queue."""
        if order.order_id < self._min_valid_order_id or self.om.quotes.get(order.order_id) is None:
            return
        q = self.om.quotes[order.order_id]
        book = self.books.get(order.ticker)
        if book is None:
            self.om.on_create_failed(order.order_id, rejected=False)
            return
        fill = resolve_marketable(order, book)
        filled = 0.0
        if fill is not None:
            filled = fill.size
            self.portfolio.record_fill(order, fill.price, fill.size, pd.Timestamp.now(tz="UTC"), "taker")
            self.check_position_limit()
        if order.size - filled > 1e-9 and q.state != om.CANCELLING:
            tracker = resolve_resting(order, book)
            if not tracker.known:
                tracker = QueueTracker(order, order.price, 0.0)
            tracker.filled_qty = filled
            self.queue_trackers.append(tracker)
            self.trackers_by_id[order.order_id] = tracker
        self.om.on_created(order.order_id, f"shadow-{order.order_id}", filled=filled)

    def _rest_blocking(self, order: Order) -> tuple:
        """Worker thread -- touches no engine state. Returns (ack, exception)."""
        try:
            return self.order_client.place_resting(order), None
        except Exception as exc:
            return None, exc

    def _on_real_quote_result(self, order: Order, result: tuple):
        """Back on the event loop: register what is now resting, and book
        whatever crossed on the way in."""
        ack, exc = result
        self.n_real_in_flight -= 1
        if order.order_id < self._min_valid_order_id:
            # Session discarded while in flight: nothing here remembers it,
            # so cancel it on the way past.
            if ack is not None and ack.exchange_order_id is not None:
                self._cancel_exchange_order(ack.exchange_order_id, order.ticker, "orphan")
            return
        if exc is not None and http_status(exc) == 429:
            # Throttled, not broken: nothing rests; the manager re-queues it.
            self.order_client.note_rate_limited("new_quote")
            self.om.on_create_failed(order.order_id, rejected=False)
            self._unresolved(order, "rate_limited_429")
            return
        if exc is not None and http_status(exc) is None:
            # No HTTP answer (timeout, connection reset): the order may be
            # resting at Kalshi. Keep the slot occupied and go find it by
            # client id -- freeing it would let the ladder stack a second
            # order on an unknown first one.
            self.real_order_errors += 1
            self.consecutive_order_errors += 1
            print(f"[live] WARN: no response resting quote {order.order_id} "
                  f"({order.ticker} {order.side} {order.size}@{order.price}): {exc!r}; looking it up")
            q = self.om.quotes.get(order.order_id)
            if q is not None:
                q.state = om.CANCELLING
            self._run_blocking(lambda: self._find_lost(order),
                               lambda res: self._on_lost_found(order, res))
            return
        if exc is not None:
            self.real_order_errors += 1
            self.consecutive_order_errors += 1
            self.om.on_create_failed(order.order_id, rejected=True)
            self._unresolved(order, "real_quote_error")
            print(f"[live] ERROR resting REAL quote {order.order_id} "
                  f"({order.ticker} {order.side} {order.size}@{order.price}): {exc!r}")
            if self.consecutive_order_errors >= MAX_CONSECUTIVE_ORDER_ERRORS:
                print(f"[live] CIRCUIT BREAKER: {self.consecutive_order_errors} consecutive real-order "
                      f"errors -- no further orders will be sent. Fix the cause and restart.")
            return
        self.consecutive_order_errors = 0
        filled = 0.0
        if ack.fill is not None:
            # Priced a tick inside the far touch when sent, so this only
            # happens when the book moved during the round trip. Same limit
            # sanity check as a take.
            if ack.fill.price > order.price + PRICE_SANITY_TOL or ack.fill.price < 0.0:
                self.halted = True
                self.halt_reason = (f"quote fill price {ack.fill.price:.4f} outside limit "
                                    f"{order.price:.4f} on {order.ticker} {order.side}")
                print(f"[live] HALT: {self.halt_reason}. Booking it anyway so the ledger reflects "
                      f"the trade, but NO further orders will be sent.")
            filled = ack.fill.size
            self._book_real_fill(order, ack.exchange_order_id, None, ack.fill.price, ack.fill.size,
                                 ack.fill.fee, "taker", pd.Timestamp.now(tz="UTC"))
        if ack.parse_error is not None:
            # A fill we could not read: our position is unknown.
            self.halted = True
            self.halt_reason = f"unreadable fill on quote {order.ticker} {order.side}: {ack.parse_error!r}"
            print(f"[live] HALT: {self.halt_reason}. Pulling this quote; check "
                  f"kalshi.orders._parse_fill against Kalshi's current response shape.")
            if ack.exchange_order_id is not None:
                self._cancel_exchange_order(ack.exchange_order_id, order.ticker, "orphan")
            self.om.on_create_failed(order.order_id, rejected=False)
            return
        if ack.exchange_order_id is None and order.size - filled > 1e-9:
            # On the book with no handle to cancel it by.
            self.halted = True
            self.halt_reason = (f"quote on {order.ticker} {order.side} rested with no order_id in the "
                                f"response -- it cannot be cancelled from here")
            print(f"[live] HALT: {self.halt_reason}. CANCEL IT BY HAND on Kalshi.")
            self.om.on_create_failed(order.order_id, rejected=False)
            return
        if ack.exchange_order_id is not None:
            self._exchange_to_quote[ack.exchange_order_id] = order.order_id
        # The manager cancels it at once if it was cancelled in flight, or if
        # the edge moved through it while it was in flight.
        self.om.on_created(order.order_id, ack.exchange_order_id, filled=filled)
        if not self.live_trading:
            q = self.om.quotes.get(order.order_id)
            if q is not None:
                self.om._cancel(q, "disarmed_in_flight")

    def _find_lost(self, order: Order) -> tuple:
        """Worker thread: did a create with no response land? Returns
        ((exchange_id, ticker) | None, exception)."""
        cid = getattr(order, "client_order_id", None)
        if cid is None or self.event_ticker is None:
            # The POST never got as far as building its body: nothing was sent.
            return None, None
        try:
            return self.order_client.find_by_client_id(self.event_ticker, cid), None
        except Exception as exc:
            return None, exc

    def _on_lost_found(self, order: Order, result: tuple):
        """Cancel a create that landed despite no response; free the slot only
        once we know it isn't resting. If the lookup itself fails the slot
        stays held -- the shutdown/disarm sweep cancels by listing."""
        hit, exc = result
        if exc is not None:
            print(f"[live] WARN: lookup of lost quote {order.order_id} failed ({exc!r}); "
                  f"slot held until the next sweep")
            return
        if hit is None:
            self.om.on_create_failed(order.order_id, rejected=False)
            return
        exchange_id, ticker = hit
        self._exchange_to_quote[exchange_id] = order.order_id
        q = self.om.quotes.get(order.order_id)
        if q is not None:
            q.exchange_id = exchange_id
            self._om_cancel(q, "lost_create")

    def _cancel_send(self, exchange_id: str, ticker: str | None) -> tuple:
        """Worker thread -- touches no engine state; tokens already debited.
        Returns (ok, exception)."""
        try:
            return self.order_client.cancel(exchange_id, ticker), None
        except Exception as exc:
            return None, exc

    def _cancel_blocking(self, exchange_id: str, ticker: str | None = None, reason: str = "untagged") -> tuple:
        """Debit + send, for cancels outside the manager (orphans, the sweep)."""
        self.order_client.allow_now("cancel", force=True, reason=reason)
        return self._cancel_send(exchange_id, ticker)

    def _on_cancel_result(self, order_id: int, result: tuple, reason: str = "retry"):
        ok, exc = result
        if exc is not None:
            if http_status(exc) == 429:
                # Throttled, not refused: re-queue; it goes again as the
                # bucket refills, ahead of any create.
                self.order_client.note_rate_limited("cancel")
                self.om.on_cancel_failed(order_id, reason)
                return
            self.failed_cancels += 1
            self.om.on_cancel_failed(order_id, reason)
            print(f"[live] ERROR cancelling resting order {order_id}: {exc!r} -- it is still on "
                  f"the book and will be retried.")
            return
        # ok is False for a 404: already gone, usually because it filled. The
        # fill feed still matches it by exchange id (_exchange_to_quote keeps it).
        self.n_real_cancelled += 1
        self.om.on_cancel_done(order_id)

    def _cancel_exchange_order(self, exchange_id: str, ticker: str | None = None, reason: str = "orphan"):
        """Cancel by exchange id, for orders we never got to track (a session
        reset or an unreadable response mid-flight). Pass the ticker: without
        it the cancel can't be routed to a non-zero shard."""
        self._run_blocking(lambda: self._cancel_blocking(exchange_id, ticker, reason), lambda res: None)

    # -----------------------------------------------------------------
    # Real fills (websocket `fill` channel, plus a REST backstop)
    # -----------------------------------------------------------------

    def _book_real_fill(self, order: Order, exchange_id, trade_id, price, size, fee, liquidity, when):
        key = (exchange_id, trade_id)
        if trade_id is not None:
            if key in self._seen_real_fills:
                return False
            self._seen_real_fills.add(key)
        if trade_id is None:
            # Booked off the create response, which has no trade id: the fill
            # feed reports the same execution again later, with one.
            if exchange_id is not None:
                self._ack_booked[exchange_id] = self._ack_booked.get(exchange_id, 0.0) + size
        elif liquidity == "taker" and self._ack_booked.get(exchange_id, 0.0) > 1e-9:
            # Consumed by size, not matched by trade: a cross that swept several
            # levels is one summed fill in the ack but one feed message per level.
            # Double-booking these halted the audit (2026-10-10 26OCT101115 BTC/ETH).
            credit = self._ack_booked[exchange_id]
            used = min(credit, size)
            self._ack_booked[exchange_id] = credit - used
            if size - used <= 1e-9:
                return False
            fee = fee * (size - used) / size
            size -= used
        self.n_real_fills += 1
        self.portfolio.record_fill(order, price, size, when, liquidity, fee=fee,
                                   exchange_order_id=exchange_id, trade_id=trade_id)
        self.om.on_fill(order.order_id, size)
        print(f"[live] REAL QUOTE FILL {order.ticker} {order.side} {size:g} @ {price:.4f} "
              f"({liquidity}, fee ${fee:.4f})")
        return True

    def on_real_fill(self, f: dict):
        """One fill of OUR order, from the websocket fill channel or the REST
        backstop poll (same dict shape as KalshiOrderClient.fills). Takes are
        already booked off their create response, so only resting quotes are
        matched here -- by exchange order id, deduped on (order_id, trade_id)."""
        our_id = self._exchange_to_quote.get(f.get("order_id"))
        if our_id is None:
            return
        order = self._quote_orders.get(our_id)
        if order is None:
            return
        if f["side"] != order.side:
            print(f"[live] WARN: fill on {f['ticker']} reports side {f['side']} for our "
                  f"{order.side} quote {our_id} -- booking as {order.side}")
        when = pd.Timestamp(f["time"]) if f.get("time") is not None else pd.Timestamp.now(tz="UTC")
        if self._book_real_fill(order, f.get("order_id"), f.get("trade_id"), f["price"], f["size"],
                                f["fee"], f["liquidity"], when):
            self.check_position_limit()

    def apply_real_fills(self, fills: list[dict]):
        for f in fills:
            self.on_real_fill(f)
        self._fill_poll_since = int(time.time()) - 2

    def fill_poll_min_ts(self) -> int | None:
        """`min_ts` for the REST backstop poll: a couple of seconds behind
        what we've already read, so a boundary fill is read twice (deduped)
        rather than never."""
        return self._fill_poll_since

    def has_quotes(self) -> bool:
        return bool(self.om.quotes)

    def requeue_lip(self, positions: dict[str, float], min_ahead: float) -> list[tuple]:
        """Move LIP quotes that have worked toward the front of their queue
        back to the end. `positions`: Kalshi's queue position (contracts ahead
        at the order's own price) per exchange id. One with fewer than
        `min_ahead` ahead and size displayed behind it is re-queued,
        fewest-ahead first, while the write bucket allows. Returns
        [(ticker, side, price, ahead, behind, requeued)] for logging."""
        cands = []
        for q in self.om.live(role="lip"):
            if q.state != om.RESTING or q.exchange_id not in positions:
                continue
            ahead = positions[q.exchange_id]
            book = self.books.get(q.ticker)
            ladder = getattr(book, "ladder", None) if book is not None else None
            level = 0.0 if ladder is None else \
                (ladder.yes_bids if q.side == "yes" else ladder.no_bids).get(round(q.price, 2), 0.0)
            behind = max(level - ahead - q.remaining, 0.0)
            self.lip_queue[(q.ticker, q.side)] = (ahead, behind)
            cands.append((ahead, behind, q))
        out = []
        for ahead, behind, q in sorted(cands, key=lambda c: c[0]):
            # Already last: re-queueing would change nothing and spend a write.
            want = ahead + 1e-9 < min_ahead and behind > 1e-9
            # Front quarter of the threshold: next in line for a sweep, so it
            # may use any token above the cancel reserve; otherwise only spare ones.
            urgent = ahead < min_ahead / 4
            done = self.om.requeue_lip(q, urgent) if want else False
            out.append((q.ticker, q.side, q.price, ahead, behind, done))
        return out

    def has_uncancelled_quotes(self) -> bool:
        return any(q.state != om.CANCELLING for q in self.om.quotes.values())

    def has_quote_history(self) -> bool:
        """Any real quote reached Kalshi this session, so its fills may still
        need booking (one that was cancelled can have filled first)."""
        return bool(self._exchange_to_quote)

    def pull_quotes(self):
        """Withdraw every quote, real or shadow (stale signal, halt, disarm,
        session reset). Nothing is re-created until the quoter publishes
        again. Cancelling a resting offer is not flattening."""
        self.om.pull_all("pull")

    def cancel_resting(self):
        """BLOCKING sweep: cancel every order this account still has resting
        in our event, whether or not we know about it -- a crashed
        predecessor, a cancel that errored, an order whose response never came
        back. Used when disarming and when shutting the event down."""
        if self.order_client is None or self.event_ticker is None:
            return
        # Several passes: a POST in flight when the sweep starts lands after a
        # single listing, and Kalshi's order list lags a fresh create slightly.
        deadline = time.monotonic() + SWEEP_MAX_S
        empty_passes = 0
        while empty_passes < 2 and time.monotonic() < deadline:
            try:
                resting = self.order_client.resting_orders(self.event_ticker)
            except Exception:
                print(f"[live] WARN: could not list resting orders for {self.event_ticker}; "
                      f"CHECK KALSHI BY HAND for leftover quotes:")
                traceback.print_exc()
                time.sleep(SWEEP_PASS_S)
                continue
            if not resting:
                empty_passes += 1
            else:
                empty_passes = 0
                print(f"[live] cancelling {len(resting)} resting order(s) on {self.event_ticker}")
                for exchange_id, ticker in resting:
                    try:
                        self.order_client.allow_now("cancel", force=True, reason="sweep")
                        self.order_client.cancel(exchange_id, ticker)
                    except Exception as exc:
                        self.failed_cancels += 1
                        print(f"[live] ERROR cancelling resting order {exchange_id}: {exc!r}")
            time.sleep(SWEEP_PASS_S)
        if empty_passes < 2:
            print(f"[live] WARN: resting orders may remain on {self.event_ticker} after "
                  f"{SWEEP_MAX_S:.0f}s of sweeping -- CHECK KALSHI BY HAND.")
        # Everything is off the book; ids stay in _exchange_to_quote so a fill
        # that raced the cancel still books.
        self.om.forget_all()

    def _unresolved(self, order: Order, reason: str):
        """Record a non-fill WITH its cause. A bare count can't distinguish
        "the touch moved inside the round trip" (the latency model working)
        from "our book had no depth there" (a data-fidelity gap vs the
        backtest's replayed L2), and those call for opposite responses."""
        self.unresolved_orders.append(order)
        self.unresolved_reasons[reason] = self.unresolved_reasons.get(reason, 0) + 1

    def cancel_order(self, order_id: int, reason: str = "strategy"):
        """Cancel one order by our id: a quote goes through the manager (P0,
        never budgeted); anything else only stops its shadow queue model."""
        q = self.om.quotes.get(order_id)
        if q is not None:
            self.om._cancel(q, reason)
            return
        self.cancelled_ids.add(order_id)
        tracker = self.trackers_by_id.get(order_id)
        if tracker is not None:
            tracker.cancelled = True

    def on_trade(self, ticker: str, trade: Trade, now: pd.Timestamp):
        """Live analog of engine.py's trade_true handler -- advances every
        live resting order on this ticker's queue against a real printed
        trade."""
        for tracker in self.queue_trackers:
            if tracker.done or tracker.order.ticker != ticker:
                continue
            fill = tracker.on_trade(trade)
            if fill is not None:
                self.portfolio.record_fill(tracker.order, fill.price, fill.size, now, fill.liquidity)
                self.om.on_fill(tracker.order.order_id, fill.size)
                self.check_position_limit()
        self.queue_trackers = [t for t in self.queue_trackers if not t.done]

    def dispatch(self, last_event: object, now: pd.Timestamp):
        """Builds a fresh LiveContext and calls the strategy -- the live
        counterpart of engine.py's "wakeup" handler.

        The callback's own wall-clock cost is measured and carried into the
        next order's arrival delay, the same way the backtest instruments
        processing_s: a strategy that thinks longer really does reach the
        exchange later."""
        ctx = LiveContext(time=now, last_event=last_event, portfolio=self.portfolio,
                          _books=self.books, _engine=self)
        t0 = time.perf_counter()
        try:
            self.strategy(ctx)
        finally:
            processing_s = time.perf_counter() - t0
            # Takes first (P1), then whatever creates the quote budget allows.
            self._flush_pending(processing_s)
            self.pump_quotes()

    def pump_quotes(self):
        """Send queued quote creates. Also called on a timer by the session,
        so a backlog drains as the budget refills even when no market moves."""
        if self.blocked_reason() is not None:
            return
        self.om.pump()

    def _flush_pending(self, processing_s: float):
        """Schedule every order this dispatch placed to resolve after
        send_s + processing_s. Without a running event loop (tests, any
        synchronous caller) they resolve immediately, preserving the old
        behaviour rather than silently never resolving.

        REAL mode waits for NOTHING. send_s exists to model the trip to the
        exchange so a shadow fill isn't more optimistic than the backtest --
        but when the order is genuinely being sent, that trip is about to
        happen for real. Sleeping it out first paid the latency twice:
        ~34ms of modelled send plus the strategy's own processing time,
        purely as dead air ahead of a POST that then takes its own ~34ms.
        On a strategy whose entire edge is being early to an option
        repricing, that was the single largest removable delay in the path.
        """
        if not self._pending_submissions:
            return
        pending, self._pending_submissions = self._pending_submissions, []
        delay = 0.0 if self.live_trading else SHADOW_SEND_S + processing_s
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for order in pending:
            if loop is not None and delay > 0:
                self.n_scheduled += 1
                loop.call_later(delay, self._resolve_scheduled, order, delay)
            else:
                self._resolve_arrival(order, 0.0)

    def _resolve_scheduled(self, order: Order, delay_s: float):
        """call_later target. Decrements the in-flight counter even if
        resolution raises -- asyncio swallows exceptions from a call_later
        callback, so without this an order that blew up mid-resolution would
        silently vanish from the accounting entirely."""
        if order.order_id < self._min_valid_order_id:
            # Discarded session. reset_session already zeroed n_scheduled, so
            # this must return BEFORE the decrement below or it goes negative.
            return
        try:
            self._resolve_arrival(order, delay_s)
        except Exception:
            # asyncio's default handler routes this to a logger that this
            # process doesn't configure, so without printing it here a
            # resolution failure is completely invisible -- the order just
            # disappears from the fill/unresolved/resting accounting.
            self.resolution_errors += 1
            self._unresolved(order, "resolution_error")
            print(f"[live] ERROR resolving order {order.order_id} "
                  f"({order.ticker} {order.side} {order.size}@{order.price}):")
            traceback.print_exc()
        finally:
            self.n_scheduled -= 1


class _Executor:
    """The OrderManager's write interface, backed by a LiveEngine."""

    def __init__(self, engine: "LiveEngine"):
        self._e = engine

    def next_order_id(self):
        return self._e.next_order_id()

    def create(self, q):
        return self._e._om_create(q)

    def amend(self, q, price, remaining):
        return self._e._om_amend(q, price, remaining)

    def cancel(self, q, reason):
        self._e._om_cancel(q, reason)

    def bucket_tokens(self):
        return self._e._om_bucket_tokens()
