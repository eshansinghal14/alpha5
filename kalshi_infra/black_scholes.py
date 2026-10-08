"""
Implied spot analysis for Kalshi temperature binary / range options.

KXHIGHLAX market structure
---------------------------
Each daily event lists a mix of "between" (range bucket, e.g. 78-79 inclusive
both ends), "greater" (strict upper tail, e.g. > 85), and "less" (strict lower
tail, e.g. < 78) markets -- see predexon_functions.load_predexon_long_df for
how these are parsed off each ticker, and strike_type disambiguates them
(Kalshi's own floor_strike/cap_strike fields alone aren't enough: they don't
distinguish a "between" bucket from a "less" tail sharing the same cap).

Settlement (NWS Climatological Report daily high) is a whole-degree integer,
and every strike is inclusive of the named integer(s) -- see
_continuity_corrected_bounds for the exact ±0.5 correction this requires
before treating T as a continuous Normal, and why skipping it makes correctly
-priced markets look internally inconsistent.

To back out the implied spot (expected high temperature), each market's own
model-implied probability (bucket_model_prob_and_grad) is fit directly
against its own [bid, ask] band -- see fit_normal_to_bucket_bid_ask for the
solve and the comment block above it for why this fits each market
independently rather than via a cross-strike cumulative survival function.
"""

import datetime
import math
import os

import numpy as np
import pandas as pd

import predexon_functions
from scipy.special import ndtr

_INV_SQRT_2PI = 0.3989422804014327  # 1/sqrt(2*pi)
_SQRT2 = 1.4142135623730951


def _std_normal_pdf(z):
    """
    Standard normal PDF via np.exp -- works on a scalar OR a numpy array (np.exp
    handles both transparently), unlike _ndpdf's math.exp (scalar-only, used in
    the plain-Python per-market hot loop instead -- see its own docstring).
    Paired with scipy.special.ndtr (the CDF counterpart) in bucket_model_prob_
    and_grad, which needs BOTH: array support (plotting.wing_greeks_timeseries/
    plot_vol_skew_diagnostic call it vectorized across a whole timeseries) AND
    to be fast at high call volume (fit_local_sigma_smile's bisection search
    calls it ~20-30 times per market per snapshot -- see ndtr's own docstring
    on why scipy.stats.norm.cdf/pdf specifically cannot serve both needs).
    """
    return np.exp(-0.5 * z * z) * _INV_SQRT_2PI


def _ndtr_scalar(z):
    """Scalar standard normal CDF via math.erf. Used in
    fit_normal_to_bucket_bid_ask's per-market hot path (called ~100k times/day,
    over ~5-6 markets, in plain Python -- not numpy, see
    _bucket_residuals_and_jac's docstring for why) instead of scipy.stats.norm.cdf,
    whose generic distribution-object dispatch costs ~15-30x more per call --
    negligible for occasional use elsewhere in this module, but not at this
    call volume."""
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


def _ndpdf(z):
    """Scalar standard normal PDF via math.exp -- see _ndtr_scalar's docstring
    for why this bypasses scipy.stats.norm.pdf."""
    return math.exp(-0.5 * z * z) * _INV_SQRT_2PI


# ---------------------------------------------------------------------------
# Fitting within bid/ask bands, per market, directly (no cumulative SF)
# ---------------------------------------------------------------------------
#
# A per-strike point price (WAP/mid/touch) forces a choice for illiquid/
# one-sided books that invents information that isn't there. Fitting each
# market's OWN price directly against its own model-implied probability --
# no cross-strike summation at all -- avoids that:
#   range (floor, cap):   P(floor <= T < cap) = Phi((cap-mu)/sigma) - Phi((floor-mu)/sigma)
#   upper tail (>= floor): P(T >= floor)        = Phi((mu-floor)/sigma)
#   lower tail (< floor):  P(T < floor)         = Phi((floor-mu)/sigma)
# A range bucket's probability is a *difference* of two Gaussian CDFs, which
# is nonlinear in (mu, sigma) -- unlike the single-CDF cumulative construction,
# this can't reduce to a linear program, so it's fit via a smooth, gradient-
# guided nonlinear solve (see fit_normal_to_bucket_bid_ask) instead of an LP.


def _continuity_corrected_bounds(floor: float, cap: float | None, strike_type: str) -> tuple[float, float | None]:
    """
    Kalshi settles KXHIGHLAX off the NWS Climatological Report, which reports
    the daily high as a whole-degree integer, and every strike is inclusive
    of the integer(s) it names -- confirmed directly from each market's own
    rules_primary text: a 'between' bucket with floor_strike=78/cap_strike=79
    reads "is between 78-79", i.e. inclusive-inclusive over the two integers
    {78, 79}; a 'greater' market (floor_strike=85) reads "is greater than 85"
    (strict), i.e. {86, 87, ...}; a 'less' market (cap_strike=78) reads "is
    less than 78" (strict), i.e. {..., 76, 77}.

    To model T with a continuous Normal, every named integer k stands for the
    continuous interval [k-0.5, k+0.5) (the standard rounding continuity
    correction). Applying that here is what makes adjacent buckets tile with
    NO gap: 'between' 78-79 becomes continuous [77.5, 79.5), which is exactly
    where the next bucket's 80-81 -> [79.5, 81.5) picks up. Skipping this
    correction (treating floor/cap as already-continuous cutoffs, as this
    code used to) silently invents fake 1-degree gaps between every bucket
    and made real, correctly-priced market data look internally inconsistent
    under a continuous model when it never was -- see git history / session
    notes on the "impossible fit" investigation this fixed.
    """
    if strike_type == "between":
        return floor - 0.5, cap + 0.5
    elif strike_type == "greater":
        return floor + 0.5, None
    elif strike_type == "less":
        return floor - 0.5, None
    else:
        raise ValueError(f"unknown strike_type: {strike_type!r}")


def bucket_model_prob_and_grad(
    mu: float, sigma: float, floor: float, cap: float | None, strike_type: str
) -> tuple[float, float, float]:
    """
    Model-implied probability for one market under T ~ N(mu, sigma^2), plus its
    exact partial derivatives w.r.t. mu and sigma. Scalar/single-market
    convenience version (e.g. for plotting one strike's model curve); the fit
    itself (fit_normal_to_bucket_bid_ask) uses its own vectorized-across-
    markets version of this same math for speed.

    strike_type: 'between' (range bucket, inclusive of both named integers),
    'greater' (upper tail, strictly > floor), or 'less' (lower tail, strictly
    < floor). floor/cap are passed in as Kalshi's raw integer strikes;
    _continuity_corrected_bounds converts them to continuous cutoffs before
    use (see its docstring for why that conversion matters).

    sigma=0 (a genuine point mass, e.g. from fit_normal_to_bucket_bid_ask's
    near-certain-outcome short-circuit) divides by zero here -- callers must
    special-case it themselves (p is 1 if mu falls in the bucket else 0, with
    no meaningful curve to plot).

    Uses scipy.special.ndtr/_std_normal_pdf, not scipy.stats.norm.cdf/pdf --
    the latter's generic rv_continuous dispatch (argument validation, support-
    mask checks, broadcast_arrays, ...) measured as ~92% of this function's
    OWN caller's total runtime once fit_local_sigma_smile's bisection search
    started calling it at real volume (confirmed via cProfile: a 20-minute,
    1200-row window took 37s total, of which fit_normal_to_bucket_bid_ask's
    own actual model fit -- the real work -- was only 2.3s; the rest was
    ndtr-equivalent-but-slow scipy.stats calls this function made, extrapolating
    to ~50min/day at 1s granularity, not the "tens of seconds/day" this module's
    own docs assumed elsewhere). Same fix _ndtr_scalar/_ndpdf already apply to
    the primary per-market fit loop, for the same documented reason -- this
    function just couldn't reuse those two directly (math.erf/math.exp are
    scalar-only, and this function is also called with numpy arrays -- see
    plotting.wing_greeks_timeseries/plot_vol_skew_diagnostic).

    Returns (p, dp_dmu, dp_dsigma).
    """
    floor, cap = _continuity_corrected_bounds(floor, cap, strike_type)
    if strike_type == "between":
        zc = (cap - mu) / sigma
        zf = (floor - mu) / sigma
        p = ndtr(zc) - ndtr(zf)
        dp_dmu = (_std_normal_pdf(zf) - _std_normal_pdf(zc)) / sigma
        dp_dsigma = (_std_normal_pdf(zf) * zf - _std_normal_pdf(zc) * zc) / sigma
    elif strike_type == "greater":
        z = (mu - floor) / sigma
        p = ndtr(z)
        dp_dmu = _std_normal_pdf(z) / sigma
        dp_dsigma = -_std_normal_pdf(z) * z / sigma
    elif strike_type == "less":
        zf = (floor - mu) / sigma
        p = ndtr(zf)
        dp_dmu = -_std_normal_pdf(zf) / sigma
        dp_dsigma = -_std_normal_pdf(zf) * zf / sigma
    else:
        raise ValueError(f"unknown strike_type: {strike_type!r}")
    return p, dp_dmu, dp_dsigma


