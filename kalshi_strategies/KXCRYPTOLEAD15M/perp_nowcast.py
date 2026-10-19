"""
Perp nowcast: the spot-median mid moved to where the perps (Binance, Bybit,
OKX USDT perps) already are, for pricing a few hundred ms sooner.

    gap      = median over fresh venues of (log perp_v - basis_v) - log spot_mid
    nowcast  = spot_mid * exp(clip(gap, +-CAP_BP))

basis_v is venue v's trailing BASIS_S mean of log(perp_v / spot_mid): USDT
quote and funding, so only the perps' lead carries over, never their level.
As spot catches up the gap closes on its own.

ETH 10-18/19 (old us-east-1 box): perp prints led our spot median by ~250ms,
and a nowcast like this moved with the Kalshi mid at 0s where our input lagged
it by ~200ms. Shadow only: logged as x_perp_* on decision rows, never priced on.
"""
from __future__ import annotations

import collections
import math

import numpy as np

PERPS = ("binance_perp", "bybit_perp", "okx_perp")
# Basis averaging window. Long next to the ~250ms lead, so a lead doesn't leak
# into the basis and cancel itself; short enough to follow funding drift.
BASIS_S = 30.0
# Largest correction (bp). ETH nowcast vs spot input: p90 1.9bp, p99 6.2bp
# (10-18/19); past ~2bp it was mostly perps decoupling from spot.
CAP_BP = 2.0
# A venue's last trade older than this is left out; under MIN_VENUES fresh
# the nowcast is the spot mid unchanged.
FRESH_S = 2.0
MIN_VENUES = 2
# Basis samples a venue needs before it counts.
MIN_BASIS_N = 20


class PerpNowcast:
    """Per coin. `on_perp` / `on_spot` on one monotonic clock;
    `nowcast(coin, now, spot_mid)` -> (price, gap_bp clipped, n fresh venues)."""

    def __init__(self):
        self._px: dict[tuple[str, str], tuple[float, float]] = {}      # (venue, coin) -> (log px, t)
        self._basis: dict[tuple[str, str], collections.deque] = {}     # (venue, coin) -> (t, log perp - log spot)
        self._bsum: dict[tuple[str, str], float] = {}
        self._spot: dict[str, float] = {}                              # coin -> latest log spot

    def on_spot(self, coin: str, mid: float | None):
        if mid is not None and mid > 0:
            self._spot[coin] = math.log(mid)

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
        vals = []
        for v in PERPS:
            k = (v, coin)
            p, q = self._px.get(k), self._basis.get(k)
            if p is None or not q or now - p[1] > FRESH_S or len(q) < MIN_BASIS_N:
                continue
            vals.append(p[0] - self._bsum[k] / len(q))
        if len(vals) < MIN_VENUES:
            return spot_mid, None, len(vals)
        gap = float(np.median(vals)) - math.log(spot_mid)
        cap = CAP_BP * 1e-4
        gap = max(-cap, min(cap, gap))
        return spot_mid * math.exp(gap), gap * 1e4, len(vals)


__all__ = ["PerpNowcast", "PERPS", "CAP_BP", "BASIS_S", "FRESH_S", "MIN_VENUES"]
