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
            s_bounds: tuple[float, float]) -> Observation:
    """One implied-vol observation from the Kalshi mid `m`. `mu`, `b`, `spot_err`
    in log units; `spot_err` already includes any recent-move (lag) term."""
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
    _, delta, vega = greeks(mu, g, s, te, b, nu)
    if vega == 0:
        return Observation(s, float("inf"), vega, delta, "no_vega")
    dm = max(half_spread, mid_noise_floor)
    se_s = math.sqrt(dm * dm + (delta * spot_err) ** 2) / abs(vega)
    se_log = se_s / s
    # Second order: volga from a central difference of vega.
    h = 0.05 * s
    volga = (greeks(mu, g, s + h, te, b, nu)[2] - greeks(mu, g, s - h, te, b, nu)[2]) / (2 * h)
    if abs(volga) * se_s / abs(vega) > max_curvature:
        return Observation(s, se_log, vega, delta, "curvature")
    if se_log > max_obs_se:
        return Observation(s, se_log, vega, delta, "se")
    return Observation(s, se_log, vega, delta, None)


class ImpliedVolEMA:
    """Precision-weighted, time-decayed mean of ln s. Session-lifetime: it is
    carried across events, so each event starts from the last one's estimate."""

    def __init__(self):
        self._sw = 0.0      # decayed sum of weights
        self._swx = 0.0     # decayed sum of weight * ln s
        self._n = 0.0       # decayed count of accepted observations
        self._t: float | None = None
        self.last: Observation | None = None
        self.n_seen = 0
        self.rejects: dict[str, int] = {}

    def _decay(self, now_s: float, tau_s: float):
        if self._t is not None and now_s > self._t:
            k = math.exp(-(now_s - self._t) / tau_s)
            self._sw *= k
            self._swx *= k
            self._n *= k
        self._t = now_s if self._t is None else max(self._t, now_s)

    def add(self, now_s: float, obs: Observation, tau_s: float):
        self._decay(now_s, tau_s)
        self.last = obs
        self.n_seen += 1
        if obs.reason is not None:
            self.rejects[obs.reason] = self.rejects.get(obs.reason, 0) + 1
            return
        w = 1.0 / (obs.se_log * obs.se_log)
        self._sw += w
        self._swx += w * math.log(obs.s)
        self._n += 1.0

    def value(self, now_s: float, tau_s: float, min_obs: float) -> float | None:
        """Implied per-sqrt-second vol (log units), or None with too few
        accepted observations inside the EMA."""
        self._decay(now_s, tau_s)
        if self._n < min_obs or self._sw <= 0:
            return None
        return math.exp(self._swx / self._sw)

    def n_eff(self) -> float:
        return self._n

    def snapshot(self) -> dict:
        o = self.last
        return {"n_eff": round(self._n, 1), "n_seen": self.n_seen, "rejects": dict(self.rejects),
                "last_s_bp": None if o is None or not np.isfinite(o.s) else round(o.s * 1e4, 4),
                "last_se_log": None if o is None or not np.isfinite(o.se_log) else round(o.se_log, 3),
                "last_reason": None if o is None else o.reason}


__all__ = ["ImpliedVolEMA", "Observation", "observe", "greeks"]