def _bucket_residuals_and_jac(mu, sigma, markets, bids, asks, anchor_targets, sqrt_anchor_w, midprice_mask, midprices):
    """
    Plain-Python (not numpy) per-market loop: for the ~5-6 markets/event this
    fits, numpy's fixed per-array-op dispatch overhead exceeds the actual
    math -- measured ~25x faster as scalar math.erf/exp than as numpy calls
    over arrays this tiny (see fit_normal_to_bucket_bid_ask's docstring).

    Two residual styles, chosen per market via midprice_mask:
      - Genuinely two-sided markets (mask False): the usual band-violation
        hinge residual (0 whenever bid <= p <= ask), plus an anchor residual
        if this market has a WAP/mid anchor target (see fit_normal_to_bucket_
        bid_ask's docstring).
      - One-sided markets (mask True, i.e. bid_depth<=0 or ask_depth<=0 --
        there's no real resting order on one side, so bid/ask is really just
        "0" or "1" by convention, not a meaningful band): fit directly to the
        raw midprice at full weight instead of the hinge. A market whose
        [0, 0.01]-style band is satisfied by *any* (mu, sigma) contributes
        zero gradient once inside it -- with several such markets in one fit,
        that can leave too few informative constraints, letting a small
        anchor term on the remaining two-sided markets fully determine
        (mu, sigma) on its own and land on a degenerate near-delta-function
        solution (see session notes on the JUL31 case this fixed). A direct
        midprice fit keeps every market contributing real gradient signal.

    Returns (r, Jmu, Jsig, band_cost): r has one residual per market (hinge or
    midprice, per midprice_mask), plus one anchor residual per two-sided
    market with an anchor target (None entries skipped) appended after.
    band_cost is the sum of squared per-market residuals ONLY (excluding the
    anchor terms) -- this is the signal fit_normal_to_bucket_bid_ask reports
    back as a data-quality/fit-trust indicator, since the anchor term is just
    a tie-breaker among otherwise-valid solutions and its residual doesn't
    indicate genuinely unfittable data the way a large band/midprice
    violation does.
    """
    r, Jmu, Jsig = [], [], []
    band_cost = 0.0
    for i, (floor, cap, strike_type) in enumerate(markets):
        floor, cap = _continuity_corrected_bounds(floor, cap, strike_type)
        if strike_type == "between":
            zc = (cap - mu) / sigma
            zf = (floor - mu) / sigma
            pdf_zc, pdf_zf = _ndpdf(zc), _ndpdf(zf)
            p = _ndtr_scalar(zc) - _ndtr_scalar(zf)
            dp_dmu = (pdf_zf - pdf_zc) / sigma
            dp_dsigma = (pdf_zf * zf - pdf_zc * zc) / sigma
        elif strike_type == "greater":
            z = (mu - floor) / sigma
            pdf_z = _ndpdf(z)
            p = _ndtr_scalar(z)
            dp_dmu = pdf_z / sigma
            dp_dsigma = -pdf_z * z / sigma
        else:  # "less"
            zf = (floor - mu) / sigma
            pdf_zf = _ndpdf(zf)
            p = _ndtr_scalar(zf)
            dp_dmu = -pdf_zf / sigma
            dp_dsigma = -pdf_zf * zf / sigma

        if midprice_mask[i]:
            d, dmu_i, dsig_i = p - midprices[i], dp_dmu, dp_dsigma
        else:
            bid, ask = bids[i], asks[i]
            if p < bid:
                d, dmu_i, dsig_i = p - bid, dp_dmu, dp_dsigma
            elif p > ask:
                d, dmu_i, dsig_i = p - ask, dp_dmu, dp_dsigma
            else:
                d, dmu_i, dsig_i = 0.0, 0.0, 0.0
        r.append(d)
        Jmu.append(dmu_i)
        Jsig.append(dsig_i)
        band_cost += d * d

        target = anchor_targets[i]
        if target is not None:
            r.append(sqrt_anchor_w * (p - target))
            Jmu.append(sqrt_anchor_w * dp_dmu)
            Jsig.append(sqrt_anchor_w * dp_dsigma)

    return r, Jmu, Jsig, band_cost


def fit_normal_to_bucket_bid_ask(
    markets: list[tuple[float, float | None, str]],
    bids: list[float],
    asks: list[float],
    bid_depths: list[float] | None = None,
    ask_depths: list[float] | None = None,
    sigma_min: float = 0.05,
    anchor_weight: float = 0.05,
    max_iter: int = 15,
    certain_bid: float = 0.99,
    certain_ask: float = 0.01,
    warm_start: tuple[float, float] | None = None,
    lambda_min_threshold: float = 0.001,
) -> tuple[float | None, float | None, float | None]:
    """Fit (mu, sigma) for T ~ N(mu, sigma^2) so each market's model-implied
    probability stays inside its own [bid, ask].

    markets: (floor, cap, strike_type) per market, strike_type
        'between'/'greater'/'less'.

    Minimizes squared band-violations with a hand-rolled Levenberg-Marquardt
    solve. Not scipy: a range bucket's probability is a difference of two
    Gaussian CDFs, so this isn't linear-programmable, and at ~100k fits/day
    scipy's dispatch plus norm.cdf/pdf cost 15-50ms per fit vs ~0.3ms for plain
    Python scalar math (numpy is ~10ms -- arrays this small are all dispatch
    overhead). Gradients are analytic: numerical gradients on a hinge loss read
    as ~0 near a kink and falsely converge. Two starting points
    (method-of-moments off mid prices, and a strike-spread-only fallback);
    lowest cost wins.

    A well-fitting (mu, sigma) may not exist. Kalshi skips bucket ranges, and if
    the listed buckets price to ~1 on their own, no continuous normal can be ~0
    on a gap and large beside it. That's not solver failure -- it shows up as a
    large `band_cost`, mostly in wide-spread moments.

    warm_start: optional (mu, sigma), typically the previous timestep's fit,
    added as an extra LM start (floored at sigma_min). Guards a different
    failure: when only one or two markets carry signal, what's constrained is
    the z-score at that strike, not (mu, sigma) separately, so wildly different
    pairs sit on the same near-zero-cost ridge. Anchoring near the previous
    point keeps the solve on the sane end.

    lambda_min (returned): smallest eigenvalue of J^T J at the converged point
    -- how tightly any direction is pinned; small means the ridge above.
    Informational only, it no longer gates the return, and lambda_min_threshold
    is accepted but unused.

    The one rejection: if a 'less'/'greater' market's fitted probability exceeds
    0.5 (an open-ended tail priced more likely than not), (mu, sigma) is
    withheld as (None, None) with band_cost/lambda_min still returned. Not
    applied to 'between', where >0.5 near the peak is normal.

    bid_depths/ask_depths (optional): two-sided markets (both depths > 0) keep
    the band hinge plus a small `anchor_weight` pull toward their WAP/midpoint,
    which only breaks ties and never overrides a band. One-sided markets have no
    meaningful band, so they're fit directly to their midprice at full weight --
    otherwise they contribute zero gradient and the anchor term alone can drive
    the fit to a near-delta solution. Without depths, every market gets the
    plain hinge.

    certain_bid/certain_ask: when one market's bid >= certain_bid and every
    other ask <= certain_ask, the market has called the outcome -- skip the LM
    solve and return that bucket's location at sigma=0.0 (a true point mass),
    band_cost 0. Near expiry the cost surface otherwise goes flat along a ridge
    leaving sigma too high, and since iv_scaled_to_day divides by sqrt(days
    remaining) that inflates into an apparent vol increase into expiry.

    On that path mu always reuses warm_start's mu when available. Any mu inside
    the certain region fits every market equally, so snapping to a convention
    point would bake in a fake jump -- and downstream realized-vol calculations
    diff consecutive mu values, counting it as real variance. The
    midpoint/floor+-1 convention only applies with no warm_start.

    sigma=0.0 (not sigma_min) is unique to that path. Callers dividing by sigma
    must guard for it; compute_implied_spot_timeseries and everything downstream
    divide only by sqrt(time).
    """
    n = len(markets)
    if n < 2:
        return None, None, None, None

    for i, (floor, cap, strike_type) in enumerate(markets):
        if bids[i] < certain_bid:
            continue
        if all(asks[j] <= certain_ask for j in range(n) if j != i):
            if warm_start is not None:
                mu = warm_start[0]
            else:
                mu = (floor + cap) / 2.0 if strike_type == "between" else floor + (1.0 if strike_type == "greater" else -1.0)
            return float(mu), 0.0, 0.0, float('inf')

    anchor_targets = [None] * n
    midprice_mask = [False] * n
    midprices = [0.0] * n
    if bid_depths is not None and ask_depths is not None:
        for i in range(n):
            bd, ad = bid_depths[i], ask_depths[i]
            if bd > 0 and ad > 0:
                spread = asks[i] - bids[i]
                if spread <= 0.01 + 1e-9:
                    anchor_targets[i] = (bids[i] * ad + asks[i] * bd) / (bd + ad)
                else:
                    anchor_targets[i] = (bids[i] + asks[i]) / 2.0
            else:
                midprice_mask[i] = True
                midprices[i] = (bids[i] + asks[i]) / 2.0
    sqrt_anchor_w = anchor_weight ** 0.5

    def lm_fit(mu, sigma):
        lam = 1e-2
        r, Jmu, Jsig, band_cost = _bucket_residuals_and_jac(mu, sigma, markets, bids, asks, anchor_targets, sqrt_anchor_w, midprice_mask, midprices)
        cost = sum(v * v for v in r)
        for _ in range(max_iter):
            A00 = A01 = A11 = b0 = b1 = 0.0
            for jm, js, ri in zip(Jmu, Jsig, r):
                A00 += jm * jm
                A01 += jm * js
                A11 += js * js
                b0 += jm * ri
                b1 += js * ri
            a00 = A00 * (1.0 + lam) if A00 > 1e-12 else A00 + lam * 1e-12
            a11 = A11 * (1.0 + lam) if A11 > 1e-12 else A11 + lam * 1e-12
            det = a00 * a11 - A01 * A01
            if abs(det) < 1e-300:
                break
            rhs0, rhs1 = -b0, -b1
            delta_mu = (rhs0 * a11 - A01 * rhs1) / det
            delta_sigma = (a00 * rhs1 - A01 * rhs0) / det
            mu_new = mu + delta_mu
            sigma_new = sigma + delta_sigma
            if sigma_new < sigma_min:
                sigma_new = sigma_min
            r2, Jmu2, Jsig2, band_cost2 = _bucket_residuals_and_jac(mu_new, sigma_new, markets, bids, asks, anchor_targets, sqrt_anchor_w, midprice_mask, midprices)
            cost2 = sum(v * v for v in r2)
            if cost2 < cost:
                improved = cost - cost2
                mu, sigma, r, Jmu, Jsig, cost, band_cost = mu_new, sigma_new, r2, Jmu2, Jsig2, cost2, band_cost2
                lam *= 0.4
                if improved < 1e-10:
                    break
            else:
                lam *= 3.0
                if lam > 1e8:
                    break
        # lambda_min of [[A00,A01],[A01,A11]] -- the UNDAMPED Gauss-Newton
        # curvature (J^T J, no lam inflation) AT THE FINAL RETURNED (mu,
        # sigma) specifically: recomputed explicitly from Jmu/Jsig/r here
        # rather than reused from inside the loop above, since whichever
        # iteration accepts the LAST step leaves the loop's own A00/A01/A11
        # one step stale (computed from the pre-step point, not post-step) --
        # see lambda_min_threshold's docstring for what this measures.
        A00 = A01 = A11 = 0.0
        for jm, js in zip(Jmu, Jsig):
            A00 += jm * jm
            A01 += jm * js
            A11 += js * js
        trace = A00 + A11
        disc = max(trace * trace - 4.0 * (A00 * A11 - A01 * A01), 0.0)
        lambda_min = (trace - disc ** 0.5) / 2.0
        return mu, sigma, cost, band_cost, lambda_min

    # Primary start: method-of-moments off the markets' own mid prices (treated as
    # unnormalized probability weight at each market's representative location --
    # bucket center for "between", one degree past the threshold for a tail).
    # This lands far closer to the true optimum than a strike-spread-only guess:
    # a spread-based sigma0 that's too large relative to the market's actual
    # (possibly tight) implied uncertainty can send the very first Newton step
    # into a collapsing sigma, and once sigma is tiny the residuals are locally
    # flat (a near-delta-function's model probability barely changes for small
    # mu/sigma perturbations almost everywhere), so LM has no gradient signal to
    # climb back out -- confirmed on a real snapshot where a spread-based start
    # converged to sigma_min with cost 0.16 while the MoM start converged to the
    # true minimum (cost ~1e-8) in the same number of iterations.
    xs, ws = [], []
    for floor, cap, strike_type in markets:
        x = (floor + cap) / 2.0 if strike_type == "between" else floor + (1.0 if strike_type == "greater" else -1.0)
        xs.append(x)
    ws = [(b + a) / 2.0 for b, a in zip(bids, asks)]
    w_sum = sum(ws)
    if w_sum > 1e-9:
        mu_mom = sum(x * w for x, w in zip(xs, ws)) / w_sum
        # max(..., 0.0) before **0.5: a caller passing a negative bid/ask
        # (e.g. find_and_correct_mispriced_market's fair_value substitution,
        # before it started guarding against an out-of-[0,1] result itself)
        # gives a negative weight here, which can make this weighted-
        # variance sum go negative -- **0.5 on a negative float returns a
        # COMPLEX number in Python (not a domain error), which then fails
        # confusingly much later (comparing float to complex). Flooring at
        # 0 is just a defensive backstop for this one MoM starting guess;
        # it doesn't change what a VALID input converges to.
        var_mom = max(sum((x - mu_mom) ** 2 * w for x, w in zip(xs, ws)) / w_sum, 0.0)
        sigma_mom = max(var_mom ** 0.5, 0.3)
    else:
        mu_mom = sum(xs) / n
        sigma_mom = 1.0

    floors_only = [m[0] for m in markets]
    default_sigma = max((max(floors_only) - min(floors_only)) / 2.0, 1.0)
    mean_floor = sum(floors_only) / n
    starts = [(mu_mom, sigma_mom), (mean_floor, default_sigma)]
    if warm_start is not None:
        ws_mu, ws_sigma = warm_start
        starts.append((ws_mu, max(ws_sigma, sigma_min)))  # sigma=0 would divide by zero as a literal start

    # Take whichever starting point converges to the lowest cost, full stop --
    # NOT gated by lambda_min_threshold. This used to reject (return None,
    # None, ...) whenever every candidate's lambda_min fell below threshold,
    # specifically to guard the degenerate-ridge failure mode (see this
    # function's own docstring: multiple wildly different (mu, sigma) fitting
    # every band equally well). That gate is gone -- always report the best
    # point actually found, with lambda_min still returned alongside it as an
    # informational signal a caller CAN use to judge how tightly that point
    # was pinned down, rather than this function silently deciding on the
    # caller's behalf that a poorly-constrained point isn't worth reporting
    # at all. lambda_min_threshold is still accepted as a parameter (every
    # caller up the stack still passes it through) but no longer used here.
    best = None
    best_lambda_min = None  # the most charitable (highest) lambda_min seen across
    # all candidates -- still computed and returned, just not used to gate.
    for mu0, sigma0 in starts:
        mu, sigma, cost, band_cost, lambda_min = lm_fit(mu0, sigma0)
        if best_lambda_min is None or lambda_min > best_lambda_min:
            best_lambda_min = lambda_min
        if best is None or cost < best[2]:
            best = (mu, sigma, cost, band_cost, lambda_min)
    mu_best, sigma_best, _cost_best, band_cost_best, _lambda_min_best = best

    # One remaining gate on the whole result -- NOT a general fit-quality
    # threshold like lambda_min was, but a narrow, targeted check: if any
    # 'less'/'greater' ("<F"/">F") market's OWN fitted probability at this
    # point exceeds 0.5, the market is pricing that open-ended tail as MORE
    # LIKELY THAN NOT -- deliberately checked only for 'less'/'greater', not
    # 'between' (a 'between' bucket routinely prices >0.5 near the peak of a
    # calm, well-behaved distribution; that's normal, not a red flag).
    # band_cost is withheld here TOO, not just (mu, sigma): band_cost is "how
    # much violation remains AT this point," so it's only a meaningful,
    # standalone number if the point itself is trustworthy enough to
    # evaluate anything at -- unlike the lambda_min case above, where
    # band_cost stayed valid regardless of how tightly (mu, sigma) was
    # pinned down (a different, unrelated question).
    if sigma_best > 0:
        for floor, cap, strike_type in markets:
            if strike_type not in ("less", "greater"):
                continue
            p, _, _ = bucket_model_prob_and_grad(mu_best, sigma_best, floor, cap, strike_type)
            if p > 0.5:
                return None, None, None, best_lambda_min

    return float(mu_best), float(sigma_best), float(band_cost_best), best_lambda_min


