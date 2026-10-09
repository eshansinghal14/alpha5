"""
Generic quoter: turns per-market `Fair`s into takes and resting quotes.

One instance per event, used as the engine's strategy callable (live via
LiveEngine, backtest via kalshi_backtest.engine.Backtest -- both provide the
same ctx surface). Series code never places orders; it publishes Fairs and,
optionally, vetoes sides through `FairModel.side_block`.

Per market and side, every decision is:

    d       = distance from fair to the band edge (prob - lo buying, hi - prob selling)
    ci      = edge_fraction * d
    lean    = (lean_multiplier * d + lean_dollars) * ratio ** lean_exponent
              ratio = effective position / max_position, only on the side that GROWS it
    take    : prob - (constant_edge + ci + lean + taker_fee) >= ask
    quote   : prob - (quoting_edge  + ci + lean + maker_fee) -> first tick outside,
              capped max_improve_ticks inside the near touch and a tick inside the far one

Fees are Kalshi's own for this event (kalshi.fees.FeeSchedule). Each term is
non-negative, so a reservation never lands on the wrong side of fair.
Effective position = fills + takes sent within inflight_timeout_s + our
resting quotes on that side (2026-08-28: 60% of same-ticker fills landed
inside one round trip of the previous one, so ignoring in-flight size let a
market run flat -> cap with zero lean charged).

Two resting orders per side:
  * the EDGE quote (order_lots) -- priced on edge alone;
  * the LIP quote (lip_lots) -- exists only to earn LIP, resting at or
    outside the edge quote's reservation (see _manage_lip).

Anything that stops a side from trading records a reason string in the
decision log (`block_bid` / `block_ask`) and counts it in `block_counts`.
"""
from __future__ import annotations

import math

import pandas as pd

from kalshi_infra.kalshi.venue import MAX_PRICE, MIN_PRICE, TICK
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.lip import LipTracker, choose_lip_level, others_ladder

EDGE, LIP = "edge", "lip"


