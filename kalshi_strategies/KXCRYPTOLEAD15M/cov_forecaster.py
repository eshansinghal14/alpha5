"""
Realized-volatility / covariance forecaster interface (section 5).

Deliberately STUB-ONLY. The real model will be designed separately. This
file defines the Protocol every pricer call goes through, plus two
non-model implementations:

  * `PlaceholderForecaster` -- raises NotImplementedError on `forecast
    _cov`. Fails fast if a pricer is wired up without a model.
  * `ConstantCovForecaster(Sigma_per_sec)` -- returns the same
    per-second covariance every time. Used in tests and smoke runs
    (section 5, "stubs"). ONE source of forecasts, so pricer behaviour
    is deterministic given a seed.

Notes for the future model (kept here so a reader of the pricer finds
them):
  * Correlations estimated from 1-second returns are biased low by
    asynchronous trading (the Epps effect). The model should either use
    returns over a window LONGER than its own autocorrelation, or
    explicitly correct for it.
  * Seasonality (intra-hour, intra-day, macro-release windows) and
    scheduled events MUST live inside the model's `forecast_cov(t0, t1)`
    -- the pricer only integrates. Giving the pricer a time-of-day
    multiplier here would couple two concerns.
"""
from __future__ import annotations

import math

import numpy as np

from .series import COINS


# ---------------------------------------------------------------------------
# Reasonable default per-coin vols for smoke runs. NOT CALIBRATED.
#
# Rough annualized log-return vols as of 2026, picked from publicly
# available spot-vol estimates for each coin's recent history. Order
# matches COINS. HYPE is new-listing-wide; SOL/XRP sit between the
# majors and HYPE.
#
# Converted to per-second via sigma_sec = sigma_annual / sqrt(seconds_per_year),
# seconds_per_year = 365.25 * 86400 = 31_557_600. For BTC at 55% annualized,
# that gives sigma_sec ~ 0.55 / sqrt(31.56M) ~ 9.8e-5 (~0.98 bps/s). 15 min
# of integrated variance: (9.8e-5)^2 * 900 ~ 8.6e-6, so one-sigma 15-min
# move ~ 0.29% -- in the right ballpark for BTC on a quiet 15-min window.
# ---------------------------------------------------------------------------

_SECONDS_PER_YEAR = 365.25 * 86400.0

DEFAULT_ANNUAL_VOL = {
    "BTC":  0.55,
    "ETH":  0.65,
    "SOL":  0.85,
    "XRP":  0.75,
    "HYPE": 1.10,
}

# Correlation matrix. BTC-ETH highest; everything-with-HYPE loosest
# (newest listing, less overlap in order flow). Rough but internally
# consistent (PSD-check happens in Cholesky at pricer time).
#   indices follow COINS order: BTC, ETH, SOL, XRP, HYPE
DEFAULT_CORRELATION = np.array([
    #  BTC   ETH   SOL   XRP  HYPE
    [1.00, 0.80, 0.65, 0.55, 0.40],   # BTC
    [0.80, 1.00, 0.70, 0.55, 0.45],   # ETH
    [0.65, 0.70, 1.00, 0.55, 0.45],   # SOL
    [0.55, 0.55, 0.55, 1.00, 0.35],   # XRP
    [0.40, 0.45, 0.45, 0.35, 1.00],   # HYPE
])


def _default_sigma_per_sec() -> np.ndarray:
    """5x5 per-second covariance matrix built from DEFAULT_ANNUAL_VOL
    and DEFAULT_CORRELATION. Public so callers (and tests) can pull
    the exact matrix the default forecaster will use."""
    sigmas_sec = np.array([DEFAULT_ANNUAL_VOL[c] / math.sqrt(_SECONDS_PER_YEAR)
                           for c in COINS])
    D = np.diag(sigmas_sec)
    return D @ DEFAULT_CORRELATION @ D