def find_and_correct_mispriced_market(
    markets: list[tuple[float, float | None, str]],
    bids: list[float],
    asks: list[float],
    bid_depths: list[float] | None = None,
    ask_depths: list[float] | None = None,
    warm_start: tuple[float, float] | None = None,
) -> dict:
    """
    Identify the single market most responsible for a snapshot's band_cost,
    replace ITS price with the value conservation demands, and refit
    (mu, sigma) off that corrected snapshot -- a trading-relevant
    alternative to just excluding the outlier (find_worst_fits' leave-one-
    out diagnostic answers "how much does this market disagree with
    consensus"; this answers "what should this market actually be worth,
    and what does the REST of the market look like once it's fixed").

    Relies on KXHIGHLAX settlement being a discrete integer temperature: the
    listed markets for one event, once continuity-corrected, exhaustively
    and exclusively partition the outcome space (see _continuity_corrected_
    bounds's docstring) -- so their TRUE probabilities must sum to EXACTLY
    1, a model-free constraint independent of any (mu, sigma) fit. That
    means the "fair value" of any ONE market is pinned down directly by the
    other markets' own quotes: 1 - sum(other markets' own midpoints), no
    normal-distribution assumption required for this step at all.

    Procedure:
      1. Fit (mu, sigma) once off every market's real quote (the ordinary
         fit_normal_to_bucket_bid_ask call).
      2. Find the single market with the largest band violation at that
         point -- the one most responsible for band_cost.
      3. Compute ITS conservation-implied fair value from the other
         markets' own midpoints (1 - their sum) -- not from the model.
      4. Refit (mu, sigma) with that ONE market's quote replaced by its fair
         value (as a point, not a band -- the whole reason to do this
         instead of just excluding it is to keep using its slot in the fit,
         now with a trustworthy target instead of a distorting one) and
         every other market UNCHANGED, real quotes.

    Confirmed on a real case (KXHIGHLAX-26JUL27, 2026-07-27 07:15:31 PT):
    flagged B83.5 (quoted 7-8%), conservation implied a 2.5% fair value
    (matching the model's own independent ~2% prediction closely), and
    refitting with that correction dropped band_cost from 0.0031 to 0.0005
    -- an ~85% reduction from correcting just that one market, confirming
    it really was the dominant source of the inconsistency, not an
    artifact spread thin across several markets.

    Returns a dict: mu/sigma/band_cost (the corrected fit's own results,
    None if even the baseline fit fails -- see fit_normal_to_bucket_bid_ask
    for when that happens), mispriced_index (position in `markets`),
    fair_value (the conservation-implied probability), quoted_bid/
    quoted_ask (that market's own real quote, for comparing against
    fair_value directly -- fair_value > quoted_ask means the market is
    underpriced, i.e. worth buying; fair_value < quoted_bid means
    overpriced, worth selling), and baseline_mu/baseline_sigma/
    baseline_band_cost (the uncorrected fit, for comparison).
    """
    baseline_mu, baseline_sigma, baseline_band_cost, _ = fit_normal_to_bucket_bid_ask(
        markets, bids, asks, bid_depths, ask_depths, warm_start=warm_start)
    if baseline_mu is None:
        return {
            "mu": None, "sigma": None, "band_cost": None,
            "mispriced_index": None, "fair_value": None,
            "quoted_bid": None, "quoted_ask": None,
            "baseline_mu": None, "baseline_sigma": None, "baseline_band_cost": baseline_band_cost,
        }

    worst_i, worst_viol = None, -1.0
    for i, (floor, cap, strike_type) in enumerate(markets):
        p, _, _ = bucket_model_prob_and_grad(baseline_mu, baseline_sigma, floor, cap, strike_type)
        viol = max(bids[i] - p, 0.0) + max(p - asks[i], 0.0)
        if viol > worst_viol:
            worst_viol, worst_i = viol, i

    others_mid_sum = sum((bids[j] + asks[j]) / 2.0 for j in range(len(markets)) if j != worst_i)
    fair_value = 1.0 - others_mid_sum
    if not (0.0 <= fair_value <= 1.0):
        # The "one bad market, rest trustworthy" premise requires the OTHER
        # markets to already be internally consistent -- if they alone sum
        # to more (or less) than the full [0, 1] budget, there's no single
        # market left to blame; the whole snapshot is suspect (confirmed on
        # a real case, KXHIGHLAX-26MAR12 2026-03-12 00:42:34 PT: every
        # single listed market quoted the uninformative bid=0/ask=1
        # placeholder, giving fair_value=-1.5 -- clearly pre-market
        # data, not a real mispricing). Feeding a negative/>1 "probability"
        # back into the refit as both bid and ask produces a NEGATIVE
        # weight in fit_normal_to_bucket_bid_ask's method-of-moments start
        # (weight = (bid+ask)/2), which can make its weighted-variance sum
        # go negative -- `** 0.5` on a negative float returns a COMPLEX
        # number in Python, not a math domain error, which then fails much
        # later and confusingly (TypeError comparing float to complex).
        # Bailing out here instead of ever constructing that input.
        return {
            "mu": None, "sigma": None, "band_cost": None,
            "mispriced_index": None, "fair_value": None,
            "quoted_bid": None, "quoted_ask": None,
            "baseline_mu": baseline_mu, "baseline_sigma": baseline_sigma, "baseline_band_cost": baseline_band_cost,
        }

    bids_c = list(bids)
    asks_c = list(asks)
    bids_c[worst_i] = fair_value
    asks_c[worst_i] = fair_value
    mu_c, sigma_c, band_cost_c, _ = fit_normal_to_bucket_bid_ask(
        markets, bids_c, asks_c, bid_depths, ask_depths, warm_start=warm_start)

    return {
        "mu": mu_c, "sigma": sigma_c, "band_cost": band_cost_c,
        "mispriced_index": worst_i, "fair_value": fair_value,
        "quoted_bid": bids[worst_i], "quoted_ask": asks[worst_i],
        "baseline_mu": baseline_mu, "baseline_sigma": baseline_sigma, "baseline_band_cost": baseline_band_cost,
    }


# ---------------------------------------------------------------------------
# Local-sigma smile (superseded the single-skew-parameter approach above --
# see fit_local_sigma_smile's docstring for why)
# ---------------------------------------------------------------------------

_WING_Z_FLAT_CORE = 1.3  # see expand_smile_pillars's docstring; shared with
# _wing_side_markets/_fit_wing_sigma_joint below so the ramp-start boundary
# used while SOLVING for sigma_wing always matches the one the pillar curve
# actually uses at pricing time.


