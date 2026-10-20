"""
Fair-lag guard: block the side a stale fair would buy into when Kalshi has
moved and our fair hasn't.

Per market, lag = (mid now - mid W ago) - (fair now - fair W ago). Kalshi
falling against an unmoved fair (lag < -L) means our fair is stale high, so
YES bids are blocked; lag > L blocks NO bids. A market blocks its own side,
and when `markets` or more priced markets lag the same way at once the side
is blocked on every market (one underlying moved; the fair froze for all of
them). Blocks last `hold_s` after the last trigger.

Why: KXINXU 2026-10-20 11:12:18-22 ET, SPX sold off, Kalshi bids fell 15c in
4s while the hub's fairs froze (most option quotes were held as blown-out
spreads), signal age stayed fresh, and nine 5-lot YES bids filled at
-18 to -29c/lot at 60s. fair_suspect (15c outside the touch) fired after
the last of them.
"""
from __future__ import annotations

import collections

# Only markets whose fair is inside this range vote or are checked: deep
# markets barely move with the underlying, so their lag is noise.
PRICED_LO, PRICED_HI = 0.05, 0.95


class FairLagGuard:
    def __init__(self):
        self._hist: dict[str, collections.deque] = {}
        self._lag: dict[str, tuple[float, float]] = {}       # ticker -> (time, lag)
        self._until: dict[tuple[str | None, str], float] = {}  # (ticker | None=all, side) -> time

    def observe(self, ticker: str, now_s: float, mid: float | None, fair: float | None, p) -> None:
        """Record one market's mid and fair and re-check its lag. `p` is the
        risk section (fair_lag_* fields); fair_lag_max None turns it off."""
        if p.fair_lag_max is None or mid is None or fair is None:
            return
        w = p.fair_lag_window_s
        h = self._hist.setdefault(ticker, collections.deque())
        h.append((now_s, mid, fair))
        # Keep exactly one sample at or before now - w as the reference.
        while len(h) >= 2 and h[1][0] <= now_s - w:
            h.popleft()
        t0, mid0, fair0 = h[0]
        if now_s - t0 < w - 1e-9 or not (PRICED_LO <= fair <= PRICED_HI):
            self._lag.pop(ticker, None)
            return
        lag = (mid - mid0) - (fair - fair0)
        self._lag[ticker] = (now_s, lag)
        lim, hold = p.fair_lag_max, p.fair_lag_hold_s
        if lag < -lim:
            self._until[(ticker, "yes")] = now_s + hold
        elif lag > lim:
            self._until[(ticker, "no")] = now_s + hold
        k = p.fair_lag_markets
        if k is None:
            return
        fresh = [g for t, g in self._lag.values() if now_s - t <= w]
        if sum(g < -lim for g in fresh) >= k:
            self._until[(None, "yes")] = now_s + hold
        if sum(g > lim for g in fresh) >= k:
            self._until[(None, "no")] = now_s + hold

    def blocked(self, ticker: str, side: str, now_s: float) -> bool:
        return (now_s <= self._until.get((ticker, side), -1e18)
                or now_s <= self._until.get((None, side), -1e18))
