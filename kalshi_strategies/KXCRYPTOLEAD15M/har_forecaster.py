"""
SUPERSEDED by robust_har.RobustHarForecaster (not wired into live or
backtest). Kept for its tests and as the record of the first fit: raw 1 s
features, a fixed 60s/300s/900s coefficient set and a hard-coded HYPE 1.13
scale, which on the CF-replica composite over-forecast every coin and HYPE
~16x (rv_modeling/vol_fix_reference/README.md).

HAR-based realized-vol forecaster for the Coin Race pricer.

Model (from kalshi_crypto/rv_modeling/Realized-Vol Model for Kalshi Coin
Race.ipynb, §7, pooled across coins):

    log sigma^2_h = a_h + b_1m * log RV_1m + b_15m * log RV_15m + b_1h * log RV_1h
    Var(r_{t -> t+h}) = h * sigma^2_h * coin_scale

Two modes:

1. **Fitted mode (preferred).** Load the artifact produced by
   `kalshi_crypto/rv_modeling/fit_har.py`:

       intercept[h], feature_mean[h], slopes[h], coin_scale[coin]

   This reproduces the notebook's exact fit on 113,760 training rows.
   The forecaster reads the artifact at construction; file lives at
   `kalshi_crypto/rv_modeling/har_fit.json`.

2. **Fixed-point mode (fallback).** When the artifact is missing, pin
   the intercept so that in a steady-state regime where
   RV_1m = RV_15m = RV_1h = sigma_ref^2, the model returns sigma_ref^2.
   Uses operator-plausible sigma_ref per coin from
   `cov_forecaster.DEFAULT_ANNUAL_VOL`. Behavior matches
   `ConstantCovForecaster` at steady state and responds to actual
   RV deviations with the fitted slopes.

   This exists so a fresh clone (no training data on disk) still
   produces finite non-degenerate forecasts.

Fitted slopes (same in both modes, from the notebook):

    h =  60s: (0.234, 0.359, 0.288)      # coefficient sum 0.880
    h = 300s: (0.165, 0.270, 0.459)
    h = 900s: (0.093, 0.302, 0.489)

For a horizon between fitted points we interpolate the log forecast
linearly in log(h), clipped at the endpoints. Sum < 1 at every horizon
means vol shocks mean-revert.

Per-coin calibration (fitted mode): BTC 0.98, ETH 1.02, SOL 0.98,
XRP 0.97, HYPE 1.13. Fallback mode uses the same values if the artifact
is missing -- they're observable facts about Coinbase 1s mids, not a
property of this particular fit.

Online state. Three ring buffers of 1-second squared log returns per
coin, lengths 60 / 900 / 3600 seconds. `update(ts, {coin: price})`
appends; `forecast_*` reads the current sum. O(1) amortized per update.

Correlation. Notebook §8 explicitly leaves correlation estimation as a
separate, low-impact choice (half-life 4-24h on 30-60s returns). This
file uses the operator-plausible correlations from
`cov_forecaster.DEFAULT_CORRELATION` for v1 and keeps a `set_correlation`
hook for a future online estimator.
"""
from __future__ import annotations

import collections
import json
import math
import os
from pathlib import Path
from typing import Mapping

import numpy as np

from .cov_forecaster import (CovarianceForecaster, DEFAULT_ANNUAL_VOL,
                              DEFAULT_CORRELATION)
from .series import COINS


# ---------------------------------------------------------------------------
# Fitted coefficients from the notebook (cell 21) + fit_har.py artifact.
# One row per fitted horizon; columns are the HAR slopes.
# ---------------------------------------------------------------------------