def _wing_side_markets(mu, sigma_atm, markets, bids, asks, bid_depths, ask_depths,
                        tail_idx, strike_type, z_wing, max_violation_to_include):
    """
    Indices of 'between' markets (besides the tail market itself, tail_idx)
    whose own price actually depends on this side's wing sigma, given the
    flat-core clamp expand_smile_pillars applies -- i.e. every 'between'
    market with an edge past whichever z the ramp toward z_wing actually
    starts at (z_flat_core if the wing pillar itself sits beyond it, else 0.0
    directly -- see expand_smile_pillars's docstring on why that clamp point
    moves). A market entirely inside the flat plateau is UNAFFECTED by
    sigma_wing no matter its value, so there's nothing to gain by including
    it in the joint fit below.

    Two-sided only (bid_depth>0 and ask_depth>0), and excluded if its OWN
    ATM-sigma violation already exceeds max_violation_to_include -- the same
    frozen/disconnected-quote guard fit_local_sigma_smile applies to the tail
    market itself, for the same reason (see its docstring).
    """
    ramp_start = _WING_Z_FLAT_CORE if abs(z_wing) > _WING_Z_FLAT_CORE else 0.0
    idx = []
    for i, (floor, cap, mtype) in enumerate(markets):
        if i == tail_idx or mtype != "between":
            continue
        if bid_depths is not None and ask_depths is not None and (bid_depths[i] <= 0 or ask_depths[i] <= 0):
            continue
        floor_c, cap_c = _continuity_corrected_bounds(floor, cap, "between")
        edge = cap_c if strike_type == "greater" else floor_c
        if strike_type == "greater":
            if (edge - mu) / sigma_atm <= ramp_start:
                continue
        else:
            if (edge - mu) / sigma_atm >= -ramp_start:
                continue
        p0, _, _ = bucket_model_prob_and_grad(mu, sigma_atm, floor, cap, "between")
        violation0 = max(bids[i] - p0, 0.0) + max(p0 - asks[i], 0.0)
        if violation0 > max_violation_to_include:
            continue
        idx.append(i)
    return idx


def _fit_wing_sigma_joint(mu, sigma_atm, z_wing, strike_type, wing_specs, sigma_lo, sigma_hi, iters=60):
    """
    The sigma (mu, sigma_atm, and z_wing all held fixed) minimizing TOTAL
    squared band-violation across every market in wing_specs jointly -- a
    list of (floor, cap, strike_type, bid, ask) tuples: the side's own tail
    market plus whichever 'between' markets _wing_side_markets found
    genuinely affected by this pillar. Supersedes solving sigma_wing to
    exactly reproduce the TAIL market's own price alone (the previous
    design): with only one data point pinning it, that forced an exact-
    interpolation node which could make the ramp toward it arbitrarily
    steep, and a 'between' market straddling that ramp could then price
    outside [0, 1] entirely -- confirmed on a real case (KXHIGHLAX-26JUL23,
    2026-07-22 14:18:49 UTC: T86's own 16% quote forced sigma_greater=5.10
    against sigma_atm=2.81, steep enough that B85.5's own two edges landed on
    different enough sigma to price it at -0.023, a negative "probability").

    Each market's squared violation moves in a CONSISTENT but OPPOSITE
    direction as sigma_wing varies -- larger sigma_wing raises the tail
    market's own price (pulling toward its bid) but LOWERS a 'between'
    market's far-edge CDF term (pulling toward ITS OWN band from the other
    side, since spreading mass out past a fixed nearby point lowers the
    probability of landing on or before it) -- so the sum is a genuine
    trade-off with one interior minimum, not one equation with one root.
    With only the tail market in wing_specs (the common case -- most
    snapshots have no 'between' market reaching this far out), this
    converges to exactly the old exact-inversion answer, since there's
    nothing else to trade off against.

    Bounded golden-section search over [sigma_lo, sigma_hi] (same bracket/
    sanity cap fit_local_sigma_smile already uses) -- deliberately not a
    single bisection root-find, since the joint objective has no guaranteed
    sign-crossing the way a single monotonic equation does.
    """
    def cost(sigma_wing):
        if abs(z_wing) > _WING_Z_FLAT_CORE:
            core = _WING_Z_FLAT_CORE if strike_type == "greater" else -_WING_Z_FLAT_CORE
            pillars = [(0.0, sigma_atm), (core, sigma_atm), (z_wing, sigma_wing)]
        else:
            pillars = [(0.0, sigma_atm), (z_wing, sigma_wing)]
        pillars.sort(key=lambda t: t[0])
        total = 0.0
        for floor, cap, mtype, bid, ask in wing_specs:
            p = smile_bucket_prob(mu, sigma_atm, pillars, floor, cap, mtype)
            v = max(bid - p, 0.0) + max(p - ask, 0.0)
            total += v * v
        return total

    invphi = (5 ** 0.5 - 1) / 2
    a, b = sigma_lo, sigma_hi
    c = b - invphi * (b - a)
    d = a + invphi * (b - a)
    fc, fd = cost(c), cost(d)
    for _ in range(iters):
        if fc <= fd:
            b, d, fd = d, c, fc
            c = b - invphi * (b - a)
            fc = cost(c)
        else:
            a, c, fc = c, d, fd
            d = a + invphi * (b - a)
            fd = cost(d)
        if b - a < 1e-9:
            break
    return 0.5 * (a + b)


def fit_local_sigma_smile(
    mu: float,
    sigma_atm: float,
    markets: list[tuple[float, float | None, str]],
    bids: list[float],
    asks: list[float],
    bid_depths: list[float] | None = None,
    ask_depths: list[float] | None = None,
    max_violation_to_include: float = 0.25,
    sigma_bound_mult: tuple[float, float] = (0.2, 5.0),
) -> tuple[float | None, float | None, float | None, float | None]:
    """Per-snapshot local-volatility smile: given (mu, sigma_atm) from
    fit_normal_to_bucket_bid_ask, solve each side's wing pillar -- the
    (z, sigma) implied by the listed 'less'/'greater' market's own price,
    holding mu/sigma_atm fixed -- for expand_smile_pillars/smile_bucket_prob to
    interpolate between. The primary solver is untouched.

    Replaces an earlier single-skew-parameter (split-normal) design. The real
    bias is concentrated in the deepest delta bin (>=0.92) with 0.5-0.85 close
    to flat, so one knob for a whole half of the distribution necessarily
    over-widened everything between mu and the tail -- a structural flaw, not a
    tuning problem. A per-market local sigma keeps pillars near z=0 at
    sigma_atm, so a deep-tail correction doesn't move ATM prices at all.

    Uses whichever 'greater'/'less' markets are actually listed rather than a
    fixed 15d/85d target: a typical event lists only ~6 markets, and 25-33% of
    snapshots had nothing within 10 delta points of a fixed pillar. ~98% of days
    list exactly one 'less' and one 'greater' (Kalshi always brackets the
    'between' buckets), quoted at ~86.5% of snapshots. If several qualify on one
    side, the last checked wins.

    A candidate must be two-sided, and is dropped if its ATM-sigma violation
    exceeds max_violation_to_include -- the frozen-quote guard (one market's
    bid/ask sat at 0.55/0.56 all day while mu/sigma moved independently; no
    plausible local sigma explains that). If sigma_atm already prices the market
    inside its band, that pillar is None.

    The wing sigma is not an exact single-point inversion: that could make the
    ramp steep enough to break monotonicity in a nearby 'between' market. It's
    the sigma minimizing total squared band violation across the tail market and
    every 'between' market _wing_side_markets finds affected. With none in range
    (the common case) this reduces to the exact-inversion answer.

    sigma_bound_mult caps how far a local sigma may move from sigma_atm, as both
    the search bracket and the sanity clamp.

    Returns (z_less, sigma_less, z_greater, sigma_greater); a pair is
    (None, None) if that side has no listed wing market, no quote survived, or
    sigma_atm already explained it. Kept in this flat 2-sided form so it stores
    as columns in the cached timeseries.
    """
    if sigma_atm is None or sigma_atm <= 0:
        return None, None, None, None

    if bid_depths is not None and ask_depths is not None:
        candidate_idx = [i for i in range(len(markets)) if bid_depths[i] > 0 and ask_depths[i] > 0]
    else:
        candidate_idx = list(range(len(markets)))

    lo_mult, hi_mult = sigma_bound_mult
    sigma_lo_bound = sigma_atm * lo_mult
    sigma_hi_bound = sigma_atm * hi_mult

    z_less = sigma_less = z_greater = sigma_greater = None
    for i in candidate_idx:
        floor, cap, strike_type = markets[i]
        if strike_type not in ("greater", "less"):
            continue
        p0, _, _ = bucket_model_prob_and_grad(mu, sigma_atm, floor, None, strike_type)
        bid, ask = bids[i], asks[i]
        violation0 = max(bid - p0, 0.0) + max(p0 - ask, 0.0)
        if violation0 > max_violation_to_include:
            continue
        if not (p0 < bid or p0 > ask):
            continue  # sigma_atm already prices this within its own band

        floor_c, _ = _continuity_corrected_bounds(floor, None, strike_type)
        z = (floor_c - mu) / sigma_atm

        wing_specs = [(floor, cap, strike_type, bid, ask)]
        for j in _wing_side_markets(mu, sigma_atm, markets, bids, asks, bid_depths, ask_depths,
                                     i, strike_type, z, max_violation_to_include):
            fj, cj, tj = markets[j]
            wing_specs.append((fj, cj, tj, bids[j], asks[j]))

        sigma_local = _fit_wing_sigma_joint(
            mu, sigma_atm, z, strike_type, wing_specs, sigma_lo_bound, sigma_hi_bound,
        )
        if strike_type == "less":
            z_less, sigma_less = z, sigma_local
        else:
            z_greater, sigma_greater = z, sigma_local

    return z_less, sigma_less, z_greater, sigma_greater


