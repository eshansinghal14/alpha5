"""
HAR forecaster tests.

Two sets:
  * Fixed-point mode (force_fallback=True): no training artifact needed.
    Exercises the ring buffer, steady-state intercept, log-h interp,
    clipping, correlation. These are the "math is wired right" tests.
  * Fitted mode (artifact present): verifies the artifact is loaded,
    coin_scale comes from it, and the slopes are the notebook's values.
"""
import math
import os
import sys

import numpy as np
import pytest


from kalshi_strategies.KXCRYPTOLEAD15M.har_forecaster import (HarCovarianceForecaster,
                                           HAR_COEFFS, FITTED_HORIZONS_S,
                                           HAR_WINDOWS_S, load_fit_artifact)
from kalshi_strategies.KXCRYPTOLEAD15M.cov_forecaster import DEFAULT_ANNUAL_VOL, DEFAULT_CORRELATION
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS


SIGMA_REF_SQ = {c: (DEFAULT_ANNUAL_VOL[c] / math.sqrt(365.25 * 86400.0)) ** 2
                for c in COINS}


# Fixed-point tests exercise the fallback intercept semantics. The real
# forecaster in production uses the fitted artifact; force_fallback here
# so these tests are deterministic regardless of whether har_fit.json
# exists on disk.
def _fp_forecaster(**kwargs) -> HarCovarianceForecaster:
    return HarCovarianceForecaster(force_fallback=True, **kwargs)


def _seed_steady(f: HarCovarianceForecaster, coin: str, variance_per_sec: float,
                 n: int = 4000):
    """Push n ticks of 1s returns with variance `variance_per_sec` so every
    ring buffer is filled to that level.

    Uses IID normal returns (seeded, scaled to the exact target variance)
    rather than a +/- alternating pattern. The ring now applies a
    first-order Hansen-Lunde bounce correction, and a strictly alternating
    series looks like 100% bid-ask bounce (lag-1 autocorr = -1), which the
    correction would (correctly, for that signal) shrink away. IID returns
    have ~0 autocovariance, so the corrected RV equals the raw RV -- which
    is what 'steady state at sigma_ref' is meant to represent."""
    # Block pattern [+s, +s, -s, -s] repeating. Like the old +/- alternating
    # seed it makes sum(r^2)/W == variance_per_sec EXACTLY for any window W
    # that is a multiple of 4 (60, 900, 3600 all are), so the exact
    # fixed-point assertions hold -- but unlike strict alternation its
    # lag-1 autocovariance is ~0, so it isn't mistaken for 100% bid-ask
    # bounce by the HL correction.
    sigma = math.sqrt(variance_per_sec)
    pattern = (sigma, sigma, -sigma, -sigma)
    p = 1.0
    f.update(0.0, {coin: p})           # seeds last_price
    for i in range(n):
        p *= math.exp(pattern[i % 4])
        f.update(float(i + 1), {coin: p})
    return n   # last timestamp used, so callers can continue contiguously


def test_fixed_point_steady_state_at_sigma_ref():
    """When RV_1m = RV_15m = RV_1h = sigma_ref^2, forecast == sigma_ref^2
    * coin_scale (times h for integrated). Uses the baked-in intercept.

    bounce_correct=False: this asserts the EXACT HAR fixed-point math at
    1e-6, and the first-order autocovariance correction adds a small
    (genuinely ~0 but not to 1e-6) finite-sample term on IID returns. The
    correction itself is exercised in test_bounce_correction_*."""
    f = _fp_forecaster(bounce_correct=False)
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin])
    # forecast_cov_rate uses the 60s horizon; diagonal variance should
    # match sigma_ref^2 * coin_scale.
    rate = f.forecast_cov_rate(0.0)
    diag = np.diag(rate)
    for i, coin in enumerate(COINS):
        expected = SIGMA_REF_SQ[coin] * f.coin_scale.get(coin, 1.0)
        assert math.isclose(diag[i], expected, rel_tol=1e-6), \
            f"{coin}: {diag[i]:.4e} vs {expected:.4e}"