class CovarianceForecaster:
    """Pricer-facing interface. Not a formal Protocol -- a plain base
    class so tests can `isinstance` and so a stub forecaster can share
    the one-off helper methods below."""

    coin_order: tuple[str, ...] = COINS

    def update(self, ts: float, composite_prices: dict[str, float]) -> None:
        """Ingest a 1s composite price snapshot. May be called many times
        between forecasts; the model decides when to roll."""
        raise NotImplementedError

    def forecast_cov(self, t0: float, t1: float) -> np.ndarray:
        """5x5 covariance of LOG returns, INTEGRATED over [t0, t1].
        Units: return^2 (NOT per-second). Coin order:
        self.coin_order."""
        raise NotImplementedError

    def forecast_cov_rate(self, t: float) -> np.ndarray:
        """Instantaneous per-second covariance at `t`. Used by the pricer
        for sensitivities and latency widening. Defaults to a numeric
        derivative of forecast_cov, which is good enough for anything
        smoother than a jump."""
        eps = 0.5
        return (self.forecast_cov(t, t + eps) - self.forecast_cov(t, t)) / eps


class PlaceholderForecaster(CovarianceForecaster):
    """Raises on any forecast. Wire this in to guarantee a KeyError-at-
    import style failure if someone tries to run the pricer without a
    real model."""

    def update(self, ts, composite_prices):
        return

    def forecast_cov(self, t0, t1):
        raise NotImplementedError(
            "no covariance model wired -- see kalshi_crypto/cov_forecaster.py. "
            "Use ConstantCovForecaster for tests/smoke runs.")

    def forecast_cov_rate(self, t):
        raise NotImplementedError(
            "no covariance model wired -- see kalshi_crypto/cov_forecaster.py.")


class ConstantCovForecaster(CovarianceForecaster):
    """Returns the SAME per-second covariance for every call.

    Default: `_default_sigma_per_sec()` -- per-coin annualized vols
    (BTC 55%, ETH 65%, SOL 85%, XRP 75%, HYPE 110%) converted to
    per-second and combined with DEFAULT_CORRELATION. See the module
    docstring and DEFAULT_ANNUAL_VOL for provenance.

    Not calibrated against our own composite returns -- just "operator-
    plausible" numbers so the pricer produces reasonable fairs during
    smoke/paper runs. Replace with the real realised-vol model before
    trusting any live PnL.

    Alternative constructors for tests:
      * `Sigma_per_sec=M`              -- pass a specific 5x5 matrix.
      * `sigma_per_sec=s, rho=r`       -- uniform diag + uniform corr.
        Used only when `Sigma_per_sec is None AND uniform_sigma=True`.
    """

    def __init__(self, Sigma_per_sec: np.ndarray | None = None,
                 sigma_per_sec: float | None = None, rho: float | None = None,
                 uniform_sigma: bool = False):
        if Sigma_per_sec is None:
            if uniform_sigma or (sigma_per_sec is not None):
                n = len(self.coin_order)
                s = sigma_per_sec if sigma_per_sec is not None else 5e-4
                r = rho if rho is not None else 0.5
                Sigma_per_sec = np.full((n, n), r * s ** 2)
                np.fill_diagonal(Sigma_per_sec, s ** 2)
            else:
                Sigma_per_sec = _default_sigma_per_sec()
        Sigma_per_sec = np.asarray(Sigma_per_sec, dtype=float)
        if Sigma_per_sec.shape != (len(self.coin_order), len(self.coin_order)):
            raise ValueError(f"Sigma_per_sec must be {len(self.coin_order)}x{len(self.coin_order)}")
        self.Sigma = Sigma_per_sec

    def update(self, ts, composite_prices):
        return

    def forecast_cov(self, t0, t1):
        dt = max(0.0, t1 - t0)
        return self.Sigma * dt

    def forecast_cov_rate(self, t):
        return self.Sigma.copy()


__all__ = ["CovarianceForecaster", "PlaceholderForecaster", "ConstantCovForecaster",
           "DEFAULT_ANNUAL_VOL", "DEFAULT_CORRELATION"]