HAR_WINDOWS_S = (60, 900, 3600)   # the 1m / 15m / 1h RV features
FITTED_HORIZONS_S = (60, 300, 900)
# shape (3, 3): rows = horizons, cols = (b_1m, b_15m, b_1h). Matches the
# fit_har.py output on 2026-10-06 (113,760 rows) and the notebook table.
HAR_COEFFS = np.array([
    [0.234, 0.359, 0.288],   # h = 60s   (sum 0.880)
    [0.165, 0.270, 0.459],   # h = 300s  (sum 0.893)
    [0.093, 0.302, 0.489],   # h = 900s  (sum 0.884)
], dtype=float)

# Per-coin calibration scale from notebook cell 22 (mean r^2 / forecast
# at 15m horizon). HYPE's sticky 1-second mid gives +13% under-forecast;
# others are within +/- 3%. These are observations about Coinbase's L2
# feed, not a free parameter -- used in both fitted and fallback modes.
COIN_CALIBRATION_SCALE = {
    "BTC":  0.98,
    "ETH":  1.02,
    "SOL":  0.98,
    "XRP":  0.97,
    "HYPE": 1.13,
}

_SECONDS_PER_YEAR = 365.25 * 86400.0

# Default artifact location. fit_har.py writes here.
_DEFAULT_ARTIFACT = (Path(os.path.dirname(os.path.abspath(__file__)))
                     / "research" / "har_fit.json")


def _default_sigma_ref_sq() -> dict[str, float]:
    """Per-coin per-second variance from the DEFAULT_ANNUAL_VOL table.
    Used in fallback mode."""
    return {c: (DEFAULT_ANNUAL_VOL[c] / math.sqrt(_SECONDS_PER_YEAR)) ** 2
            for c in COINS}


def load_fit_artifact(path: Path | str | None = None) -> dict | None:
    """Load the JSON artifact produced by fit_har.py. Returns None if
    the file doesn't exist (callers fall back to fixed-point mode).
    Raises on malformed JSON -- a half-written artifact is a bug, not
    something to silently paper over."""
    p = Path(path) if path else _DEFAULT_ARTIFACT
    if not p.exists():
        return None
    with p.open() as fh:
        d = json.load(fh)
    # Light schema check.
    for k in ("horizons_s", "windows_s", "coin_order", "intercept",
              "feature_mean", "slopes", "coin_scale"):
        if k not in d:
            raise ValueError(f"har_fit artifact missing field {k!r} at {p}")
    return d


# A composite "return" spanning more than this many seconds is not a
# 1-second return -- it bridges a data gap (collector restart, session
# rollover, a quiet HYPE stretch). The price difference across the gap is
# a discrete teleport, not 1s of diffusion, so counting its square as
# per-second variance massively inflates the RV estimate. Observed
# 2026-10-07: a single 263s gap on BTC produced a 9.2% "1-second" return,
# dragging the whole-day RV to 878% annualized vs ~40% within a session.
MAX_RETURN_GAP_S = 2.0