def expand_smile_pillars(
    sigma_atm: float,
    z_less: float | None,
    sigma_less: float | None,
    z_greater: float | None,
    sigma_greater: float | None,
    z_flat_core: float = _WING_Z_FLAT_CORE,
) -> list[tuple[float, float]]:
    """
    Rebuild the full (z, sigma) interpolation list local_sigma_at/
    smile_bucket_prob use, from fit_local_sigma_smile's compact 2-sided
    return (or the equivalent cached implied_wing_* columns -- pass NaN as
    None, both are treated the same via pd.isna).

    A first version connected (0.0, sigma_atm) straight to each wing pillar
    with nothing in between. A full validation rerun showed that bled the
    tail correction across almost the ENTIRE upper (or lower) half,
    including bins the original diagnostic found were already close to
    fine -- e.g. the 0.5-0.583 delta bin picked up a new -0.008 mean
    mispricing it didn't have before, and even the nominal ATM bin drifted
    by a similar amount in several months. Root cause: a straight line from
    z=0 to a wing pillar typically only ~2-2.5 sigma out means even a
    delta-0.6 market sits 25-40% of the way along that line, inheriting a
    meaningful chunk of the tail's correction despite its own quote never
    asking for one. The diagnostic's own binned table shows where the real
    bias actually starts: the 0.833-0.917 bin was close to flat/mixed-sign
    in every month, only 0.917-1.0 was consistently and substantially off --
    i.e. roughly z >= 1.3-1.4 (Phi^-1(0.917) ~= 1.386). z_flat_core encodes
    that directly: an extra pillar is pinned at (+-z_flat_core, sigma_atm)
    on whichever side has a wing pillar beyond it, so the curve is FORCED
    flat at sigma_atm for all |z| <= z_flat_core and only starts moving
    toward the wing's own implied value beyond that threshold, rather than
    hoping two sparse points happen to produce that shape on their own. If a
    wing pillar's own |z| is already inside the threshold, no clamp is added
    for that side -- that market's own price is real, closer-in data, not
    something to override with an artificial flat constraint.

    A PHANTOM pillar is also added one ramp-length beyond each real wing
    pillar, on whichever side(s) have one: local_sigma_at flat-extrapolates
    past the outermost pillar it's given, so without this, sigma stops
    growing the instant z passes the one real market that's actually listed
    out there -- capping how fat the tail can ever price, no matter how
    strongly the fit wants to keep rising. There's no market this far out to
    fit against, so the phantom's sigma isn't fit to data at all: it mirrors
    the real ramp (from the flat-core clamp, or 0.0 if the pillar itself sits
    inside z_flat_core, out to the real wing pillar) one more time at the
    SAME slope -- z_phantom = 2*z_wing - ramp_start, sigma_phantom =
    2*sigma_wing - sigma_atm (floored at 0.05 in case sigma_wing < sigma_atm,
    i.e. a wing narrower than ATM, which would otherwise mirror to a
    non-positive value). This is a deliberate extrapolation ASSUMPTION, not
    a fit -- see the caller conversation this was added from -- purely to
    give local_sigma_at/smile_bucket_prob room to keep the tail fattening
    past the last real quote instead of going flat there.

    Returns a list of (z, sigma) pairs sorted by z, always including
    (0.0, sigma_atm).
    """
    pillars = [(0.0, sigma_atm)]
    if z_less is not None and not pd.isna(z_less):
        ramp_start = -z_flat_core if abs(z_less) > z_flat_core else 0.0
        if abs(z_less) > z_flat_core:
            pillars.append((ramp_start, sigma_atm))
        pillars.append((z_less, sigma_less))
        pillars.append((2 * z_less - ramp_start, max(2 * sigma_less - sigma_atm, 0.05)))
    if z_greater is not None and not pd.isna(z_greater):
        ramp_start = z_flat_core if abs(z_greater) > z_flat_core else 0.0
        if abs(z_greater) > z_flat_core:
            pillars.append((ramp_start, sigma_atm))
        pillars.append((z_greater, sigma_greater))
        pillars.append((2 * z_greater - ramp_start, max(2 * sigma_greater - sigma_atm, 0.05)))
    pillars.sort(key=lambda t: t[0])
    return pillars


def local_sigma_at(z: float, pillars: list[tuple[float, float]]) -> float:
    """
    Piecewise-linear interpolation of sigma(z) through `pillars` (sorted
    (z, sigma) pairs from fit_local_sigma_smile, always including
    (0.0, sigma_atm)). Flat extrapolation beyond the outermost pillar on
    either side -- simplest reasonable default given only one wing pillar
    per side typically exists (nothing to extrapolate a SLOPE from beyond a
    single point); see smile_bucket_prob for how this feeds into pricing.
    """
    if len(pillars) == 1:
        return pillars[0][1]
    if z <= pillars[0][0]:
        return pillars[0][1]
    if z >= pillars[-1][0]:
        return pillars[-1][1]
    for (z0, s0), (z1, s1) in zip(pillars[:-1], pillars[1:]):
        if z0 <= z <= z1:
            frac = (z - z0) / (z1 - z0)
            return s0 + frac * (s1 - s0)
    return pillars[-1][1]  # unreachable given the z >= pillars[-1][0] check above


def smile_bucket_prob(mu: float, sigma_atm: float, pillars: list[tuple[float, float]], floor: float, cap: float | None, strike_type: str) -> float:
    """
    Reprice one market off the local-sigma smile (see fit_local_sigma_smile)
    instead of a single constant sigma. For 'greater'/'less', this is just
    bucket_model_prob_and_grad evaluated at that boundary's own interpolated
    local sigma. For 'between', each edge (floor and cap) gets its OWN local
    sigma from the curve -- i.e. this is priced as F(cap; sigma(z_cap)) -
    F(floor; sigma(z_floor)) using the plain normal CDF at each edge's own
    point on the smile, the same way a call spread is priced off each leg's
    own smile-implied vol in equity options, rather than one shared sigma for
    the whole bucket. Not a single coherent terminal distribution in the
    strict sense (the two edges technically come from different normals),
    but neither is any discrete vol smile -- it's a standard, practical
    pricing convention, and unlike the skew approach, every market is priced
    off ITS OWN local curve value, so it can't drag an unrelated market's
    price the way a single global skew parameter did.

    sigma_atm is passed explicitly (not read off pillars[0]) because pillars
    is sorted by z, not by insertion order -- the ATM pillar (z=0.0) sorts
    wherever 0.0 falls relative to a 'less' pillar's negative z and a
    'greater' pillar's positive z, not necessarily first. z is always
    computed in sigma_atm units, matching exactly how fit_local_sigma_smile
    computed each pillar's own z in the first place.

    Uses scipy.special.ndtr, not scipy.stats.norm.cdf -- see bucket_model_
    prob_and_grad's docstring on why the generic scipy.stats dispatch isn't
    free at real call volume (plot_vol_skew_diagnostic calls this per market
    per sampled snapshot across every cached day).
    """
    floor, cap = _continuity_corrected_bounds(floor, cap, strike_type)
    if strike_type == "between":
        z_floor = (floor - mu) / sigma_atm
        z_cap = (cap - mu) / sigma_atm
        sigma_floor = local_sigma_at(z_floor, pillars)
        sigma_cap = local_sigma_at(z_cap, pillars)
        return ndtr((cap - mu) / sigma_cap) - ndtr((floor - mu) / sigma_floor)
    elif strike_type == "greater":
        z = (floor - mu) / sigma_atm
        sigma_local = local_sigma_at(z, pillars)
        return ndtr((mu - floor) / sigma_local)
    elif strike_type == "less":
        z = (floor - mu) / sigma_atm
        sigma_local = local_sigma_at(z, pillars)
        return ndtr((floor - mu) / sigma_local)
    else:
        raise ValueError(f"unknown strike_type: {strike_type!r}")


# ---------------------------------------------------------------------------
# Time-series analysis
# ---------------------------------------------------------------------------

DEFAULT_STALENESS_THRESHOLD = "90s"
DEFAULT_MISMATCH_TOL = 0.03


def _asof_linear_interp(eval_times: pd.Series, src: pd.DataFrame, time_col: str, value_cols: list[str]) -> dict[str, np.ndarray]:
    """
    Bracket each eval time between the nearest known source rows at-or-before
    and at-or-after it, and linearly interpolate value_cols between them.

    Kalshi's 1-minute candles only report the BBO at each minute's close, so a
    plain backward as-of merge (what this replaces) holds the previous
    minute's value flat for the whole next minute and then snaps instantly to
    the new one at the boundary -- a real step function is never what
    happened; we just don't know the true intra-minute path. Assuming it
    moved linearly between the two known closes is a strictly better guess
    than "no movement, then teleport," and it's what makes bid, ask, their
    spread (width), and any other quoted quantity (e.g. volume) evolve
    smoothly across a Kalshi-only minute instead of kinking the implied-spot
    fit at every candle boundary.

    Outside the source's time range: after the last known point, holds it
    flat (nothing later to interpolate toward -- same as a backward asof).
    Before the first known point, returns NaN (nothing earlier to interpolate
    from either -- same as a backward asof would've returned).
    """
    eval_df = pd.DataFrame({"time": eval_times})
    cols = [time_col] + value_cols
    prev = pd.merge_asof(eval_df, src[cols], left_on="time", right_on=time_col, direction="backward")
    nxt = pd.merge_asof(eval_df, src[cols], left_on="time", right_on=time_col, direction="forward")

    prev_t = prev[time_col]
    next_t = nxt[time_col]
    t = eval_df["time"]

    has_prev = prev_t.notna().to_numpy()
    has_next = next_t.notna().to_numpy()
    both = has_prev & has_next

    # tz-aware datetime subtraction must stay in pandas (a numpy object-array
    # of Timestamps otherwise, since .to_numpy() drops tz info into dtype=object).
    span = (next_t - prev_t).dt.total_seconds().to_numpy()
    elapsed = (t - prev_t).dt.total_seconds().to_numpy()
    frac = np.zeros(len(t))
    nonzero_span = both & (span > 0)
    frac[nonzero_span] = elapsed[nonzero_span] / span[nonzero_span]
    frac = np.clip(frac, 0.0, 1.0)

    out = {}
    for col in value_cols:
        pv = prev[col].to_numpy(dtype=float)
        nv = nxt[col].to_numpy(dtype=float)
        result = np.full(len(t), np.nan)
        result[both] = pv[both] + frac[both] * (nv[both] - pv[both])
        only_prev = has_prev & ~has_next
        result[only_prev] = pv[only_prev]
        out[col] = result
    return out


def _asof_bracket_band(eval_times: pd.Series, src: pd.DataFrame, time_col: str, bid_col: str = "bid", ask_col: str = "ask") -> dict[str, np.ndarray]:
    """
    Bracket each eval time between the nearest known source rows at-or-before
    and at-or-after it -- same backward/forward as-of merge as _asof_linear_
    interp -- but instead of linearly interpolating a single point in
    between, returns the full [min(prev_bid, next_bid), max(prev_ask,
    next_ask)] BAND spanning both bracketing quotes.

    Why: a linearly-interpolated POINT implicitly assumes we know exactly
    where the price is at every intra-candle instant, which then gets fed
    into fit_normal_to_bucket_bid_ask as a one-sided market's hard, full-
    weight midprice target (see that function's docstring on one-sided
    markets). But the true probability path under a smoothly-evolving
    (mu, sigma) is a CDF difference, genuinely nonlinear -- so a straight
    line in PRICE space systematically diverges from it mid-candle, forcing
    the solver into an increasingly strained compromise against every other
    (real, unchanging) market before snapping back at the next real candle
    close. Confirmed directly on a real case (KXHIGHLAX-26JUL25, 2026-07-25
    10:58 PT): band_cost climbed smoothly from ~0 right after one candle
    close to 0.083 forty seconds in, then collapsed back to ~0 at the next
    candle -- while every OTHER listed market's own quote was completely
    unchanged the entire minute, and excluding the interpolated market
    entirely reproduced the SAME (low) cost at every second, conclusively
    isolating it as the cause.

    Treating the true intra-candle value as EQUALLY PLAUSIBLE anywhere
    between the two bracketing quotes -- a discrete-uniform assumption ("it
    moved SOME time during this minute, we don't know when") rather than a
    specific continuous path -- and feeding that as a real [bid, ask] band
    (see compute_implied_spot_timeseries's use of this) lets fit_normal_to_
    bucket_bid_ask's ordinary band-violation hinge treat ANY (mu, sigma)
    landing inside it as a perfect match, the same tolerance a genuinely
    two-sided market's own quoted spread already gets -- instead of forcing
    an exact, increasingly wrong point.

    Same edge-case handling as _asof_linear_interp: after the last known
    point, holds flat at that point's own (bid, ask) (nothing later to
    bracket against); before the first known point, NaN (nothing to bracket
    against at all).
    """
    eval_df = pd.DataFrame({"time": eval_times})
    cols = [time_col, bid_col, ask_col]
    prev = pd.merge_asof(eval_df, src[cols], left_on="time", right_on=time_col, direction="backward")
    nxt = pd.merge_asof(eval_df, src[cols], left_on="time", right_on=time_col, direction="forward")

    has_prev = prev[time_col].notna().to_numpy()
    has_next = nxt[time_col].notna().to_numpy()
    both = has_prev & has_next
    only_prev = has_prev & ~has_next

    prev_bid = prev[bid_col].to_numpy(dtype=float)
    prev_ask = prev[ask_col].to_numpy(dtype=float)
    next_bid = nxt[bid_col].to_numpy(dtype=float)
    next_ask = nxt[ask_col].to_numpy(dtype=float)

    band_lo = np.full(len(eval_df), np.nan)
    band_hi = np.full(len(eval_df), np.nan)
    band_lo[both] = np.minimum(prev_bid[both], next_bid[both])
    band_hi[both] = np.maximum(prev_ask[both], next_ask[both])
    band_lo[only_prev] = prev_bid[only_prev]
    band_hi[only_prev] = prev_ask[only_prev]
    return {"bid": band_lo, "ask": band_hi}


