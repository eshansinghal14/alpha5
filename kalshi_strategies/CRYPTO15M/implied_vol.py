"""
Vol implied by the Kalshi mid, as a precision-weighted EMA.

Each second the mid gives one observation of the per-sqrt-second vol s that
would price the market at its mid, holding our spot level x fixed:

    m = F(mu / sd),  sd^2 = s^2 * te + b^2   =>   s = sqrt(((mu / F^-1(m))^2 - b^2) / te)

How much that observation is worth depends on how strongly the price depends
on s at that point. Its error, to first order, is

    ds = (dm - delta * dx) / vega,   vega = dP/ds,  delta = dP/dx

with dm the mid's own noise (half-spread, floored) and dx the error in our
spot level (basis noise, plus Kalshi lagging a recent spot move). So

    var(ln s) = (dm^2 + delta^2 dx^2) / (vega * s)^2

and the observation enters the EMA with weight 1 / var(ln s). At the money
vega -> 0 and the weight goes to zero by itself; just after a spot jump the
delta term swamps it. Observations whose relative SE is over max_obs_se are
dropped, as are ones where the first-order error is wrong: when
|volga| * se_s / |vega| is large, a symmetric mid error gives a lopsided
vol error, so the linear weight misstates it.

The EMA decays in time (tau_s), not per observation, so a quiet ATM stretch
keeps the last informative estimate rather than diluting it.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
from scipy import special

from kalshi_strategies.CRYPTO15M.pricer import _cdf, _ppf, variance_clock


def _pdf(z: float, nu: float | None) -> float:
    """Density of the unit-variance t(nu) (normal when nu is None)."""
    if nu is None:
        return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    k = math.sqrt(nu / (nu - 2.0))
    u = z * k
    lg = special.gammaln((nu + 1) / 2) - special.gammaln(nu / 2) - 0.5 * math.log(nu * math.pi)
    return k * math.exp(lg - (nu + 1) / 2 * math.log1p(u * u / nu))


def greeks(mu: float, g: float, s: float, te: float, b: float, nu: float | None) -> tuple[float, float, float]:
    """(P, dP/dx, dP/ds) at per-sqrt-second vol s (log units). g = d mu / d x."""
    sd = math.sqrt(s * s * te + b * b)
    z = mu / sd
    f = _pdf(z, nu)
    p = float(_cdf(z, nu))
    delta = f * g / sd
    vega = f * (-mu / (sd * sd)) * (s * te / sd)
    return p, delta, vega


@dataclasses.dataclass
class Observation:
    s: float            # implied per-sqrt-second vol, log units
    se_log: float       # SE of ln s
    vega: float
    delta: float
    reason: str | None  # why it was rejected, None if used


def observe(m: float, half_spread: float, mu: float, g: float, tau: float, b: float, nu: float | None, *,
            mid_noise_floor: float, spot_err: float, max_obs_se: float, max_curvature: float,
            s_bounds: tuple[float, float], ref_s: float | None = None) -> Observation:
    """One implied-vol observation from the Kalshi mid `m`. `mu`, `b`, `spot_err`
    in log units; `spot_err` already includes any recent-move (lag) term.

    `ref_s`: the vol the weight (vega, SE, curvature) is evaluated at -- the
    EMA's current value. Evaluated at the reading's own vol instead, a high
    reading got more weight than an equally noisy low one, biasing the mean
    up (KXETH15M 10-18 22:39-23:10: fast EMA 1.01bp vs ~0.93bp the mid implied
    at our spot level, theo above the ask 61% of seconds). None = the reading's
    own vol (no estimate yet)."""
    te, _, phase = variance_clock(tau)
    if phase != "in_window" or te <= 0:
        return Observation(float("nan"), float("inf"), 0.0, 0.0, "not_in_window")
    zm = _ppf(min(max(m, 1e-4), 1 - 1e-4), nu)
    if abs(zm) < 1e-9 or mu * zm <= 0:
        # Mid on the other side of 0.5 from our spot: a level disagreement, no vol in it.
        return Observation(float("nan"), float("inf"), 0.0, 0.0, "sign")
    sd2 = (mu / zm) ** 2 - b * b
    if sd2 <= 0:
        return Observation(float("nan"), float("inf"), 0.0, 0.0, "below_basis")
    s = math.sqrt(sd2 / te)
    if not (s_bounds[0] <= s <= s_bounds[1]):
        return Observation(s, float("inf"), 0.0, 0.0, "out_of_bounds")
    w = s if ref_s is None or not (ref_s > 0) else ref_s
    _, delta, vega = greeks(mu, g, w, te, b, nu)
    if vega == 0:
        return Observation(s, float("inf"), vega, delta, "no_vega")
    dm = max(half_spread, mid_noise_floor)
    se_s = math.sqrt(dm * dm + (delta * spot_err) ** 2) / abs(vega)
    se_log = se_s / w
    # Second order: volga from a central difference of vega.
    h = 0.05 * w
    volga = (greeks(mu, g, w + h, te, b, nu)[2] - greeks(mu, g, w - h, te, b, nu)[2]) / (2 * h)
    if abs(volga) * se_s / abs(vega) > max_curvature:
        return Observation(s, se_log, vega, delta, "curvature")
    if se_log > max_obs_se:
        return Observation(s, se_log, vega, delta, "se")
    return Observation(s, se_log, vega, delta, None)


class _Decayed:
    """Time-decayed sums of weight, weight * ln s, and count."""
    __slots__ = ("sw", "swx", "n", "t")

    def __init__(self):
        self.sw = self.swx = self.n = 0.0
        self.t: float | None = None

    def decay(self, now_s: float, tau_s: float):
        if self.t is not None and now_s > self.t:
            k = math.exp(-(now_s - self.t) / tau_s)
            self.sw *= k
            self.swx *= k
            self.n *= k
        self.t = now_s if self.t is None else max(self.t, now_s)

    def add(self, w: float, lx: float):
        self.sw += w
        self.swx += w * lx
        self.n += 1.0


class ImpliedVolEMA:
    """Precision-weighted, time-decayed mean of ln s, slow (the pricing vol) and
    fast (the check on it). Session-lifetime: carried across events, so each
    event starts from the last one's estimate."""

    def __init__(self):
        self.slow, self.fast = _Decayed(), _Decayed()
        self.last: Observation | None = None
        self.n_seen = 0
        self.n_recal = 0
        self.rejects: dict[str, int] = {}

    def add(self, now_s: float, obs: Observation, tau_s: float, fast_tau_s: float = 60.0):
        self.slow.decay(now_s, tau_s)
        self.fast.decay(now_s, fast_tau_s)
        self.last = obs
        self.n_seen += 1
        if obs.reason is not None:
            self.rejects[obs.reason] = self.rejects.get(obs.reason, 0) + 1
            return
        w, lx = 1.0 / (obs.se_log * obs.se_log), math.log(obs.s)
        self.slow.add(w, lx)
        self.fast.add(w, lx)

    def value(self, now_s: float, tau_s: float, min_obs: float) -> float | None:
        """Implied per-sqrt-second vol (log units), or None with too few
        accepted observations inside the slow EMA."""
        self.slow.decay(now_s, tau_s)
        if self.slow.n < min_obs or self.slow.sw <= 0:
            return None
        return math.exp(self.slow.swx / self.slow.sw)

    def fast_stats(self, now_s: float, fast_tau_s: float) -> tuple[float | None, float, float]:
        """(mean ln s, its SE, decayed count) of the fast EMA. The SE treats
        readings as independent; seconds share a spot-level error, so it is
        optimistic."""
        self.fast.decay(now_s, fast_tau_s)
        if self.fast.sw <= 0:
            return None, math.inf, self.fast.n
        return self.fast.swx / self.fast.sw, 1.0 / math.sqrt(self.fast.sw), self.fast.n

    def recalibrate(self, now_s: float, tau_s: float, fast_tau_s: float):
        """Restart the slow EMA from the fast one: its mean, weight and count."""
        self.slow.decay(now_s, tau_s)
        self.fast.decay(now_s, fast_tau_s)
        self.slow.sw, self.slow.swx, self.slow.n = self.fast.sw, self.fast.swx, self.fast.n
        self.n_recal += 1

    def n_eff(self) -> float:
        return self.slow.n

    def snapshot(self) -> dict:
        o = self.last
        return {"n_eff": round(self.slow.n, 1), "n_fast": round(self.fast.n, 1), "n_seen": self.n_seen,
                "n_recal": self.n_recal, "rejects": dict(self.rejects),
                "last_s_bp": None if o is None or not np.isfinite(o.s) else round(o.s * 1e4, 4),
                "last_se_log": None if o is None or not np.isfinite(o.se_log) else round(o.se_log, 3),
                "last_reason": None if o is None else o.reason}


__all__ = ["ImpliedVolEMA", "Observation", "observe", "greeks"]
