"""
Generic quoter: turns per-market `Fair`s into takes and a DESIRED quote ladder.

One instance per event, used as the engine's strategy callable (live via
LiveEngine, backtest via kalshi_backtest.engine.Backtest -- both provide the
same ctx surface). Series code never places orders; it publishes Fairs and,
optionally, vetoes sides through `FairModel.side_block`.

Per market and side, every decision is:

    d       = distance from fair to the band edge (prob - lo buying, hi - prob selling)
    ci      = edge_fraction * d
    lean    = lean_dollars * ratio
              ratio = effective position / max_position, signed toward the side that GROWS it;
              negative on the reducing side, where the edge then floors at 0
    const   = constant_edge, x sqrt(constant_edge_ref_s / time left) inside the last ref seconds,
              + the model's Fair.edge_add
    take    : prob - (const + ci + lean + taker_fee) >= ask
    quote   : prob - (const + quoting_edge + ci + lean + maker_fee) -> edge limit

Fees are Kalshi's own for this event (kalshi.fees.FeeSchedule). The edge before
fees is floored at 0, so a reservation never lands on the wrong side of fair.
Effective position = fills + takes sent within CONFIRM_S + every
live quote on that side (2026-08-28: 60% of same-ticker fills landed inside
one round trip of the previous one, so ignoring in-flight size let a market
run flat -> cap with zero lean charged).

Takes go straight to ctx.buy (top priority, never budgeted). Quotes do NOT:
the quoter publishes, per side, one level at the first tick outside the
quote reservation (capped max_improve_ticks inside the near touch -- the
touch net of our own orders -- and a tick inside the far touch), plus
quote_levels_behind: how many ticks under it an earlier order may be left
resting, via ctx.set_quote_ladder. The
engine's OrderManager decides what to actually send, in priority order and
from what the write bucket holds above quoting.cancels_token. A LIP quote
per side, when lip.enable_lip_quoting is on, goes through the same manager
at a lower priority (ctx.set_lip_quote). See order_manager.py.

Anything that stops a side from trading records a reason string in the
decision log (`block_bid` / `block_ask`) and counts it in `block_counts`.
"""
from __future__ import annotations

import math

import pandas as pd

from kalshi_infra.kalshi.venue import MAX_PRICE, MIN_PRICE, TICK
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.fair_lag import FairLagGuard
from kalshi_infra.trading.lip import LipTracker, levels_with_ours, others_ladder, remaining_cap_usd, reward_rate
from kalshi_infra.trading.order_manager import Desired, LipTarget
from kalshi_infra.trading.params import lip_yes_price

# Fair is clipped to [eps, 1-eps] before quoting (logged unclipped). Well
# inside Kalshi's 1c minimum price, so it only guards against a 0/1 fair.
FAIR_CLIP_EPS = 0.001

# How long until Kalshi confirms what we sent; a sent take counts toward the
# position this long. Real crypto takes from EC2, 2026-10-12 02:19-03:30 UTC:
# send -> booked fill p50 29ms, p99 53ms, max 55ms (n=106); ~2x the max.
CONFIRM_S = 0.1

# A take's signal must hold this long first, and fires at most once per side
# per window. Not a latency: at 0.1s the bot fired 72 and 105 takes into the
# final-minute leader flips of 26OCT120300/0315 and lost $16 (2026-10-12).
TAKE_HOLD_S = 0.5

# Kalshi pays nothing for a market-period whose LIP comes to under $1.00
# (CFTC filing 2026-07-15), so a market whose best case -- accrued plus the
# current $/s held to the LIP stop, never filled -- is below it isn't quoted.
LIP_MIN_PAYOUT_USD = 1.0
# Kalshi rounds the payout down to the cent, so once a market pays without a
# side, a side adding under this over the rest of the period earns nothing.
LIP_PAYOUT_TICK_USD = 0.01
# A resting LIP quote is pulled for a soft reason only once it has lasted this
# long. Live 2026-10-15 16:02-17:08 UTC: 513 LIP creates vs 22 moves, the order
# up ~20% of its eligible time, mostly from sub-second fair-margin flicker.
# spot_burst is not soft: held 2s, ~9 of 48 big LIP sweeps (10-15 17:00 to
# 10-16 07:45 UTC) landed inside the hold.
LIP_PULL_DEBOUNCE_S = 2.0
# A LIP side (or an exit's band size on top of its inventory) must add more
# than this $/s of reward to rest; otherwise it is only fill risk.
LIP_MIN_RATE_USD_S = 1e-9
# A LIP quote rests up to this many ticks behind the best bid if it keeps
# LIP_STEP_BACK_KEEP of the best bid's reward rate (see _lip_step_back).
LIP_STEP_BACK_TICKS = 3
LIP_STEP_BACK_KEEP = 0.98


