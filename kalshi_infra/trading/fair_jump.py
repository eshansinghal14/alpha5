"""
Fair-jump hold: when our own fair jumps, don't chase it, and don't leave the
side it exposes resting.

A jump is |fair(t) - fair(t - W)| >= J on a market priced 5-95c. For
`hold_s` after the last such move (so it extends while the fair keeps
moving), the market is held:
  - the exposed side is blocked: an up jump makes our offer (NO bid) cheap
    against the new fair; a down jump does the same to the YES bid;
  - the chase side keeps quoting off the pre-jump fair (anchor = fair W
    before the move that started the hold), so a jump never raises it.
A jump the other way inside a hold starts a new one from that move.

Why: KXINXU 10-16/19/20, 55-59% of 2c-in-0.5s jumps were mostly gone 2-5s
later, and fills in the 5s after one marked out -10.3c/lot at 60s on the
chase side, -19.5c when the jump reverted, -3.6c on the exposed side (all
fills -0.9c). Losing fills landed a median 1.5-2.5s after the jump, so a 1s
hold missed most of them; 3s caught $8.74 of losses holding 2.5% of the time.
"""
from __future__ import annotations

import collections

from kalshi_infra.trading.fair_lag import PRICED_HI, PRICED_LO


class FairJumpGuard:
    def __init__(self):
        self._hist: dict[str, collections.deque] = {}
        # ticker -> (hold until, direction +1/-1, anchor fair)
        self._hold: dict[str, tuple[float, int, float]] = {}

    def observe(self, ticker: str, now_s: float, fair: float | None, p) -> None:
        """Record one market's fair. `p` is the risk section (fair_jump_*);
        fair_jump_max None turns it off."""
        if p.fair_jump_max is None or fair is None:
            return
        w = p.fair_jump_window_s
        h = self._hist.setdefault(ticker, collections.deque())
        h.append((now_s, fair))
        while len(h) >= 2 and h[1][0] <= now_s - w:
            h.popleft()
        t0, f0 = h[0]
        if now_s - t0 < w - 1e-9:
            return
        d = fair - f0
        if abs(d) < p.fair_jump_max or not (PRICED_LO <= fair <= PRICED_HI and PRICED_LO <= f0 <= PRICED_HI):
            return
        direction = 1 if d > 0 else -1
        cur = self._hold.get(ticker)
        if cur is not None and now_s <= cur[0] and cur[1] == direction:
            self._hold[ticker] = (now_s + p.fair_jump_hold_s, direction, cur[2])
        else:
            self._hold[ticker] = (now_s + p.fair_jump_hold_s, direction, f0)

    def state(self, ticker: str, now_s: float) -> tuple[int, float] | None:
        """(direction, anchor fair) while held, else None."""
        cur = self._hold.get(ticker)
        if cur is None or now_s > cur[0]:
            return None
        return cur[1], cur[2]