def test_forecast_cov_scales_linearly_with_h_at_fixed_point():
    """At the fixed point, integrated variance over h seconds is h * sigma_ref^2
    * coin_scale, interpolated across the three fitted horizons."""
    f = _fp_forecaster(bounce_correct=False)   # exact fixed-point math; see note above
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin])
    # At h = 60: Var = 60 * sigma_ref^2 * coin_scale
    cov60 = f.forecast_cov(0.0, 60.0)
    for i, coin in enumerate(COINS):
        expected = 60.0 * SIGMA_REF_SQ[coin] * f.coin_scale.get(coin, 1.0)
        assert math.isclose(cov60[i, i], expected, rel_tol=1e-6)
    # At h = 900: same, times 15.
    cov900 = f.forecast_cov(0.0, 900.0)
    for i, coin in enumerate(COINS):
        expected = 900.0 * SIGMA_REF_SQ[coin] * f.coin_scale.get(coin, 1.0)
        assert math.isclose(cov900[i, i], expected, rel_tol=1e-6)


def test_log_h_interpolation_between_fitted_horizons():
    """At h = 300 (middle fitted horizon), the result should match the
    middle row of HAR_COEFFS exactly. For an in-between h (say 150s),
    the per-second variance should sit between the h=60 and h=300 values."""
    f = _fp_forecaster()
    # Perturb one coin away from sigma_ref so the HAR mapping is non-trivial.
    _seed_steady(f, "BTC", SIGMA_REF_SQ["BTC"] * 4.0)   # 2x vol
    for coin in COINS[1:]:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin])
    btc = COINS.index("BTC")
    v60 = f.forecast_cov(0.0, 60.0)[btc, btc] / 60.0
    v300 = f.forecast_cov(0.0, 300.0)[btc, btc] / 300.0
    v150 = f.forecast_cov(0.0, 150.0)[btc, btc] / 150.0
    # Monotone in h for a stressed short window (short horizon reacts more)?
    # No -- it depends on the coefficient pattern. What we CAN assert is
    # that v150 is strictly between v60 and v300 in log space (log-h linear
    # interp guarantees this).
    assert min(v60, v300) <= v150 <= max(v60, v300) + 1e-18


def test_clipping_at_short_horizons():
    """h < 60: variance rate equals the h=60 value (clipped)."""
    f = _fp_forecaster()
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin] * 2.0)
    # Rate at h=10s should equal rate at h=60s.
    rate_short = f.forecast_cov(0.0, 10.0) / 10.0
    rate_60 = f.forecast_cov(0.0, 60.0) / 60.0
    np.testing.assert_allclose(rate_short, rate_60, rtol=1e-10)


def test_clipping_at_long_horizons():
    """h > 900: variance rate equals the h=900 value (clipped)."""
    f = _fp_forecaster()
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin] * 2.0)
    rate_long = f.forecast_cov(0.0, 3600.0) / 3600.0
    rate_900 = f.forecast_cov(0.0, 900.0) / 900.0
    np.testing.assert_allclose(rate_long, rate_900, rtol=1e-10)


def test_forecast_cov_rate_matches_60s_horizon():
    """`forecast_cov_rate(t)` is defined to be the per-second variance at
    the shortest fitted horizon (60s). The pricer uses it for theo_vol."""
    f = _fp_forecaster()
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin] * 1.5)
    rate = f.forecast_cov_rate(0.0)
    cov60 = f.forecast_cov(0.0, 60.0)
    np.testing.assert_allclose(rate, cov60 / 60.0, rtol=1e-10)


def test_update_steady_price_floors_at_vfloor():
    """A frozen composite price produces r^2 = 0 everywhere; the ring
    buffers sum to 0 and the per-second RV is pinned at `vfloor`. The
    forecast then collapses to sigma_ref^2 * exp(sum(b) * log(vfloor /
    sigma_ref^2)) * coin_scale."""
    f = _fp_forecaster()
    for _ in range(100):
        for coin in COINS:
            f.update(0.0, {coin: 100.0})
    rate = f.forecast_cov_rate(0.0)
    diag = np.diag(rate)
    # Each diagonal is BELOW sigma_ref^2 (because log vfloor << log
    # sigma_ref^2, and b_sum < 1 means the log-forecast shrinks).
    for i, coin in enumerate(COINS):
        assert diag[i] < SIGMA_REF_SQ[coin], \
            f"{coin} should drop below sigma_ref under all-zero returns"


