"""Takes fired straight off a CME tick, without waiting on the trading loop.

Each quoter pass publishes a Ticket per side: every gate the normal take path
checks (blocks, caps, cooldown, loss budget, move gate) is evaluated there,
and the edge test is folded into one number -- the CME whole-touch shift from
the model's reference touch at which the take price clears the edge. The
venue-reader thread compares each CME touch against it and, on a cross,
sends the IOC itself. KXBTC15M 11-06: CME at the box -> take sent was 2.8 ms
p50 / ~9.5 ms p90 through the loop; misses lost the touch by 9 ms p50.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass

import pandas as pd

from kalshi_backtest.core import Order

TICKET_TTL_S = 0.5


@dataclass(frozen=True)
class Ticket:
    ticker: str
    side: str                 # "yes" fires on an up shift, "no" on a down shift
    coin: str
    ref_lb: float             # log CME bid / ask the shift is measured from
    ref_la: float
    thr_bp: float             # signed: yes needs shift >= thr, no needs shift <= thr
    cap_bp: float
    price: float              # in the order's own side terms
    size: float
    not_before: float         # wall s (take cooldown)
    expires: float            # wall s
    req_prob: float


def whole_touch_shift_bp(lb: float, la: float, ref_lb: float, ref_la: float, cap_bp: float) -> float:
    """PerpNowcast._touch_shift on log prices, capped like increment()."""
    db, da = lb - ref_lb, la - ref_la
    if db == 0 or da == 0 or (db > 0) != (da > 0):
        return 0.0
    s = math.copysign(min(abs(db), abs(da)), db) * 1e4
    return max(-cap_bp, min(cap_bp, s))


class FastTaker:
    """Sends only with quoting.fast_take_live (read per fire, so it hot-reloads)
    and a connection; otherwise logs what it would have sent (order event
    fast_take_shadow)."""

    def __init__(self, engine, quoter, conn=None, loop=None):
        self.engine, self.quoter, self.conn = engine, quoter, conn
        self.loop = loop
        self._tickets: dict[tuple[str, str], Ticket] = {}
        # (ticker, side) -> wall s of the last fast fire; the quoter's
        # one-take-per-window check reads it so both paths share the window.
        self.fired: dict[tuple[str, str], float] = {}
        self.n_fired = 0
        self._lock = threading.Lock()

    # --- loop thread -----------------------------------------------------

    def publish(self, ticker: str, side: str, ticket: Ticket | None):
        if ticket is None:
            self._tickets.pop((ticker, side), None)
        else:
            self._tickets[(ticker, side)] = ticket

    def clear(self):
        self._tickets.clear()

    # --- venue-reader thread ---------------------------------------------

    def on_cme_touch(self, coin: str, bid: float, ask: float, recv_wall: float):
        """Returns a callable that sends the take(s) this touch crosses, or None.
        Split so the reader can queue the frame for the loop (quote pulls)
        before blocking on the send."""
        if bid <= 0 or ask <= 0 or not self._tickets:
            return None
        now = time.time()
        lb, la = math.log(bid), math.log(ask)
        hits = []
        with self._lock:
            for key, tk in list(self._tickets.items()):
                if tk.coin != coin or now > tk.expires or now < tk.not_before:
                    continue
                s = whole_touch_shift_bp(lb, la, tk.ref_lb, tk.ref_la, tk.cap_bp)
                if (s >= tk.thr_bp) if tk.side == "yes" else (s <= tk.thr_bp):
                    self._tickets.pop(key, None)
                    self.fired[key] = now
                    hits.append((tk, s))
        if not hits:
            return None
        return lambda: [self._fire(tk, s, recv_wall) for tk, s in hits]

    def _fire(self, tk: Ticket, shift_bp: float, recv_wall: float):
        eng = self.engine
        t0 = time.time()
        meta = dict(ticker=tk.ticker, side=tk.side, price=tk.price, size=tk.size, shift_bp=shift_bp,
                    thr_bp=tk.thr_bp, req_prob=tk.req_prob, since_cme_ms=(t0 - recv_wall) * 1e3)
        blocked = (eng.blocked_reason() or ("market_paused" if eng.market_paused(tk.ticker) else None)
                   or (None if eng.live_trading else "not_live"))
        live = self.conn is not None and getattr(self.quoter.cfg.quoting, "fast_take_live", False)
        if not live or blocked is not None:
            self._post(eng._oev, "fast_take_shadow", blocked=blocked, **meta)
            return
        if not eng.order_client.allow_now("order", reason="take"):
            self._post(eng._oev, "fast_take_shadow", blocked="rate_limited", **meta)
            return
        order = Order(order_id=eng.next_order_id(), ticker=tk.ticker, side=tk.side, order_type="limit",
                      price=tk.price, size=tk.size, decision_time=pd.Timestamp(t0, unit="s", tz="UTC"),
                      intent="take")
        order.role = "edge"
        self._post(eng.fast_take_sent, order, self.quoter, meta)
        p0 = time.perf_counter()
        try:
            result = (self.conn.place_ioc(order), None)
        except Exception as exc:
            result = (None, exc)
        rtt = time.perf_counter() - p0
        self._post(eng._on_real_take_timed, order, (result, rtt, 0.0, time.perf_counter()))

    def _post(self, fn, *a, **kw):
        loop = self.loop
        if loop is None:
            fn(*a, **kw)
        else:
            loop.call_soon_threadsafe(lambda: fn(*a, **kw))


__all__ = ["FastTaker", "Ticket", "whole_touch_shift_bp", "TICKET_TTL_S"]
