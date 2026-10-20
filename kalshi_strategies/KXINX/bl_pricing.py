"""
Two ways to back out a risk-neutral probability that the S&P 500 lands
above/below a given level at expiry -- i.e. the option-market-implied
version of a KXINX bucket probability -- from SPXW 0DTE option quotes:

1. **Model-free (Breeden-Litzenberger)** -- the spline fits live_pricing
   uses (prob_above_from_splines), and prob_above_finite_diff. P(S_T > K) = -dC/dK
   (equivalently 1 - dP/dK from puts), estimated as a discrete finite
   difference of the quoted price-vs-strike curve. No volatility, no
   time-to-expiry, no assumption about how variance accrues through the
   day (which isn't actually uniform in calendar time -- confirmed this
   matters directly: a naive Black-76-with-calendar-time model was found
   to have no clean way to correct for that without a tedious, error-prone
   intraday variance-clock calibration, which this sidesteps entirely by
   never needing T at all).
2. **Black-76** (below) -- forward-based option pricing, used to invert a
   per-strike implied vol from an assumed forward + time-to-expiry. Kept
   here as general, correct, reusable infra (also useful for other 0DTE
   analysis that does want a vol surface), but no longer what the main
   bucket-probability pipeline uses, for the reason above.

Black-76 (forward-based), not spot-based Black-Scholes, because the forward
F is estimated directly from put-call parity on the SPXW quotes themselves
(see forward_from_parity) rather than from a separately-sourced spot price --
using the parity-implied forward means no separate risk-free rate/dividend
input is needed, it's already embedded in F.

d1 = (ln(F/K) + 0.5*sigma^2*T) / (sigma*sqrt(T))
d2 = d1 - sigma*sqrt(T)
call = F*N(d1) - K*N(d2)   (undiscounted -- see note below)
put  = K*N(-d2) - F*N(-d1)

Undiscounted (no e^{-rT} factor) throughout: for same-day 0DTE options T is
at most a few hours, so e^{-rT} ~ 1 to within a few bps and isn't worth
carrying a separate rate input for. N(d2) is used directly as the
risk-neutral P(S_T > K) under the T-forward measure, which is the standard
digital-option interpretation.
"""
import time as _time

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import brentq
from scipy.stats import norm

_MIN_T_YEARS = 30 / (365 * 24 * 3600)  # 30s floor -- see note in implied_vol


def forward_from_parity(call_mid: float, put_mid: float, strike: float) -> float:
    """Put-call parity, undiscounted: F = K + (C - P). Average this across
    several strikes/times for a more robust forward estimate than any single
    pair -- a single strike's C-P is noisy relative to the bid-ask spread."""
    return strike + (call_mid - put_mid)


def black76_price(forward: float, strike: float, t_years: float, sigma: float, right: str) -> float:
    if t_years <= 0 or sigma <= 0:
        intrinsic = max(forward - strike, 0.0) if right == "C" else max(strike - forward, 0.0)
        return intrinsic
    d1 = (np.log(forward / strike) + 0.5 * sigma * sigma * t_years) / (sigma * np.sqrt(t_years))
    d2 = d1 - sigma * np.sqrt(t_years)
    if right == "C":
        return forward * norm.cdf(d1) - strike * norm.cdf(d2)
    return strike * norm.cdf(-d2) - forward * norm.cdf(-d1)


def black76_vega(forward: float, strike: float, t_years: float, sigma: float) -> float:
    if t_years <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(forward / strike) + 0.5 * sigma * sigma * t_years) / (sigma * np.sqrt(t_years))
    return forward * norm.pdf(d1) * np.sqrt(t_years)


def implied_vol(
    price: float, forward: float, strike: float, t_years: float, right: str,
    lo: float = 1e-4, hi: float = 8.0,
) -> float | None:
    """
    Black-76 implied vol via bisection (brentq) on price. Returns None
    (rather than a garbage/extreme value) when:
      - t_years is below _MIN_T_YEARS: vega collapses to ~0 as a 0DTE
        contract nears its expiry (last ~30s of the session), so price is
        almost pure intrinsic value and essentially uninformative about
        vol -- solving here just amplifies bid-ask noise into wild swings.
      - price is outside [intrinsic, forward+strike] (arb-violating quote,
        e.g. a crossed/stale book) -- brentq has no valid root to find.
    """
    if t_years < _MIN_T_YEARS:
        return None
    intrinsic = max(forward - strike, 0.0) if right == "C" else max(strike - forward, 0.0)
    if price <= intrinsic or price >= forward + strike:
        return None

    def f(sigma):
        return black76_price(forward, strike, t_years, sigma, right) - price

    try:
        if f(lo) > 0 or f(hi) < 0:
            return None
        return brentq(f, lo, hi, xtol=1e-6, maxiter=100)
    except (ValueError, RuntimeError):
        return None


