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
    take    : prob - (constant_edge + ci + lean + taker_fee) >= ask
    quote   : prob - (constant_edge + quoting_edge + ci + lean + maker_fee) -> edge limit

Fees are Kalshi's own for this event (kalshi.fees.FeeSchedule). The edge before
fees is floored at 0, so a reservation never lands on the wrong side of fair.
Effective position = fills + takes sent within CONFIRM_S + every
live quote on that side (2026-08-28: 60% of same-ticker fills landed inside
one round trip of the previous one, so ignoring in-flight size let a market
run flat -> cap with zero lean charged).

Takes go straight to ctx.buy (top priority, never budgeted). Quotes do NOT:
the quoter publishes, per side, a ladder of up to quote_levels_behind
consecutive ticks starting at the first tick outside the quote reservation
(capped max_improve_ticks inside the near touch -- the touch net of our own
orders -- and a tick inside the far touch), via ctx.set_quote_ladder. The
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
from kalshi_infra.trading.lip import LipTracker, choose_lip_level, others_ladder
from kalshi_infra.trading.order_manager import Desired, LipTarget

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
        # Fresh "since forever" until a stale episode: the recovery hold is
        # for flapping, and applying it at event start held every crypto
        # rollover ~2s stale with good data (2026-10-09).
        self._fresh_since: float | None = float("-inf")
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
        for f in fills[self._fills_seen:]:
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

    def signal_stale(self) -> bool:
        """Fair inputs older than max_signal_age_s, or not yet fresh for
        fresh_recovery_s after a stale episode (no flapping)."""
        r = self.cfg.risk
        age = self.model.signal_age_s()
        now = pd.Timestamp.now(tz="UTC").timestamp()
        if age is None or age > r.max_signal_age_s:
            self._fresh_since = None
            return True
        if self._fresh_since is None:
            self._fresh_since = now
        return (now - self._fresh_since) < r.fresh_recovery_s

    def _atm_tradeable(self, ticker: str) -> bool:
        """Among the max_markets fairs closest to 0.5. Ties break on ticker;
        members stay until they rank below max_markets + atm_hysteresis."""
        q = self.cfg.quoting
        n = q.max_markets
        if n is None:
            return True
        ranked = sorted((t for t, f in self.fairs.items() if f is not None),
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
        if gate is not None:
            for side in ("yes", "no"):
                self._publish(ctx, ticker, side, None)
                self._publish_lip(ctx, ticker, side, None)    # a stale fill is the big adverse one
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
        # Floored at 0 before the fee: a negative lean may bring a side to fair, never past it.
        bid_edge = max(q.constant_edge + ci_bid + lean_bid, 0.0) + fee_bid
        ask_edge = max(q.constant_edge + ci_ask + lean_ask, 0.0) + fee_ask
        bid_res, ask_res = prob - bid_edge, prob + ask_edge

        block_bid = block_ask = None
        if not self._atm_tradeable(ticker):
            block_bid = block_ask = "outside_atm_set"
        else:
            block_bid = self.model.side_block(ticker, "yes", fair, now_s)
            block_ask = self.model.side_block(ticker, "no", fair, now_s)
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
            ctx.buy(ticker=ticker, side="yes", size=q.order_lots, price=round(bid_final, 2),
                    order_type="limit")
            self._sent.setdefault((ticker, "yes"), []).append((now, q.order_lots))
        if ask_final is not None:
            # Selling yes at p == buying no at 1 - p.
            ctx.buy(ticker=ticker, side="no", size=q.order_lots, price=round(1.0 - ask_final, 2),
                    order_type="limit")
            self._sent.setdefault((ticker, "no"), []).append((now, q.order_lots))

        # Quotes: the take edge minus the taker fee, plus quoting_edge on top
        # (a resting quote is picked off by whoever is faster). Maker fee,
        # priced at the pre-fee reservation.
        q_base_bid = max(q.constant_edge + q.quoting_edge + ci_bid + lean_bid, 0.0)
        q_base_ask = max(q.constant_edge + q.quoting_edge + ci_ask + lean_ask, 0.0)
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
        out, lips = {}, {}
        for side, res in (("yes", res_b), ("no", res_a)):
            d = self._ladder(ticker, side, res, market_bid, market_ask, ladder, net, now)
            self._publish(ctx, ticker, side, d)
            out[side] = d
            lips[side] = self._lip_target(ticker, side, res, book, ladder, net)
            self._publish_lip(ctx, ticker, side, lips[side])
        quote_bid = _top_yes("yes", out["yes"])
        quote_ask = _top_yes("no", out["no"])
        lip_bid = None if lips["yes"] is None else lips["yes"].price
        lip_ask = None if lips["no"] is None else round(1.0 - lips["no"].price, 2)

        if self.record_debug:
            self._log_row(ctx, now, ticker, fair, market_bid, market_ask, prob=prob,
                          ci_bid=ci_bid, ci_ask=ci_ask, bid_edge=bid_edge, ask_edge=ask_edge,
                          lean_bid=lean_bid, lean_ask=lean_ask, fee_bid=fee_bid, fee_ask=fee_ask,
                          pos=pos, net=net, eff_yes=eff_yes, eff_no=eff_no, ratio=_ratio(net),
                          bid_res=bid_res, ask_res=ask_res, bid_final=bid_final, ask_final=ask_final,
                          inflight_yes=inflight_yes, inflight_no=inflight_no,
                          resting_yes=resting_yes, resting_no=resting_no,
                          cap_yes=cap_yes, cap_no=cap_no, mfee_bid=mfee_bid, mfee_ask=mfee_ask,
                          qres_bid=qres_bid, qres_ask=qres_ask, quote_bid=quote_bid, quote_ask=quote_ask,
                          lip_bid=lip_bid, lip_ask=lip_ask, block_bid=block_bid, block_ask=block_ask,
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

    def _lip_target(self, ticker, side, res, book, ladder, net) -> LipTarget | None:
        """Where the one LIP quote on this side should rest, or None.

        Levels: every tick at or below the first tick outside the quote
        reservation (never inside the edge) and a tick inside the far touch.
        Among them, choose_lip_level takes the deepest earning >= prop_max_lip
        of the best LIP $/s that has >= lip_min_depth_ahead resting ahead --
        ALL size counts, ours included (it counts toward the reference price
        too). Our current LIP quote's own size is left out of "ahead" at its
        price, since it's what's being placed. Size: up to lip_lots minus the
        position on this side and minus our resting edge size here."""
        lc = self.cfg.lip
        params = self.lip.params.get(ticker) if self.lip is not None else None
        if (res is None or params is None or ladder is None or not lc.enable_lip_quoting
                or lc.lip_lots < 1 or book is None):
            return None
        held = net if side == "yes" else -net
        room = lc.lip_lots - max(held, 0.0) - self._resting(ticker, side)
        max_size = math.floor(room + 1e-9)
        if max_size < 1:
            return None
        top = _first_outside(side, res)
        far = book.ask if side == "yes" else (None if book.bid is None else 1.0 - book.bid)
        if far is not None:
            top = min(top, round(far - TICK, 2))
        if top < MIN_PRICE:
            return None
        live = self._lip_live.get((ticker, side))
        ours = {s: list(self._shown.get((ticker, s), [])) if self._own_in_book else
                [(p, z) for p, z in self._ours.get((ticker, s), [])] for s in ("yes", "no")}
        if live is not None:
            # Our LIP quote is the order being placed: score and count depth
            # without it, or it would always see itself ahead.
            lp, lz = live
            ours[side] = [(p, z) for p, z in ours[side] if not (abs(p - lp) < 1e-9 and abs(z - lz) < 1e-9)]
        displayed = {"yes": dict(ladder.yes_bids), "no": dict(ladder.no_bids)}
        others = {s: others_ladder(displayed[s], ours[s]) if self._own_in_book else displayed[s]
                  for s in ("yes", "no")}
        everyone = displayed[side] if self._own_in_book else _add(displayed[side], ours[side])

        def depth_ahead(price):
            return sum(z for p, z in everyone.items() if p >= price - 1e-9)

        choice = choose_lip_level(side=side, others_yes=others["yes"], others_no=others["no"],
                                  ours_yes=ours["yes"], ours_no=ours["no"], params=params,
                                  top_price=top, max_size=max_size, prop_max=lc.prop_max_lip,
                                  min_depth_ahead=lc.lip_min_depth_ahead, depth_ahead_fn=depth_ahead)
        if choice.price is None:
            return None
        return LipTarget(choice.price, choice.size)

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
        n = max(int(q.quote_levels_behind), 0) + 1
        if until is not None and now < until:
            # Recently hit: keep what's up (still outside the edge), add nothing.
            return Desired([], q.order_lots, edge_limit, room, ahead, max_levels=n)
        top = self._top_price(ticker, side, res, market_bid, market_ask, ladder)
        if top is None:
            return Desired([], q.order_lots, edge_limit, room, ahead, max_levels=n)
        cap = q.skip_quote_depth_ahead
        others = self._others_bids(ticker, side, ladder) if cap is not None else None
        prices = []
        for i in range(n):
            p = round(top - i * TICK, 2)
            if p < MIN_PRICE - 1e-9:
                break
            if others is not None and sum(z for px, z in others.items() if px >= p - 1e-9) > cap + 1e-9:
                self._skipped_deep += 1
                break                               # every deeper level has at least as much ahead
            prices.append(p)
        return Desired(prices, q.order_lots, edge_limit, room, ahead, max_levels=n)

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
        if side == "yes":
            if near is not None:
                yes = min(yes, near + n * TICK)
            if market_ask is not None:
                yes = min(yes, market_ask - TICK)
        else:
            if near is not None:
                yes = max(yes, near - n * TICK)
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
            "constant_edge": self.cfg.quoting.constant_edge,
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
            "block_bid": block or v.get("block_bid"), "block_ask": block or v.get("block_ask"),
            "atm_tradeable": ticker in self._atm_set if self.cfg.quoting.max_markets else True,
        }
        if fair is not None:
            for k, val in fair.extras.items():
                row[f"x_{k}"] = val
        if self.lip is not None and ticker in self.lip.params:
            row["lip_accrued_usd"] = self.lip.accrued_usd[ticker]
        self.debug_log.append(row)


def _add(bids: dict, ours: list) -> dict:
    """`bids` with our size added (shadow: the feed never shows our orders)."""
    out = dict(bids)
    for p, z in ours:
        out[p] = out.get(p, 0.0) + z
    return out


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
