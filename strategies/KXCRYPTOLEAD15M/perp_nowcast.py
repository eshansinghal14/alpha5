"""
Perp nowcast: the spot-median mid moved to where the perps (Binance, with OKX
as its check) already are, for pricing a few hundred ms sooner.

    gap      = (log perp_LEAD - basis_LEAD) - log spot_mid       (Binance)
    nowcast  = spot_mid * exp(clip(gap, +-CAP_BP))

Binance alone, not the median of the three: the median moves only when the
second venue does, and OKX/Bybit reach us ~40ms after Binance. On 9,523
informed KXSOL15M takers 10-20 00:25-02:15 UTC (Kalshi mid >= 1c their way in
5s, our receipt clock), half of each venue's move came at Binance -10ms, OKX
+15, Bybit +45, Coinbase spot -2.5; Binance showed 0.29bp at the print vs
0.17 for the others, and beat Coinbase on 72% of paired moves (median 85ms).
OKX only checks: when Binance is more than CHECK_BP from OKX the nowcast is
spot unchanged (with one checker neither can be trusted); when Binance is
stale, OKX stands in.

perp_v is the venue's top-of-book mid (Binance bookTicker, Bybit orderbook.1,
OKX bbo-tbt). basis_v is venue v's trailing BASIS_S mean of
log(perp_v / spot_mid): USDT quote and funding, so only the perps' lead
carries over, never their level. As spot catches up the gap closes on its own.

Fed the book mid, not the last trade: a last trade bounces between bid and
ask, and on a 3 min us-east-2 recording (10-20 00:58 UTC) Binance bookTicker
arrived as fast as trades (p50 ~72ms both) and moved with them on big moves.

ETH 10-18/19 (old us-east-1 box, trade input): perps led our spot median by
~250ms, and the nowcast moved with the Kalshi mid at 0s where our input lagged
it by ~200ms. Shadow only on the race: logged as x_perp_* on decision rows.
"""
from __future__ import annotations

import collections
import datetime as dt
import math
import time
from zoneinfo import ZoneInfo

import numpy as np

# Bybit dropped 10-20: slowest perp to us (p50 113-145ms vs OKX 80-120), and
# OKX alone covers the veto and the stale-Binance fallback.
PERPS = ("binance_perp", "okx_perp")
LEAD = "binance_perp"
# Tracked (price, basis), never in nowcast(); in increment() only when listed
# in pricing.nowcast_leads. CME reached this box first on most moves: Binance
# moves showed on CME 22-49 ms earlier (BTC/ETH/SOL/XRP, live 11-01 23:00 -
# 11-02 03:00 UTC, research/latency/cme_live_rec.py).
SHADOW = ("cme",)
# A touch venue's increment counts only when its whole touch moved since spot
# last did: bid and ask the same way, by the smaller of the two. A one-sided
# change (a level filled or pulled) moves the mid half a tick, and spot
# followed 3% of it within 1s; whole-touch shifts were followed 59-64% (BTC
# CME moves >= 2bp in 250ms, 11-01 23:00 - 11-02 21:00 UTC).
TOUCH_VENUES = ("cme",)
# Weight on a touch venue's increment when pricing (takes use it in full).
# Same data, 1s ahead: at 0.5 it predicted BTC spot (0.9971 of no-lead error)
# and the KXBTC15M mid (0.9954) better than Binance (1.0031, 0.9998); at 1.0
# it was worse than no lead.
TOUCH_WEIGHT = 0.5
# How much touch history touch_shift() can look back over (s).
TOUCH_HIST_S = 2.0
# CME's touch is ignored (so it goes stale and the lead falls back) while
# Globex crypto is in its daily halt or weekend close, and when wider than
# this. In the halt (16:00-17:00 Chicago) the feed keeps sending a pre-open
# book: 11-06 22:00 UTC its spread was 8.5bp p50 vs 2.0 open, its touch jumped
# 1-2bp on order entry, and KXBTC15M traded those jumps both ways (174 takes,
# -2c at 10s). Open hours 11-05/06: spread p99 11-12.5bp.
TOUCH_MAX_SPREAD_BP = 8.0
CME_TZ = ZoneInfo("America/Chicago")


def cme_closed(t_wall: float) -> bool:
    """Globex crypto futures closed: 16:00-17:00 CT Mon-Thu, Fri 16:00 CT to Sun 17:00 CT."""
    d = dt.datetime.fromtimestamp(t_wall, CME_TZ)
    wd, h = d.weekday(), d.hour
    if wd == 5 or (wd == 4 and h >= 16) or (wd == 6 and h < 17):
        return True
    return wd < 4 and h == 16