def test_large_shock_increases_short_horizon_more():
    """A single large return should push the h=60 forecast UP more than
    the h=900 forecast, because b_1m is largest at the short horizon."""
    f = _fp_forecaster(bounce_correct=False)
    last_ts = 0
    for coin in COINS:
        last_ts = _seed_steady(f, coin, SIGMA_REF_SQ[coin])
    btc = COINS.index("BTC")
    rate_before = f.forecast_cov_rate(0.0)[btc, btc]
    cov900_before = f.forecast_cov(0.0, 900.0)[btc, btc] / 900.0
    # Inject a 1% jump on the NEXT contiguous second (a gap would be
    # skipped by the ring's gap filter).
    last_p = f._rings["BTC"].last_price
    f.update(float(last_ts + 1), {"BTC": last_p * math.exp(0.01)})
    rate_after = f.forecast_cov_rate(0.0)[btc, btc]
    cov900_after = f.forecast_cov(0.0, 900.0)[btc, btc] / 900.0
    short_mult = rate_after / rate_before
    long_mult = cov900_after / cov900_before
    assert short_mult > long_mult, \
        f"short-horizon reaction ({short_mult:.3f}) should exceed long ({long_mult:.3f})"


def test_update_with_subset_of_coins_leaves_others_alone():
    """A tick that only mentions BTC must not touch the HYPE ring."""
    f = _fp_forecaster()
    _seed_steady(f, "HYPE", SIGMA_REF_SQ["HYPE"])
    hype_sum_before = {W: f._rings["HYPE"].sums[W] for W in HAR_WINDOWS_S}
    f.update(99999.0, {"BTC": 100.0})
    f.update(99999.0, {"BTC": 101.0})
    hype_sum_after = {W: f._rings["HYPE"].sums[W] for W in HAR_WINDOWS_S}
    assert hype_sum_before == hype_sum_after


def test_covariance_matrix_symmetric_and_uses_correlation():
    """Off-diagonals = corr[i,j] * sqrt(var[i] * var[j])."""
    f = _fp_forecaster()
    for coin in COINS:
        _seed_steady(f, coin, SIGMA_REF_SQ[coin])
    cov = f.forecast_cov_rate(0.0)
    diag = np.diag(cov)
    # Symmetric.
    np.testing.assert_allclose(cov, cov.T, atol=1e-18)
    # Off-diagonal consistency with DEFAULT_CORRELATION.
    for i in range(len(COINS)):
        for j in range(len(COINS)):
            expected = DEFAULT_CORRELATION[i, j] * math.sqrt(diag[i] * diag[j])
            assert math.isclose(cov[i, j], expected, rel_tol=1e-10)


# ---------------------------------------------------------------------------
# Fitted-mode tests. Skipped if no training artifact exists on disk --
# the fixed-point mode above covers the full math surface regardless.
# ---------------------------------------------------------------------------

_FITTED = load_fit_artifact()
_HAS_FITTED = _FITTED is not None


@pytest.mark.skipif(not _HAS_FITTED, reason="no har_fit.json artifact")
def test_fitted_mode_matches_notebook_slopes():
    """Loaded slopes agree with the notebook's published values to 3dp
    (fit_har.py reproduces cell 20 of the notebook)."""
    f = HarCovarianceForecaster()
    assert f.mode == "fitted"
    expected = HAR_COEFFS   # the notebook's published 3x3 slope matrix
    np.testing.assert_allclose(f._slopes, expected, atol=5e-4)


@pytest.mark.skipif(not _HAS_FITTED, reason="no har_fit.json artifact")
def test_fitted_mode_coin_scale_includes_hype_bias():
    """Notebook calibration cell 22 shows HYPE at ~1.13 and the rest
    within +/- 3% of 1. The artifact must carry those values forward."""
    f = HarCovarianceForecaster()
    assert f.mode == "fitted"
    # HYPE is the one coin where the Coinbase 1s mid is sticky enough
    # to produce a systematic under-forecast; everything else is near
    # unbiased.
    assert 1.08 <= f.coin_scale["HYPE"] <= 1.18, \
        f"HYPE scale {f.coin_scale['HYPE']:.3f} outside expected 1.08-1.18"
    for coin in ("BTC", "ETH", "SOL", "XRP"):
        assert 0.95 <= f.coin_scale[coin] <= 1.05, \
            f"{coin} scale {f.coin_scale[coin]:.3f} far from 1.0"


