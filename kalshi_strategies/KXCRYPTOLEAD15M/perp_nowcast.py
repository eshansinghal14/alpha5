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
import math

import numpy as np

# Bybit dropped 10-20: slowest perp to us (p50 113-145ms vs OKX 80-120), and
# OKX alone covers the veto and the stale-Binance fallback.
PERPS = ("binance_perp", "okx_perp")
LEAD = "binance_perp"
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
        # Latest-mover state per coin: Binance's log price when spot last
        # moved, so `increment` is only what Binance did since then.
        self._lead_at_spot: dict[str, float] = {}

    def on_spot(self, coin: str, mid: float | None):
        if mid is not None and mid > 0:
            self._spot[coin] = math.log(mid)
            p = self._px.get((LEAD, coin))
            if p is not None:
                self._lead_at_spot[coin] = p[0]

    def increment(self, coin: str, now: float, spot_mid: float | None) -> tuple[float | None, float | None]:
        """Latest-mover nowcast (shadow): spot plus Binance's move since spot
        last moved, capped at CAP_BP. Unlike nowcast(), a spot move is never
        pulled back toward the perp level while Binance hasn't updated: the
        increment resets to 0 on every spot move. Returns (price, inc_bp);
        inc None when Binance is stale or hasn't printed since startup."""
        if spot_mid is None or spot_mid <= 0:
            return None, None
        p, ref = self._px.get((LEAD, coin)), self._lead_at_spot.get(coin)
        if p is None or ref is None or now - p[1] > FRESH_S:
            return spot_mid, None
        cap = CAP_BP * 1e-4
        inc = max(-cap, min(cap, p[0] - ref))
        return spot_mid * math.exp(inc), inc * 1e4

    def on_perp(self, venue: str, coin: str, price: float, t: float):
        if price <= 0:
            return
        k = (venue, coin)
        lp = math.log(price)
        self._px[k] = (lp, t)
        s = self._spot.get(coin)
        if s is None:
            return
        q = self._basis.setdefault(k, collections.deque())
        q.append((t, lp - s))
        self._bsum[k] = self._bsum.get(k, 0.0) + (lp - s)
        while q and q[0][0] < t - BASIS_S:
            self._bsum[k] -= q.popleft()[1]

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


__all__ = ["PerpNowcast", "PERPS", "LEAD", "CHECK_BP", "CAP_BP", "BASIS_S", "FRESH_S", "MIN_VENUES"]