def compute_implied_spot_timeseries(
    long_df: pd.DataFrame,
    start_ts,
    end_ts,
    freq: str = "1min",
    max_lookback: str | None = None,
    staleness_threshold: str = DEFAULT_STALENESS_THRESHOLD,
    mismatch_tol: float = DEFAULT_MISMATCH_TOL,
    lambda_min_threshold: float = 0.001,
    fit_smile: bool = False,
    kalshi_backup: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Implied spot and sigma at each time step, fitting each snapshot with
    fit_normal_to_bucket_bid_ask.

    Parameters
    ----------
    long_df : long-form frame from predexon_functions.load_predexon_long_df
        (ticker, floor_strike, cap_strike, strike_type, time, bid, ask,
        bid_depth, ask_depth). Grouped by ticker -- floor_strike alone isn't
        unique, since a range bucket and the lower-tail market can share a floor.
    start_ts, end_ts : Unix seconds, bounds for the eval grid
    freq : pandas offset string for the grid (e.g. "1min", "1s")
    max_lookback : drop a market from the fit if its quote is older than this
        offset. None (default) forward-fills indefinitely.
    staleness_threshold, mismatch_tol : Predexon's feed has real capture gaps
        (one confirmed case sat ~21 minutes silent while the true price moved
        35+ cents). Every quote is cross-checked against Kalshi's own 1-minute
        candles, and Kalshi's BBO is used whenever Predexon's is older than
        `staleness_threshold` or disagrees by more than `mismatch_tol`. Kalshi
        is authoritative; Predexon is a finer-resolution mirror.

        A distrusted row is replaced by a BAND, not an interpolated point
        (_asof_bracket_band: [min(prev_bid, next_bid), max(prev_ask, next_ask)])
        with nominal depth 1.0, so it fits through the ordinary two-sided hinge.
        A straight line in price diverges from the true nonlinear probability
        path mid-candle, which forces a strained compromise against every other
        market and then snaps back at the next candle close -- observed as
        band_cost climbing to 0.083 over 40s then collapsing, while no other
        market's quote moved at all. The band says "it moved sometime this
        minute, we don't know when."

        Stale rows bracket against the union of both sources; mismatch rows
        bracket against Kalshi only, since Predexon's quote is precisely what's
        being distrusted there.
    fit_smile : also run fit_local_sigma_smile on each row's converged
        (mu, sigma). Default False. Cheap when enabled -- it reuses the matrices
        built here.

    Returns
    -------
    Frame with time, implied_spot, implied_sigma, implied_lambda_min,
    implied_band_cost, n_two_sided, and the four implied_wing_* columns (NaN
    unless fit_smile and that side has a correction).

    implied_band_cost is the sum of squared band violations at the best fit --
    0 means every market's model price landed inside its own band. Ranking by it
    surfaces snapshots the model can't explain, as opposed to ranking by jump
    size, which surfaces big moves regardless of fit quality.

    ONE ROW PER GRID POINT, never dropped. Rows with fewer than 2 quoted markets,
    or a 'less'/'greater' market fitting >0.5, are left NaN rather than filled or
    removed -- if the data can't support an estimate, none is reported. Keeping
    the rows is what lets consumers treat the stretch as unknown: pandas skips
    diffs touching a NaN endpoint, so variance_till_expiry and
    realized_vol_scaled contribute zero there with no gap-detection of their own.
    Plotting code should shade NaN stretches explicitly so they don't read as a
    rendering glitch.

    Each fit warm-starts from the previous row's (mu, sigma); without it, a
    quote oscillating a fraction of a cent across the certain_bid threshold could
    flip between a sane point mass and a degenerate sigma=17 between adjacent
    points. Only successful rows update the anchor.

    Per ticker, the latest quote at-or-before each grid timestamp comes from one
    vectorized as-of merge, which keeps the (n_times, n_markets) matrices fast at
    second granularity over a full day (~100k points). Each fit is ~0.3-0.6ms.

    lambda_min_threshold passes through to each fit but is no longer used there.
    """
    if long_df.empty:
        return pd.DataFrame(columns=["time", "implied_spot", "implied_sigma"])

    # Build eval grid bounded by actual data range (avoids plotting into silence)
    data_start = long_df["time"].min()
    data_end = long_df["time"].max()
    grid_start = max(pd.Timestamp(start_ts, unit="s", tz="UTC"), data_start)
    grid_end = min(pd.Timestamp(end_ts, unit="s", tz="UTC"), data_end)

    eval_times = pd.date_range(start=grid_start, end=grid_end, freq=freq)
    if len(eval_times) == 0:
        return pd.DataFrame(columns=["time", "implied_spot", "implied_sigma"])
    tolerance = pd.Timedelta(max_lookback) if max_lookback is not None else None
    eval_df = pd.DataFrame({"time": eval_times})

    tickers = sorted(long_df["ticker"].unique())
    event_ticker = tickers[0].rsplit("-", 1)[0]
    # kalshi_backup=None (default): fetch Kalshi's own 1-min candles as the
    # staleness/mismatch fallback source, same as always. An explicit
    # DataFrame (including an empty one) is used AS-IS instead -- no fetch,
    # no live Kalshi API call -- for a caller whose long_df is ALREADY
    # Kalshi's own data (e.g. kalshi_ws L2) and has no use for a second,
    # redundant live-polled copy of the same exchange's own quotes, or who
    # simply needs zero live-network calls (e.g. rebuilding a large cached
    # day list entirely from already-collected local/S3 data).
    if kalshi_backup is None:
        kalshi_backup = predexon_functions.load_kalshi_backup_long_df(event_ticker)
    staleness_td = pd.Timedelta(staleness_threshold)

    # One as-of merge per ticker -> (n_times, n_markets) matrices of latest-known
    # bid/ask/depth, plus each ticker's (floor, cap, strike_type) for the fit.
    # Each ticker also gets a second as-of merge against Kalshi's own 1-min
    # candles (see staleness_threshold/mismatch_tol docs above): Predexon's
    # bid/ask/depth get overwritten by Kalshi's whenever Predexon's own quote
    # is stale or disagrees, with depth zeroed for those cells (no real book
    # depth from a 1-min candle) so they fall through to the one-sided/
    # midprice-fit path in fit_normal_to_bucket_bid_ask.
    market_meta = []
    bid_cols, ask_cols, bidd_cols, askd_cols = [], [], [], []
    n_stale = n_mismatch = n_total = 0
    for tk in tickers:
        sub = long_df.loc[long_df["ticker"] == tk].sort_values("time")
        cap_raw = sub["cap_strike"].iloc[0]
        market_meta.append((
            float(sub["floor_strike"].iloc[0]),
            float(cap_raw) if pd.notna(cap_raw) else None,
            sub["strike_type"].iloc[0],
        ))
        sub = sub.rename(columns={"time": "quote_time"})
        merged = pd.merge_asof(
            eval_df, sub[["quote_time", "bid", "ask", "bid_depth", "ask_depth"]],
            left_on="time", right_on="quote_time", direction="backward", tolerance=tolerance,
        )
        bid = merged["bid"].to_numpy()
        ask = merged["ask"].to_numpy()
        bidd = merged["bid_depth"].to_numpy()
        askd = merged["ask_depth"].to_numpy()
        age = (merged["time"] - merged["quote_time"]).to_numpy()
        is_stale = pd.isna(age) | (age > staleness_td)

        ksub = kalshi_backup.loc[kalshi_backup["ticker"] == tk].sort_values("time")
        if not ksub.empty:
            k_interp = _asof_linear_interp(eval_df["time"], ksub.rename(columns={"time": "k_time"}), "k_time", ["bid", "ask"])
            k_bid = k_interp["bid"]
            k_ask = k_interp["ask"]
            has_kalshi = ~np.isnan(k_bid) & ~np.isnan(k_ask)
            mismatch = (~is_stale) & has_kalshi & ((np.abs(bid - k_bid) > mismatch_tol) | (np.abs(ask - k_ask) > mismatch_tol))
            use_kalshi = has_kalshi & (is_stale | mismatch)

            # Stale rows (a genuine Predexon gap) are bracketed by the nearest
            # KNOWN quote on each side from the UNION of both sources -- a
            # Kalshi candle or a Predexon tick, whichever is actually closer to
            # this eval time -- rather than always assuming the bracket is
            # Kalshi-candle-to-Kalshi-candle. If Predexon itself resumes
            # partway through the gap (a tick lands before the next Kalshi
            # candle), this brackets off that closer, more informative anchor
            # instead of ignoring it. kalshi's own points are a subset of
            # `combined`, so wherever has_kalshi can bracket a row, so can this.
            #
            # Both this AND a mismatch row (Predexon fresh but disagreeing --
            # see below) are bracketed as a BAND (_asof_bracket_band), not
            # linearly interpolated to a single point: see that function's
            # docstring for why a linear-price point becomes a hard, full-
            # weight target a real (nonlinear) probability path systematically
            # diverges from mid-candle -- confirmed on TWO independent real
            # cases now, one via the stale/gap path (KXHIGHLAX-26JUL25) and
            # one via mismatch (KXHIGHLAX-26JUL26, 2026-07-26 13:04 PT: same
            # band_cost-climbs-then-collapses shape, same root cause, just
            # reached with Predexon never going stale at all -- Kalshi's own
            # price is STILL only known at 1-minute granularity regardless of
            # why it's the one being used). Given depth>0 (nominal -- 1.0, not
            # a real book size) specifically so fit_normal_to_bucket_bid_ask's
            # depth-based classification treats it as a genuinely two-sided
            # market (band-violation hinge, zero cost anywhere inside) rather
            # than a one-sided market collapsed to its own midpoint at full
            # weight -- the actual mechanism that let the mid-candle drift
            # dominate before, on both paths.
            combined = pd.concat([
                sub[["quote_time", "bid", "ask"]].rename(columns={"quote_time": "c_time"}),
                ksub[["time", "bid", "ask"]].rename(columns={"time": "c_time"}),
            ]).sort_values("c_time")
            gap_band = _asof_bracket_band(eval_df["time"], combined, "c_time")
            gap_bid, gap_ask = gap_band["bid"], gap_band["ask"]

            # Mismatch's own bracket is Kalshi-ONLY (not the union `combined`
            # above): Predexon's own quote here is exactly what's being
            # distrusted, so it shouldn't reenter as a bracket edge the way it
            # legitimately can for a genuine gap.
            k_band = _asof_bracket_band(eval_df["time"], ksub.rename(columns={"time": "k_time"}), "k_time")
            k_band_bid, k_band_ask = k_band["bid"], k_band["ask"]

            n_total += len(bid)
            n_stale += int((is_stale & has_kalshi).sum())
            n_mismatch += int(mismatch.sum())

            is_gap = is_stale & has_kalshi
            bid = np.where(is_gap, gap_bid, np.where(mismatch, k_band_bid, bid))
            ask = np.where(is_gap, gap_ask, np.where(mismatch, k_band_ask, ask))
            bidd = np.where(use_kalshi, 1.0, bidd)
            askd = np.where(use_kalshi, 1.0, askd)

        bid_cols.append(bid)
        ask_cols.append(ask)
        bidd_cols.append(bidd)
        askd_cols.append(askd)

    if n_total > 0:
        print(f"  [staleness fallback] {event_ticker}: {n_stale / n_total * 100:.2f}% ticker-points used Kalshi "
              f"backup (stale), {n_mismatch / n_total * 100:.2f}% used Kalshi backup (fresh but mismatched)")

    bid_matrix = np.column_stack(bid_cols)
    ask_matrix = np.column_stack(ask_cols)
    bidd_matrix = np.column_stack(bidd_cols)
    askd_matrix = np.column_stack(askd_cols)

    valid_mask = ~(np.isnan(bid_matrix) | np.isnan(ask_matrix))
    ok_rows = np.nonzero(valid_mask.sum(axis=1) >= 2)[0]

    mu = np.full(len(eval_times), np.nan)
    sigma = np.full(len(eval_times), np.nan)
    lambda_min_col = np.full(len(eval_times), np.nan)
    band_cost_col = np.full(len(eval_times), np.nan)
    n_two_sided_col = np.zeros(len(eval_times), dtype=int)
    wing_z_less_col = np.full(len(eval_times), np.nan)
    wing_sigma_less_col = np.full(len(eval_times), np.nan)
    wing_z_greater_col = np.full(len(eval_times), np.nan)
    wing_sigma_greater_col = np.full(len(eval_times), np.nan)
    warm_start = None
    for i in ok_rows:
        m = valid_mask[i]
        markets_i = [market_meta[j] for j in range(len(tickers)) if m[j]]
        bid_i = bid_matrix[i, m].tolist()
        ask_i = ask_matrix[i, m].tolist()
        bidd_i = bidd_matrix[i, m].tolist()
        askd_i = askd_matrix[i, m].tolist()
        mu_i, sigma_i, band_cost_i, lambda_min_i = fit_normal_to_bucket_bid_ask(
            markets_i,
            bid_i,
            ask_i,
            bidd_i,
            askd_i,
            warm_start=warm_start,
            lambda_min_threshold=lambda_min_threshold,
        )
        lambda_min_col[i] = lambda_min_i if lambda_min_i is not None else np.nan
        band_cost_col[i] = band_cost_i if band_cost_i is not None else np.nan
        n_two_sided_col[i] = int(np.sum((bidd_matrix[i, m] > 0) & (askd_matrix[i, m] > 0)))
        if mu_i is not None:
            mu[i] = mu_i
            sigma[i] = sigma_i
            warm_start = (mu_i, sigma_i)
            if fit_smile:
                z_less, sigma_less, z_greater, sigma_greater = fit_local_sigma_smile(
                    mu_i, sigma_i, markets_i, bid_i, ask_i, bidd_i, askd_i,
                )
                wing_z_less_col[i] = z_less if z_less is not None else np.nan
                wing_sigma_less_col[i] = sigma_less if sigma_less is not None else np.nan
                wing_z_greater_col[i] = z_greater if z_greater is not None else np.nan
                wing_sigma_greater_col[i] = sigma_greater if sigma_greater is not None else np.nan

    return pd.DataFrame({
        "time": eval_times, "implied_spot": mu, "implied_sigma": sigma,
        "implied_lambda_min": lambda_min_col, "implied_band_cost": band_cost_col,
        "n_two_sided": n_two_sided_col,
        "implied_wing_z_less": wing_z_less_col, "implied_wing_sigma_less": wing_sigma_less_col,
        "implied_wing_z_greater": wing_z_greater_col, "implied_wing_sigma_greater": wing_sigma_greater_col,
    })


_DEFAULT_TS_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "kalshi_vol_trading", "data", "implied_ts")


def implied_spot_cache_path(
    event_ticker: str,
    freq: str = "1min",
    max_lookback: str | None = None,
    cache_dir: str | None = None,
) -> str:
    """
    Path compute_implied_spot_timeseries_cached reads/writes for a given
    (event_ticker, freq, max_lookback).
    """
    if cache_dir is None:
        cache_dir = _DEFAULT_TS_CACHE_DIR
    lookback_tag = max_lookback if max_lookback is not None else "none"
    return os.path.join(cache_dir, f"{event_ticker}_{freq}_{lookback_tag}.parquet")


def list_cached_days(
    freq: str = "1min",
    max_lookback: str | None = None,
    cache_dir: str | None = None,
    on_or_before: datetime.date | None = None,
) -> list:
    """
    Every date with an implied_spot/sigma series already cached on disk at
    the given (freq, max_lookback) -- i.e. every day compute_implied_spot_
    timeseries_cached could serve instantly with zero Predexon calls.

    Parses each date directly back out of its cached file's own event ticker
    (`{event_ticker}_{freq}_{max_lookback}.parquet`, e.g. "KXHIGHLAX-26JUL22_
    1min_none.parquet") via the same %y%b%d convention api_functions.
    event_ticker_from_date uses to build it in the first place, rather than
    requiring a separate day list to be passed in and cross-checked against
    the cache directory.

    on_or_before: if given, drops any date after it -- e.g. to exclude the
    current in-progress month, where "cached" days are still accruing one by
    one and the most recent might only have partial ticks (an open market),
    unlike every closed historical day compute_implied_spot_timeseries_cached
    otherwise treats as immutable.
    """
    if cache_dir is None:
        cache_dir = _DEFAULT_TS_CACHE_DIR
    lookback_tag = max_lookback if max_lookback is not None else "none"
    suffix = f"_{freq}_{lookback_tag}.parquet"
    if not os.path.isdir(cache_dir):
        return []

    dates = []
    for fname in os.listdir(cache_dir):
        if not fname.endswith(suffix):
            continue
        ticker = fname[: -len(suffix)]
        date_str = ticker.rsplit("-", 1)[-1]  # e.g. "26JUL22"
        try:
            d = datetime.datetime.strptime(date_str, "%y%b%d").date()
        except ValueError:
            continue
        if on_or_before is not None and d > on_or_before:
            continue
        dates.append(d)
    return sorted(dates)


def compute_implied_spot_timeseries_cached(
    long_df_loader,
    start_ts,
    end_ts,
    event_ticker: str,
    freq: str = "1min",
    max_lookback: str | None = None,
    cache_dir: str | None = None,
    lambda_min_threshold: float = 0.001,
    fit_smile: bool = False,
    staleness_threshold: str = DEFAULT_STALENESS_THRESHOLD,
    kalshi_backup: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Like compute_implied_spot_timeseries, but caches the result to parquet
    (keyed by event_ticker + freq + max_lookback) instead of recomputing.

    fit_smile is passed straight through to compute_implied_spot_timeseries
    on a cache miss (see its docstring), but -- same caveat as lambda_min_
    threshold below -- it's NOT part of the cache key either: an existing
    cached file written with fit_smile=False (the default, and every file
    written before this option existed) simply won't have the implied_wing_*
    columns, cache hit or not. Use a distinct `cache_dir` for a smile-enabled
    cache rather than deleting/overwriting an existing one in place, unless
    you specifically want to replace it.

    The per-timestep fit is the expensive part of this pipeline (~0.3-0.6ms/point,
    ~100k points/day at 1s granularity -- tens of seconds per day), so once a
    day's result has been computed, later runs (e.g. reopening the notebook)
    should just load it back rather than resolving the same ~100k fits again.
    Treated as immutable once written: closed historical days don't change, so
    there's no staleness/refresh logic here the way the raw-tick cache has for
    still-open markets -- delete the file to force a recompute.

    IMPORTANT: the cache key does NOT include lambda_min_threshold or a
    code-version tag, so an existing cached file was NOT necessarily built
    with the current fit_normal_to_bucket_bid_ask (e.g. files written before
    the lambda_min degenerate-fit rejection was added -- see compute_implied_
    spot_timeseries's docstring -- can still contain values that mechanism
    now rejects). Delete a file to force it to be rebuilt with whatever
    fitting logic is current.

    long_df_loader: zero-arg callable returning the long-form DataFrame (e.g.
    `lambda: predexon_functions.load_predexon_long_df(event_ticker)`), called
    ONLY on an actual cache miss. Takes a loader rather than a DataFrame so
    callers never need to build long_df -- and pay for load_predexon_long_df's
    live Predexon API call plus a multi-MB raw-tick parquet read -- just to
    hand it to a function that's about to ignore it on a cache hit; the
    cache-hit check lives here, once, instead of every caller re-deriving
    implied_spot_cache_path to pre-check it themselves.

    staleness_threshold/kalshi_backup: passed straight through to compute_
    implied_spot_timeseries on a cache miss (also not part of the cache key,
    same caveat as fit_smile above). kalshi_backup=None (default) keeps that
    function's own behavior of fetching Kalshi's 1-min candles live as the
    staleness/mismatch fallback; pass an explicit DataFrame (an EMPTY one is
    the common case) when long_df is already Kalshi's own data with no need
    for a second live-polled copy of it, or when the caller wants a
    guaranteed-zero-live-calls run (e.g. rebuilding a large day list purely
    from already-collected local/S3 data, with no network available or
    wanted) -- an empty kalshi_backup also means staleness_threshold's
    default (tuned for Predexon's much denser tick cadence) may be too
    tight for a source like kalshi_ws that only logs a new quote when the
    top of book actually changes; widen it explicitly in that case.
    """
    if cache_dir is None:
        cache_dir = _DEFAULT_TS_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = implied_spot_cache_path(event_ticker, freq, max_lookback, cache_dir)

    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df

    long_df = long_df_loader()
    if long_df.empty:
        return pd.DataFrame(columns=["time", "implied_spot", "implied_sigma"])

    df = compute_implied_spot_timeseries(long_df, start_ts, end_ts, freq=freq, max_lookback=max_lookback,
                                          lambda_min_threshold=lambda_min_threshold, fit_smile=fit_smile,
                                          staleness_threshold=staleness_threshold, kalshi_backup=kalshi_backup)
    if not df.empty:
        df.to_parquet(cache_path, index=False)
    return df


def snapshot_market_quotes(
    long_df: pd.DataFrame,
    kalshi_backup: pd.DataFrame,
    t,
    staleness_threshold: str = DEFAULT_STALENESS_THRESHOLD,
    mismatch_tol: float = DEFAULT_MISMATCH_TOL,
) -> pd.DataFrame:
    """
    Every market's own bid/ask/depth at a single instant t, resolved through
    the exact same Predexon-staleness/mismatch -> Kalshi-BBO fallback policy
    as compute_implied_spot_timeseries (see its docstring): a stale row (a
    genuine Predexon gap covering t) is _asof_bracket_band'd between the
    nearest KNOWN quote on each side of t drawn from the union of Predexon
    ticks and Kalshi candles -- not Kalshi candles alone, and not collapsed
    to a single linearly-interpolated point either (see _asof_bracket_band's
    docstring for why a point becomes a hard target a real, nonlinear
    probability path diverges from mid-candle) -- so a Predexon tick that
    resumes shortly before/after t brackets it directly (kalshi<->predexon or
    predexon<->kalshi) instead of the gap being bridged purely off Kalshi's
    coarser 1-minute grid. Given nominal depth (1.0) so a caller refitting
    off this snapshot (e.g. plotting._fit_and_smile_from_snapshot) reproduces
    the same two-sided/band-hinge treatment the cached pipeline itself uses,
    not a one-sided/midpoint fit. A mismatch row (Predexon fresh but
    disagreeing with Kalshi) is banded the same way, off a Kalshi-ONLY
    bracket (not the union) -- Predexon's own quote is exactly what's being
    distrusted there, so it shouldn't reenter as a bracket edge, but Kalshi's
    own price is still only known at 1-minute granularity regardless of why
    it's the one being trusted, so it needs the same band (not point)
    treatment as the stale case -- confirmed on a real mismatch-path case
    too (KXHIGHLAX-26JUL26, 2026-07-26 13:04 PT).
    Point-in-time equivalent of that function's vectorized eval-grid version,
    for diagnostics that need one market snapshot at an arbitrary timestamp
    (e.g. plotting the book right before/after a big implied_spot jump) rather
    than a full day's series. Kept in this module rather than in whatever
    calls it so the fallback policy has exactly one implementation -- this
    used to be reimplemented ad hoc at each call site, which silently drifted
    out of sync with compute_implied_spot_timeseries's own fallback logic
    (e.g. missed the interpolation fix) until the two were compared directly.

    Adds a 'source' column ('predexon'/'kalshi') so a caller can distinguish
    which feed each row's BBO actually came from, and 'x_pos': a market's
    representative x-axis location for plotting -- a between bucket's true
    center ((floor+cap)/2, e.g. 75.5 for B75.5) or a tail's raw threshold
    (e.g. 75 for T75). These never collide even though floor_strike alone can
    (B75.5 and T75 both have floor_strike=75): bucket centers always land on
    a .5, tail thresholds never do.

    Returns a DataFrame (one row per ticker with a quote at-or-before t, sorted
    by x_pos): ticker, floor_strike, cap_strike, strike_type, x_pos, bid, ask,
    bid_depth, ask_depth, source.
    """
    staleness_td = pd.Timedelta(staleness_threshold)
    t_ts = pd.Timestamp(t)
    rows = []
    for tk in sorted(long_df["ticker"].unique()):
        pred = long_df[long_df["ticker"] == tk]
        sub = pred[pred["time"] <= t_ts]
        if sub.empty:
            continue
        last = sub.sort_values("time").iloc[-1]
        floor, cap = last["floor_strike"], last["cap_strike"]
        x_pos = (floor + cap) / 2.0 if pd.notna(cap) else floor
        bid, ask = last["bid"], last["ask"]
        bidd, askd = last.get("bid_depth", np.nan), last.get("ask_depth", np.nan)
        source = "predexon"

        is_stale = (t_ts - last["time"]) > staleness_td
        ksub = kalshi_backup[kalshi_backup["ticker"] == tk] if not kalshi_backup.empty else kalshi_backup
        if not ksub.empty:
            k_interp = _asof_linear_interp(
                pd.Series([t_ts]), ksub.sort_values("time").rename(columns={"time": "k_time"}),
                "k_time", ["bid", "ask"],
            )
            k_bid, k_ask = k_interp["bid"][0], k_interp["ask"][0]
            has_kalshi = not (np.isnan(k_bid) or np.isnan(k_ask))
            mismatch = has_kalshi and (not is_stale) and (abs(bid - k_bid) > mismatch_tol or abs(ask - k_ask) > mismatch_tol)
            if has_kalshi and is_stale:
                # Bracket off the union of both sources (pred's FULL history,
                # not just sub's <=t slice, so a Predexon tick resuming just
                # after t can end the gap on that side) -- see docstring.
                combined = pd.concat([
                    pred[["time", "bid", "ask"]],
                    ksub[["time", "bid", "ask"]],
                ]).sort_values("time").rename(columns={"time": "c_time"})
                c_band = _asof_bracket_band(pd.Series([t_ts]), combined, "c_time")
                bid, ask = c_band["bid"][0], c_band["ask"][0]
                bidd, askd = 1.0, 1.0
                source = "kalshi"
            elif has_kalshi and mismatch:
                # Kalshi-ONLY bracket (not the union `combined` above):
                # Predexon's own quote here is exactly what's being
                # distrusted, so it shouldn't reenter as a bracket edge the
                # way it legitimately can for a genuine gap. Banded, not
                # k_bid/k_ask's linear point, for the same reason the is_stale
                # branch above is -- see _asof_bracket_band's docstring
                # (confirmed on a real mismatch-path case too now, KXHIGHLAX-
                # 26JUL26 2026-07-26 13:04 PT, same shape as the gap-path case).
                k_band = _asof_bracket_band(
                    pd.Series([t_ts]), ksub.sort_values("time").rename(columns={"time": "k_time"}), "k_time")
                bid, ask = k_band["bid"][0], k_band["ask"][0]
                bidd, askd = 1.0, 1.0
                source = "kalshi"

        rows.append({
            "ticker": tk, "floor_strike": floor, "cap_strike": cap, "x_pos": x_pos,
            "strike_type": last["strike_type"], "bid": bid, "ask": ask,
            "bid_depth": bidd, "ask_depth": askd, "source": source,
        })
    return pd.DataFrame(rows).sort_values("x_pos").reset_index(drop=True)


def market_live_matrix(
    long_df: pd.DataFrame,
    kalshi_backup: pd.DataFrame,
    eval_times,
    staleness_threshold: str = DEFAULT_STALENESS_THRESHOLD,
    mismatch_tol: float = DEFAULT_MISMATCH_TOL,
) -> dict:
    """
    Per-ticker (bid, ask, is_live) over `eval_times`, deciding "live" through
    the exact same Predexon-staleness/mismatch check compute_implied_spot_
    timeseries and snapshot_market_quotes use to decide whether a market's
    own BBO -- in the cached fit, and in any snapshot rendered off it --
    actually came from Predexon directly or was overridden by Kalshi's
    coarser 1-min-candle fallback. Staleness ALONE isn't enough: a technically
    fresh Predexon quote (age <= staleness_threshold) can still disagree with
    Kalshi's own candle by more than mismatch_tol, which the cached pipeline
    treats as fallback-worthy too (see compute_implied_spot_timeseries's own
    mismatch handling) -- so is_live is False on BOTH conditions here, not
    staleness alone. Pulled out as the one shared implementation of that
    decision, for any caller (e.g. a diagnostic scanning many days'
    worth of grid points) that needs to know which cells were live without
    paying for snapshot_market_quotes's own per-timestamp full-day rescan
    (see that function's docstring on why it's unsuitable for bulk scanning)
    or re-deriving the fallback policy ad hoc -- exactly the mistake
    snapshot_market_quotes's own docstring describes fixing once already.

    Unlike compute_implied_spot_timeseries, this does NOT reconstruct a
    fallback bid/ask band for a stale/mismatched cell -- nothing here needs
    to fit against one. bid/ask returned are always Predexon's own raw
    last-known quote (backward as-of), never Kalshi-substituted; is_live
    tells the caller which of those to actually trust.

    Returns {ticker: (bid, ask, is_live)}, each a length-len(eval_times)
    array. is_live is False wherever Predexon has no quote yet, its last
    quote is older than staleness_threshold, it mismatches Kalshi's own
    linearly-interpolated candle by more than mismatch_tol on either side, OR
    the quote itself is CROSSED (bid > ask) -- a genuine, if infrequent,
    Predexon data-capture defect confirmed directly on real cached data
    (KXHIGHLAX-26FEB22 2026-02-22 06:28 UTC: three markets simultaneously
    crossed, one by 13 cents -- bid=0.66 vs ask=0.53 -- not a momentary
    quarter-cent bid-ask-bounce artifact, and not economically real: a market
    cannot legitimately sit crossed since either side would be free
    arbitrage). A crossed quote can't be compared against a fitted curve at
    all (which side would 'deviation' even measure against), so it's exactly
    as fallback-worthy as a stale or mismatched one, not merely low-quality
    data to pass through as-is.
    """
    staleness_td = pd.Timedelta(staleness_threshold)
    eval_df = pd.DataFrame({"time": eval_times})
    out = {}
    for tk in sorted(long_df["ticker"].unique()):
        sub = long_df.loc[long_df["ticker"] == tk].sort_values("time")
        merged = pd.merge_asof(
            eval_df, sub[["time", "bid", "ask"]].rename(columns={"time": "quote_time"}),
            left_on="time", right_on="quote_time", direction="backward",
        )
        bid = merged["bid"].to_numpy()
        ask = merged["ask"].to_numpy()
        age = (merged["time"] - merged["quote_time"]).to_numpy()
        is_stale = pd.isna(age) | (age > staleness_td)
        is_crossed = bid > ask

        mismatch = np.zeros(len(eval_df), dtype=bool)
        ksub = kalshi_backup[kalshi_backup["ticker"] == tk] if not kalshi_backup.empty else kalshi_backup
        if not ksub.empty:
            k_interp = _asof_linear_interp(
                eval_df["time"], ksub.sort_values("time").rename(columns={"time": "k_time"}), "k_time", ["bid", "ask"],
            )
            k_bid, k_ask = k_interp["bid"], k_interp["ask"]
            has_kalshi = ~np.isnan(k_bid) & ~np.isnan(k_ask)
            mismatch = (~is_stale) & has_kalshi & ((np.abs(bid - k_bid) > mismatch_tol) | (np.abs(ask - k_ask) > mismatch_tol))

        out[tk] = (bid, ask, ~is_stale & ~mismatch & ~is_crossed)
    return out