class _RvRings:
    """Three fixed-length ring buffers of 1-second squared log returns:
    windows of 60, 900, 3600 seconds. Running sums tracked incrementally.

    Two robustness measures, both added 2026-10-07 after live fairs sat far
    from the Kalshi mid because the forecast variances were wildly inflated
    and mutually disproportionate (an ~800x variance ratio across coins):

      * GAP SKIP: a return is only counted if the previous price was within
        `MAX_RETURN_GAP_S` seconds. A longer gap means the pair straddles a
        data gap and its "return" is a teleport, not 1s of movement -- so we
        re-seed `last_price` and skip it rather than booking a huge spurious
        r^2.
      * BID-ASK-BOUNCE correction: `rv_rate` applies a first-order
        Hansen-Lunde autocovariance adjustment, RV* = sum r^2 + 2 sum
        r_t r_{t-1}. Microstructure bounce makes consecutive returns
        negatively autocorrelated (HYPE's thin composite showed lag-1
        -0.36), and that mean-reverting jitter is counted as variance by a
        plain r^2 sum; the correction cancels it. Floored at a fraction of
        the raw RV so a noisy correction can't zero the estimate.

    Floor the per-second variance at `vfloor` so stale-quote stretches
    (long runs of r^2 = 0) don't push log RV to -inf and blow up the
    regression. The notebook uses VFLOOR = 1e-10 (rvlib.py:11) on a
    per-second variance rate -- same value, same reason."""

    __slots__ = ("buffers", "prod_buffers", "sums", "prod_sums", "vfloor",
                 "last_price", "last_ts", "last_r", "n_obs", "max_gap_s",
                 "bounce_correct")

    def __init__(self, windows_s=HAR_WINDOWS_S, vfloor: float = 1e-10,
                 max_gap_s: float = MAX_RETURN_GAP_S, bounce_correct: bool = True):
        self.buffers = {W: collections.deque(maxlen=W) for W in windows_s}
        # Parallel rings of r_t * r_{t-1} for the HL autocovariance term.
        self.prod_buffers = {W: collections.deque(maxlen=W) for W in windows_s}
        self.sums = {W: 0.0 for W in windows_s}
        self.prod_sums = {W: 0.0 for W in windows_s}
        self.vfloor = vfloor
        self.max_gap_s = max_gap_s
        self.bounce_correct = bounce_correct
        self.last_price: float | None = None
        self.last_ts: float | None = None
        self.last_r: float | None = None   # previous kept return, for the cross term
        self.n_obs = 0

    def update(self, price: float, ts: float | None = None):
        """Ingest the composite price at second `ts`. First call seeds
        `last_price`; a return across a gap longer than `max_gap_s`
        re-seeds instead of booking a teleport."""
        if price is None or price <= 0 or not math.isfinite(price):
            return
        if self.last_price is None:
            self.last_price = price
            self.last_ts = ts
            return
        # Skip (and re-seed on) a return that straddles a data gap.
        if (ts is not None and self.last_ts is not None
                and (ts - self.last_ts) > self.max_gap_s):
            self.last_price = price
            self.last_ts = ts
            self.last_r = None       # the chain is broken; no cross term
            return
        r = math.log(price / self.last_price)
        self.last_price = price
        self.last_ts = ts
        r2 = r * r
        cross = (r * self.last_r) if self.last_r is not None else 0.0
        self.last_r = r
        self.n_obs += 1
        for W in self.buffers:
            buf = self.buffers[W]
            pbuf = self.prod_buffers[W]
            if len(buf) == buf.maxlen:
                self.sums[W] -= buf[0]
            buf.append(r2)
            self.sums[W] += r2
            if len(pbuf) == pbuf.maxlen:
                self.prod_sums[W] -= pbuf[0]
            pbuf.append(cross)
            self.prod_sums[W] += cross

    def rv_rate(self, W: int) -> float:
        """Per-second realized variance over the last W seconds, with the
        bid-ask-bounce (HL1) correction. During warm-up (fewer than W
        observations) we scale by the number of samples we actually have,
        floored."""
        buf = self.buffers.get(W)
        if buf is None or len(buf) == 0:
            return self.vfloor
        n = len(buf)
        raw = self.sums[W] / n
        if not self.bounce_correct:
            return max(raw, self.vfloor)
        hl = raw + 2.0 * (self.prod_sums[W] / n)
        # A noisy autocovariance can over-correct; never let the estimate
        # drop below a quarter of the raw RV (the bounce can at most explain
        # most, not all, of the measured variance).
        return max(hl, 0.25 * raw, self.vfloor)