def implied_vol_vec(
    price: np.ndarray, forward: np.ndarray, strike: float, t_years: np.ndarray, right: str,
    lo: float = 1e-4, hi: float = 8.0, n_iter: int = 60, price_tol: float = 0.02,
) -> np.ndarray:
    """
    Vectorized counterpart to implied_vol -- same Black-76 model, but
    Newton-Raphson over whole numpy arrays instead of one scipy.brentq call
    per point. Needed because build_iv_curve calls this ~114 times (57
    strikes x 2 sides) rather than once per (strike, time) pair: the
    original per-point brentq version was 4680 grid points x 57 strikes x
    up to 2 sides = ~533k individual scalar solves, confirmed directly to
    take several minutes for one event's grid. This does the same total
    work as ~114 vectorized array passes instead, which is a few orders of
    magnitude fewer Python-level calls.

    strike/right are scalar (this is always called for one strike + one
    side at a time from build_iv_curve); price/forward/t_years are arrays
    of matching shape (one entry per grid timestamp).

    Returns NaN (not a garbage value) wherever implied_vol would have
    returned None: t_years below _MIN_T_YEARS, price outside the
    no-arbitrage band, or Newton failing to converge to price_tol.
    """
    price = np.asarray(price, dtype=float)
    forward = np.asarray(forward, dtype=float)
    t_years = np.asarray(t_years, dtype=float)

    intrinsic = np.maximum(forward - strike, 0.0) if right == "C" else np.maximum(strike - forward, 0.0)
    valid = (
        (t_years >= _MIN_T_YEARS) & ~np.isnan(price) & ~np.isnan(forward)
        & (price > intrinsic) & (price < forward + strike)
    )

    t_safe = np.clip(t_years, 1e-8, None)
    f_safe = np.where(forward > 0, forward, 1.0)
    sigma = np.clip(np.sqrt(2 * np.pi / t_safe) * np.nan_to_num(price) / f_safe, lo, hi)

    with np.errstate(invalid="ignore", divide="ignore"):
        for _ in range(n_iter):
            sqrt_t = np.sqrt(t_safe)
            d1 = (np.log(f_safe / strike) + 0.5 * sigma * sigma * t_safe) / (sigma * sqrt_t)
            d2 = d1 - sigma * sqrt_t
            if right == "C":
                model = f_safe * norm.cdf(d1) - strike * norm.cdf(d2)
            else:
                model = strike * norm.cdf(-d2) - f_safe * norm.cdf(-d1)
            vega = f_safe * norm.pdf(d1) * sqrt_t
            step = (model - np.nan_to_num(price)) / np.maximum(vega, 1e-8)
            sigma = np.clip(sigma - step, lo, hi)

        sqrt_t = np.sqrt(t_safe)
        d1 = (np.log(f_safe / strike) + 0.5 * sigma * sigma * t_safe) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        final_model = (
            f_safe * norm.cdf(d1) - strike * norm.cdf(d2) if right == "C"
            else strike * norm.cdf(-d2) - f_safe * norm.cdf(-d1)
        )
    converged = np.abs(final_model - np.nan_to_num(price)) < price_tol

    return np.where(valid & converged, sigma, np.nan)


def prob_above(forward: float, strike: float, t_years: float, sigma: float) -> float:
    """Risk-neutral P(S_T > strike) under the T-forward measure -- N(d2)."""
    if t_years <= 0 or sigma <= 0:
        return 1.0 if forward > strike else 0.0
    d1 = (np.log(forward / strike) + 0.5 * sigma * sigma * t_years) / (sigma * np.sqrt(t_years))
    d2 = d1 - sigma * np.sqrt(t_years)
    return float(norm.cdf(d2))


