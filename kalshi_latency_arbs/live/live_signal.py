"""
Live, single-snapshot counterpart to latency_arbs_functions.bucket_probabilities_ci
-- same Breeden-Litzenberger math, same floor/cap combination arithmetic
(transcribed verbatim below), but evaluated against ONE current live
quote snapshot instead of a whole day's precomputed grid. Pure
computation, no I/O: callers (live_ib_feed.py) own the actual
network/state-tracking side.

Deliberately NOT bucket_probabilities_ci itself: that function's
grid/caching/should_abort machinery exists to amortize 6 spline fits
across thousands of grid rows in one backtest pass -- the wrong shape for
"refit on every incremental live update, as fast as possible." Reusing
pricing.fit_price_splines/prob_above_from_splines/
prob_above_bounds_from_splines directly on a single (1, n_strikes) row is
already just as correct (those functions were already row-count-agnostic)
and avoids paying for grid bookkeeping this doesn't need.
"""
import os
import sys

import numpy as np

_ARBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ARBS_DIR not in sys.path:
    sys.path.insert(0, _ARBS_DIR)

import pricing


def compute_live_bucket_probs(
    markets: list[dict],
    call_strikes: np.ndarray, call_mid_row: np.ndarray, call_bid_row: np.ndarray, call_ask_row: np.ndarray,
    put_strikes: np.ndarray, put_mid_row: np.ndarray, put_bid_row: np.ndarray, put_ask_row: np.ndarray,
) -> dict[str, tuple[float, float, float]]:
    """One live instant's (prob_lo, prob_mid, prob_hi) per Kalshi bucket in
    `markets` -- same union-of-call-and-put-bounds definition as
    bucket_probabilities_ci (see that function's own docstring for why
    union, not average, for the CI). `*_row` arrays are shape (n_strikes,)
    -- ONE row, not a grid -- already aligned to their matching `*_strikes`
    array (sorted ascending, matching the columns of whatever built the
    row). Rows may be all-NaN for a side with fewer than 2 strikes (no
    finite-difference possible); handled the same way as the batch path
    (nanmean/nanmin/nanmax skip NaNs elementwise).

    Returns {ticker: (prob_lo, prob_mid, prob_hi)} -- clipped to [0, 1],
    same as bucket_probabilities_ci.
    """
    has_calls = call_strikes.size >= 2
    has_puts = put_strikes.size >= 2

    call_mid_splines = pricing.fit_price_splines(call_strikes, call_mid_row[None, :]) if has_calls else None
    call_bid_splines = pricing.fit_price_splines(call_strikes, call_bid_row[None, :]) if has_calls else None
    call_ask_splines = pricing.fit_price_splines(call_strikes, call_ask_row[None, :]) if has_calls else None
    put_mid_splines = pricing.fit_price_splines(put_strikes, put_mid_row[None, :]) if has_puts else None
    put_bid_splines = pricing.fit_price_splines(put_strikes, put_bid_row[None, :]) if has_puts else None
    put_ask_splines = pricing.fit_price_splines(put_strikes, put_ask_row[None, :]) if has_puts else None

    def call_above(level: float) -> float:
        if not has_calls:
            return float("nan")
        return float(pricing.prob_above_from_splines(call_mid_splines, level, is_call=True)[0])

    def put_above(level: float) -> float:
        if not has_puts:
            return float("nan")
        return float(pricing.prob_above_from_splines(put_mid_splines, level, is_call=False)[0])

    def mid_above(level: float) -> float:
        # Explicit all-NaN check rather than a bare np.nanmean, which emits
        # "Mean of empty slice" when NEITHER side has a usable estimate at
        # this level -- routine on thin strikes (and constant overnight),
        # where it spammed the live log. NaN is the right answer there and
        # callers already skip on it; see pricing.nan_safe_min_max.
        vals = [v for v in (call_above(level), put_above(level)) if not np.isnan(v)]
        return float(np.mean(vals)) if vals else float("nan")

    def bounds_above(level: float) -> tuple[float, float]:
        los, his = [], []
        if has_calls:
            lo, hi = pricing.prob_above_bounds_from_splines(call_bid_splines, call_ask_splines, level, is_call=True)
            los.append(float(lo[0]))
            his.append(float(hi[0]))
        if has_puts:
            lo, hi = pricing.prob_above_bounds_from_splines(put_bid_splines, put_ask_splines, level, is_call=False)
            los.append(float(lo[0]))
            his.append(float(hi[0]))
        los = [v for v in los if not np.isnan(v)]
        his = [v for v in his if not np.isnan(v)]
        if not los or not his:
            return float("nan"), float("nan")
        return float(min(los)), float(max(his))

    # Same per-level memoization as bucket_probabilities_ci -- multiple
    # buckets can share a boundary (one's cap is the next one's floor).
    mid_cache: dict[float, float] = {}
    bounds_cache: dict[float, tuple[float, float]] = {}

    def cached_mid(level):
        if level not in mid_cache:
            mid_cache[level] = mid_above(level)
        return mid_cache[level]

    def cached_bounds(level):
        if level not in bounds_cache:
            bounds_cache[level] = bounds_above(level)
        return bounds_cache[level]

    out: dict[str, tuple[float, float, float]] = {}
    for m in markets:
        floor = float(m["floor_strike"]) if m.get("floor_strike") is not None else None
        cap = float(m["cap_strike"]) if m.get("cap_strike") is not None else None
        if floor is not None and cap is not None:
            prob_mid = cached_mid(floor) - cached_mid(cap)
            floor_lo, floor_hi = cached_bounds(floor)
            cap_lo, cap_hi = cached_bounds(cap)
            prob_hi = floor_hi - cap_lo
            prob_lo = floor_lo - cap_hi
        elif floor is not None:
            prob_mid = cached_mid(floor)
            prob_lo, prob_hi = cached_bounds(floor)
        elif cap is not None:
            prob_mid = 1.0 - cached_mid(cap)
            cap_lo, cap_hi = cached_bounds(cap)
            prob_lo, prob_hi = 1.0 - cap_hi, 1.0 - cap_lo
        else:
            continue
        if np.isnan(prob_mid) or np.isnan(prob_lo) or np.isnan(prob_hi):
            continue
        # Same band-preserving clip as the batch path -- see
        # pricing.clip_band_preserving_width.
        lo_c, mid_c, hi_c = pricing.clip_band_preserving_width(prob_lo, prob_mid, prob_hi)
        out[m["ticker"]] = (float(lo_c), float(mid_c), float(hi_c))
    return out


def pick_atm_signal(bucket_probs: dict[str, tuple[float, float, float]]) -> dict | None:
    """Whichever bucket's own prob_mid sits closest to 0.5 right now --
    the live counterpart of precompute_atm_signal's per-grid-tick
    `idxmin(axis=1)` over |prob_mid - 0.5|, applied to one instant's dict
    instead of a DataFrame row. Returns {"ticker","prob","prob_lo","prob_hi"}
    (matching SPOptionsLagQuotingStrategy._on_signal's expected shape) or
    None if there's nothing to pick from yet (e.g. no live quotes received
    for any bracketing strike so far)."""
    if not bucket_probs:
        return None
    ticker = min(bucket_probs, key=lambda t: abs(bucket_probs[t][1] - 0.5))
    lo, mid, hi = bucket_probs[ticker]
    return {"ticker": ticker, "prob": mid, "prob_lo": lo, "prob_hi": hi}