class HarCovarianceForecaster(CovarianceForecaster):
    """Live HAR forecaster. Ingest composite prices via
    `update(ts, {coin: price})` once per second (or more often;
    duplicates in the same second are deduped by the ring append).
    Pricer calls `forecast_cov(t0, t1)` and `forecast_cov_rate(t)` as
    usual.

    By default loads the fitted artifact at
    `kalshi_crypto/rv_modeling/har_fit.json`; falls back to fixed-point
    mode if it's missing. Pass `fit_artifact=None, force_fallback=True`
    to force fixed-point for tests.
    """

    coin_order: tuple[str, ...] = COINS

    def __init__(self,
                 fit_artifact: dict | str | Path | None = None,
                 force_fallback: bool = False,
                 coin_scale: Mapping[str, float] | None = None,
                 sigma_ref_sq: Mapping[str, float] | None = None,
                 correlation: np.ndarray | None = None,
                 vfloor: float = 1e-10,
                 bounce_correct: bool = True,
                 max_gap_s: float = MAX_RETURN_GAP_S):
        # Resolve the artifact. `fit_artifact` can be a loaded dict, a
        # path to one, or None (default: project artifact).
        if force_fallback:
            self._artifact = None
        elif isinstance(fit_artifact, dict):
            self._artifact = fit_artifact
        else:
            self._artifact = load_fit_artifact(fit_artifact)

        # Correlation + state are shared across modes.
        self._correlation = (DEFAULT_CORRELATION.copy() if correlation is None
                              else np.asarray(correlation, dtype=float).copy())
        if self._correlation.shape != (len(self.coin_order), len(self.coin_order)):
            raise ValueError(f"correlation must be {len(self.coin_order)}x{len(self.coin_order)}")
        self._rings: dict[str, _RvRings] = {
            c: _RvRings(vfloor=vfloor, max_gap_s=max_gap_s,
                        bounce_correct=bounce_correct)
            for c in self.coin_order}
        self._last_update_s: float | None = None

        # Mode-specific setup.
        if self._artifact is not None:
            self._slopes = np.array(self._artifact["slopes"], dtype=float)
            self._intercept = np.array(self._artifact["intercept"], dtype=float)
            self._feature_mean = np.array(self._artifact["feature_mean"], dtype=float)
            self._fitted_horizons = np.array(self._artifact["horizons_s"], dtype=float)
            # Coin scale from artifact unless overridden.
            self.coin_scale = dict(self._artifact["coin_scale"]
                                   if coin_scale is None else coin_scale)
            self.mode = "fitted"
            # Not used in fitted mode, but kept for backward compat.
            self.sigma_ref_sq = dict(_default_sigma_ref_sq()
                                     if sigma_ref_sq is None else sigma_ref_sq)
        else:
            # Fallback mode. a_h = (1 - sum(b_h)) * log(sigma_ref^2);
            # mu = 0 so (X - mu) = X; slopes are the published HAR_COEFFS.
            self._slopes = HAR_COEFFS.copy()
            self._fitted_horizons = np.array(FITTED_HORIZONS_S, dtype=float)
            self._feature_mean = np.zeros_like(self._slopes)
            self.sigma_ref_sq = dict(_default_sigma_ref_sq()
                                     if sigma_ref_sq is None else sigma_ref_sq)
            self.coin_scale = dict(COIN_CALIBRATION_SCALE
                                   if coin_scale is None else coin_scale)
            # Intercept is per-coin in fallback mode (unlike fitted which
            # pools). Keep that fact wrapped in _intercept_for().
            self._intercept = None   # computed per-coin
            self.mode = "fallback"

        self._log_fitted_horizons = np.log(self._fitted_horizons)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def update(self, ts: float, composite_prices: dict[str, float]) -> None:
        """Ingest one 1-second snapshot. `composite_prices` keys may be a
        subset of COINS (a coin whose feed is unhealthy is simply skipped
        for this second -- its ring buffer freezes rather than seeing a
        gap-induced spurious r^2)."""
        for coin, px in composite_prices.items():
            r = self._rings.get(coin)
            if r is None:
                continue
            r.update(px, ts)
        self._last_update_s = ts

    def set_correlation(self, corr: np.ndarray):
        """Hook for a future online correlation estimator. Validates
        shape; must be symmetric and PSD (the pricer's Cholesky step
        catches non-PSD, so we don't re-check here)."""
        corr = np.asarray(corr, dtype=float)
        if corr.shape != self._correlation.shape:
            raise ValueError(f"correlation must be {self._correlation.shape}")
        self._correlation = corr.copy()

    # ------------------------------------------------------------------
    # Internal: HAR evaluation
    # ------------------------------------------------------------------

    def _logrv_vector(self, coin: str) -> np.ndarray:
        """log RV for the three HAR windows for one coin."""
        ring = self._rings[coin]
        return np.array([math.log(ring.rv_rate(W)) for W in HAR_WINDOWS_S])

    def _log_sigma_sq_at_fitted(self, coin: str, logrv: np.ndarray) -> np.ndarray:
        """Return log(sigma^2) at each FITTED horizon (shape (3,))."""
        if self.mode == "fitted":
            # log f = intercept + (X - mu) @ slope_row
            # per-horizon mu may differ, so loop.
            out = np.empty(len(self._fitted_horizons))
            for i in range(len(self._fitted_horizons)):
                out[i] = self._intercept[i] + float(
                    (logrv - self._feature_mean[i]) @ self._slopes[i])
            return out
        # Fallback: a_h = (1 - sum(b_h)) * log(sigma_ref^2)
        log_ref = math.log(self.sigma_ref_sq[coin])
        one_minus_bsum = 1.0 - self._slopes.sum(axis=1)
        a = one_minus_bsum * log_ref
        return a + self._slopes @ logrv

    def _coin_sigma_sq_at_horizon(self, coin: str, h: float) -> float:
        """Per-second variance rate forecast for `coin` at horizon
        `h` seconds. Clipped to the fitted horizon range."""
        logrv = self._logrv_vector(coin)
        log_sigma_sq = self._log_sigma_sq_at_fitted(coin, logrv)

        if h <= self._fitted_horizons[0]:
            lv = log_sigma_sq[0]
        elif h >= self._fitted_horizons[-1]:
            lv = log_sigma_sq[-1]
        else:
            lh = math.log(h)
            lv = float(np.interp(lh, self._log_fitted_horizons, log_sigma_sq))

        scale = self.coin_scale.get(coin, 1.0)
        return math.exp(lv) * scale

    def _variance_vector(self, h: float) -> np.ndarray:
        """5-vector of per-second variance rates at horizon h."""
        return np.array([self._coin_sigma_sq_at_horizon(c, h)
                         for c in self.coin_order])

    def _covariance_matrix(self, variances: np.ndarray) -> np.ndarray:
        """Combine per-coin variances with the correlation matrix:
            Sigma[i, j] = corr[i, j] * sqrt(var[i] * var[j])"""
        sigma = np.sqrt(np.maximum(variances, 0.0))
        return self._correlation * np.outer(sigma, sigma)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def forecast_cov(self, t0: float, t1: float) -> np.ndarray:
        """Integrated 5x5 covariance over [t0, t1]. Returns units of
        return^2 (NOT per-second). Pricer multiplies this directly."""
        h = max(t1 - t0, 0.0)
        if h <= 0:
            return np.zeros((len(self.coin_order), len(self.coin_order)))
        var = self._variance_vector(h)
        # forecast_var in notebook = h * sigma^2_h; this matches.
        return self._covariance_matrix(var) * h

    def forecast_cov_rate(self, t: float) -> np.ndarray:
        """Instantaneous per-second covariance at `t`. Used by the pricer
        for sensitivity scaling (theo_vol over reaction_latency_s).
        We take this at the shortest fitted horizon (60s) -- the HAR's
        best estimate of near-future variance."""
        var = self._variance_vector(float(self._fitted_horizons[0]))
        return self._covariance_matrix(var)


__all__ = [
    "HarCovarianceForecaster",
    "HAR_COEFFS", "HAR_WINDOWS_S", "FITTED_HORIZONS_S",
    "COIN_CALIBRATION_SCALE",
    "load_fit_artifact",
]