class Quoter:
    def __init__(self, cfg, model, fees, *, close_time: pd.Timestamp | None,
                 lip: LipTracker | None = None, record_debug: bool = True):
        """`cfg`: the series config (.quoting, .risk, .lip; held by reference
        so live edits apply). `model`: the series' FairModel. `fees`: the
        event's kalshi.fees.FeeSchedule."""
        self.cfg = cfg
        self.model = model
        self.fees = fees
        self.close_time = close_time
        self.lip = lip
        self.record_debug = record_debug

        self.fairs: dict[str, Fair | None] = {}
        self._sent: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
        # (ticker, side, role) -> {"order_id", "price", "size", "placed"}; price in that side's terms.
        self._quotes: dict[tuple[str, str, str], dict] = {}
        self._quote_block_until: dict[tuple[str, str], pd.Timestamp] = {}
        self._fills_seen = 0
        self._atm_set: set[str] = set()
        self._take_since: dict[tuple[str, str], float] = {}
        self._take_last: dict[tuple[str, str], float] = {}
        self._fresh_since: float | None = None
        self.block_counts: dict[str, int] = {}
        self.debug_log: list[dict] = []

    # ------------------------------------------------------------------
    # Engine interface
    # ------------------------------------------------------------------

    def live_quote_order_ids(self) -> list[int]:
        return [q["order_id"] for q in self._quotes.values()]

    def clear_quotes(self):
        """Forget quotes WITHOUT cancelling -- the engine has just cancelled them."""
        self._quotes.clear()

    def drop_quote(self, order_id: int):
        """Forget a quote that never reached the book (rate limit, guard,
        reject). Without this the quoter prices against a phantom order and,
        since it only requotes when the level moves, leaves the market bare."""
        for key, q in list(self._quotes.items()):
            if q["order_id"] == order_id:
                del self._quotes[key]
                return

    def resting_by_side(self) -> dict[tuple[str, str], list[tuple[float, float]]]:
        """(ticker, side) -> [(price, size)] of every resting quote, for LIP scoring."""
        out: dict[tuple[str, str], list] = {}
        for (t, s, _), q in self._quotes.items():
            out.setdefault((t, s), []).append((q["price"], q["size"]))
        return out

    def __call__(self, ctx):
        # Across all tickers: a quote fills whenever someone crosses it,
        # unrelated to whatever woke us. Keyed on having quotes, not on
        # enable_quoting, so turning quoting off still retires what rests.
        if self._quotes:
            self._sync_quote_fills(ctx)
        else:
            self._fills_seen = len(ctx.portfolio.fills)

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
        """Retire any quote of ours that traded and start its side's requote
        delay. Read off the portfolio's fill list -- the one signal identical
        in backtest and live. Resets when the list shrinks (arming swaps in a
        fresh Portfolio)."""
        fills = ctx.portfolio.fills
        if len(fills) < self._fills_seen:
            self._fills_seen = 0
        if len(fills) == self._fills_seen:
            return
        by_order = {q["order_id"]: key for key, q in self._quotes.items()}
        for f in fills[self._fills_seen:]:
            key = by_order.pop(f.order_id, None)
            if key is None:
                continue
            self._quotes.pop(key, None)
            ctx.cancel(f.order_id)   # any partial-fill remainder
            self._quote_block_until[key[:2]] = f.time + pd.Timedelta(seconds=self.cfg.quoting.requote_delay_s)
        self._fills_seen = len(fills)

    def _inflight(self, ticker: str, side: str, now: pd.Timestamp) -> float:
        """Take size sent on (ticker, side) within inflight_timeout_s. A take
        fills on arrival or not at all, so a size total is enough."""
        key = (ticker, side)
        recent = self._sent.get(key)
        if not recent:
            return 0.0
        cutoff = now - pd.Timedelta(seconds=self.cfg.quoting.inflight_timeout_s)
        recent = [(t, s) for t, s in recent if t > cutoff]
        self._sent[key] = recent
        return sum(s for _, s in recent)

    def _resting(self, ticker: str, side: str, role: str | None = None) -> float:
        return sum(q["size"] for (t, s, r), q in self._quotes.items()
                   if t == ticker and s == side and (role is None or r == role))

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
        """Signal has held for take_confirm_s, and at most one take per side
        per window after that. 0 = immediate."""
        w = self.cfg.quoting.take_confirm_s
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

        gate = None
        if fair is None:
            gate = self._block("no_fair")
        elif self.signal_stale():
            gate = self._block("stale_signal")
        if gate is not None:
            self._manage_quotes(ctx, ticker, None, None, now, None)
            self._manage_lip(ctx, ticker, book, None, None, now)
            if self.record_debug:
                self._log_row(ctx, now, ticker, None, market_bid, market_ask, block=gate)
            return

        eps = q.fair_clip_eps
        prob = min(max(fair.prob, eps), 1.0 - eps)
        d_lo = max(fair.prob - fair.lo, 0.0)
        d_hi = max(fair.hi - fair.prob, 0.0)
        ci_bid, ci_ask = q.edge_fraction * d_lo, q.edge_fraction * d_hi

        pos = ctx.position(ticker)
        net = pos.yes_qty - pos.no_qty
        max_pos = q.max_position
        inflight_yes, inflight_no = self._inflight(ticker, "yes", now), self._inflight(ticker, "no", now)
        resting_yes, resting_no = self._resting(ticker, "yes"), self._resting(ticker, "no")
        eff_yes = net + inflight_yes + resting_yes
        eff_no = net - inflight_no - resting_no

        def _ratio(x):
            return 0.0 if max_pos <= 0 else max(-1.0, min(1.0, x / max_pos))

        lean_bid = (q.lean_multiplier * d_lo + q.lean_dollars) * max(_ratio(eff_yes), 0.0) ** q.lean_exponent
        lean_ask = (q.lean_multiplier * d_hi + q.lean_dollars) * max(-_ratio(eff_no), 0.0) ** q.lean_exponent

        fee_bid = self.fees.taker(market_ask) if market_ask is not None else 0.0
        fee_ask = self.fees.taker(market_bid) if market_bid is not None else 0.0
        bid_edge = q.constant_edge + ci_bid + lean_bid + fee_bid
        ask_edge = q.constant_edge + ci_ask + lean_ask + fee_ask
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

        cap_yes = eff_yes < max_pos - 1e-9
        cap_no = eff_no > -max_pos + 1e-9

        key_b, key_a = (ticker, "take_yes"), (ticker, "take_no")
        want_b = block_bid is None and market_ask is not None and bid_res >= market_ask and cap_yes
        want_a = block_ask is None and market_bid is not None and ask_res <= market_bid and cap_no
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

        # Quotes: quoting_edge replaces constant_edge (a rest and a cross are
        # different risks); CI and lean are shared. Maker fee, priced at the
        # pre-fee reservation.
        q_base_bid = q.quoting_edge + ci_bid + lean_bid
        q_base_ask = q.quoting_edge + ci_ask + lean_ask
        mfee_bid = self.fees.maker(prob - q_base_bid)
        mfee_ask = self.fees.maker(prob + q_base_ask)
        qres_bid = prob - (q_base_bid + mfee_bid)
        qres_ask = prob + (q_base_ask + mfee_ask)

        quoting = q.enable_quoting
        res_b = qres_bid if (quoting and block_bid is None) else None
        res_a = qres_ask if (quoting and block_ask is None) else None
        quote_bid, quote_ask = self._manage_quotes(ctx, ticker, res_b, res_a, now, prob,
                                                   market_bid=market_bid, market_ask=market_ask, net=net)
        lip_bid, lip_ask = self._manage_lip(ctx, ticker, book, res_b, res_a, now, net=net)

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
                          lip_bid=lip_bid, lip_ask=lip_ask, block_bid=block_bid, block_ask=block_ask)

    # ------------------------------------------------------------------
    # Edge quotes
    # ------------------------------------------------------------------

    def _room(self, ticker, side, net, now, exclude_role):
        """Position room for a new quote on `side`, counting takes in flight
        and our OTHER resting quotes on that side."""
        other = self._resting(ticker, side) - self._resting(ticker, side, exclude_role)
        if side == "yes":
            return self.cfg.quoting.max_position - (net + self._inflight(ticker, "yes", now) + other)
        return self.cfg.quoting.max_position + (net - self._inflight(ticker, "no", now) - other)

    def _manage_quotes(self, ctx, ticker, res_bid, res_ask, now, prob, *,
                       market_bid=None, market_ask=None, net=None):
        """Bring both sides' edge quotes in line with the reservations (None =
        don't quote that side). Returns the (bid, ask) quoted, YES terms.
        Cancel-then-replace, one order per side."""
        q = self.cfg.quoting
        if net is None:
            pos = ctx.position(ticker)
            net = pos.yes_qty - pos.no_qty
        # No usable touch on either side: nothing to cap against, so withdraw
        # rather than rest blind. One-sided books are fine.
        have_touch = market_bid is not None or market_ask is not None
        want = {}
        for side, res in (("yes", res_bid), ("no", res_ask)):
            ok = res is not None and have_touch and self._room(ticker, side, net, now, EDGE) >= q.order_lots - 1e-9
            want[side] = self._desired(side, res, market_bid, market_ask, (ticker, side), now) if ok else None
        improve = q.requote_improve_ticks * TICK
        for side, w in want.items():
            key = (ticker, side, EDGE)
            live = self._quotes.get(key)
            if live is not None:
                if w is None:
                    stale = True
                else:
                    inside = live["price"] > w + 1e-9           # now inside the edge we demand
                    chase = (w - live["price"]) >= improve - 1e-9  # level ran far enough to be worth queue
                    stale = inside or chase
                    young = now - live["placed"] < pd.Timedelta(seconds=q.min_quote_life_s)
                    if stale and young and not _past_fair(side, live["price"], prob):
                        stale = False
                if stale:
                    ctx.cancel(live["order_id"])
                    self._quotes.pop(key, None)
                    live = None
            if live is None and w is not None:
                order = ctx.buy(ticker=ticker, side=side, size=q.order_lots, price=w,
                                order_type="limit", intent="quote", role=EDGE)
                self._quotes[key] = {"order_id": order.order_id, "price": w, "size": q.order_lots,
                                     "placed": now}
        yq, nq = self._quotes.get((ticker, "yes", EDGE)), self._quotes.get((ticker, "no", EDGE))
        return (None if yq is None else yq["price"], None if nq is None else round(1.0 - nq["price"], 2))

    def _desired(self, side, reservation, market_bid, market_ask, key, now) -> float | None:
        """Resting price in that side's own terms, or None. First tick
        OUTSIDE the reservation (rounding to nearest would put half our
        quotes inside the edge), at most max_improve_ticks inside the near
        touch, and a tick inside the far touch so it never crosses -- if it
        were marketable, the take test already priced that with the taker fee."""
        until = self._quote_block_until.get(key)
        if until is not None and now < until:
            return None
        top = _first_outside(side, reservation)
        n = self.cfg.quoting.max_improve_ticks
        yes = top if side == "yes" else round(1.0 - top, 2)
        if side == "yes":
            if market_bid is not None:
                yes = min(yes, market_bid + n * TICK)
            if market_ask is not None:
                yes = min(yes, market_ask - TICK)
        else:
            if market_ask is not None:
                yes = max(yes, market_ask - n * TICK)
            if market_bid is not None:
                yes = max(yes, market_bid + TICK)
        yes = round(yes, 2)
        if not (MIN_PRICE <= yes <= MAX_PRICE):
            return None
        return yes if side == "yes" else round(1.0 - yes, 2)

    # ------------------------------------------------------------------
    # LIP quote
    # ------------------------------------------------------------------

    def _manage_lip(self, ctx, ticker, book, res_bid, res_ask, now, *, net=None):
        """Rest up to lip_lots per side purely for LIP, every decision:

        1. Levels considered: every tick at or outside the edge quote's own
           reservation (first tick outside it, own-side terms), never inside
           the far touch.
        2. The best LIP $/s over those levels is the reference; we take the
           DEEPEST level earning >= prop_max_lip of it that has at least
           lip_min_depth_ahead resting ahead of us (better prices plus the
           whole same-price queue -- we always join at the back).
        3. Size is cut to the fewest lots earning the same reward.
        4. Same level: amend the size (Kalshi sends an increase to the back
           of the queue; a decrease is done as cancel + new order, so the
           quote always sits at the back). Different level: cancel + new.

        Safety cancels -- no fair, no level, too little depth ahead -- go out
        first with force (they bypass the write-rate limiter). Returns the
        (bid, ask) LIP prices quoted, YES terms."""
        lc = self.cfg.lip
        params = self.lip.params.get(ticker) if self.lip is not None else None
        ladder = getattr(book, "ladder", None) if book is not None else None
        out = {}
        for side, res in (("yes", res_bid), ("no", res_ask)):
            key = (ticker, side, LIP)
            live = self._quotes.get(key)
            choice = None
            if (res is not None and params is not None and ladder is not None and lc.lip_lots >= 1
                    and self.cfg.quoting.enable_quoting):
                choice = self._lip_choice(ticker, side, res, book, ladder, params, live, net, now)
            if choice is None or choice.price is None:
                if live is not None:
                    ctx.cancel(live["order_id"], force=True)   # safety cancel: never rate-limited
                    self._quotes.pop(key, None)
                out[side] = None
                continue
            if live is not None and abs(live["price"] - choice.price) < 1e-9:
                if abs(choice.size - live["size"]) > 1e-9:
                    if choice.size > live["size"] and ctx.amend(live["order_id"], choice.size):
                        live["size"] = choice.size      # increase: Kalshi moves it to the back
                    else:
                        # A decrease would KEEP priority (and amend may be
                        # unavailable): cancel + new order to stay at the back.
                        ctx.cancel(live["order_id"])
                        self._quotes.pop(key, None)
                        live = None
            elif live is not None:
                ctx.cancel(live["order_id"])
                self._quotes.pop(key, None)
                live = None
            if live is None:
                order = ctx.buy(ticker=ticker, side=side, size=choice.size, price=choice.price,
                                order_type="limit", intent="quote", role=LIP)
                self._quotes[key] = {"order_id": order.order_id, "price": choice.price,
                                     "size": choice.size, "placed": now}
            q = self._quotes[key]
            out[side] = q["price"] if side == "yes" else round(1.0 - q["price"], 2)
        return out.get("yes"), out.get("no")

    def _lip_choice(self, ticker, side, res, book, ladder, params, live, net, now):
        lc = self.cfg.lip
        if self._quote_block_until.get((ticker, side)) is not None and now < self._quote_block_until[(ticker, side)]:
            return None
        if net is None:
            net = 0.0
        room = self._room(ticker, side, net, now, LIP)
        max_size = math.floor(min(lc.lip_lots, room) + 1e-9)
        if max_size < 1:
            return None
        top = _first_outside(side, res)
        # Never at or through the far touch: own-side price below 1 - opposite best.
        far = book.ask if side == "yes" else (None if book.bid is None else 1.0 - book.bid)
        if far is not None:
            top = min(top, round(far - TICK, 2))
        if top < MIN_PRICE:
            return None
        ours = self.resting_by_side()
        mine = {s: [(p, z) for p, z in ours.get((ticker, s), [])] for s in ("yes", "no")}
        if live is not None:
            mine[side] = [(p, z) for p, z in mine[side] if not (abs(p - live["price"]) < 1e-9 and z == live["size"])]
        others_yes = others_ladder(ladder.yes_bids, ours.get((ticker, "yes"), []))
        others_no = others_ladder(ladder.no_bids, ours.get((ticker, "no"), []))
        others = others_yes if side == "yes" else others_no

        def depth_ahead(price):
            # Everything better than us plus the whole same-price queue: we
            # always join at the back. Our own edge quote counts -- it trades
            # before us too.
            ahead = sum(z for p, z in others.items() if p >= price - 1e-9)
            return ahead + sum(z for p, z in mine[side] if p >= price - 1e-9)

        return choose_lip_level(side=side, others_yes=others_yes, others_no=others_no,
                                ours_yes=mine["yes"], ours_no=mine["no"], params=params,
                                top_price=top, max_size=max_size, prop_max=lc.prop_max_lip,
                                min_depth_ahead=lc.lip_min_depth_ahead, depth_ahead_fn=depth_ahead)

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


def _first_outside(side: str, reservation: float) -> float:
    """First tick at or outside the reservation, in `side`'s own price terms."""
    if side == "yes":
        return round(math.floor(reservation / TICK + 1e-9) * TICK, 2)
    return round(1.0 - math.ceil(reservation / TICK - 1e-9) * TICK, 2)


def _past_fair(side: str, price: float, prob: float | None) -> bool:
    """Resting quote (own-side terms) at or beyond fair. Unknown fair counts
    as past it, so the quote is replaced."""
    if prob is None or math.isnan(prob):
        return True
    f = prob if side == "yes" else 1.0 - prob
    return price >= f - 1e-9
