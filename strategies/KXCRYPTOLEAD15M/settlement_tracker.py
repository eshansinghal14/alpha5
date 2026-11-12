"""
SettlementTracker: Kalshi's settlement for one event, from the CF index.

  start[coin]  -- mean of the index's whole-second values over [S-60, S)
  end[coin]    -- the same over [T-60, T)
  return[coin] -- (end - start) / start * 100, rounded to 3 dp in percent;
                  the highest wins, ties pay floor(100 / n_tied) cents each.

[S-60, S) is the window that reproduces Kalshi's expiration_value
(2026-10-12..18: BTC 0.0004bp max). Kalshi's websocket
`last_60s_windowed_average_15min` uses (S-60, S] instead, 0.003-0.03bp off;
don't substitute it.

The pricer works in log returns; only this module uses Kalshi's arithmetic
returns.
"""
from __future__ import annotations

import dataclasses
import math

from .series import COINS, START_END_AVG_S, RETURN_ROUNDING_PCT


@dataclasses.dataclass
class EventWindow:
    """Timings for one event. S and T in unix seconds."""
    event_ticker: str
    S: float
    T: float

    @property
    def start_avg_begin(self) -> float:
        return self.S - START_END_AVG_S

    @property
    def end_avg_begin(self) -> float:
        return self.T - START_END_AVG_S

    def phase(self, now_s: float) -> str:
        if now_s < self.start_avg_begin:
            return "pre_start_avg"
        if now_s < self.S:
            return "forming_start"
        if now_s < self.end_avg_begin:
            return "in_window"
        if now_s < self.T:
            return "last_minute"
        return "settled"


@dataclasses.dataclass
class SettlementSnapshot:
    phase: str
    tau: float
    start: dict[str, float | None]          # None until all 60 start seconds are in
    start_n: dict[str, int]
    end_log_sum: dict[str, float]           # sum of ln(CF_s / start) over end seconds received
    end_n: dict[str, int]                   # those seconds' count


@dataclasses.dataclass
class SettlementOutcome:
    event_ticker: str
    returns_pct: dict[str, float]
    winners: list[str]
    payout_cents_each: int
    start: dict[str, float]
    end: dict[str, float]


class SettlementTracker:
    """One per event. `cf` is the session's CfIndex (anything with
    value(coin, ts_s) and window(coin, lo, hi))."""

    def __init__(self, window: EventWindow, cf):
        self.window = window
        self.cf = cf
        self._start: dict[str, float] = {}
        self._cache: tuple | None = None

    def start(self, coin: str) -> float | None:
        """Locked once all 60 seconds are in; never recomputed after."""
        s = self._start.get(coin)
        if s is None:
            w = self.window
            vals = self.cf.window(coin, w.start_avg_begin, w.S)
            if len(vals) == START_END_AVG_S:
                s = self._start[coin] = sum(vals) / START_END_AVG_S
        return s

    def snapshot(self, now_s: float) -> SettlementSnapshot:
        """End seconds counted are those before now_s that have arrived."""
        w = self.window
        key = (math.floor(now_s), float(now_s).is_integer(), getattr(self.cf, "version", None))
        if self._cache is not None and self._cache[0] == key and key[2] is not None:
            return dataclasses.replace(self._cache[1], tau=max(0.0, w.T - now_s))
        start, start_n, end_sum, end_n = {}, {}, {}, {}
        hi = min(math.ceil(now_s), w.T)
        for c in COINS:
            s = self.start(c)
            start[c] = s
            start_n[c] = START_END_AVG_S if s is not None else len(self.cf.window(c, w.start_avg_begin, w.S))
            vals = self.cf.window(c, w.end_avg_begin, hi) if now_s > w.end_avg_begin else []
            end_n[c] = len(vals)
            end_sum[c] = sum(math.log(v / s) for v in vals) if s is not None else 0.0
        snap = SettlementSnapshot(phase=w.phase(now_s), tau=max(0.0, w.T - now_s), start=start, start_n=start_n,
                                  end_log_sum=end_sum, end_n=end_n)
        self._cache = (key, snap)
        return snap

    def finalize(self) -> SettlementOutcome | None:
        """Kalshi's result, or None while any coin lacks a full start or end."""
        w = self.window
        start, end = {}, {}
        for c in COINS:
            s = self.start(c)
            vals = self.cf.window(c, w.end_avg_begin, w.T)
            if s is None or len(vals) < START_END_AVG_S:
                return None
            start[c], end[c] = s, sum(vals) / START_END_AVG_S
        rounded = {c: round((end[c] - start[c]) / start[c] * 100.0 / RETURN_ROUNDING_PCT) * RETURN_ROUNDING_PCT
                   for c in COINS}
        best = max(rounded.values())
        winners = [c for c, r in rounded.items() if math.isclose(r, best, abs_tol=1e-9)]
        return SettlementOutcome(event_ticker=w.event_ticker, returns_pct=rounded, winners=winners,
                                 payout_cents_each=100 // len(winners), start=start, end=end)


__all__ = ["EventWindow", "SettlementSnapshot", "SettlementTracker", "SettlementOutcome"]
