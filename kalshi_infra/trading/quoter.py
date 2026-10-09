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
    take    : prob - (constant_edge + ci + lean + taker_fee * taker_fee_edge_multiplier) >= ask
    quote   : prob - (quoting_edge  + ci + lean + maker_fee) -> first tick outside,
              capped max_improve_ticks inside the near touch and a tick inside the far one

Each term is non-negative, so a reservation can never land on the wrong side
of fair however loaded the book is. Effective position = fills + takes sent
within inflight_timeout_s + our resting quote on that side: with a ~34ms round
trip and a decision on every book tick, a market otherwise runs flat -> cap
in one burst with zero lean charged (2026-08-28: 60% of same-ticker fills
landed inside one round trip of the previous one).

Anything that stops a side from trading records a reason string in the
decision log (`block_bid` / `block_ask`) and counts it in `block_counts`.
"""
from __future__ import annotations

import math

import pandas as pd

from kalshi_infra.kalshi.venue import MAX_PRICE, MIN_PRICE, TICK, fee
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.lip import LipTracker


class Quoter:
    def __init__(self, cfg, model, *, close_time: pd.Timestamp | None,
                 lip: LipTracker | None = None, record_debug: bool = True):
        """`cfg`: the series config (needs .quoting, .fees, .close, .risk,
        held by reference so live edits apply). `model`: the series'
        FairModel."""
        self.cfg = cfg
        self.model = model
        self.close_time = close_time
        self.lip = lip
        self.record_debug = record_debug

        self.fairs: dict[str, Fair | None] = {}
        self._sent: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
        # (ticker, side) -> {"order_id", "price", "size", "placed"}; price in that side's terms.
        self._quotes: dict[tuple[str, str], dict] = {}
        self._quote_block_until: dict[tuple[str, str], pd.Timestamp] = {}
        self._fills_seen = 0
        self._atm_set: set[str] = set()
        self._take_since: dict[tuple[str, str], float] = {}
        self._take_last: dict[tuple[str, str], float] = {}
        self._fresh_since: float | None = None
        self._was_stale = True
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

    def resting_quotes(self) -> dict[tuple[str, str], dict]:
        return self._quotes

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
            self.lip.snapshot(now_s, {t: ctx.book(t) for t in self.lip.params}, self._quotes)

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _sync_quote_fills(self, ctx):
        """Retire any of our quotes that traded and start its requote delay.
        Read off the portfolio's fill list -- the one signal identical in
        backtest and live. Resets when the list shrinks (arming swaps in a
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
            self._quote_block_until[key] = f.time + pd.Timedelta(seconds=self.cfg.quoting.requote_delay_s)
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

    def _resting(self, ticker: str, side: str) -> float:
        q = self._quotes.get((ticker, side))
        return 0.0 if q is None else q["size"]

    def _block(self, reason: str):
        self.block_counts[reason] = self.block_counts.get(reason, 0) + 1
        return reason

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def signal_stale(self) -> bool:
        """Fair inputs older than the risk limit, or not yet fresh for
        fresh_recovery_s after a stale episode (no flapping)."""
        r = self.cfg.risk
        age = self.model.signal_age_s()
        limit = r.max_signal_age_s
        rem = self._remaining_s()
        if rem is not None and r.near_expiry_s > 0 and rem <= r.near_expiry_s:
            limit = min(limit, r.max_signal_age_near_expiry_s)
        now = pd.Timestamp.now(tz="UTC").timestamp()
        if age is None or age > limit:
            self._fresh_since = None
            return True
        if self._fresh_since is None:
            self._fresh_since = now
        return (now - self._fresh_since) < r.fresh_recovery_s

    def _remaining_s(self, now_s: float | None = None) -> float | None:
        if self.close_time is None:
            return None
        if now_s is None:
            now_s = pd.Timestamp.now(tz="UTC").timestamp()
        return self.close_time.timestamp() - now_s

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

    def _close_phase_block(self, fair: Fair, prob_q: float, now_s: float) -> str | None:
        c = self.cfg.close
        rem = self._remaining_s(now_s)
        if c.last_phase_s <= 0 or rem is None or rem > c.last_phase_s:
            return None
        half_band = max(fair.prob - fair.lo, fair.hi - fair.prob, 0.0)
        if c.last_phase_max_half_band is not None and half_band > c.last_phase_max_half_band:
            return "last_phase_band"
        eps = c.last_phase_otm_fair_max
        if eps is not None and eps < prob_q < 1.0 - eps:
            return "last_phase_not_otm"
        return None

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
        q, fees_cfg = self.cfg.quoting, self.cfg.fees
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

        fee_bid = (fee(market_ask, 1.0, fees_cfg.taker_fee_multiplier) * fees_cfg.taker_fee_edge_multiplier
                   if market_ask is not None else 0.0)
        fee_ask = (fee(market_bid, 1.0, fees_cfg.taker_fee_multiplier) * fees_cfg.taker_fee_edge_multiplier
                   if market_bid is not None else 0.0)
        bid_edge = q.constant_edge + ci_bid + lean_bid + fee_bid
        ask_edge = q.constant_edge + ci_ask + lean_ask + fee_ask
        bid_res, ask_res = prob - bid_edge, prob + ask_edge

        # Side vetoes, in priority order. Same reasons gate takes and quotes.
        block_bid = block_ask = None
        if not self._atm_tradeable(ticker):
            block_bid = block_ask = "outside_atm_set"
        else:
            cp = self._close_phase_block(fair, prob, now_s)
            if cp is not None:
                block_bid = block_ask = cp
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
            ctx.buy(ticker=ticker, side="yes", size=q.order_size, price=round(bid_final, 2),
                    order_type="limit")
            self._sent.setdefault((ticker, "yes"), []).append((now, q.order_size))
        if ask_final is not None:
            # Selling yes at p == buying no at 1 - p.
            ctx.buy(ticker=ticker, side="no", size=q.order_size, price=round(1.0 - ask_final, 2),
                    order_type="limit")
            self._sent.setdefault((ticker, "no"), []).append((now, q.order_size))

        # Quotes: quoting_edge replaces constant_edge (a rest and a cross are
        # different risks); CI and lean describe the market/inventory and are
        # shared. Maker fee, not taker, priced at the pre-fee reservation.
        q_base_bid = q.quoting_edge + ci_bid + lean_bid
        q_base_ask = q.quoting_edge + ci_ask + lean_ask
        mfee_bid = fee(prob - q_base_bid, 1.0, fees_cfg.maker_fee_multiplier)
        mfee_ask = fee(prob + q_base_ask, 1.0, fees_cfg.maker_fee_multiplier)
        qres_bid = prob - (q_base_bid + mfee_bid)
        qres_ask = prob + (q_base_ask + mfee_ask)

        quoting = q.enable_quoting
        quote_bid, quote_ask = self._manage_quotes(
            ctx, ticker,
            qres_bid if (quoting and block_bid is None) else None,
            qres_ask if (quoting and block_ask is None) else None,
            now, prob, market_bid=market_bid, market_ask=market_ask, net=net)

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
                          block_bid=block_bid, block_ask=block_ask)

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def _manage_quotes(self, ctx, ticker, res_bid, res_ask, now, prob, *,
                       market_bid=None, market_ask=None, net=None):
        """Bring both sides' resting quotes in line with the reservations
        (None = don't quote that side). Returns the (bid, ask) quoted, YES
        terms. Cancel-then-replace, one order per side."""
        q = self.cfg.quoting
        if net is None:
            pos = ctx.position(ticker)
            net = pos.yes_qty - pos.no_qty
        # Room for the NEW quote excludes our current one (about to be
        # replaced) but includes takes sent this very call.
        base_yes = net + self._inflight(ticker, "yes", now)
        base_no = net - self._inflight(ticker, "no", now)
        # No usable touch on either side: nothing to cap against, so withdraw
        # rather than rest blind. One-sided books are fine.
        have_touch = market_bid is not None or market_ask is not None
        want = {
            "yes": None if res_bid is None or not have_touch
            or base_yes + q.order_size > q.max_position + 1e-9
            else self._desired("yes", res_bid, market_bid, market_ask, (ticker, "yes"), now),
            "no": None if res_ask is None or not have_touch
            or base_no - q.order_size < -q.max_position - 1e-9
            else self._desired("no", res_ask, market_bid, market_ask, (ticker, "no"), now),
        }
        improve = q.requote_improve_ticks * TICK
        for side, w in want.items():
            key = (ticker, side)
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
                order = ctx.buy(ticker=ticker, side=side, size=q.order_size, price=w,
                                order_type="limit", intent="quote")
                self._quotes[key] = {"order_id": order.order_id, "price": w,
                                     "size": q.order_size, "placed": now}
        yq, nq = self._quotes.get((ticker, "yes")), self._quotes.get((ticker, "no"))
        return (None if yq is None else yq["price"],
                None if nq is None else round(1.0 - nq["price"], 2))

    def _desired(self, side, reservation, market_bid, market_ask, key, now) -> float | None:
        """Resting price in that side's own terms, or None. First tick
        OUTSIDE the reservation (rounding to nearest would put half our
        quotes inside the edge), at most max_improve_ticks inside the near
        touch, and a tick inside the far touch so it never crosses -- if it
        were marketable, the take test already priced that with the taker fee."""
        until = self._quote_block_until.get(key)
        if until is not None and now < until:
            return None
        n = self.cfg.quoting.max_improve_ticks
        if side == "yes":
            px = math.floor(reservation / TICK + 1e-9) * TICK
            if n is not None and market_bid is not None:
                px = min(px, market_bid + n * TICK)
            if market_ask is not None:
                px = min(px, market_ask - TICK)
        else:
            px = math.ceil(reservation / TICK - 1e-9) * TICK
            if n is not None and market_ask is not None:
                px = max(px, market_ask - n * TICK)
            if market_bid is not None:
                px = max(px, market_bid + TICK)
        px = round(px, 2)
        if not (MIN_PRICE <= px <= MAX_PRICE):
            return None
        return px if side == "yes" else round(1.0 - px, 2)

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
            "block_bid": block or v.get("block_bid"), "block_ask": block or v.get("block_ask"),
            "atm_tradeable": ticker in self._atm_set if self.cfg.quoting.max_markets else True,
        }
        if fair is not None:
            for k, val in fair.extras.items():
                row[f"x_{k}"] = val
        if self.lip is not None and ticker in self.lip.params:
            row["lip_accrued_usd"] = self.lip.accrued_usd[ticker]
        self.debug_log.append(row)


def _past_fair(side: str, price: float, prob: float | None) -> bool:
    """Resting quote (own-side terms) at or beyond fair. Unknown fair counts
    as past it, so the quote is replaced."""
    if prob is None or math.isnan(prob):
        return True
    f = prob if side == "yes" else 1.0 - prob
    return price >= f - 1e-9
