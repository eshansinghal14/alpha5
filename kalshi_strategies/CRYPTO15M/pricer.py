"""
P(up) for one single-coin 15-minute market: YES when the 60s average of the
coin's CF RTI before T is at least the 60s average before S (the strike).

Same variance clock and tails as the Coin Race pricer (KXCRYPTOLEAD15M/pricer.py),
reduced to one coin, so it is closed form instead of Monte Carlo:

    Y = mu + sd * eps,  eps unit-variance Student-t(nu),  P(up) = P(Y >= 0)

with x = ln(mid / strike), the strike on our mid's level, and

    in window (tau >= 60):   mu = x                te = tau - 40
    last minute (tau < 60):  mu = I + (tau/60) x   te = tau^3 / 10800

sd^2 = rate * te + basis^2. I = the observed part of the end average, as a
sum over 60 (see model._price).
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
from scipy import special

L = 60.0
# Gauss-Hermite (probabilists') nodes for E|dP| under a normal spot move.
_GH_X, _GH_W = np.polynomial.hermite_e.hermegauss(24)
_GH_W = _GH_W / math.sqrt(2.0 * math.pi)


@dataclasses.dataclass
class UpDownResult:
    prob: float
    mu: float
    sd: float
    te: float
    phase: str
    theo_vol: float


def _cdf(z, nu: float | None):
    """CDF of a unit-variance t(nu); normal when nu is None."""
    if nu is None:
        return special.ndtr(z)
    return special.stdtr(nu, np.asarray(z) * math.sqrt(nu / (nu - 2.0)))


def _ppf(p: float, nu: float | None) -> float:
    if nu is None:
        return float(special.ndtri(p))
    return float(special.stdtrit(nu, p) / math.sqrt(nu / (nu - 2.0)))


def variance_clock(tau: float) -> tuple[float, float, str]:
    """(te, d mu / d x, phase)."""
    if tau >= L:
        return tau - 40.0, 1.0, "in_window"
    if tau <= 0:
        return 0.0, 0.0, "settled"
    return tau ** 3 / 10800.0, tau / L, "last_minute"


def prob_up(x: float, I: float, tau: float, rate: float, *, nu: float | None, basis_sd: float = 0.0,
            reaction_s: float = 0.0, with_theo_vol: bool = True) -> UpDownResult:
    """`x`: ln(mid / strike) now. `I`: partial end-average sum (0 outside the
    last minute). `rate`: per-second log variance. `basis_sd`: sd of our
    final return vs the settlement index's, log units."""
    te, g, phase = variance_clock(tau)
    mu = I + g * x
    sd = math.sqrt(max(rate * te, 0.0) + basis_sd * basis_sd)
    if sd <= 0:
        return UpDownResult(1.0 if mu >= 0 else 0.0, mu, 0.0, te, phase, 0.0)
    p = float(_cdf(mu / sd, nu))
    tv = 0.0
    s_move = g * math.sqrt(max(rate * reaction_s, 0.0))
    if with_theo_vol and s_move > 0:
        # sigma-equivalent fair move over reaction_s: E|dP| * sqrt(pi/2), so it
        # equals slope x 1-sigma when P is linear but still prices the step an
        # ATM fair takes near expiry.
        moved = _cdf((mu + s_move * _GH_X) / sd, nu)
        tv = float(np.sum(_GH_W * np.abs(moved - p))) * math.sqrt(math.pi / 2.0)
    return UpDownResult(p, mu, sd, te, phase, tv)


def implied_mu(p_market: float, sd: float, nu: float | None, clip: float = 0.03) -> float:
    """The mu that prices at `p_market` with this sd."""
    p = min(max(p_market, clip), 1.0 - clip)
    return sd * _ppf(p, nu)


__all__ = ["UpDownResult", "prob_up", "implied_mu", "variance_clock"]