class Quoter:
    def __init__(self, cfg, model, fees, *, close_time: pd.Timestamp | None,
                 lip: LipTracker | None = None, record_debug: bool = True):
        """`cfg`: the series config (.quoting, .risk, .lip; held by reference
        so live edits apply). `model`: the series' FairModel. `fees`: the
        event's kalshi.fees.FeeSchedule. `lip`: accrual tracker only -- LIP
        quoting is off until it's rebuilt on the order manager."""
        self.cfg = cfg
        self.model = model
        self.fees = fees
        self.close_time = close_time
        self.lip = lip
        self.record_debug = record_debug

        self.fairs: dict[str, Fair | None] = {}
        self._sent: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
        self._quote_block_until: dict[tuple[str, str], pd.Timestamp] = {}
        self._fills_seen = 0
        self._atm_set: set[str] = set()
        self._take_since: dict[tuple[str, str], float] = {}
        self._take_last: dict[tuple[str, str], float] = {}
        self._last_signal_age: float | None = None
        self.block_counts: dict[str, int] = {}
        self.debug_log: list[dict] = []
        # Our live quotes as last reported by the ctx, per (ticker, side):
        # [(price, remaining)]. Used for risk and to net our size out of the book.
        self._ours: dict[tuple[str, str], list[tuple[float, float]]] = {}
        # The part of _ours Kalshi's book actually shows (acked, at the price
        # the book has them). Only this is netted out of the displayed ladder.
        self._shown: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self._own_in_book = False
        self._skipped_deep = 0   # ladder levels left out by skip_quote_depth_ahead
        # Our live LIP quote per (ticker, side): (price, remaining) or None.
        self._lip_live: dict[tuple[str, str], tuple[float, float] | None] = {}
        # Best-case LIP payout per market with a LIP target, and the markets
        # currently holding a lip_max_markets slot.
        self._lip_proj: dict[str, float] = {}
        self._lip_set: set[str] = set()
        # Soft-pull debounce per (ticker, side): when the pull started, and the
        # last target published (held while the pull is younger than the debounce).
        self._lip_soft_since: dict[tuple[str, str], float] = {}
        # Net yes bought by LIP fills per market (exit fills count against it).
        self._lip_inv: dict[str, float] = {}
        # Inventory part of this decision's exit target, per (ticker, side).
        self._lip_exit_qty: dict[tuple[str, str], float] = {}
        self._lip_last: dict[tuple[str, str], LipTarget] = {}
        # Per ticker: epoch seconds until which its fair is held as suspect.
        self._suspect_until: dict[str, float] = {}
        self._lag = FairLagGuard()
        # Per (ticker, side): own-side price our top edge quote was alone at
        # (see _front_hold).
        self._front: dict[tuple[str, str], float] = {}
        self._now_s = 0.0

    # ------------------------------------------------------------------
    # Engine interface
    # ------------------------------------------------------------------

    def resting_by_side(self) -> dict[tuple[str, str], list[tuple[float, float]]]:
        """(ticker, side) -> [(price, size)] of every live quote, edge and
        LIP, for LIP scoring."""
        out = {k: list(v) for k, v in self._ours.items() if v}
        for k, v in self._lip_live.items():
            if v is not None:
                out.setdefault(k, []).append(v)
        return out

    def __call__(self, ctx):
        self._own_in_book = bool(getattr(ctx, "own_orders_in_book", False))
        self._sync_quote_fills(ctx)

        now_s = ctx.time.timestamp()
        self._now_s = now_s
        evt = ctx.last_event
        if isinstance(evt, FairUpdate):
            self.fairs.update(evt.fairs)
            targets = list(evt.fairs)
        else:
            ticker = getattr(evt, "ticker", None)
            if ticker is None:
                return
            refreshed = self.model.on_market_event(ticker, now_s)
            if refreshed:
                self.fairs.update(refreshed)
            if ticker not in self.fairs:
                return  # no fair for this market yet
            targets = [ticker]

        # Before any _act: the ATM ranking reads every fair, so one market's
        # new broken fair must be flagged before another market is ranked.
        changed = evt.fairs if isinstance(evt, FairUpdate) else {**(refreshed or {}), ticker: None}
        for t in changed:
            self._check_fair(ctx, t, now_s)
        for ticker in targets:
            self._act(ctx, ticker, now_s)

        if self.lip is not None and self.lip.due(now_s):
            self.lip.snapshot(now_s, {t: ctx.book(t) for t in self.lip.params}, self.resting_by_side())

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _sync_quote_fills(self, ctx):
        """Start a side's requote delay when one of our quotes trades. Read off
        the portfolio's fill list -- the one signal identical in backtest and
        live. Resets when the list shrinks (arming swaps in a fresh Portfolio)."""
        fills = ctx.portfolio.fills
        if len(fills) < self._fills_seen:
            self._fills_seen = 0
        delay = pd.Timedelta(seconds=self.cfg.quoting.requote_delay_s)
        if self._fills_seen == 0:
            self._lip_inv = {}
        for f in fills[self._fills_seen:]:
            if getattr(f, "role", None) == "lip":
                # Net yes bought by LIP orders, per market: what the LIP exit offers back.
                self._lip_inv[f.ticker] = self._lip_inv.get(f.ticker, 0.0) + (f.size if f.side == "yes" else -f.size)
            if f.liquidity == "maker":
                self._quote_block_until[(f.ticker, f.side)] = f.time + delay
        self._fills_seen = len(fills)

    def _inflight(self, ticker: str, side: str, now: pd.Timestamp) -> float:
        """Take size sent on (ticker, side) within CONFIRM_S. A take fills on
        arrival or not at all, so a size total is enough."""
        key = (ticker, side)
        recent = self._sent.get(key)
        if not recent:
            return 0.0
        cutoff = now - pd.Timedelta(seconds=CONFIRM_S)
        recent = [(t, s) for t, s in recent if t > cutoff]
        self._sent[key] = recent
        return sum(s for _, s in recent)

    def _refresh_ours(self, ctx, ticker: str):
        """Our live quotes (risk) and the subset Kalshi's book shows (netting
        our size out of the displayed ladder), per side."""
        get = getattr(ctx, "quote_ladder", None)
        shown = getattr(ctx, "quotes_on_book", None)
        lipq = getattr(ctx, "lip_quote", None)
        for side in ("yes", "no"):
            self._ours[(ticker, side)] = list(get(ticker, side)) if get is not None else []
            self._lip_live[(ticker, side)] = lipq(ticker, side) if lipq is not None else None
            self._shown[(ticker, side)] = (list(shown(ticker, side)) if shown is not None
                                           else list(self._ours[(ticker, side)]))

    def _resting(self, ticker: str, side: str) -> float:
        return sum(z for _, z in self._ours.get((ticker, side), []))

    def _block(self, reason: str):
        self.block_counts[reason] = self.block_counts.get(reason, 0) + 1
        return reason

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _constant_edge(self, now: pd.Timestamp) -> float:
        """constant_edge, scaled by sqrt(ref / time left) inside the last
        `constant_edge_ref_s` seconds; flat when that's empty or there's no close."""
        q = self.cfg.quoting
        ref = getattr(q, "constant_edge_ref_s", None)
        if not ref or self.close_time is None:
            return q.constant_edge
        left = (self.close_time - now).total_seconds()
        if left >= ref:
            return q.constant_edge
        # Floored at 1s: at the close the multiplier would be unbounded, and no
        # quote rests there anyway (real_trading_cutoff_s).
        return q.constant_edge * math.sqrt(ref / max(left, 1.0))

    def _alt_signal_age(self) -> float | None:
        fn = getattr(self.model, "signal_age_alt_s", None)
        return fn() if fn is not None else None

    def signal_stale(self) -> bool:
        """Fair inputs older than max_signal_age_s. Quotes return the moment
        the age is back under the limit: the old fresh_recovery_s hold was 83%
        of stale-blocked decisions with the age already under it (2026-10-14)."""
        age = self.model.signal_age_s()
        self._last_signal_age = age
        return age is None or age > self.cfg.risk.max_signal_age_s

    def _check_fair(self, ctx, ticker: str, now_s: float) -> None:
        """Flag a fair that can't be right: band wider than risk.max_fair_band,
        or more than risk.max_fair_outside_touch outside Kalshi's touch."""
        r = self.cfg.risk
        fair = self.fairs.get(ticker)
        if fair is None:
            return
        book = ctx.book(ticker)
        if book is not None and book.bid is not None and book.ask is not None:
            self._lag.observe(ticker, now_s, (book.bid + book.ask) / 2, fair.prob, r)
        bad = r.max_fair_band is not None and fair.hi - fair.lo > r.max_fair_band
        lim = r.max_fair_outside_touch
        if not bad and lim is not None and book is not None:
            bad = ((book.bid is not None and fair.prob < book.bid - lim)
                   or (book.ask is not None and fair.prob > book.ask + lim))
        if bad:
            self._suspect_until[ticker] = now_s + r.fair_suspect_hold_s

    def _fair_suspect(self, ticker: str, now_s: float) -> bool:
        # Inclusive, so a 0s hold still blocks the failing reading itself.
        return now_s <= self._suspect_until.get(ticker, -math.inf)

    def _atm_tradeable(self, ticker: str) -> bool:
        """Among the max_markets fairs closest to 0.5, suspect fairs excluded.
        Ties break on ticker; members stay until they rank below max_markets +
        atm_hysteresis."""
        q = self.cfg.quoting
        n = q.max_markets
        if n is None:
            return True
        ranked = sorted((t for t, f in self.fairs.items() if f is not None and not self._fair_suspect(t, self._now_s)),
                        key=lambda t: (abs(self.fairs[t].prob - 0.5), t))
        keep_within = set(ranked[:n + max(q.atm_hysteresis, 0)])
        members = [t for t in ranked if t in self._atm_set and t in keep_within]
        for t in ranked:
            if len(members) >= n:
                break
            if t not in members:
                members.append(t)
        self._atm_set = set(members[:n])
        return ticker in self._atm_set

    def _take_ok(self, key, now_s: float) -> bool:
        """Signal has held for TAKE_HOLD_S, and at most one take per side per
        window after that."""
        w = TAKE_HOLD_S
        if w <= 0:
            return True
        since = self._take_since.setdefault(key, now_s)
        if now_s - since < w:
            return False
        if now_s - self._take_last.get(key, -1e18) < w:
            return False
        self._take_last[key] = now_s
        return True

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def _act(self, ctx, ticker: str, now_s: float):
        q = self.cfg.quoting
        now = ctx.time
        fair = self.fairs.get(ticker)
        book = ctx.book(ticker)
        market_bid = book.bid if book is not None else None
        market_ask = book.ask if book is not None else None
        self._refresh_ours(ctx, ticker)

        gate = None
        if fair is None:
            gate = self._block("no_fair")
        elif self.signal_stale():
            gate = self._block("stale_signal")
        elif self._fair_suspect(ticker, now_s):
            gate = self._block("fair_suspect")
        if gate is not None:
            for side in ("yes", "no"):
                self._publish(ctx, ticker, side, None)
                self._publish_lip(ctx, ticker, side, None)    # a stale fill is the big adverse one
            self._lip_proj.pop(ticker, None)
            self._lip_set.discard(ticker)
            for side in ("yes", "no"):
                self._lip_soft_since.pop((ticker, side), None)
                self._lip_last.pop((ticker, side), None)
            if self.record_debug:
                # The fair still exists under a stale_signal block; logging it
                # as None drew a theo gap on every block (~12% of rows,
                # 2026-10-10). The edge bands still gap: nothing was priced.
                self._log_row(ctx, now, ticker, fair, market_bid, market_ask, block=gate)
            return

        prob = min(max(fair.prob, FAIR_CLIP_EPS), 1.0 - FAIR_CLIP_EPS)
        d_lo = max(fair.prob - fair.lo, 0.0)
        d_hi = max(fair.hi - fair.prob, 0.0)
        ci_bid, ci_ask = q.edge_fraction * d_lo, q.edge_fraction * d_hi

        pos = ctx.position(ticker)
        net = pos.yes_qty - pos.no_qty
        max_pos = q.max_position
        inflight_yes, inflight_no = self._inflight(ticker, "yes", now), self._inflight(ticker, "no", now)
        # Every live quote level counts: a fast move can fill all of them.
        resting_yes, resting_no = self._resting(ticker, "yes"), self._resting(ticker, "no")
        eff_yes = net + inflight_yes + resting_yes
        eff_no = net - inflight_no - resting_no

        def _ratio(x):
            return 0.0 if max_pos <= 0 else max(-1.0, min(1.0, x / max_pos))

        # Signed lean: positive grows the edge on the side that adds to the
        # position, negative cuts it on the side that reduces it. Unsigned, the
        # reducing side asked the full edge at the cap and sat a median 9c
        # behind the touch, unquoted 53% of the time (KXCRYPTOLEAD15M 2026-10-11).
        lean_bid = q.lean_dollars * _ratio(eff_yes)
        lean_ask = q.lean_dollars * -_ratio(eff_no)

        fee_bid = self.fees.taker(market_ask) if market_ask is not None else 0.0
        fee_ask = self.fees.taker(market_bid) if market_bid is not None else 0.0
        const = self._constant_edge(now) + fair.edge_add
        # Floored at 0 before the fee: a negative lean may bring a side to fair, never past it.
        bid_edge = max(const + ci_bid + lean_bid, 0.0) + fee_bid
        ask_edge = max(const + ci_ask + lean_ask, 0.0) + fee_ask
        bid_res, ask_res = prob - bid_edge, prob + ask_edge

        block_bid = block_ask = None
        if not self._atm_tradeable(ticker):
            block_bid = block_ask = "outside_atm_set"
        else:
            block_bid = self.model.side_block(ticker, "yes", fair, now_s)
            block_ask = self.model.side_block(ticker, "no", fair, now_s)
            if not block_bid and self._lag.blocked(ticker, "yes", now_s):
                block_bid = "fair_lag"
            if not block_ask and self._lag.blocked(ticker, "no", now_s):
                block_ask = "fair_lag"
        if block_bid:
            self._block(block_bid)
        if block_ask and block_ask != block_bid:
            self._block(block_ask)

        # Takes are capped on fills + in-flight takes only: a resting quote
        # isn't position yet, and counting the whole ladder here would stop us
        # taking a clear edge just because we're also bidding.
        cap_yes = net + inflight_yes < max_pos - 1e-9
        cap_no = net - inflight_no > -max_pos + 1e-9

        key_b, key_a = (ticker, "take_yes"), (ticker, "take_no")
        taking = q.enable_taking
        want_b = taking and block_bid is None and market_ask is not None and bid_res >= market_ask and cap_yes
        want_a = taking and block_ask is None and market_bid is not None and ask_res <= market_bid and cap_no
        bid_final = market_ask if (want_b and self._take_ok(key_b, now_s)) else None
        ask_final = market_bid if (want_a and self._take_ok(key_a, now_s)) else None
        if not want_b:
            self._take_since.pop(key_b, None)
        if not want_a:
            self._take_since.pop(key_a, None)

        if bid_final is not None:
            z = self._fit_to_loss(ctx, ticker, "yes", "take", [round(bid_final, 2)], q.order_lots)
            if z >= 0.01:
                ctx.buy(ticker=ticker, side="yes", size=z, price=round(bid_final, 2), order_type="limit")
                self._sent.setdefault((ticker, "yes"), []).append((now, z))
            else:
                self._block("max_loss_budget")
        if ask_final is not None:
            # Selling yes at p == buying no at 1 - p.
            z = self._fit_to_loss(ctx, ticker, "no", "take", [round(1.0 - ask_final, 2)], q.order_lots)
            if z >= 0.01:
                ctx.buy(ticker=ticker, side="no", size=z, price=round(1.0 - ask_final, 2), order_type="limit")
                self._sent.setdefault((ticker, "no"), []).append((now, z))
            else:
                self._block("max_loss_budget")

        # Quotes: the take edge minus the taker fee, plus quoting_edge on top
        # (a resting quote is picked off by whoever is faster). Maker fee,
        # priced at the pre-fee reservation.
        q_base_bid = max(const + q.quoting_edge + ci_bid + lean_bid, 0.0)
        q_base_ask = max(const + q.quoting_edge + ci_ask + lean_ask, 0.0)
        mfee_bid = self.fees.maker(prob - q_base_bid)
        mfee_ask = self.fees.maker(prob + q_base_ask)
        qres_bid = prob - (q_base_bid + mfee_bid)
        qres_ask = prob + (q_base_ask + mfee_ask)

        quoting = q.enable_quoting
        qblock = getattr(self.model, "quote_block", None)
        quote_gate = qblock(ticker, now_s) if qblock is not None else None
        if quote_gate:
            self._block(quote_gate)
            quoting = False
        res_b = qres_bid if (quoting and block_bid is None) else None
        res_a = qres_ask if (quoting and block_ask is None) else None
        ladder = getattr(book, "ladder", None) if book is not None else None
        lc = self.cfg.lip
        # The age signal_stale() just read; not None here (None is stale).
        lip_stale = (lc.lip_max_signal_age_s is not None and self._last_signal_age is not None
                     and self._last_signal_age > lc.lip_max_signal_age_s)
        if lip_stale:
            self._block("lip_stale_signal")
        out, lips = {}, {}
        for side, res in (("yes", res_b), ("no", res_a)):
            d = self._ladder(ticker, side, res, market_bid, market_ask, ladder, net, now)
            if d.prices:
                # Worst case assumes every level fills; shrink the lots (and the
                # side's room) until the whole ladder fits under max_loss_usd.
                z = self._fit_to_loss(ctx, ticker, side, "edge", d.prices, d.lots)
                if z < d.lots - 1e-9:
                    self._block("max_loss_budget")
                    if z < 0.01:
                        d = Desired([], d.lots, d.edge_limit, 0.0, d.max_ahead_price, max_levels=d.max_levels)
                    else:
                        d = Desired(d.prices, z, d.edge_limit, min(d.room, z * len(d.prices)),
                                    d.max_ahead_price, max_levels=d.max_levels)
            self._publish(ctx, ticker, side, d)
            out[side] = d
            # LIP obeys enable_quoting, the model's side blocks and quote_block
            # (spot_burst): a big-trade burst is when a 300-lot LIP order gets
            # swept, and the fair it's priced off is momentarily least reliable.
            side_ok = (q.enable_quoting and not quote_gate and not lip_stale
                       and (block_bid if side == "yes" else block_ask) is None)
            lips[side] = self._lip_target(ticker, side, book, ladder, net, now, prob) if side_ok else None
        lips = self._lip_select(ctx, ticker, lips, ladder, now)
        # quote_gate (spot_burst) pulls LIP at once, exit included, same as the
        # edge ladder: the fair is least reliable exactly when a sweep comes.
        # The time cutoff stops the exit too: inside it LIP inventory is held.
        # 26OCT162045: an exit bid HYPE 0.48 x77 at 83s left, 9c over the edge
        # threshold, and fair fell to 0.09 within 17s.
        hard_off = (not lc.enable_lip_quoting or not q.enable_quoting or bool(quote_gate) or lip_stale
                    or (self.close_time is not None
                        and (self.close_time - now).total_seconds() < lc.lip_min_time_left_s))
        for side in ("yes", "no"):
            side_blocked = (block_bid if side == "yes" else block_ask) is not None
            lips[side] = self._lip_debounce(ticker, side, lips[side], now_s, hard_off or side_blocked)
            exit_t = None if (side_blocked or hard_off) else \
                self._lip_exit(ticker, side, book, ladder, net, prob)
            if exit_t is not None:
                # One order per side: the exit (inventory only, front of the
                # book) replaces the farming quote until the inventory is gone.
                lips[side] = exit_t
            t = lips[side]
            if t is not None:
                # The worst-case loss budget sizes everything beyond the exit
                # inventory; the inventory part reduces risk and is never cut.
                floor_qty = self._lip_exit_qty.get((ticker, side), 0.0) if t.front else 0.0
                z = self._fit_to_loss(ctx, ticker, side, "lip", [t.price], t.size)
                if z < t.size - 1e-9:
                    self._block("max_loss_budget")
                    z = max(math.floor(z), floor_qty)
                    lips[side] = LipTarget(t.price, float(z), front=t.front) if z >= 0.01 else None
            self._lip_exit_qty.pop((ticker, side), None)
            self._publish_lip(ctx, ticker, side, lips[side])
        quote_bid = _top_yes("yes", out["yes"])
        quote_ask = _top_yes("no", out["no"])
        lip_bid = None if lips["yes"] is None else lips["yes"].price
        lip_ask = None if lips["no"] is None else round(1.0 - lips["no"].price, 2)
        lq = getattr(ctx, "lip_queue", None)
        lq_yes = lq(ticker, "yes") if lq is not None else None
        lq_no = lq(ticker, "no") if lq is not None else None

        if self.record_debug:
            self._log_row(ctx, now, ticker, fair, market_bid, market_ask, prob=prob, const=const,
                          ci_bid=ci_bid, ci_ask=ci_ask, bid_edge=bid_edge, ask_edge=ask_edge,
                          lean_bid=lean_bid, lean_ask=lean_ask, fee_bid=fee_bid, fee_ask=fee_ask,
                          pos=pos, net=net, eff_yes=eff_yes, eff_no=eff_no, ratio=_ratio(net),
                          bid_res=bid_res, ask_res=ask_res, bid_final=bid_final, ask_final=ask_final,
                          inflight_yes=inflight_yes, inflight_no=inflight_no,
                          resting_yes=resting_yes, resting_no=resting_no,
                          cap_yes=cap_yes, cap_no=cap_no, mfee_bid=mfee_bid, mfee_ask=mfee_ask,
                          qres_bid=qres_bid, qres_ask=qres_ask, quote_bid=quote_bid, quote_ask=quote_ask,
                          lip_bid=lip_bid, lip_ask=lip_ask, block_bid=block_bid, block_ask=block_ask,
                          quote_block=quote_gate,
                          lip_size_bid=None if lips["yes"] is None else lips["yes"].size,
                          lip_size_ask=None if lips["no"] is None else lips["no"].size,
                          lip_proj_usd=self._lip_proj.get(ticker),
                          lip_ahead_bid=None if lq_yes is None else lq_yes[0],
                          lip_ahead_ask=None if lq_no is None else lq_no[0],
                          n_levels_bid=len(out["yes"].prices) if out["yes"] else 0,
                          n_levels_ask=len(out["no"].prices) if out["no"] else 0)

    # ------------------------------------------------------------------
    # Quote ladder
    # ------------------------------------------------------------------

    @staticmethod
    def _publish(ctx, ticker, side, d: Desired | None):
        pub = getattr(ctx, "set_quote_ladder", None)
        if pub is not None:
            pub(ticker, side, d)

    @staticmethod
    def _publish_lip(ctx, ticker, side, t: LipTarget | None):
        pub = getattr(ctx, "set_lip_quote", None)
        if pub is not None:
            pub(ticker, side, t)

    # ------------------------------------------------------------------
    # LIP quote
    # ------------------------------------------------------------------

    def _lip_target(self, ticker, side, book, ladder, net, now, prob) -> LipTarget | None:
        """Where the one LIP quote on this side should rest, or None.

        Price: others' best bid on this side (our own orders, LIP included,
        netted out, so the quote follows the book down rather than holding
        itself up), capped a tick inside the far touch and at own-side fair
        minus lip_constant_edge. Quoted only if that price is in a band
        (lip.lots_at) and at least lip_min_time_left_s remain. The full edge
        reservation is NOT used: it left the quote almost never at the best
        bid, where nearly all the LIP is (LIP_REPORT.md Â§6).

        Size: lots_at(price) minus the position already held on this side and
        our resting edge size there."""
        lc = self.cfg.lip
        params = self.lip.params.get(ticker) if self.lip is not None else None
        if not lc.enable_lip_quoting or params is None or ladder is None or book is None:
            return None
        if not lc.market_allowed(ticker):
            self._block("lip_market_off")
            return None
        if self.close_time is not None and (self.close_time - now).total_seconds() < lc.lip_min_time_left_s:
            return None
        bids = ladder.yes_bids if side == "yes" else ladder.no_bids
        if self._own_in_book:
            mine = list(self._shown.get((ticker, side), []))
            bids = others_ladder(bids, mine)
        if not bids:
            return None
        price = round(max(bids), 2)
        far = book.ask if side == "yes" else (None if book.bid is None else 1.0 - book.bid)
        if far is not None:
            price = min(price, round(far - TICK, 2))
        if lc.lip_constant_edge is not None:
            # Cap at fair - edge rather than skip the side: a bid behind the
            # best earns less LIP, but _lip_select scores it at this price, so
            # it only rests if it can still clear the payout floor.
            cap = self._lip_fair_cap(side, prob)
            if price > cap + 1e-9:
                self._block("lip_capped_at_fair")
                price = cap
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return None
        # Hard cap before sizing: the sizing below adds back a position held
        # the other way, which with a 0 band would still rest an order on a
        # market at a yes price over the cap.
        if lip_yes_price(price, side) >= lc.max_price(side) - 1e-9:
            return None
        # The band size is the most position we want on this side: quote up to
        # it, net of the signed position held there (a position the other way
        # adds to it).
        held = net if side == "yes" else -net

        def size_at(px):
            return math.floor(lc.lots_at(px, side) - held - self._resting(ticker, side) + 1e-9)

        size = size_at(price)
        if size < 1:
            return None
        price = self._lip_step_back(ticker, side, price, size_at, ladder)
        return LipTarget(price, float(size_at(price)))

    def _lip_step_back(self, ticker, side, best, size_at, ladder) -> float:
        """Lowest price within LIP_STEP_BACK_TICKS of `best` that still earns
        LIP_STEP_BACK_KEEP of the reward rate at `best`, the other side as it
        stands. Kalshi scores every bid at or above the reference price in
        full, so a tick or two back often earns the same while a sweep has to
        clear the levels in front first. Live books 10-17 00:30 UTC: one tick
        back kept >=95% of the reward on 55% of sides, two ticks on 21%.
        Prefers the price already resting, so a quote doesn't hop."""
        if self.lip is None or ticker not in self.lip.params:
            return best
        here = {side: LipTarget(best, float(size_at(best)))}
        base = self._lip_rate(ticker, here, ladder)
        if base <= LIP_MIN_RATE_USD_S:
            return best
        live = self._lip_live.get((ticker, side))
        resting_px = None if live is None else round(live[0], 2)
        chosen = best
        for k in range(1, LIP_STEP_BACK_TICKS + 1):
            px = round(best - k * TICK, 2)
            if px < MIN_PRICE:
                break
            z = size_at(px)
            if z < 1:
                break
            if self._lip_rate(ticker, {side: LipTarget(px, float(z))}, ladder) < LIP_STEP_BACK_KEEP * base:
                break
            chosen = px
        if resting_px is not None and chosen <= resting_px <= best:
            z = size_at(resting_px)
            if z >= 1 and self._lip_rate(ticker, {side: LipTarget(resting_px, float(z))}, ladder) \
                    >= LIP_STEP_BACK_KEEP * base:
                chosen = resting_px
        if chosen < best - 1e-9:
            self._block("lip_stepped_back")
        return chosen

    def _lip_exit(self, ticker, side, book, ladder, net, prob) -> LipTarget | None:
        """Offer back what LIP orders bought: inventory long yes from LIP fills
        is offered on the no side (and vice versa), sized to that inventory
        (capped at the position actually held) and nothing more, so a full
        fill flattens rather than flips. Unlike a LIP quote it WANTS the
        fill: it steps a tick in front of others' best bid (alone at the front
        of the book) and is marked `front`, so it is never re-queued. Bypasses
        the bands, lots, payout floor and max-markets, but not the LIP time
        cutoff (the caller holds the inventory inside it);
        keeps the far-touch and lip_constant_edge price caps, and like every
        LIP order never rests at a yes price of its side's cap (lc.max_price) or more. Never
        stepped back: it wants the fill."""
        inv = self._lip_inv.get(ticker, 0.0)
        qty = min(max(inv, 0.0), max(net, 0.0)) if side == "no" else min(max(-inv, 0.0), max(-net, 0.0))
        qty = math.floor(qty * 100 + 1e-9) / 100
        if qty < 0.01 or ladder is None or book is None:
            return None
        bids = ladder.yes_bids if side == "yes" else ladder.no_bids
        if self._own_in_book:
            bids = others_ladder(bids, list(self._shown.get((ticker, side), [])))
        far = book.ask if side == "yes" else (None if book.bid is None else 1.0 - book.bid)
        if bids:
            price = round(max(bids) + TICK, 2)
        elif far is not None:
            # Nobody else bids this side (one-sided book): still offer the
            # inventory back, a tick inside the far touch; the fair cap below
            # keeps it from paying up.
            price = round(far - TICK, 2)
        else:
            return None
        if far is not None:
            price = min(price, round(far - TICK, 2))
        lc = self.cfg.lip
        if lc.lip_constant_edge is not None:
            price = min(price, self._lip_fair_cap(side, prob))
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return None
        if lip_yes_price(price, side) >= lc.max_price(side) - 1e-9:
            self._block("lip_exit_over_max")
            return None
        self._block("lip_exit")
        # The inventory alone. Band size on top flipped the position at the
        # front of the book: ETH 10-16 14:23 UTC went -70 -> +18, and HYPE
        # 07:32 -53 -> +101, each in one sweep.
        self._lip_exit_qty[(ticker, side)] = float(qty)
        return LipTarget(price, float(qty), front=True)

    def _lip_fair_cap(self, side, prob) -> float:
        """Highest own-side LIP price that keeps lip_constant_edge below fair."""
        fair_own = prob if side == "yes" else 1.0 - prob
        return round(math.floor((fair_own - self.cfg.lip.lip_constant_edge) / TICK + 1e-9) * TICK, 2)

    # ------------------------------------------------------------------
    # Worst-case loss budget
    # ------------------------------------------------------------------

    def _settle_pnl(self, ctx, extra: dict | None = None, skip: set | None = None) -> dict:
        """Settlement PnL of the event's positions plus every live order (edge
        and LIP, all assumed filled at its price), plus `extra`
        {(ticker, side, role): [(price, size)]}, for each possible winner.
        `skip` {(ticker, side, role)}: live orders left out (the ones `extra`
        replaces). Keys of the result are the winning ticker or None (an
        outcome where none of our markets win). Each market of the event
        settles YES only if its coin wins."""
        skip = skip or set()
        legs: dict[str, list] = {}
        for t in self.fairs:
            pos = ctx.position(t)
            legs[t] = [("yes", pos.yes_qty, pos.yes_cost), ("no", pos.no_qty, pos.no_cost), ("fee", pos.fees_paid, 0.0)]
        live = [((t, s, "edge"), z) for (t, s), z in self._ours.items()]
        live += [((t, s, "lip"), [v]) for (t, s), v in self._lip_live.items() if v]
        for key, ours in live:
            if key in skip:
                continue
            for p, z in ours:
                legs.setdefault(key[0], []).append((key[1], z, p * z))
        for (t, side, _role), ours in (extra or {}).items():
            for p, z in ours:
                legs.setdefault(t, []).append((side, z, p * z))
        out = {}
        for winner in list(legs) + [None]:
            total = 0.0
            for t, ls in legs.items():
                yes_wins = t == winner
                for side, qty, cost in ls:
                    if side == "fee":
                        total -= qty
                    elif side == "yes":
                        total += (qty if yes_wins else 0.0) - cost
                    else:
                        total += (0.0 if yes_wins else qty) - cost
            out[winner] = total
        return out

    def _worst_case(self, ctx, extra=None, skip=None) -> float:
        """Worst settlement PnL of this event over every outcome."""
        return min(self._settle_pnl(ctx, extra, skip).values())

    def _fit_to_loss(self, ctx, ticker, side, role, prices, size) -> float:
        """Largest per-level size <= `size` for orders at `prices` (one level
        each) on (ticker, side, role), REPLACING what that role has resting
        there, that keeps the worst-case loss inside risk.max_loss_usd. Whole
        hundredths; 0 if none fits. Every order is assumed to fill: a sweep can
        take all of them."""
        lim = -self.cfg.risk.max_loss_usd
        key = (ticker, side, role)

        def ok(z):
            return self._worst_case(ctx, {key: [(p, z) for p in prices]}, {key}) >= lim - 1e-9
        if size <= 0 or not prices or ok(size):
            return size
        lo, hi = 0.0, size
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if ok(mid) else (lo, mid)
        return math.floor(lo * 100 + 1e-9) / 100

    def _lip_debounce(self, ticker, side, target, now_s, hard: bool):
        """Hold a resting LIP quote through a SOFT pull (payout floor,
        max-markets slot, out of band, fair cap) until it has lasted
        LIP_PULL_DEBOUNCE_S: each pull is a cancel, and coming back means a new
        order at the back of the queue. `hard` pulls (LIP or quoting off,
        spot_burst, side block, end-of-event cutoff) go through at once; stale
        signal and no fair never get here (the gate publishes None directly)."""
        key = (ticker, side)
        if target is not None:
            self._lip_soft_since.pop(key, None)
            self._lip_last[key] = target
            return target
        live = self._lip_live.get(key)
        last = self._lip_last.get(key)
        if hard or live is None or last is None:
            self._lip_soft_since.pop(key, None)
            self._lip_last.pop(key, None)
            return None
        since = self._lip_soft_since.setdefault(key, now_s)
        if now_s - since < LIP_PULL_DEBOUNCE_S:
            self._block("lip_pull_held")
            # Hold what's resting, never more: a fill during the hold must not
            # be topped back up by an amend. _lip_last is left alone, so the
            # clock keeps running until a real target comes back.
            return LipTarget(live[0], min(last.size, live[1]))
        self._lip_soft_since.pop(key, None)
        self._lip_last.pop(key, None)
        return None

    def _lip_rate(self, ticker, lips, ladder) -> float:
        """$/s this market's LIP targets would earn against the book as it is
        now (others' size plus our edge quotes), never filled."""
        params = self.lip.params.get(ticker)
        if params is None or ladder is None:
            return 0.0
        sides = {}
        for side in ("yes", "no"):
            bids = ladder.yes_bids if side == "yes" else ladder.no_bids
            if self._own_in_book:
                bids = others_ladder(bids, list(self._shown.get((ticker, side), [])))
            ours = list(self._ours.get((ticker, side), []))
            t = lips.get(side)
            if t is not None:
                ours.append((t.price, t.size))
            sides[side] = levels_with_ours(bids, ours)
        return reward_rate(sides["yes"], sides["no"], params)

    def _lip_left_s(self, now) -> float | None:
        """Seconds of LIP quoting left this period (to the LIP stop); None
        without a close time."""
        if self.close_time is None:
            return None
        return max((self.close_time - now).total_seconds() - self.cfg.lip.lip_min_time_left_s, 0.0)

    def _lip_future(self, ticker, rate, now) -> float:
        """$ a `rate` ($/s) adds over the rest of the LIP window, never filled,
        capped at what the account cap still allows."""
        future = rate * (self._lip_left_s(now) or 0.0)
        params = self.lip.params.get(ticker)
        return min(future, remaining_cap_usd(params, self.lip.accrued_usd.get(ticker, 0.0))) if params else future

    def _lip_adds(self, ticker, with_lips, without_lips, ladder, now) -> bool:
        """Whether going from `without_lips` to `with_lips` can change what
        Kalshi pays for this market: it must earn something, and once the
        market reaches $1 without it, at least a cent over the rest of the
        period (payouts round down to the cent). A part the market needs to
        reach $1 is always kept; the payout floor judges the whole market."""
        r_with = self._lip_rate(ticker, with_lips, ladder)
        r_without = self._lip_rate(ticker, without_lips, ladder)
        if r_with - r_without <= LIP_MIN_RATE_USD_S:
            return False
        if self._lip_left_s(now) is None:
            return True
        accrued = self.lip.accrued_usd.get(ticker, 0.0)
        with_usd = accrued + self._lip_future(ticker, r_with, now)
        without_usd = accrued + self._lip_future(ticker, r_without, now)
        return without_usd < LIP_MIN_PAYOUT_USD or with_usd - without_usd >= LIP_PAYOUT_TICK_USD - 1e-12

    def _lip_select(self, ctx, ticker, lips, ladder, now) -> dict:
        """Apply the per-market LIP gates to this market's side targets: the
        payout floor (the rest of the period must be able to earn what's still
        short of $1, never filled) and lip_max_markets (only the event's top
        markets by that projection)."""
        lc = self.cfg.lip
        if self.lip is None or all(t is None for t in lips.values()):
            self._lip_proj.pop(ticker, None)
            self._lip_set.discard(ticker)
            return lips
        # A side that can't change the payout is fill risk for nothing: a one-
        # sided book (Kalshi excludes the snapshot), or a cheap side behind a
        # 100k-lot wall (SOL @0.01 at a 0.1% share: under a cent a period,
        # 10-16 10:05 UTC). Each side is judged with the other as it stands, so
        # two sides the market needs together to reach $1 both stay.
        lips = dict(lips)
        for side in ("yes", "no"):
            if lips[side] is not None and not self._lip_adds(ticker, lips, {**lips, side: None}, ladder, now):
                lips[side] = None
                self._block("lip_no_reward")
        if all(t is None for t in lips.values()):
            self._lip_proj.pop(ticker, None)
            self._lip_set.discard(ticker)
            return lips
        left = self._lip_left_s(now)
        accrued = self.lip.accrued_usd.get(ticker, 0.0)
        future = self._lip_future(ticker, self._lip_rate(ticker, lips, ladder), now)
        proj = accrued + future
        self._lip_proj[ticker] = proj
        off = {"yes": None, "no": None}
        # What the rest of the period still has to earn: $1 less what's already
        # accrued (0 once we're past $1). One threshold for starting and keeping
        # a quote: a x1.2 start margin re-applied after every burst pull (~every
        # 11s) and kept LIP up only 5.6% of its window (10-16 09:10-10:15 UTC).
        need = max(LIP_MIN_PAYOUT_USD - accrued, 0.0)
        if left is not None and future < need:
            # Out of the ranking too: a market that can't pay must not hold a slot.
            self._lip_proj.pop(ticker, None)
            self._lip_set.discard(ticker)
            self._block("lip_below_floor")
            return off
        n = lc.lip_max_markets
        if n is not None:
            # Members hold their place unless beaten by 25%: re-ranking on every
            # book tick would move 300-lot orders between markets constantly.
            ranked = sorted(self._lip_proj, key=lambda t: -self._lip_proj[t])
            keep = [t for t in ranked if t in self._lip_set and t in self._lip_proj]
            for t in ranked:
                if len(keep) >= n:
                    weakest = min(keep, key=lambda k: self._lip_proj[k])
                    if t not in keep and self._lip_proj[t] > 1.25 * self._lip_proj[weakest]:
                        keep.remove(weakest)
                        keep.append(t)
                elif t not in keep:
                    keep.append(t)
            new_set = set(keep[:n])
            # Markets decide one at a time, so one that lost its slot to this
            # one already published a target: pull it now, not on its next tick.
            for t in self._lip_set - new_set:
                if t != ticker:
                    for s in ("yes", "no"):
                        self._publish_lip(ctx, t, s, None)
            self._lip_set = new_set
            if ticker not in self._lip_set:
                self._block("lip_not_top")
                return off
        return lips

    def _others_bids(self, ticker, side, ladder) -> dict | None:
        """Other participants' bids on one side, own-side prices: the displayed
        ladder minus our quotes the book actually shows (real mode only -- in
        shadow and backtest the feed never contains our orders). None without
        a ladder."""
        if ladder is None:
            return None
        bids = ladder.yes_bids if side == "yes" else ladder.no_bids
        if not self._own_in_book:
            return dict(bids)
        return others_ladder(bids, self._shown.get((ticker, side), []))

    def _others_touch(self, ticker, side, market_bid, market_ask, ladder):
        """Near touch net of our own resting size, YES terms; None if nobody
        else is on that side. Capping against the displayed touch let a quote
        that WAS the touch step over itself one tick per decision (27% of
        write spend, 2026-10-10)."""
        shown = market_bid if side == "yes" else market_ask
        if ladder is None or not self._own_in_book:
            return shown
        others = self._others_bids(ticker, side, ladder)
        if not others:
            return None
        best = max(others)
        return round(best, 2) if side == "yes" else round(1.0 - best, 2)

    def _ladder(self, ticker, side, res, market_bid, market_ask, ladder, net, now) -> Desired:
        """The levels this side should have resting, own-side terms, best
        first. Empty (edge_limit None) when the side may not be quoted.

        A level with more than skip_quote_depth_ahead of OTHERS' size at its
        price or better is left out: deep behind the touch it almost never
        fills, so creating it only spends budget, and as the queue in front
        grows and shrinks it would be added and dropped over and over. Only
        new levels are affected -- the manager never cancels a quote for
        going deep."""
        q = self.cfg.quoting
        if res is None or (market_bid is None and market_ask is None):
            return Desired([], q.order_lots, None, 0.0)
        until = self._quote_block_until.get((ticker, side))
        edge_limit = _first_outside(side, res)
        # Room for resting size under max_position, counting fills and
        # in-flight takes. Every level counts (all can fill together).
        if side == "yes":
            room = q.max_position - (net + self._inflight(ticker, "yes", now))
        else:
            room = q.max_position + (net - self._inflight(ticker, "no", now))
        room = max(room, 0.0)
        ahead = self._max_ahead_price(ticker, side, market_bid, market_ask, ladder)
        # One level per side. quote_levels_behind is how far an earlier
        # order may lag behind it and be left resting, not extra levels.
        behind = max(int(q.quote_levels_behind), 0)
        if until is not None and now < until:
            # Recently hit: keep what's up (still outside the edge), add nothing.
            return Desired([], q.order_lots, edge_limit, room, ahead, max_levels=1, behind=behind)
        top = self._top_price(ticker, side, res, market_bid, market_ask, ladder)
        if top is None:
            return Desired([], q.order_lots, edge_limit, room, ahead, max_levels=1, behind=behind)
        cap = q.skip_quote_depth_ahead
        others = self._others_bids(ticker, side, ladder) if cap is not None else None
        prices = []
        if top >= MIN_PRICE - 1e-9:
            if others is not None and sum(z for px, z in others.items() if px >= top - 1e-9) > cap + 1e-9:
                self._skipped_deep += 1
            else:
                prices.append(round(top, 2))
        return Desired(prices, q.order_lots, edge_limit, room, ahead, max_levels=1, behind=behind)

    def _max_ahead_price(self, ticker, side, market_bid, market_ask, ladder) -> float | None:
        """Highest own-side price a resting quote may keep relative to OTHERS'
        touch: their best + max_improve_ticks, the same limit new quotes are
        placed under. None when nobody else is bidding (nothing to be ahead
        of). Touch flickers are filtered by the order manager's grace period
        (quote_ahead_grace_s), not by extra slack here."""
        near = self._others_touch(ticker, side, market_bid, market_ask, ladder)
        if near is None:
            return None
        near_own = near if side == "yes" else round(1.0 - near, 2)
        return round(near_own + self.cfg.quoting.max_improve_ticks * TICK, 2)

    def _front_hold(self, ticker, side, ladder) -> float | None:
        """Own-side price to hold the top quote at, or None. If our best
        resting quote was alone at its price on an earlier decision and others
        have since joined it there, we're still first in that queue, so it
        isn't stepped up a tick over them. 10-17/18: 5.7% of book-driven
        step-ups were this case. Someone bidding above us is not held."""
        key = (ticker, side)
        shown = self._shown.get(key) or []
        others = self._others_bids(ticker, side, ladder)
        if not shown or others is None:
            self._front.pop(key, None)
            return None
        mine = round(max(p for p, _ in shown), 2)
        best = round(max(others), 2) if others else None
        if best is None or best < mine - 1e-9:
            self._front[key] = mine
            return None
        if self._front.get(key) != mine:
            self._front.pop(key, None)
            return None
        if abs(best - mine) < 1e-9:
            self._block("front_hold")
            return mine
        return None

    def _top_price(self, ticker, side, res, market_bid, market_ask, ladder) -> float | None:
        """Best level in own-side terms: first tick outside the reservation
        (rounding to nearest would put half our quotes inside the edge), at
        most max_improve_ticks inside the near touch (net of our own orders),
        and a tick inside the far touch so it never crosses -- a marketable
        price is the take test's job, which charges the taker fee."""
        n = self.cfg.quoting.max_improve_ticks
        top = _first_outside(side, res)
        yes = top if side == "yes" else round(1.0 - top, 2)
        near = self._others_touch(ticker, side, market_bid, market_ask, ladder)
        hold = self._front_hold(ticker, side, ladder)
        if side == "yes":
            if near is not None:
                yes = min(yes, near + n * TICK)
            if hold is not None:
                yes = min(yes, hold)
            if market_ask is not None:
                yes = min(yes, market_ask - TICK)
        else:
            if near is not None:
                yes = max(yes, near - n * TICK)
            if hold is not None:
                yes = max(yes, round(1.0 - hold, 2))
            if market_bid is not None:
                yes = max(yes, market_bid + TICK)
        yes = round(yes, 2)
        if not (MIN_PRICE <= yes <= MAX_PRICE):
            return None
        return yes if side == "yes" else round(1.0 - yes, 2)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_row(self, ctx, now, ticker, fair, market_bid, market_ask, block=None, **v):
        evt = ctx.last_event
        row = {
            "time": now, "ticker": ticker,
            "event_kind": "fair" if isinstance(evt, FairUpdate) else type(evt).__name__,
            "market_bid": market_bid, "market_ask": market_ask,
            "prob": v.get("prob"), "prob_raw": None if fair is None else fair.prob,
            "prob_lo": None if fair is None else fair.lo, "prob_hi": None if fair is None else fair.hi,
            "ci_edge": v.get("ci_bid"), "ci_edge_ask": v.get("ci_ask"),
            # The constant edge actually applied (time-scaled near close).
            "constant_edge": v.get("const", self.cfg.quoting.constant_edge),
            # The age the stale gate just saw, so a tighter max_signal_age_s
            # can be costed from history instead of guessed.
            "signal_age_s": self._last_signal_age,
            # The other clock while a model runs two (KXCRYPTOLEAD15M receipt vs exchange).
            "signal_age_alt_s": self._alt_signal_age(),
            "bid_edge": v.get("bid_edge"), "ask_edge": v.get("ask_edge"),
            "lean_bid": v.get("lean_bid"), "lean_ask": v.get("lean_ask"),
            "lean_dollars": self.cfg.quoting.lean_dollars,
            "taker_fee_bid": v.get("fee_bid"), "taker_fee_ask": v.get("fee_ask"),
            "net_position": v.get("net"),
            "yes_qty": None if "pos" not in v else v["pos"].yes_qty,
            "no_qty": None if "pos" not in v else v["pos"].no_qty,
            "eff_yes": v.get("eff_yes"), "eff_no": v.get("eff_no"), "ratio": v.get("ratio"),
            "bid_reservation": v.get("bid_res"), "ask_reservation": v.get("ask_res"),
            "bid_final": v.get("bid_final"), "ask_final": v.get("ask_final"),
            "inflight_yes": v.get("inflight_yes"), "inflight_no": v.get("inflight_no"),
            "resting_yes": v.get("resting_yes"), "resting_no": v.get("resting_no"),
            "cap_allows_yes": v.get("cap_yes"), "cap_allows_no": v.get("cap_no"),
            "maker_fee_bid": v.get("mfee_bid"), "maker_fee_ask": v.get("mfee_ask"),
            "quoting_edge": self.cfg.quoting.quoting_edge,
            "quote_bid_reservation": v.get("qres_bid"), "quote_ask_reservation": v.get("qres_ask"),
            "quote_bid": v.get("quote_bid"), "quote_ask": v.get("quote_ask"),
            "n_levels_bid": v.get("n_levels_bid"), "n_levels_ask": v.get("n_levels_ask"),
            "lip_bid": v.get("lip_bid"), "lip_ask": v.get("lip_ask"),
            "lip_size_bid": v.get("lip_size_bid"), "lip_size_ask": v.get("lip_size_ask"),
            "lip_proj_usd": v.get("lip_proj_usd"),
            "lip_ahead_bid": v.get("lip_ahead_bid"), "lip_ahead_ask": v.get("lip_ahead_ask"),
            "block_bid": block or v.get("block_bid"), "block_ask": block or v.get("block_ask"),
            # The model's event-wide pull (spot_burst). Not in block_*: those are
            # per-side, and the takes stay live under it.
            "quote_block": v.get("quote_block"),
            "atm_tradeable": ticker in self._atm_set if self.cfg.quoting.max_markets else True,
        }
        if fair is not None:
            for k, val in fair.extras.items():
                row[f"x_{k}"] = val
        if self.lip is not None and ticker in self.lip.params:
            row["lip_accrued_usd"] = self.lip.accrued_usd[ticker]
        self.debug_log.append(row)


def _top_yes(side: str, d: Desired | None) -> float | None:
    """The ladder's best level in YES terms, for the log/chart."""
    if d is None or not d.prices:
        return None
    return d.prices[0] if side == "yes" else round(1.0 - d.prices[0], 2)


def _first_outside(side: str, reservation: float) -> float:
    """First tick at or outside the reservation, in `side`'s own price terms."""
    if side == "yes":
        return round(math.floor(reservation / TICK + 1e-9) * TICK, 2)
    return round(1.0 - math.ceil(reservation / TICK - 1e-9) * TICK, 2)