def fit_price_splines(strikes: np.ndarray, prices: np.ndarray) -> list:
    """Fits, ONCE PER ROW (time instant), a shape-preserving (PCHIP) cubic
    spline through EVERY listed strike with a real quote at that instant
    -- not just the 2 nearest ones -- for reuse across MANY different
    `level` queries against the SAME (strikes, prices) array via
    prob_above_from_splines / prob_above_bounds_from_splines below,
    instead of a fresh fit per query.

    PCHIP specifically, not a plain natural cubic spline -- confirmed this
    matters for real, noisy bid/ask data: an unconstrained cubic spline can
    overshoot/oscillate between knots when consecutive quotes aren't
    perfectly monotonic (ordinary bid-ask noise, not a real reversal), and
    that oscillation shows up directly in the derivative that this whole
    module extracts a probability from. PCHIP is shape-preserving (won't
    introduce an overshoot the data itself doesn't support) and is the
    standard choice in practice for extracting a smooth risk-neutral
    density from real option quotes for that reason.

    This replaced a plain 2-point secant between whichever 2 listed
    strikes locally bracketed `level` -- confirmed directly on real data
    that a 2-point secant's accuracy degrades badly with only 2 points,
    AND (more seriously) that its result depended discontinuously on which
    side of the nearest listed strike `level` fell: two adjacent Kalshi
    buckets sharing what is economically the same boundary (a cap of
    7649.9999 vs the very next bucket's floor of 7650.0, a mere $0.0001
    apart) landed on OPPOSITE sides of that discrete boundary and got two
    meaningfully different, inconsistent slope estimates for what should
    be nearly the same quantity. A spline through the whole curve doesn't
    have this discontinuity -- evaluating it at 7649.9999 vs 7650.0 gives
    nearly identical derivatives, using the market's full shape rather
    than arbitrarily picking a side.

    Fitting per-level (rather than once here, shared across levels) is
    also what made this slow in practice -- confirmed directly:
    bucket_probabilities_ci queries ~50+ distinct boundary levels per
    event, all against the same handful of call/put mid/bid/ask price
    arrays; refitting from scratch for every level (each a ~7s pass over a
    full day's ~12000-row grid) made one event's worth of levels take many
    minutes. Fitting once here (one ~7s pass per array) and evaluating
    cheaply per level afterward brings the whole computation back down to
    roughly the cost of a HANDFUL of fit passes -- one per distinct price
    array, not one per distinct level -- regardless of how many boundaries
    an event actually has.

    Returns a list of length n_time; each entry is either
    (x_min, x_max, derivative_spline) for a row with >=3 real quotes, or
    None where there wasn't enough data to fit at all.
    """
    strikes = np.asarray(strikes, dtype=float)
    order = np.argsort(strikes)
    strikes = strikes[order]
    n_time = prices.shape[0]
    out = [None] * n_time
    t0 = _time.perf_counter()
    progress_every = 2000
    for i in range(n_time):
        row = prices[i, order]
        valid = ~np.isnan(row)
        if valid.sum() < 3:
            continue
        xs, ys = strikes[valid], row[valid]
        out[i] = (xs[0], xs[-1], PchipInterpolator(xs, ys).derivative())
        if n_time > progress_every and (i + 1) % progress_every == 0:
            print(f"    [pricing] fit_price_splines: {i + 1}/{n_time} rows "
                  f"({_time.perf_counter() - t0:.1f}s elapsed)")
    if n_time > progress_every:
        print(f"    [pricing] fit_price_splines: done, {n_time} rows in {_time.perf_counter() - t0:.1f}s")
    return out


def _eval_spline_slope(splines: list, level: float) -> np.ndarray:
    """dPrice/dStrike at `level` from ALREADY-FITTED splines (see
    fit_price_splines) -- cheap, no refitting. NaN wherever that instant's
    fit is None (not enough data) or `level` falls outside its own quoted
    range (extrapolation isn't reliable)."""
    n_time = len(splines)
    out = np.full(n_time, np.nan)
    for i, entry in enumerate(splines):
        if entry is None:
            continue
        x_lo, x_hi, deriv = entry
        if level < x_lo or level > x_hi:
            continue
        out[i] = float(deriv(level))
    return out


def prob_above_from_splines(splines: list, level: float, is_call: bool) -> np.ndarray:
    """prob_above_finite_diff's P(S_T > level), evaluated against
    ALREADY-FITTED splines (see fit_price_splines) instead of fitting
    fresh -- the fast path bucket_probabilities_ci uses when it needs many
    different levels against the same price array. P(S_T > K) = -dC/dK
    for calls, 1 - dP/dK for puts (see prob_above_finite_diff's own
    docstring on the undiscounted 0DTE approximation)."""
    slope = _eval_spline_slope(splines, level)
    return -slope if is_call else 1.0 - slope


