"""
SettlementTracker: Kalshi contract-mechanics replica.

Section 3 of the spec. Given per-second composite bars for all 5 coins,
reconstruct the exact number Kalshi settles against:

  * `start_twap[coin]`  -- 60-sample mean of composite price over
                           [S-60, S). Final at S.
  * `end_twap[coin]`    -- 60-sample mean over [T-60, T). Final at T.
  * return[coin]        -- (end - start) / start * 100, rounded to 3
                           decimal places in percent.
  * replicated_winner() -- argmax(return); ties pay `floor(100 / n_tied)`
                           cents each (handled in `settlement_outcome`).

Caveat: the pricer in pricer.py uses LOG returns because they are more
convenient numerically (equivalent for ranking). Kalshi uses arithmetic
returns -- so this module keeps arithmetic returns for the settlement
replica. The two modules handle ONLY their own quantity; nothing silently
converts.

Health flag: `start_quality` ("ok" | "partial" | "missing") reports
whether the start TWAP has the full 60 samples. If `partial`, the
projected returns are still produced -- the pricer will just see a larger
variance for coins whose start was partial -- but the LIP strategy
should widen or pull on those legs. See the `start_quality` field in the
viz panel.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np

from .composite import CompositeBar
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
    """What the tracker currently knows. Produced on demand -- the
    tracker itself stores raw bars."""
    event_ticker: str
    phase: str
    tau: float                          # T - now, seconds
    start_twap: dict[str, float | None]
    start_quality: dict[str, str]       # "ok" | "partial" | "missing"
    end_twap_partial: dict[str, float | None]
    end_sample_count: dict[str, int]
    projected_return_pct: dict[str, float | None]
    projected_return_log: dict[str, float | None]  # ln(end / start) -- used by pricer

    def replicated_winner(self) -> list[str] | None:
        """List of coins currently projected to tie for first (always
        length 1 away from a tie). Returns None if any coin's start
        TWAP is missing."""
        if any(v is None for v in self.start_twap.values()):
            return None
        if any(v is None for v in self.projected_return_pct.values()):
            return None
        rounded = {c: round(self.projected_return_pct[c] / 100.0 / RETURN_ROUNDING_PCT) * RETURN_ROUNDING_PCT * 100.0
                   for c in self.projected_return_pct}
        best = max(rounded.values())
        return [c for c, r in rounded.items() if math.isclose(r, best, abs_tol=1e-9)]


class SettlementTracker:
    """One instance per event. Fed composite bars as they are sampled."""

    def __init__(self, window: EventWindow):
        # TWAPs are taken over CompositeBar.mid (venue BBO mid), the same
        # series the strategy uses for x -- start TWAP and live x must share
        # one source or the gap between sources shows up as a fake lead.
        self.window = window
        self._bars: dict[str, list[CompositeBar]] = {c: [] for c in COINS}
        self._n_ingested = 0
        self._cache: tuple | None = None
        self._proj_cache: tuple | None = None

    def ingest(self, bar: CompositeBar):
        if bar.coin not in self._bars:
            return
        # Only keep bars inside the two averaging windows + a small
        # pre-window buffer to tolerate late arrivals. Everything outside
        # does not affect settlement replication.
        w = self.window
        if bar.ts_s < w.start_avg_begin - 5 or bar.ts_s >= w.T + 1:
            return
        self._bars[bar.coin].append(bar)
        self._n_ingested += 1

    def backfill(self, bars_by_coin: dict[str, list[CompositeBar]]):
        """Seed from previously-persisted bars (section 3: "If we start up
        late, backfill from stored bars"). Duplicates are deduped by ts_s."""
        for coin, bars in bars_by_coin.items():
            if coin not in self._bars:
                continue
            seen = {b.ts_s for b in self._bars[coin]}
            for b in bars:
                if b.ts_s in seen:
                    continue
                self.ingest(b)

    def snapshot(self, now_s: float) -> SettlementSnapshot:
        """Only `tau` depends on sub-second time (the windows are whole
        seconds), so the scan over every bar is cached per (bar count,
        second); it was ~11% of the event loop when run on every price."""
        # Every comparison is against whole-second S, T and bar stamps, so
        # results are constant on each open interval (k, k+1); an exact
        # integer now_s sits on a boundary and gets its own key.
        key = (self._n_ingested, math.floor(now_s), float(now_s).is_integer())
        if self._cache is not None and self._cache[0] == key:
            return dataclasses.replace(self._cache[1], tau=max(0.0, self.window.T - now_s))
        snap = self._snapshot(now_s)
        self._cache = (key, snap)
        return snap

    def _snapshot(self, now_s: float) -> SettlementSnapshot:
        w = self.window
        start_twap: dict[str, float | None] = {}
        start_quality: dict[str, str] = {}
        end_partial: dict[str, float | None] = {}
        end_count: dict[str, int] = {}
        proj_pct: dict[str, float | None] = {}
        proj_log: dict[str, float | None] = {}

        for coin in COINS:
            bars = self._bars[coin]
            # Start window: ts in [S-60, S).
            start_prices = [b.mid for b in bars
                            if w.start_avg_begin <= b.ts_s < w.S]
            # The window is final once now >= S. Before that, start_twap
            # is a running average -- but it is NOT what Kalshi settles
            # on, so we deliberately only expose it as "formed start" and
            # mark it `partial`.
            if now_s < w.S:
                start_quality[coin] = "forming"
                start_twap[coin] = float(np.mean(start_prices)) if start_prices else None
            else:
                if not start_prices:
                    start_quality[coin] = "missing"
                    start_twap[coin] = None
                elif len(start_prices) < START_END_AVG_S:
                    start_quality[coin] = "partial"
                    start_twap[coin] = float(np.mean(start_prices))
                else:
                    start_quality[coin] = "ok"
                    start_twap[coin] = float(np.mean(start_prices))

            # End window: ts in [T-60, now) if we're inside the last minute.
            end_prices = [b.mid for b in bars
                          if w.end_avg_begin <= b.ts_s < min(now_s, w.T)]
            end_count[coin] = len(end_prices)
            end_partial[coin] = float(np.mean(end_prices)) if end_prices else None

            if start_twap[coin] is None or end_partial[coin] is None:
                proj_pct[coin] = None
                proj_log[coin] = None
            else:
                s, e = start_twap[coin], end_partial[coin]
                if s > 0:
                    proj_pct[coin] = (e - s) / s * 100.0
                    proj_log[coin] = math.log(e / s)
                else:
                    proj_pct[coin] = None
                    proj_log[coin] = None

        return SettlementSnapshot(
            event_ticker=w.event_ticker,
            phase=w.phase(now_s),
            tau=max(0.0, w.T - now_s),
            start_twap=start_twap,
            start_quality=start_quality,
            end_twap_partial=end_partial,
            end_sample_count=end_count,
            projected_return_pct=proj_pct,
            projected_return_log=proj_log,
        )

    def projected_returns(self, now_s: float, source: str) -> dict[str, float | None]:
        """Arithmetic return per coin if each coin's last bar before `now_s`
        held for the rest of the end average. `source` is "mid" (the pricing
        series) or "cf" (the CF replica, `CompositeBar.price`). None for a coin
        with no start average or no bar yet."""
        key = (self._n_ingested, math.floor(now_s), float(now_s).is_integer(), source)
        if self._proj_cache is not None and self._proj_cache[0] == key:
            return self._proj_cache[1]
        w = self.window
        get = (lambda b: b.mid) if source == "mid" else (lambda b: b.price)
        out: dict[str, float | None] = {}
        for coin in COINS:
            start = [get(b) for b in self._bars[coin] if w.start_avg_begin <= b.ts_s < w.S]
            seen = [b for b in self._bars[coin] if b.ts_s < now_s]
            if not start or not seen:
                out[coin] = None
                continue
            end = [get(b) for b in seen if b.ts_s >= w.end_avg_begin]
            last = get(max(seen, key=lambda b: b.ts_s))
            n = min(len(end), START_END_AVG_S)
            end_avg = (sum(end[:n]) + (START_END_AVG_S - n) * last) / START_END_AVG_S
            s = float(np.mean(start))
            out[coin] = None if s <= 0 else end_avg / s - 1.0
        self._proj_cache = (key, out)
        return out

    def finalize(self, now_s: float) -> "SettlementOutcome | None":
        """Called at `now_s >= T`. Returns the authoritative replica of
        Kalshi's settlement for this event, or None if either average is
        completely missing (we didn't catch any samples at all)."""
        snap = self.snapshot(max(now_s, self.window.T))
        if any(v is None for v in snap.start_twap.values()):
            return None
        if any(v is None for v in snap.end_twap_partial.values()):
            return None
        # Apply the contract's rounding: 3 decimals in percent, which is
        # 1e-5 in return units.
        rounded_pct = {c: round(snap.projected_return_pct[c] / RETURN_ROUNDING_PCT) * RETURN_ROUNDING_PCT
                       for c in COINS}
        best = max(rounded_pct.values())
        winners = [c for c, r in rounded_pct.items() if math.isclose(r, best, abs_tol=1e-9)]
        payout_cents_each = 100 // len(winners) if winners else 0
        return SettlementOutcome(
            event_ticker=self.window.event_ticker,
            returns_pct=rounded_pct,
            winners=winners,
            payout_cents_each=payout_cents_each,
            start_twap=snap.start_twap,
            end_twap=snap.end_twap_partial,
            start_quality=snap.start_quality,
            end_sample_count=snap.end_sample_count,
        )


@dataclasses.dataclass
class SettlementOutcome:
    event_ticker: str
    returns_pct: dict[str, float]
    winners: list[str]
    payout_cents_each: int
    start_twap: dict[str, float]
    end_twap: dict[str, float]
    start_quality: dict[str, str]
    end_sample_count: dict[str, int]


__all__ = ["EventWindow", "SettlementSnapshot", "SettlementTracker", "SettlementOutcome"]