@pytest.mark.skipif(not _HAS_FITTED, reason="no har_fit.json artifact")
def test_fitted_mode_forecast_sits_in_plausible_range():
    """At steady state matching the fitted feature_mean (so the
    (X - mu) term is zero and only the intercept drives), the per-second
    variance should fall in a crypto-plausible range -- not blow up to
    1e-4 or collapse to 1e-14."""
    f = HarCovarianceForecaster()
    # Seed each coin at exp(feature_mean[1]) -- the h=300 fit's center.
    for i, coin in enumerate(COINS):
        var_per_sec = math.exp(f._feature_mean[1, 0])   # log RV_1m center
        _seed_steady(f, coin, var_per_sec)
    rate = f.forecast_cov_rate(0.0)
    diag = np.diag(rate)
    # Reasonable per-second variance for crypto: 1e-10 to 1e-7
    # (corresponds to annualized vol 10%-300%).
    for i, coin in enumerate(COINS):
        assert 1e-11 < diag[i] < 1e-6, \
            f"{coin} per-second variance {diag[i]:.2e} outside plausible range"


@pytest.mark.skipif(not _HAS_FITTED, reason="no har_fit.json artifact")
def test_fitted_mode_horizon_interp_strictly_between():
    """At h=150 (between 60 and 300), log forecast is linear in log h.
    The per-second variance ratio at h=150 vs h=60 and h=300 should be
    strictly between."""
    f = HarCovarianceForecaster()
    # Seed BTC with an elevated recent RV so the short horizon reacts.
    _seed_steady(f, "BTC", math.exp(f._feature_mean[0, 0]) * 4.0)
    btc = COINS.index("BTC")
    v60 = f.forecast_cov(0.0, 60.0)[btc, btc] / 60.0
    v300 = f.forecast_cov(0.0, 300.0)[btc, btc] / 300.0
    v150 = f.forecast_cov(0.0, 150.0)[btc, btc] / 150.0
    assert min(v60, v300) < v150 < max(v60, v300) + 1e-18


# ---------------------------------------------------------------------------
# Microstructure robustness: gap skip + bid-ask-bounce correction
# ---------------------------------------------------------------------------

def test_return_across_time_gap_is_skipped():
    """A price change spanning a data gap (dt > max_gap_s) is a teleport,
    not 1s of volatility, and must not be booked as r^2. Observed
    2026-10-07: a 263s gap produced a 9.2% 'return' that dragged BTC's
    whole-day RV to ~880% annualized vs ~40% within a session."""
    f = _fp_forecaster(bounce_correct=False)
    # Seed a calm 1s series (use the 3600s ring so nothing rolls out).
    p = 100.0
    f.update(0.0, {"BTC": p})
    for i in range(1, 60):
        p *= math.exp(1e-4 * (1 if i % 2 else -1))
        f.update(float(i), {"BTC": p})
    rv_before = f._rings["BTC"].rv_rate(3600)
    # A huge jump across a 300s gap -> teleport, must be ignored.
    f.update(360.0, {"BTC": p * math.exp(0.09)})
    rv_after = f._rings["BTC"].rv_rate(3600)
    assert math.isclose(rv_before, rv_after, rel_tol=1e-9), \
        "a teleport across a gap must not change RV"
    # The SAME jump on a CONTIGUOUS second DOES count (price moves further).
    p_jumped = p * math.exp(0.09)
    f.update(361.0, {"BTC": p_jumped * math.exp(0.09)})
    assert f._rings["BTC"].rv_rate(3600) > rv_after


def test_bounce_correction_shrinks_mean_reverting_series():
    """A perfectly alternating +/- series is 100% bid-ask bounce (lag-1
    autocorr -1). The HL correction must pull its RV well below the raw
    r^2 sum (floored at 25% of raw). An IID series is left ~unchanged."""
    # Bouncing series.
    fb = _fp_forecaster(bounce_correct=True)
    fr = _fp_forecaster(bounce_correct=False)
    p = 100.0
    fb.update(0.0, {"BTC": p}); fr.update(0.0, {"BTC": p})
    for i in range(1, 500):
        p *= math.exp(1e-3 * (1 if i % 2 else -1))
        fb.update(float(i), {"BTC": p}); fr.update(float(i), {"BTC": p})
    corrected = fb._rings["BTC"].rv_rate(60)
    raw = fr._rings["BTC"].rv_rate(60)
    assert corrected < 0.5 * raw        # bounce largely removed
    assert corrected >= 0.25 * raw - 1e-18   # but floored, never zeroed