def clip_band_preserving_width(lo, mid, hi):
    """Clip a (lo, mid, hi) probability band into [0, 1] by TRANSLATING it,
    not by squashing it.

    A plain elementwise np.clip collapses the band to zero width whenever the
    whole raw band falls outside [0, 1] -- e.g. a raw [-0.03, -0.01] becomes
    [0, 0] and reports perfect certainty. That happens constantly: the
    Breeden-Litzenberger finite difference goes slightly negative on deep-OTM
    strikes where it's mostly noise, and on real data 64% of grid points came
    back with a zero-width band. Downstream that sets ci_edge to 0 and leaves
    the strategy's only protection as constant_edge + fee, so "we measured
    almost nothing" was indistinguishable from "we are certain".

    Translating keeps the measured width: raw [-0.03, -0.01] becomes
    [0, 0.02] ("at most 0.02"), raw [0.98, 1.05] becomes [0.93, 1.0]. A band
    already inside [0, 1] is untouched. Width is itself capped at 1.
    """
    lo = np.asarray(lo, dtype=float)
    mid = np.asarray(mid, dtype=float)
    hi = np.asarray(hi, dtype=float)
    width = np.clip(hi - lo, 0.0, 1.0)
    lo_c = np.clip(lo, 0.0, 1.0 - width)
    hi_c = lo_c + width
    return lo_c, np.clip(mid, lo_c, hi_c), hi_c


# Floor on a level's band width when weighting the monotone projection: one
# Kalshi tick, so a zero-width level can't get unbounded weight.
MONOTONE_MIN_WIDTH = 0.01