# Binance vs OKX, both net of basis. Above the cap so a real lead (|gap| p90
# 1.4bp, SOL 10-20) isn't vetoed while OKX catches up. 6 h replay 10-20: it
# fired 73-122/h (~0.2s each) and spot 1s later sided with the others 66-85%.
CHECK_BP = 3.0
# Basis averaging window. Long next to the ~250ms lead, so a lead doesn't leak
# into the basis and cancel itself; short enough to follow funding drift.
BASIS_S = 30.0
# Largest correction (bp). ETH nowcast vs spot input: p90 1.9bp, p99 6.2bp
# (10-18/19); past ~2bp it was mostly perps decoupling from spot.
CAP_BP = 2.0
# A venue whose book hasn't changed for this long is left out. Without a
# usable Binance the nowcast uses OKX if fresh, else the spot mid unchanged.
FRESH_S = 2.0
MIN_VENUES = 1
# Basis samples a venue needs before it counts.
MIN_BASIS_N = 20


class PerpNowcast:
    """Per coin. `on_perp(venue, coin, book mid, t)` / `on_spot` on one
    monotonic clock; `nowcast(coin, now, spot_mid)` -> (price, gap_bp
    clipped, n fresh venues)."""

    def __init__(self):
        self._px: dict[tuple[str, str], tuple[float, float]] = {}      # (venue, coin) -> (log px, t)
        self._basis: dict[tuple[str, str], collections.deque] = {}     # (venue, coin) -> (t, log perp - log spot)
        self._bsum: dict[tuple[str, str], float] = {}
        self._spot: dict[str, float] = {}                              # coin -> latest log spot
        # Latest-mover state per (venue, coin): the venue's log price when
        # spot last moved, so `increment` is only what it did since then.
        self._lead_at_spot: dict[tuple[str, str], float] = {}
        # TOUCH_VENUES: latest (log bid, log ask), and as of the last spot move.
        self._touch: dict[tuple[str, str], tuple[float, float]] = {}
        self._touch_at_spot: dict[tuple[str, str], tuple[float, float]] = {}
        # TOUCH_VENUES: (t, log bid, log ask) over the last TOUCH_HIST_S, for touch_shift().
        self._touch_hist: dict[tuple[str, str], collections.deque] = {}
        self._closed = (0.0, False)       # (wall s checked, cme_closed) -- rechecked each second
        # Venues increment() tries, first fresh one wins (the owner's
        # pricing.nowcast_leads; SpotFeed sets this).
        self.leads_fn = lambda: (LEAD,)
        self.lead_used: dict[str, str | None] = {}

    def on_spot(self, coin: str, mid: float | None):
        if mid is not None and mid > 0:
            self._spot[coin] = math.log(mid)
            for v in PERPS + SHADOW:
                p = self._px.get((v, coin))
                if p is not None:
                    self._lead_at_spot[(v, coin)] = p[0]
                tb = self._touch.get((v, coin))
                if tb is not None:
                    self._touch_at_spot[(v, coin)] = tb

    def _touch_shift(self, venue: str, coin: str) -> float | None:
        now_t, ref = self._touch.get((venue, coin)), self._touch_at_spot.get((venue, coin))
        if now_t is None or ref is None:
            return None
        db, da = now_t[0] - ref[0], now_t[1] - ref[1]
        if db == 0 or da == 0 or (db > 0) != (da > 0):
            return 0.0
        return math.copysign(min(abs(db), abs(da)), db)

    def touch_shift(self, venue: str, coin: str, now: float, window: float) -> float:
        """bp the venue's whole touch moved over the last `window` s: the smaller
        of the bid and ask moves when both went the same way, else 0. Unlike
        increment() it doesn't reset on a spot move."""
        h = self._touch_hist.get((venue, coin))
        if not h or now - h[-1][0] > FRESH_S:
            return 0.0
        ref = h[0]
        for e in h:
            if e[0] > now - window:
                break
            ref = e
        db, da = h[-1][1] - ref[1], h[-1][2] - ref[2]
        if db == 0 or da == 0 or (db > 0) != (da > 0):
            return 0.0
        return math.copysign(min(abs(db), abs(da)), db) * 1e4

    def increment(self, coin: str, now: float, spot_mid: float | None,
                  leads: tuple[str, ...] | None = None, full: bool = False) -> tuple[float | None, float | None]:
        """Latest-mover nowcast: spot plus the lead venue's move since spot
        last moved, capped at CAP_BP. The lead is the first of `leads`
        (default leads_fn()) that is fresh, so CME priced while open falls
        back to Binance on weekends and in its daily halt; `lead_used[coin]`
        says which. Unlike nowcast(), a spot move is never pulled back toward
        the lead's level: the increment resets to 0 on every spot move.
        A TOUCH_VENUES lead counts only its whole-touch shift, at TOUCH_WEIGHT
        unless `full`. Returns (price, inc_bp); inc None when no lead is fresh."""
        if spot_mid is None or spot_mid <= 0:
            return None, None
        record = leads is None and not full
        for v in leads if leads is not None else self.leads_fn():
            p, ref = self._px.get((v, coin)), self._lead_at_spot.get((v, coin))
            if p is None or ref is None or now - p[1] > FRESH_S:
                continue
            cap = CAP_BP * 1e-4
            if v in TOUCH_VENUES:
                move = self._touch_shift(v, coin)
                if move is None:
                    continue
                inc = max(-cap, min(cap, move)) * (1.0 if full else TOUCH_WEIGHT)
            else:
                inc = max(-cap, min(cap, p[0] - ref))
            if record:
                self.lead_used[coin] = v
            return spot_mid * math.exp(inc), inc * 1e4
        if record:
            self.lead_used[coin] = None
        return spot_mid, None

    def touch_ok(self, venue: str, bid: float | None, ask: float | None) -> bool:
        """False for a TOUCH_VENUES touch that must not be used (CME closed, or
        spread over TOUCH_MAX_SPREAD_BP)."""
        if venue not in TOUCH_VENUES:
            return True
        now = time.time()
        if now - self._closed[0] >= 1.0:
            self._closed = (now, cme_closed(now))
        if self._closed[1]:
            return False
        if bid is None or ask is None:
            return True
        return bid > 0 and ask >= bid and (ask - bid) / (0.5 * (ask + bid)) * 1e4 <= TOUCH_MAX_SPREAD_BP

    def on_perp(self, venue: str, coin: str, price: float, t: float,
                bid: float | None = None, ask: float | None = None):
        if price <= 0 or not self.touch_ok(venue, bid, ask):
            return
        k = (venue, coin)
        lp = math.log(price)
        self._px[k] = (lp, t)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            self._touch[k] = (math.log(bid), math.log(ask))
            h = self._touch_hist.setdefault(k, collections.deque())
            h.append((t, self._touch[k][0], self._touch[k][1]))
            while len(h) > 1 and h[1][0] <= t - TOUCH_HIST_S:
                h.popleft()
        s = self._spot.get(coin)
        if s is None:
            return
        q = self._basis.setdefault(k, collections.deque())
        q.append((t, lp - s))
        self._bsum[k] = self._bsum.get(k, 0.0) + (lp - s)
        while q and q[0][0] < t - BASIS_S:
            self._bsum[k] -= q.popleft()[1]

    def venue_gap(self, venue: str, coin: str, now: float, spot_mid: float | None) -> tuple[float | None, float | None]:
        """(venue's basis-adjusted gap to spot in bp, unclipped; its book age
        in s) or (None, age) before it has MIN_BASIS_N basis samples."""
        p, q = self._px.get((venue, coin)), self._basis.get((venue, coin))
        if p is None or spot_mid is None or spot_mid <= 0:
            return None, None
        age = now - p[1]
        if not q or len(q) < MIN_BASIS_N:
            return None, age
        return (p[0] - self._bsum[(venue, coin)] / len(q) - math.log(spot_mid)) * 1e4, age

    def nowcast(self, coin: str, now: float, spot_mid: float | None) -> tuple[float | None, float | None, int]:
        if spot_mid is None or spot_mid <= 0:
            return None, None, 0
        vals = {}
        for v in PERPS:
            k = (v, coin)
            p, q = self._px.get(k), self._basis.get(k)
            if p is None or not q or now - p[1] > FRESH_S or len(q) < MIN_BASIS_N:
                continue
            vals[v] = p[0] - self._bsum[k] / len(q)
        lead = vals.get(LEAD)
        others = [x for v, x in vals.items() if v != LEAD]
        if lead is not None and others and all(abs(lead - x) > CHECK_BP * 1e-4 for x in others):
            # With one checker a bad Binance tick and a bad OKX tick look the
            # same, so neither is trusted: spot unchanged until they agree.
            return spot_mid, None, len(vals)
        if lead is not None:
            est = lead
        elif len(others) >= MIN_VENUES:
            est = float(np.median(others))      # Binance stale: OKX stands in
        else:
            return spot_mid, None, len(vals)
        gap = est - math.log(spot_mid)
        cap = CAP_BP * 1e-4
        gap = max(-cap, min(cap, gap))
        return spot_mid * math.exp(gap), gap * 1e4, len(vals)


__all__ = ["PerpNowcast", "PERPS", "LEAD", "SHADOW", "TOUCH_VENUES", "TOUCH_WEIGHT", "CHECK_BP", "CAP_BP", "BASIS_S", "FRESH_S", "MIN_VENUES"]
