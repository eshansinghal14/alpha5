"""
Fill-intensity model for the LIP credit (spec section 6.2).

The LIP credit a resting quote earns is `reward_rate_per_s * E[time resting
before fill]`, NOT `reward_rate_per_s * remaining_period` (the old upper
bound, which assumed the quote never fills). This module estimates the
fill intensity lambda (fills/second against our resting order) so the
expected resting time can be computed in closed form.

Model
-----
  lambda_you(tau, p, side, ticks_from_best, queue_ahead)
      = lambda_side(tau, p) * g(ticks_from_best, queue_ahead)

`lambda_side(tau, p)` -- the MARKET fill rate for resting orders on one
side, as a function of time-to-close `tau` (seconds) and price bucket `p`.
Estimated from Kalshi trade history: each trade's `taker_book_side` tells
you which resting side was filled (a taker lifting the ASK fills a resting
YES ask == NO bid; a taker hitting the BID fills a resting YES bid). Bucket
filled `count` by (book_side, tau_bucket, price_bucket); divide by the
bucket's total seconds-observed and by the number of simultaneous markets
to get a per-second-per-market rate. This is the rate at which the FRONT
of the queue gets filled.

`g(ticks_from_best, queue_ahead)` -- the queue-position adjustment, in
[0, 1]. An order resting at the touch with no size ahead sees the full
`lambda_side`; an order 2 ticks back, or behind a lot of size, sees less.
v1 uses a simple separable form:
    g = depth_decay^ticks_from_best * (Q0 / (Q0 + queue_ahead))
calibrated against the virtual-order fill study (fit_fill_intensity.py).
Both factors are 1.0 at (0 ticks, 0 queue), so g <= 1 always.

Expected resting time
---------------------
With a constant hazard lambda over the remaining horizon tau:
    E[rest | horizon tau] = integral_0^tau exp(-lambda t) dt
                          = (1 - exp(-lambda tau)) / lambda          (lambda > 0)
                          = tau                                      (lambda -> 0)
So the LIP credit per contract is reward_rate_per_s * E[rest], which:
  * -> reward_rate_per_s * tau       as lambda -> 0 (the old upper bound), and
  * shrinks toward reward_rate_per_s / lambda as lambda grows (a fast-filling
    quote collects LIP only until it fills).

Artifact
--------
`fill_intensity.json` (fitted by research/fit_fill_intensity.py from
data/kalshi_ws/{SERIES}/trades + raw_ob_ticks). Absent -> a fixed-rate
fallback whose single lambda is operator-plausible, so the pricer still
gets a finite, non-degenerate credit before any fit exists.
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import math
import os
from pathlib import Path

ARTIFACT_PATH = Path(__file__).with_name("fill_intensity.json")

# Fallback hazard when no artifact is fitted yet: fills/second against a
# touch resting order, per market. 0.02/s ~ a fill every ~50s, which over a
# 900s period gives E[rest] ~ (1-e^-18)/0.02 ~ 50s rather than the full 900.
# Deliberately NON-zero so the credit stops being the full-period upper
# bound even uncalibrated; recalibrate from real trades asap.
FALLBACK_LAMBDA_PER_S = 0.02

# Tick-decay and queue half-size for the fallback g(). depth_decay 0.55 ~ a
# resting order one tick back fills a bit over half as often as the touch;
# QUEUE_HALF 50 contracts ~ once ~50 lots sit ahead of you, you see half the
# rate. Both are operator-plausible stand-ins until fit_fill_intensity.py
# measures them from the virtual-order study.
FALLBACK_DEPTH_DECAY = 0.55
FALLBACK_QUEUE_HALF = 50.0


@dataclasses.dataclass
class FillIntensityModel:
    """Runtime fill-intensity lookup. Either loads a fitted artifact or
    runs in fixed-rate fallback mode.

    `tau_edges` / `price_edges` are the (ascending) bucket right-edges;
    `lam_bid` / `lam_ask` are 2-D grids [tau_bucket][price_bucket] of
    per-second-per-market fill rate for a resting order at the FRONT of
    that side's queue. `depth_decay` and `queue_half` parametrise g()."""
    tau_edges: list[float]
    price_edges: list[float]
    lam_bid: list[list[float]]
    lam_ask: list[list[float]]
    depth_decay: float = FALLBACK_DEPTH_DECAY
    queue_half: float = FALLBACK_QUEUE_HALF
    fitted: bool = False
    fallback_lambda: float = FALLBACK_LAMBDA_PER_S

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def fallback(cls) -> "FillIntensityModel":
        """A one-bucket, fixed-rate model. Every (tau, p) returns
        `FALLBACK_LAMBDA_PER_S`."""
        return cls(
            tau_edges=[], price_edges=[],
            lam_bid=[[FALLBACK_LAMBDA_PER_S]], lam_ask=[[FALLBACK_LAMBDA_PER_S]],
            fitted=False,
        )

    @classmethod
    def load(cls, path: os.PathLike | str = ARTIFACT_PATH) -> "FillIntensityModel":
        """Load the fitted artifact, or the fallback if it's absent/bad."""
        p = Path(path)
        if not p.exists():
            return cls.fallback()
        try:
            with open(p) as f:
                d = json.load(f)
            return cls(
                tau_edges=list(d["tau_edges"]),
                price_edges=list(d["price_edges"]),
                lam_bid=[list(row) for row in d["lam_bid"]],
                lam_ask=[list(row) for row in d["lam_ask"]],
                depth_decay=float(d.get("depth_decay", FALLBACK_DEPTH_DECAY)),
                queue_half=float(d.get("queue_half", FALLBACK_QUEUE_HALF)),
                fitted=True,
                fallback_lambda=float(d.get("fallback_lambda", FALLBACK_LAMBDA_PER_S)),
            )
        except Exception:
            return cls.fallback()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def _bucket(self, edges: list[float], x: float) -> int:
        """Index of the bucket `x` falls in. `edges` are ascending right
        edges; the last bucket catches everything above the last edge."""
        if not edges:
            return 0
        return min(bisect.bisect_right(edges, x), len(edges))

    def lambda_side(self, side: str, tau: float, price: float) -> float:
        """Market front-of-queue fill rate (per second, per market) for a
        resting order on `side` ("yes"=bid or "no"=... see note), at
        time-to-close `tau` and the order's own price `price` (that side's
        own terms). In fallback mode this is the fixed rate."""
        if not self.fitted:
            return self.fallback_lambda
        grid = self.lam_bid if side == "yes" else self.lam_ask
        ti = min(self._bucket(self.tau_edges, tau), len(grid) - 1)
        row = grid[ti]
        pi = min(self._bucket(self.price_edges, price), len(row) - 1)
        return row[pi]

    def g(self, ticks_from_best: float, queue_ahead: float) -> float:
        """Queue-position multiplier in [0, 1]. 1.0 at the touch with an
        empty queue; decays with ticks back and with size resting ahead."""
        ticks = max(0.0, ticks_from_best)
        q = max(0.0, queue_ahead)
        depth = self.depth_decay ** ticks
        queue = self.queue_half / (self.queue_half + q)
        return depth * queue

    def lambda_you(self, side: str, tau: float, price: float,
                   ticks_from_best: float = 0.0, queue_ahead: float = 0.0
                   ) -> float:
        """Our order's fill intensity: lambda_side * g. `side` is "yes"
        (a resting YES bid) or "no" (a resting NO bid == YES ask)."""
        return self.lambda_side(side, tau, price) * self.g(ticks_from_best, queue_ahead)

    def expected_rest_time_s(self, lam: float, tau: float) -> float:
        """E[time resting before fill] over a remaining horizon `tau`, for
        a constant hazard `lam`. Closed form of integral_0^tau e^{-lam t} dt;
        -> tau as lam -> 0, -> 1/lam as lam*tau -> infinity. Bounded by tau."""
        tau = max(0.0, tau)
        if lam <= 0.0:
            return tau
        return (1.0 - math.exp(-lam * tau)) / lam


__all__ = ["FillIntensityModel", "ARTIFACT_PATH",
           "FALLBACK_LAMBDA_PER_S", "FALLBACK_DEPTH_DECAY", "FALLBACK_QUEUE_HALF"]