def _pava_non_increasing(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least-squares non-increasing fit (pool adjacent violators)."""
    vals, wts, cnts = [], [], []
    for v, wt in zip(y, w):
        vals.append(v); wts.append(wt); cnts.append(1)
        while len(vals) > 1 and vals[-2] < vals[-1]:
            v2, w2, c2 = vals.pop(), wts.pop(), cnts.pop()
            tw = wts[-1] + w2
            vals[-1] = (vals[-1] * wts[-1] + v2 * w2) / tw
            wts[-1] = tw
            cnts[-1] += c2
    return np.repeat(vals, cnts)


def monotone_prob_above(probs: np.ndarray, widths: np.ndarray) -> np.ndarray:
    """Project P(S_T > K), given at ascending K, onto non-increasing values in
    [0, 1]. NaN levels are skipped and stay NaN.

    Without this, B-L noise makes some P(>K) rise with K; the bucket built on
    that pair goes negative and its clip to 0 adds mass (KXINX bucket sums
    hit 1.40 at 15:50 on 2026-08-27 while the raw sum was 0.9999). Weighted
    by 1/width^2 so a noisy edge-of-scope level (live KXINXU 2026-10-12:
    stuck at 0.64, band 0.30, next to tight 1.00s) yields to its neighbours
    instead of dragging them down."""
    probs = np.asarray(probs, dtype=float)
    out = np.full(probs.shape, np.nan)
    ok = ~np.isnan(probs) & ~np.isnan(widths)
    if ok.sum() == 0:
        return out
    w = 1.0 / np.maximum(np.asarray(widths, dtype=float)[ok], MONOTONE_MIN_WIDTH) ** 2
    out[ok] = np.clip(_pava_non_increasing(probs[ok], w), 0.0, 1.0)
    return out


def nan_safe_min_max(vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Column-wise (min, max) ignoring NaNs, with NaN for any column that is
    ENTIRELY NaN.

    Same result as np.nanmin/np.nanmax, minus the "All-NaN slice
    encountered" RuntimeWarning those emit for an all-NaN column. That
    column-is-all-NaN case is not an error here and not rare: it's exactly
    what happens when `level` falls outside the quoted strike range on both
    the bid and ask curves, or when neither side had enough quotes to fit at
    all -- routine on thin/illiquid strikes and constant overnight, where it
    spammed the live log several times a second.

    NaN is the correct answer for it -- "we have no estimate here", which
    callers already handle by skipping the bucket (see
    live_pricing.compute_live_bucket_probs's own NaN check). This computes
    that explicitly rather than suppressing the warning, so the
    all-NaN-means-unknown contract is visible in the code instead of being
    an incidental property of numpy's reduction behaviour.
    """
    all_nan = np.isnan(vals).all(axis=0)
    lo = np.full(vals.shape[1], np.nan)
    hi = np.full(vals.shape[1], np.nan)
    good = ~all_nan
    if good.any():
        lo[good] = np.nanmin(vals[:, good], axis=0)
        hi[good] = np.nanmax(vals[:, good], axis=0)
    return lo, hi


def prob_above_bounds_from_splines(
    splines_bid: list, splines_ask: list, level: float, is_call: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """prob_above_finite_diff_bounds's [lo, hi] band, evaluated against
    ALREADY-FITTED bid-side/ask-side splines (see fit_price_splines)
    instead of fitting fresh -- a genuinely wide or uncertain market (bid
    and ask curves disagreeing on shape) shows up as a wide band directly,
    no separate noise/anomaly model needed.

    Returns NaN at any instant where neither side yields a usable slope --
    see nan_safe_min_max."""
    slope_bid = _eval_spline_slope(splines_bid, level)
    slope_ask = _eval_spline_slope(splines_ask, level)
    if is_call:
        vals = np.vstack([-slope_bid, -slope_ask])
    else:
        vals = np.vstack([1.0 - slope_bid, 1.0 - slope_ask])
    return nan_safe_min_max(vals)


def prob_above_finite_diff(level: float, strikes: np.ndarray, prices: np.ndarray, is_call: bool) -> np.ndarray:
    """
    Model-free (Breeden-Litzenberger) risk-neutral P(S_T > level), from ONE
    side of the chain: P(S_T > K) = -dC/dK for calls, 1 - dP/dK for puts
    (both undiscounted -- rate*T is negligible for a 0DTE option, same
    approximation black76_price already makes). No volatility, no
    time-to-expiry, anywhere: this is purely the slope of the *quoted
    price-vs-strike curve* at each instant (see fit_price_splines) -- whatever
    the market's true (possibly non-uniform, e.g. intraday-seasonal)
    variance clock is, it's already embedded in the relative prices of
    options at different strikes right now, so there's no need to model or
    assume it.

    A one-shot convenience wrapper (fit + evaluate a SINGLE level) --
    callers that need MANY levels against the same `prices` array (e.g.
    bucket_probabilities_ci) should call fit_price_splines once and then
    prob_above_from_splines per level instead, to avoid refitting.

    `prices`: (n_time, n_strikes) array aligned to `strikes` (one column
    per strike -- e.g. a wide mid-price DataFrame's .to_numpy()). Returns
    an (n_time,) array; NaN wherever that instant doesn't have enough real
    quotes to fit a curve, or `level` is outside its quoted range.
    """
    if len(strikes) < 2:
        return np.full(prices.shape[0], np.nan)
    return prob_above_from_splines(fit_price_splines(strikes, prices), level, is_call)


def prob_above_finite_diff_bounds(
    level: float, strikes: np.ndarray, price_bid: np.ndarray, price_ask: np.ndarray, is_call: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """
    [lo, hi] bounds on the same Breeden-Litzenberger P(S_T > level) as
    prob_above_finite_diff, from the bid-side and ask-side curves' own
    spline slopes. A one-shot convenience wrapper -- see
    prob_above_finite_diff's own docstring on using fit_price_splines +
    prob_above_bounds_from_splines directly instead when querying many
    levels against the same price arrays.

    `price_bid`/`price_ask`: (n_time, n_strikes) arrays aligned to
    `strikes`, same shape convention as prob_above_finite_diff's `prices`.
    Returns (lo, hi), each (n_time,).
    """
    if len(strikes) < 2:
        shape = price_bid.shape[0]
        return np.full(shape, np.nan), np.full(shape, np.nan)
    return prob_above_bounds_from_splines(
        fit_price_splines(strikes, price_bid), fit_price_splines(strikes, price_ask), level, is_call)


def nearest_iv(target_strike: float, strikes: np.ndarray, ivs: np.ndarray) -> float | None:
    """IV of whichever listed strike (with a valid, non-NaN solved IV) sits
    closest to target_strike -- no interpolation. Kalshi bucket boundaries
    are Kalshi's own construct, not independently-priced strikes (e.g. a
    cap of 7799.9999 just means "up to but not including 7800"), so a
    boundary within a cent of a listed strike should just use that strike's
    own directly-solved IV rather than blend in a neighboring strike's.
    Returns None if there's nothing usable to pick from at all."""
    mask = ~np.isnan(ivs)
    if mask.sum() == 0:
        return None
    s, v = strikes[mask], ivs[mask]
    return float(v[np.argmin(np.abs(s - target_strike))])
