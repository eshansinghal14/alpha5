"""
Plotting/diagnostic functions for the KXHIGHLAX implied-spot analysis
(analysis.ipynb). Kept out of the notebook so each cell there stays a thin
function call rather than a page of matplotlib code -- the notebook owns
what to analyze (which days, which event), this module owns how to show it.
"""
import datetime
import hashlib
import os
import sys
from datetime import timezone

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz
from scipy.stats import norm as _norm

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kalshi_infra"))
import black_scholes
from predexon_functions import load_predexon_long_df, load_kalshi_backup_long_df

# Kalshi KXHIGHLAX resolves on the LA daily high, measured in Pacific Time
# (LAX weather station) -- every plot in this module displays in this tz.
LA_TZ = pytz.timezone('America/Los_Angeles')


# ---------------------------------------------------------------------------
# Vol/variance transforms feeding plot_implied_spot_history
# ---------------------------------------------------------------------------

def variance_till_expiry(spot_ts):
    """
    Realized variance from each point in time until expiry (the last point in
    spot_ts): sum of squared future increments -- the realized quadratic
    variation of the remaining path.

    Compare this against implied_sigma**2 (implied *variance*), not implied_sigma
    directly against sqrt(this). Implied sigma is meant to track sqrt(E[QV]), and
    a single historical day only gives one realized draw of QV -- by Jensen's
    inequality, E[sqrt(QV)] <= sqrt(E[QV]) whenever QV is genuinely random, so
    sqrt(single-path QV) is *systematically* below a well-calibrated IV even with
    zero risk premium and zero bugs. Comparing in variance space (IV^2 vs QV
    directly) avoids baking in that concavity bias.

    Necessarily decreases toward 0 as t -> expiry (fewer future increments remain).
    spot_ts must be indexed by time (DatetimeIndex), chronological.

    spot_ts can contain NaN (thresholded-out/gap stretches -- see fit_normal_
    to_bucket_bid_ask's lambda_min rejection): quadratic variation is computed
    over consecutive VALID (non-NaN) observations only, so a gap's net
    observed move counts as ONE increment spanning however long the gap ran,
    rather than being split into per-row NaN diffs -- "pretend that portion
    of the day doesn't exist," same semantics used elsewhere for gaps, not
    "assume zero movement happened during the gap" (which would bias
    realized variance down exactly when the fit was least trustworthy, i.e.
    when real movement is arguably MORE likely, not less).

    An earlier version computed this via
    `sq.iloc[::-1].cumsum().iloc[::-1].shift(-1).fillna(0.0)` on the raw,
    ungapped diffs -- the trailing .fillna(0.0) was only ever meant to handle
    the single legitimate NaN case (the last row, which has no future
    increment to sum). But pandas' cumsum marks a position's OWN output NaN
    whenever that position's own input is NaN, and after shift(-1) that
    means every row immediately before a gap -- and every row whose "next"
    row falls inside one -- also came out NaN and got silently zeroed by the
    same fillna. That was invisible back when implied_spot never contained
    gaps; once the lambda_min rejection introduced them, it started
    systematically truncating realized variance to 0 right at gap
    boundaries, making "realized variance" read as implausibly low right
    where a day had any thresholded stretch -- confirmed directly: a
    synthetic [80, 81, NaN, 83, 84] path (a real 1-point move, a gap, then a
    real 2-point move) reported remaining variance at row 0 as 2.0 under the
    old formula vs. the correct 6.0 (1^2 + 2^2 + 1^2).
    """
    values = spot_ts.to_numpy()
    n = len(values)
    valid_idx = np.flatnonzero(~np.isnan(values))
    valid_vals = values[valid_idx]
    sq_valid = np.diff(valid_vals) ** 2

    # future_at_valid[k] = sum(sq_valid[k:]) -- total squared increments
    # strictly AFTER the k-th valid observation (the "suffix sum" via a
    # reversed cumsum), with the last valid observation's own remaining
    # variance being exactly 0 (nothing after it).
    future_at_valid = np.concatenate([np.cumsum(sq_valid[::-1])[::-1], [0.0]])

    result = np.full(n, np.nan)
    result[valid_idx] = future_at_valid
    # Gap-interior rows (and any leading NaN before the first valid point)
    # get the NEXT valid observation's value -- "remaining variance as of
    # the next time we actually have a trustworthy read" -- consistent with
    # this function's forward-looking framing elsewhere. Every downstream
    # caller either explicitly re-masks gap rows to NaN for display, or
    # already gets NaN there via implied_sigma being NaN on that same row,
    # so this fill direction doesn't materially affect any current caller --
    # it only exists so every row has a well-defined number to bfill/mask
    # from rather than propagating NaN into rows that DO have a trustworthy
    # implied_spot.
    result = pd.Series(result, index=spot_ts.index).bfill().to_numpy()
    result = np.nan_to_num(result, nan=0.0)  # degenerate: no valid points anywhere in spot_ts
    return pd.Series(result, index=spot_ts.index)


def two_scale_realized_variance(spot_ts, slow_freq='1min'):
    """
    Noise-robust estimate of the TOTAL realized variance over spot_ts's full
    span -- a single scalar (unlike variance_till_expiry, which is a running
    "still to come as of each row" curve) -- via the Zhang/Mykland/Aït-
    Sahalia two-scales realized variance (TSRV) estimator.

    Why this is needed: naive realized variance (sum of squared diffs) is
    biased UP by noise in the tick-to-tick series -- here, LM-fit instability
    (the solve landing on a slightly different mu from one snapshot to the
    next even when nothing real moved), rather than classic bid-ask bounce,
    but the statistical symptom is the same. Noise contributes a term to the
    sum that grows roughly linearly with the NUMBER of ticks included, while
    the true price's contribution to quadratic variation should converge to
    the same value regardless of sampling rate -- so a genuinely noisy
    series shows realized variance INCREASING, not converging, as you sample
    finer. Confirmed directly on 2026-07-29: naive total realized variance
    went 1.13 (1min) -> 1.47 (10s) -> 1.99 (1s) -- climbing, not settling.

    How TSRV fixes this: compute realized variance two ways from the SAME
    finest-grid series -- a "fast" scale (every adjacent pair, i.e. the
    noise-inflated naive RV) and a "slow" scale (RV on a coarser subsample,
    averaged across EVERY possible phase offset of that subsample rather
    than just one arbitrary alignment, so no tick is wasted and the slow-
    scale estimate itself is far less noisy than a single coarse resample).
    Since noise scales with the number of returns summed and true variance
    doesn't, these two give two equations in two unknowns (true variance,
    noise variance) -- subtracting an appropriately-scaled fast-scale RV
    from the slow-scale average cancels the noise term algebraically:

        TSRV = mean_slow_RV - (n_bar / n_fast) * RV_fast

    with the standard (1 - n_bar/n_fast)^-1 finite-sample bias correction
    applied on top (Zhang et al. 2005).

    spot_ts should be the FINEST series available (e.g. the 1s grid) --
    slow_freq sets the coarse scale to compare it against (converted to a
    tick count via spot_ts's own median spacing, same approach iv_scaled_
    to_day_realized_winddown's smoothing window uses). NaN (thresholded-out
    gap) rows are dropped before differencing -- same "diff between
    consecutive VALID observations only" principle as variance_till_expiry,
    though unlike that function this treats the whole gap-free subsequence
    as roughly evenly spaced for the offset-subsampling step, a reasonable
    approximation as long as gaps are a small fraction of the day.

    This assumes the noise is close to IID tick-to-tick, the standard TSRV
    assumption -- worth checking before trusting the correction on a new
    series (e.g. via diffs.autocorr(lag=1): a strongly negative value, near
    the textbook -0.5 for pure bid-ask bounce, means noise dominates and
    TSRV matters a lot; a weak value, as found on 2026-07-29's 1s diffs
    (-0.07 lag-1, only 36% of nonzero moves reversing sign, well below the
    50%+ a mean-reverting noise process would show), means the "noise" is
    real but modest, so TSRV's correction will be real but modest too --
    not enough on its own to fully close a big gap to implied variance.

    Returns 0.0 if spot_ts has fewer than 3 valid points (nothing to
    estimate from). Clipped at 0.0: the correction term can occasionally
    overshoot slightly negative on a very quiet/small sample, which isn't a
    meaningful realized variance in either direction.
    """
    values = spot_ts.dropna().to_numpy()
    times = spot_ts.dropna().index
    n_obs = len(values)
    if n_obs < 3:
        return 0.0

    fast_diffs = np.diff(values)
    n_fast = len(fast_diffs)
    rv_fast = float(np.sum(fast_diffs ** 2))

    median_dt = pd.Series(times).diff().median()
    if pd.isna(median_dt) or median_dt <= pd.Timedelta(0):
        return float(rv_fast)  # degenerate spacing: nothing to subsample against, fall back to naive RV
    slow_k = max(2, min(int(round(pd.Timedelta(slow_freq) / median_dt)), n_obs - 1))

    rv_slow_list = []
    n_slow_list = []
    for offset in range(slow_k):
        sub = values[offset::slow_k]
        if len(sub) < 2:
            continue
        d = np.diff(sub)
        rv_slow_list.append(float(np.sum(d ** 2)))
        n_slow_list.append(len(d))
    if not rv_slow_list:
        return float(rv_fast)

    rv_slow_bar = float(np.mean(rv_slow_list))
    n_bar = float(np.mean(n_slow_list))

    tsrv = rv_slow_bar - (n_bar / n_fast) * rv_fast
    bias_correction = 1.0 - n_bar / n_fast
    if bias_correction <= 0:
        return max(tsrv, 0.0)
    return max(tsrv / bias_correction, 0.0)


def pt_midnight_utc(date):
    """
    PT midnight for a given date, as a tz-aware UTC Timestamp -- the actual
    start of the calendar day KXHIGHLAX resolves on. Same computation as the
    notebook's own day_window_ts (event_start_pt), pulled out here so
    anything comparing implied vs. realized at "day start" anchors on this
    exact point rather than each caller re-deriving it (or worse, silently
    using a day_results series' own row 0 -- see realized_vs_implied_at_day_
    start's docstring for why that's a materially different, less
    meaningful point).
    """
    return pd.Timestamp(LA_TZ.localize(datetime.datetime(date.year, date.month, date.day)).astimezone(timezone.utc))


def realized_vs_implied_at_day_start(df_1min, df_1s, date):
    """
    naive_1min, TSRV, and iv2_start, all anchored at the SAME point in time --
    pt_midnight_utc(date), the actual start of the calendar day this event
    resolves on -- rather than each day_results series' own row 0, which is
    the start of the full 48h fetch window (day_window_ts pulls from the
    PREVIOUS PT midnight to the NEXT one), a full day earlier than the
    relevant boundary.

    Using row 0 as-is compares against the window's very first tick, right
    as the event just opened trading and liquidity hadn't built up yet --
    confirmed directly on 2026-07-29: the very first valid fit there has
    n_two_sided=2 and implied_sigma=1.94, vs. 5-6 markets and a much steadier
    ~1.1-1.7 within a few minutes. That thin opening reading inflated
    iv2_start (making implied look richer than it representatively was) AND
    naive_1min (which then includes whatever noisy move happened during that
    thin stretch, a full day before the actual settlement day even starts) --
    so the two were both distorted, in the same direction, by the same root
    cause, which is exactly what made a bug in the comparison look like a
    bug in the realized-variance calculation itself when first investigated.
    Evaluating everything at the real day boundary instead removes that
    distortion from both sides at once, rather than only one.

    naive_1min/iv2_start: variance_till_expiry/implied_sigma**2 are looked up
    at whichever row of df_1min falls NEAREST pt_midnight_utc(date) (plain
    nearest-index lookup, not an as-of merge -- day_results' own 1-min grid
    is already regular and gapless-by-construction at this granularity, see
    compute_implied_spot_timeseries).

    tsrv: unlike variance_till_expiry, two_scale_realized_variance has no
    "as of this point" concept -- it's a single scalar over whatever series
    it's given -- so getting a TSRV that's ALSO anchored at day start means
    trimming spot_1s to day start first, then computing TSRV over just that
    trimmed remainder, rather than computing it once over the full 48h
    series (which would silently re-include the pre-day-start noise TSRV was
    specifically brought in to correct for).

    Returns (naive_1min, tsrv, iv2_start), NaN for any that can't be
    computed (e.g. a day whose data starts after pt_midnight_utc(date)).
    """
    day_start = pt_midnight_utc(date)

    spot_1min = df_1min.set_index('time')['implied_spot']
    if len(spot_1min):
        rv_1min = variance_till_expiry(spot_1min)
        idx = rv_1min.index.get_indexer([day_start], method='nearest')[0]
        naive_1min = float(rv_1min.iloc[idx])
        sigma_at_start = df_1min.set_index('time')['implied_sigma'].iloc[idx]
        iv2_start = float(sigma_at_start ** 2) if pd.notna(sigma_at_start) else float('nan')
    else:
        naive_1min = float('nan')
        iv2_start = float('nan')

    spot_1s = df_1s.set_index('time')['implied_spot']
    spot_1s_from_start = spot_1s[spot_1s.index >= day_start]
    tsrv = two_scale_realized_variance(spot_1s_from_start, slow_freq='1min') if len(spot_1s_from_start) > 1 else float('nan')

    return naive_1min, tsrv, iv2_start


def iv_scaled_to_day(df, min_days=1.0 / 1440):
    """
    Implied sigma re-expressed as a 1-day-equivalent vol RATE: iv_scaled(t) =
    implied_sigma(t) / sqrt(days remaining to expiry). Raw implied_sigma is the
    stdev of the *terminal* distribution as of t, so it mechanically shrinks
    toward 0 as expiry approaches even if the market's underlying vol RATE is
    unchanged -- dividing by sqrt(time remaining) (the standard Brownian-motion
    time-scaling used to annualize/de-annualize vol) puts every point on a
    common "what would this vol look like with exactly 1 day left" basis, so it
    can be compared directly across the day and against realized_vol_scaled's
    rolling-window estimates below. `expiry` is taken as the series' own last
    point (same convention as variance_till_expiry); min_days floors the
    denominator so this doesn't blow up in the last few seconds before that --
    though even with the floor, the ratio gets naturally noisy in the last
    several minutes before expiry (small implied_sigma fluctuations get
    amplified by the shrinking sqrt(days_remaining) denominator), which is why
    plot_implied_spot_history trims the very end off the scaled-vol panel's
    displayed range rather than trying to floor its way out of that noise.
    """
    expiry = df['time'].iloc[-1]
    days_remaining = (expiry - df['time']).dt.total_seconds() / 86400.0
    days_remaining = days_remaining.clip(lower=min_days)
    return df['implied_sigma'] / np.sqrt(days_remaining)


def iv_scaled_to_day_winddown(df, date, winddown_grid, winddown_pct, min_days=1.0 / 1440):
    """
    Like iv_scaled_to_day, but instead of assuming remaining variance scales
    LINEARLY with days_remaining, rescales time using an empirically-fit
    winddown curve (winddown_grid: ascending hours-to-expiry; winddown_pct:
    a historical cohort's mean % of a day's own total variance_till_expiry
    still outstanding at that many hours to expiry -- see compute_winddown_
    curve / plot_variance_winddown) instead of a straight line.

    effective_days_remaining(t) = wind_frac(hours_to_expiry(t)) *
    days_remaining_at_day_start, where wind_frac is winddown_pct/100 linearly
    interpolated at t's own hours-to-expiry (held flat at the curve's own
    endpoints -- 100% before the cohort's observed start, 0% past its own
    expiry -- for any t outside the curve's fitted range), and days_
    remaining_at_day_start is THIS day's own true days-remaining at its
    11:00-PT-day-before start (same day_start convention as plot_variance_
    winddown / plot_implied_spot_history). Anchoring both endpoints (100% ->
    this day's own true starting days-remaining, 0% -> 0) to the same values
    the linear version (iv_scaled_to_day) uses keeps the two comparable in
    absolute level -- only the SHAPE in between changes from a straight line
    to the empirical one.

    `date` is needed (not derivable from df alone) to compute that anchor,
    same reason plot_implied_spot_history's day_start needs it.
    """
    expiry = df['time'].iloc[-1]
    days_remaining = (expiry - df['time']).dt.total_seconds() / 86400.0
    days_remaining = days_remaining.clip(lower=min_days)
    hours_to_expiry = days_remaining.to_numpy() * 24.0

    day_start = pd.Timestamp(date).tz_localize(LA_TZ) - pd.Timedelta(hours=13)
    days_remaining_at_start = max((expiry - day_start).total_seconds() / 86400.0, min_days)

    wind_frac = np.interp(hours_to_expiry, winddown_grid, winddown_pct,
                           left=winddown_pct[0], right=winddown_pct[-1]) / 100.0
    effective_days_remaining = np.maximum(wind_frac * days_remaining_at_start, min_days)
    return df['implied_sigma'] / np.sqrt(effective_days_remaining)


def iv_scaled_to_day_realized_winddown(df, date, min_days=1.0 / 1440, smooth_window='30min'):
    """
    Like iv_scaled_to_day_winddown, but instead of reweighting by an
    empirically-fit CROSS-day winddown curve, reweights by THIS day's own
    ACTUAL realized variance_till_expiry path -- "what would implied vol look
    like if the winddown shape used to rescale it were exactly what realized,
    in hindsight, on this specific day." This is a look-ahead ORACLE
    reference (it literally uses this day's own future implied_spot path to
    compute variance_till_expiry(t)), not a tradeable/predictive quantity --
    useful only as a best-case ceiling for how well ANY winddown-based
    rescaling (e.g. iv_scaled_to_day_winddown's cross-day empirical curve)
    could possibly track the "true" shape on that one day. If the empirical
    curve's line sits close to this one, the empirical curve is capturing
    most of what's capturable; a persistent gap is this day being genuinely
    unusual relative to the cohort the empirical curve was fit on.

    smooth_window: raw variance_till_expiry(t) is a step function -- it only
    drops exactly when a move realizes, so if this day's variance happens to
    concentrate in one early move, the ratio can crash to ~0 for the REST of
    the day even with hours of calendar time still left, exploding this
    function's output for that whole stretch (dividing by ~0, floored at
    min_days) -- confirmed as the actual cause of this oracle tracking RV
    worse than the empirical winddown curve on some sample days: a single
    day's realized path is one noisy draw and can be genuinely pathological
    in a way a cross-day average can't be. Smoothing variance_till_expiry
    with a CENTERED rolling window first (legitimate here specifically
    because this whole function is already a look-ahead oracle -- centering
    doesn't introduce any NEW look-ahead beyond what variance_till_expiry
    itself already uses) turns that instantaneous step into a decline spread
    over roughly `smooth_window`, so one realized move doesn't cause an
    immediate explosion, while a stretch that's quiet for longer than the
    window still correctly settles toward a small ratio, same as it should.
    Window is converted from a time duration to an approximate ROW count via
    the series' own median sampling interval -- pandas' offset-based
    `.rolling(window=<offset>)` doesn't support center=True -- so this works
    consistently whether df is on a 1s or 1min grid. Pass None to disable
    (raw, unsmoothed step function, the original behavior).
    """
    times_la = df['time'].dt.tz_convert(LA_TZ)
    expiry = df['time'].iloc[-1]
    days_remaining = (expiry - df['time']).dt.total_seconds() / 86400.0
    days_remaining = days_remaining.clip(lower=min_days)

    spot_ts = df.set_index('time')['implied_spot']
    rv = variance_till_expiry(spot_ts).reset_index(drop=True)

    if smooth_window is not None and len(df) > 2:
        median_dt = df['time'].diff().median()
        if pd.notna(median_dt) and median_dt > pd.Timedelta(0):
            window_rows = max(1, int(round(pd.Timedelta(smooth_window) / median_dt)))
            rv = rv.rolling(window_rows, center=True, min_periods=1).mean()

    day_start = pd.Timestamp(date).tz_localize(LA_TZ) - pd.Timedelta(hours=13)
    day_end = times_la.max()
    mask = (times_la >= day_start) & (times_la <= day_end)
    total_var = rv[mask].iloc[0] if mask.any() else rv.iloc[0]
    if total_var <= 0:
        return df['implied_sigma'] / np.sqrt(days_remaining)  # degenerate: nothing to reweight by

    days_remaining_at_start = max((expiry - day_start).total_seconds() / 86400.0, min_days)
    pct_remaining = (rv / total_var).clip(lower=0.0).to_numpy()
    effective_days_remaining = np.maximum(pct_remaining * days_remaining_at_start, min_days)
    return df['implied_sigma'] / np.sqrt(effective_days_remaining)


def realized_vol_scaled(spot_ts, window):
    """
    Rolling realized vol over `window` (e.g. '10min'), scaled to the same
    1-day-equivalent basis as iv_scaled_to_day: sqrt(sum of squared increments
    within the trailing window / (window length in days)). A short, recent
    window is a much more responsive (if noisier) realized-vol estimate than
    variance_till_expiry's cumulative-to-expiry sum -- useful for seeing whether
    the market's implied vol rate is actually tracking what's realizing right now,
    rather than what realizes over the whole rest of the day.

    Clips the rolling sum at 0 before the sqrt: pandas' rolling-window
    accumulation can leave tiny negative floating-point noise (e.g. -1e-18)
    where the true sum of squares is exactly 0, which would otherwise raise a
    spurious "invalid value in sqrt" warning and produce NaN.
    """
    window_days = pd.Timedelta(window) / pd.Timedelta(days=1)
    rolling_sq_sum = spot_ts.diff().pow(2).rolling(window).sum().clip(lower=0.0)
    return np.sqrt(rolling_sq_sum / window_days)


def wing_greeks_timeseries(df, wing_k=1.5, gamma_move=1.0, theta_days=1.0, min_days=1.0 / 1440):
    """
    Greeks for a symmetric OTM "wing" strangle struck off each row of an
    implied_spot/sigma series (df from compute_implied_spot_timeseries[_cached]):
    long 1 contract of the 'greater' tail market at floor = round(mu +
    wing_k*sigma), long 1 contract of the 'less' tail market at floor =
    round(mu - wing_k*sigma) -- the far-OTM strikes this project actually
    trades edge on. wing_k is in implied-sigma units (not a fixed degree
    offset) so the wing distance scales with the market's own uncertainty:
    the same wing_k stays similarly OTM on a calm day and a volatile one.
    Both legs reuse black_scholes.bucket_model_prob_and_grad directly (it
    already vectorizes over numpy arrays via scipy.stats.norm, unlike the
    plain-Python scalar path used only in the hot LM-fit loop) rather than
    reimplementing the tail-probability formulas.

    "Delta-hedged, no directional exposure": buying both tails equidistant
    (in implied-sigma units) from mu makes the position's net delta (dV/dmu)
    cancel EXACTLY at the moment it's struck -- norm.pdf((mu-floor_hi)/sigma)
    == norm.pdf((floor_lo-mu)/sigma) by symmetry of the normal pdf about mu.
    So here the wing structure itself starts delta-neutral; no separate hedge
    leg is modeled. wing_delta is returned anyway so that near-zero is
    visible directly rather than assumed (it drifts slightly off zero after
    the strikes are struck, as mu/sigma move and the strikes -- fixed at
    trade time for gamma/theta, see below -- stay put).

    Returns a DataFrame (time, wing_value, wing_delta, wing_vega, wing_gamma,
    wing_theta, floor_hi, floor_lo), one row per row of df:
      - wing_value: combined price of both legs (0 to 2 -- each leg is its
        own [0, 1] probability/price, i.e. $ per contract on a $1-settlement
        market).
      - wing_delta: dV/dmu, included per the above.
      - wing_vega: dV/d(iv_scaled_to_day) -- value increase per 1-point rise
        in the SAME 1-day-scaled vol the vol panel plots (not raw
        implied_sigma, which mechanically shrinks toward expiry regardless of
        whether the market's vol RATE actually changed -- see
        iv_scaled_to_day's docstring). Chain rule: sigma = iv_scaled *
        sqrt(days_remaining), so dV/d(iv_scaled) = dV/dsigma *
        sqrt(days_remaining).
      - wing_gamma: realized P&L of this SAME struck position (floor_hi/
        floor_lo fixed at their trade-time values) if spot then realizes a
        `gamma_move`-degree move off mu, averaged over +move and -move to
        cancel any residual first-order (delta) drift and isolate the pure
        convexity effect: 0.5*(V(mu+move) + V(mu-move)) - V(mu).
      - wing_theta: LOCAL rate of value change w.r.t. days_remaining, with the
        vol RATE (iv_scaled_to_day) held fixed and mu unchanged, linearly
        extrapolated to a full `theta_days`-sized step -- i.e. "at the
        current decay rate, what would a day of theta cost," not an actual
        simulated jump forward. Deliberately NOT computed by shrinking
        days_remaining by theta_days and repricing (as a naive finite
        difference would): these markets only run ~1.5 days total, so for
        most of the trading day days_remaining < theta_days, and stepping an
        actual day forward runs past expiry -- there's no sensible "reprice
        with negative time left." The analytic derivative has no such floor
        and stays comparable across the whole day, the same reasoning
        iv_scaled_to_day itself uses to stay comparable across time. Chain
        rule: sigma = iv_scaled*sqrt(days_remaining), so d(sigma)/d(days_
        remaining) = iv_scaled/(2*sqrt(days_remaining)), and wing_theta =
        -dV/dsigma * d(sigma)/d(days_remaining) * theta_days (negative: time
        passing shrinks days_remaining, which shrinks sigma).

    Rows where sigma == 0 (fit_normal_to_bucket_bid_ask's near-certain-outcome
    point-mass short-circuit -- see its docstring) are returned as NaN across
    every greek: there's no bell curve to strike wings off of or differentiate,
    same reasoning as bucket_model_prob_and_grad's own sigma==0 caveat.
    """
    mu = df['implied_spot'].to_numpy()
    sigma = np.where(df['implied_sigma'].to_numpy() > 0, df['implied_sigma'].to_numpy(), np.nan)

    expiry = df['time'].iloc[-1]
    days_remaining = (expiry - df['time']).dt.total_seconds().to_numpy() / 86400.0
    days_remaining = np.clip(days_remaining, min_days, None)
    iv_scaled = sigma / np.sqrt(days_remaining)

    offset = wing_k * sigma
    floor_hi = np.round(mu + offset)
    floor_lo = np.round(mu - offset)

    def leg(mu_, sigma_):
        p_hi, dmu_hi, dsig_hi = black_scholes.bucket_model_prob_and_grad(mu_, sigma_, floor_hi, None, 'greater')
        p_lo, dmu_lo, dsig_lo = black_scholes.bucket_model_prob_and_grad(mu_, sigma_, floor_lo, None, 'less')
        return p_hi + p_lo, dmu_hi + dmu_lo, dsig_hi + dsig_lo

    value, delta, dsigma = leg(mu, sigma)
    vega = dsigma * np.sqrt(days_remaining)

    value_up, _, _ = leg(mu + gamma_move, sigma)
    value_dn, _, _ = leg(mu - gamma_move, sigma)
    gamma = 0.5 * (value_up + value_dn) - value

    dsigma_d_days = iv_scaled / (2.0 * np.sqrt(days_remaining))
    theta = -dsigma * dsigma_d_days * theta_days

    return pd.DataFrame({
        'time': df['time'],
        'wing_value': value,
        'wing_delta': delta,
        'wing_vega': vega,
        'wing_gamma': gamma,
        'wing_theta': theta,
        'floor_hi': floor_hi,
        'floor_lo': floor_lo,
    })


def _padded_ylim(*series, pad_frac=0.08):
    """Min/max across series with a fractional margin, so lines don't sit flush on the axes edge."""
    vals = pd.concat([s.dropna() for s in series if s is not None and not s.empty])
    if vals.empty:
        return None
    lo, hi = vals.min(), vals.max()
    pad = (hi - lo) * pad_frac if hi > lo else (abs(hi) * pad_frac or 0.5)
    return lo - pad, hi + pad


def _decimate(stride, *series):
    """
    Uniform index stride applied only to what actually gets handed to
    ax.plot() -- a single line with ~100k points (1s grid over a trading day)
    measured ~1.4s to render on its own, and plot_implied_spot_history draws
    up to 9 such lines per day. All the real math (rolling realized vol,
    variance-to-expiry, ylim bounds) still runs on the full-resolution series;
    this only thins the vertices matplotlib has to rasterize, which at this
    point density is visually lossless (a human can't resolve single-second
    wiggles over a 24h+ x-axis anyway) and doesn't touch the underlying
    cached fit.
    """
    if stride <= 1:
        return series
    return tuple(s.iloc[::stride] for s in series)


_MAX_PLOT_POINTS = 3000

_VARIANCE_FREQ_PALETTE = ['#e74c3c', '#f39c12', '#8e44ad', '#16a085', '#2c3e50']
_GAP_COLOR = '#e74c3c'


def _nan_gap_intervals(times, is_nan):
    """
    (start, end) pairs, one per maximal consecutive run of NaN in is_nan --
    compute_implied_spot_timeseries now keeps every row of the full eval
    grid (never drops one), leaving implied_spot/implied_sigma as NaN
    wherever fit_normal_to_bucket_bid_ask's lambda_min check rejected that
    point as too poorly constrained to report (see that function's and
    compute_implied_spot_timeseries's docstrings) -- this locates those
    stretches so the plotting layer can shade them explicitly rather than
    relying on matplotlib's default "just don't draw a line through NaN"
    behavior, which reads as a rendering gap, not a deliberate "no estimate
    here." Bounds each interval at the surrounding REAL (non-NaN) timestamps
    (one row before the gap starts, one row after it ends) so the shading
    visually spans exactly the stretch the line is actually broken over.
    """
    intervals = []
    n = len(is_nan)
    is_nan_arr = np.asarray(is_nan)
    times_arr = np.asarray(times)
    i = 0
    while i < n:
        if is_nan_arr[i]:
            start_idx = max(i - 1, 0)
            j = i
            while j < n and is_nan_arr[j]:
                j += 1
            end_idx = min(j, n - 1)
            intervals.append((times_arr[start_idx], times_arr[end_idx]))
            i = j
        else:
            i += 1
    return intervals


def _shade_gaps(ax, gap_intervals, label=None):
    """Shade each (start, end) gap interval on ax -- see _nan_gap_intervals. Only the FIRST
    interval gets `label` (if given), so a legend shows one entry for the whole family of spans
    instead of one per gap."""
    for gi, (gstart, gend) in enumerate(gap_intervals):
        ax.axvspan(gstart, gend, color=_GAP_COLOR, alpha=0.12, zorder=0, lw=0,
                   label=(label if gi == 0 else None))


def _gap_fraction(df, date):
    """
    Fraction (0.0-1.0) of df's own displayed window -- 11:00 PT the day
    before -> its last data point, same day_start/day_end convention used
    throughout this module (plot_implied_spot_history, _build_winddown_per_
    day) -- that's a lambda_min-rejected (NaN implied_spot) gap. 0.0 for a
    fully trustworthy day; 1.0 if nothing in the window was ever fit.
    Deliberately restricted to that window rather than checking df's full
    NaN status: the raw grid compute_implied_spot_timeseries_cached is built
    over typically extends a day earlier/later than the market actually
    existed, so a leading NaN from before the event's markets were even
    listed isn't a "gap" a caller ranking days by this cares about.

    Sensitive to df's own sampling frequency: a day with a near-0 fraction
    at 1min can still have a handful of individually-rejected 1s ticks
    (brief, sub-minute wobbles the coarser grid never samples at all)
    inflating the fraction well above what a coarser grid would show -- rank
    days off a common, coarse grid (e.g. the 1min entry in variance_freqs,
    not whatever frequency the primary series happens to be) for a fair
    comparison across days/frequencies.
    """
    times_la = df['time'].dt.tz_convert(LA_TZ)
    day_ref = pd.Timestamp(date).tz_localize(LA_TZ)
    day_start = day_ref - pd.Timedelta(hours=13)
    day_end = times_la.max()
    mask = ((times_la >= day_start) & (times_la <= day_end)).to_numpy()
    spot_masked = df['implied_spot'].to_numpy()[mask]
    if len(spot_masked) == 0:
        return 1.0
    return float(np.isnan(spot_masked).mean())


def _lookup_ticker(day_results, ticker):
    """First (ticker, df, date) entry in day_results matching `ticker`, or None."""
    for tk, df, date in day_results:
        if tk == ticker:
            return df
    return None


def plot_implied_spot_history(day_results, variance_freqs=None, days_per_row=5, vol_panel_end_trim='10min',
                               wing_k=1.5, winddown_curve=None):
    """
    Stacked in groups of `days_per_row` days (clamped to however many days
    actually have data -- with only 3 valid days, requesting 5 columns would
    otherwise reserve 2 unused, hidden columns that still eat 2/5 of the
    figure's width, squeezing the 3 real plots down for no reason). Each group
    is its own subfigure containing a tight 6-row block per day (implied spot,
    variance-to-expiry, vol-scaled-to-a-day, wing vega, wing gamma, and wing
    theta -- top to bottom with no gap between them, all six rows equal
    height, genuinely sharex-linked so no panel can autoscale to a wider time
    range than the others); groups are separated from each other by normal
    spacing. total_height_frac sets each day's whole 6-row block's height as a
    multiple of its width.

    The bottom three (wing vega/gamma/theta) come from wing_greeks_timeseries
    -- see its docstring for exactly what position they price (a symmetric,
    delta-neutral-at-strike OTM strangle, `wing_k` implied-sigma units out on
    each side, which is what this project actually trades edge on) and what a
    1-point vol move / 1-degree realized move / 1-day theta means for it.

    winddown_curve: optional (grid, pct) pair -- typically the landmark-mean
    curve plot_variance_winddown returns (its 3rd/4th return values), fit on
    a separate, larger day pool than `day_results` itself -- reweighting the
    vol-scaled panel's implied-vol line via iv_scaled_to_day_winddown instead
    of the naive iv_scaled_to_day. If given, that becomes the panel's primary
    (black) implied-vol line, with the old linear-time-scaled version kept as
    a thin grey dashed reference so the correction's effect stays visible
    rather than silently replaced; if omitted (the default), the panel falls
    back to iv_scaled_to_day alone, unchanged from before this param existed.

    The vol-scaled panel's rolling RV10/30/60 lines are computed off a 1min
    grid (via variance_freqs['1min'], if present -- the same day_results_1min
    already fetched for the variance panel above) rather than the primary
    (often 1s) spot_ts: they're rolling-WINDOW estimates, not a per-point
    fit, so the finer grid buys nothing here and is 60x more points to roll
    over for no benefit. Falls back to the primary df's own spot_ts/times_la
    if variance_freqs is omitted or has no '1min' entry.

    X-axis per day (set explicitly on every panel, not just relied on via
    sharex, so none can end up short of the others): 11:00 PT the day before
    expiry -> the actual last available data point for that day (true
    expiry/market close, same endpoint variance_till_expiry sums to -- not an
    earlier artificial cutoff). Y-axes get a small padding margin so lines
    don't sit flush against the plot edge (which reads as cut off). The
    bottom (scaled-vol) panel's own end is separately trimmed back by
    `vol_panel_end_trim`: iv_scaled_to_day's denominator (days remaining to
    expiry) shrinks to nothing right at the end, which amplifies ordinary
    implied_sigma noise into large, uninformative spikes on that one panel
    specifically -- trimmed from its display (and from its own y-limit calc)
    rather than from the spot/variance-to-expiry panels above it, which don't
    have this issue.

    Legends use an explicit `loc` (not the 'best' default) on every panel:
    'best' makes matplotlib search for the least-overlapping position by
    checking candidate positions against every plotted point, which is slow
    -- and triggers a UserWarning -- once a line has ~100k points, as these do.

    Every line actually drawn is decimated to at most _MAX_PLOT_POINTS first
    (see _decimate) -- ylim/mask calculations still use the full-resolution
    series so bounds stay accurate, only the rendered vertices are thinned.

    variance_freqs: optional {label: day_results} dict (e.g. {'1s': day_results,
    '10s': day_results_10s, '1min': day_results_1min}) -- each entry is a full
    day_results list at that sampling frequency, fetched separately (see
    black_scholes.compute_implied_spot_timeseries_cached's freq param). The
    middle panel draws one "Realized var to expiry" line per entry, each
    computed via variance_till_expiry on THAT frequency's own implied_spot
    series, matched to the current day by ticker (not position -- a freq's
    day_results can be fetched/ordered independently). This makes visible how
    much of the fit's second-to-second wobble (see compute_implied_spot_
    timeseries's docstring on the mu/sigma degeneracy ridge) is actually
    getting counted as "realized variance" purely because of how finely the
    series was sampled, vs. surviving at coarser samplings. If omitted,
    falls back to a single line computed from `day_results` itself (its
    previous, only behavior).

    If variance_freqs has BOTH a '1s' and a '1min' entry, an additional
    "Realized var to expiry (noise-corrected)" line is drawn: the naive
    per-freq lines above are sampling-rate sensitive in both directions (see
    two_scale_realized_variance's docstring -- fine grids get inflated by
    LM-fit tick-to-tick noise, coarse grids can misrepresent one real smooth
    move as a single big jump), so this rescales the 1min freq's naive
    variance_till_expiry CURVE by two_scale_realized_variance(1s spot)
    divided by that same curve's own total -- same shape/timing as the 1min
    line, corrected total level. A rescale rather than a from-scratch
    running TSRV: TSRV itself is a whole-span estimator, not a per-row one,
    so this is the practical way to get a noise-corrected running curve
    without recomputing the full two-scale procedure at every row.

    Plots every day in `day_results` (in the order given) with any data --
    it's the CALLER's job to curate that list (e.g. picking days by how
    little of the day fell into a lambda_min-rejected gap, via
    _gap_fraction) rather than this function silently dropping days on its
    own. Gapped stretches within a plotted day are still shaded on every
    panel (see gap_intervals/_shade_gaps below), so a day with a small gap
    displays fine here -- it's just not excluded outright.
    """
    valid = [(ticker, df, date) for ticker, df, date in day_results if not df.empty]
    if not valid:
        print('No historical data to plot.')
        return

    n = len(valid)
    days_per_row = min(days_per_row, n)
    n_groups = int(np.ceil(n / days_per_row))
    colors = plt.get_cmap('tab10')(np.linspace(0, 1, n))

    col_width = 6.5
    total_height_frac = 2.8  # whole 6-row block height as a fraction of col_width
    group_height = col_width * total_height_frac
    vol_trim_td = pd.Timedelta(vol_panel_end_trim)

    fig = plt.figure(figsize=(col_width * days_per_row, group_height * n_groups))
    subfigs = fig.subfigures(n_groups, 1, hspace=0.12)
    if n_groups == 1:
        subfigs = [subfigs]

    for group in range(n_groups):
        group_items = list(zip(valid, colors))[group * days_per_row:(group + 1) * days_per_row]
        axes = subfigs[group].subplots(
            6, days_per_row,
            gridspec_kw={'height_ratios': [1, 1, 1, 1, 1, 1], 'hspace': 0},
            sharex='col',
        )
        axes = np.atleast_2d(axes).reshape(6, days_per_row)

        for col, ((ticker, df, date), color) in enumerate(group_items):
            ax_spot, ax_vol, ax_rv, ax_vega, ax_gamma, ax_theta = (
                axes[0, col], axes[1, col], axes[2, col], axes[3, col], axes[4, col], axes[5, col],
            )

            times_la = df['time'].dt.tz_convert(LA_TZ)
            spot = df['implied_spot']
            spot_ts = df.set_index('time')['implied_spot']
            stride = max(1, int(np.ceil(len(times_la) / _MAX_PLOT_POINTS)))

            day_ref = pd.Timestamp(date).tz_localize(LA_TZ)   # midnight PT on the expiry date
            day_start = day_ref - pd.Timedelta(hours=13)       # 11:00 PT the day before
            day_end = times_la.max()                           # actual expiry: last data point this day

            mask = (times_la >= day_start) & (times_la <= day_end)
            visible_spot = spot[mask]
            # Stretches fit_normal_to_bucket_bid_ask's lambda_min check rejected as
            # too poorly constrained to report (see compute_implied_spot_timeseries's
            # docstring) -- shaded on every panel below, not just the spot one, since
            # all of them (vega/gamma/theta especially) are equally untrustworthy here.
            gap_intervals = _nan_gap_intervals(times_la, spot.isna())

            # -- Implied spot --
            ax_spot.plot(*_decimate(stride, times_la, spot), color=color, lw=1.5, label='Implied spot')
            _shade_gaps(ax_spot, gap_intervals, label='No trustworthy fit')
            ax_spot.set_xlim(day_start, day_end)
            ylim = _padded_ylim(visible_spot)
            if ylim:
                ax_spot.set_ylim(*ylim)
            ax_spot.set_title(ticker, fontsize=12)
            ax_spot.set_ylabel('Implied High Temp (°F)', fontsize=9)
            ax_spot.tick_params(labelsize=8)
            ax_spot.legend(fontsize=9, loc='upper right')
            ax_spot.grid(True, alpha=0.3)
            plt.setp(ax_spot.get_xticklabels(), visible=False)

            # -- Implied variance vs. realized variance to expiry, at each sampled freq --
            iv2 = df['implied_sigma'] ** 2
            ax_vol.plot(*_decimate(stride, times_la, iv2), color='black', lw=1.4,
                        label='Implied variance', zorder=5)
            _shade_gaps(ax_vol, gap_intervals)

            freq_items = variance_freqs.items() if variance_freqs else [('', day_results)]
            rv_masked_for_ylim = []
            for fi, (freq_label, freq_day_results) in enumerate(freq_items):
                freq_df = _lookup_ticker(freq_day_results, ticker)
                if freq_df is None or freq_df.empty:
                    continue
                freq_times_la = freq_df['time'].dt.tz_convert(LA_TZ)
                freq_spot_ts = freq_df.set_index('time')['implied_spot']
                freq_rv = variance_till_expiry(freq_spot_ts).reset_index(drop=True)
                # variance_till_expiry can "look past" a gap and still compute a
                # real (non-NaN) number at a row whose OWN implied_spot is NaN --
                # summing squared FUTURE increments only needs the future path to
                # be known, not the value AT this row -- but plotting that number
                # here would misleadingly suggest a trustworthy estimate exists at
                # a timestamp we've explicitly shaded as "no trustworthy fit."
                # Mask explicitly by this freq's OWN spot NaN status rather than
                # relying on variance_till_expiry's own (inconsistent, by design)
                # NaN propagation.
                freq_rv = freq_rv.where(freq_df['implied_spot'].notna().to_numpy())
                freq_stride = max(1, int(np.ceil(len(freq_times_la) / _MAX_PLOT_POINTS)))
                freq_mask = (freq_times_la >= day_start) & (freq_times_la <= day_end)

                label = f'Realized var to expiry ({freq_label})' if freq_label else 'Realized variance to expiry'
                ax_vol.plot(*_decimate(freq_stride, freq_times_la, freq_rv),
                            color=_VARIANCE_FREQ_PALETTE[fi % len(_VARIANCE_FREQ_PALETTE)],
                            lw=1.2, alpha=0.85, label=label)
                rv_masked_for_ylim.append(freq_rv[freq_mask])

            # -- Noise-corrected realized var to expiry (see docstring): only
            # drawn if both a '1s' and a '1min' freq entry are available --
            # 1s supplies the fast scale two_scale_realized_variance needs,
            # 1min supplies the curve whose shape gets rescaled.
            tsrv_1s_df = _lookup_ticker(variance_freqs.get('1s', []), ticker) if variance_freqs else None
            tsrv_1min_df = _lookup_ticker(variance_freqs.get('1min', []), ticker) if variance_freqs else None
            if tsrv_1s_df is not None and not tsrv_1s_df.empty and tsrv_1min_df is not None and not tsrv_1min_df.empty:
                tsrv_total = two_scale_realized_variance(
                    tsrv_1s_df.set_index('time')['implied_spot'], slow_freq='1min')
                naive_1min_curve = variance_till_expiry(
                    tsrv_1min_df.set_index('time')['implied_spot']).reset_index(drop=True)
                naive_1min_total = naive_1min_curve.iloc[0]
                if naive_1min_total > 0:
                    corrected_curve = naive_1min_curve * (tsrv_total / naive_1min_total)
                    corrected_curve = corrected_curve.where(tsrv_1min_df['implied_spot'].notna().to_numpy())
                    corrected_times_la = tsrv_1min_df['time'].dt.tz_convert(LA_TZ)
                    corrected_stride = max(1, int(np.ceil(len(corrected_times_la) / _MAX_PLOT_POINTS)))
                    corrected_mask = (corrected_times_la >= day_start) & (corrected_times_la <= day_end)
                    ax_vol.plot(*_decimate(corrected_stride, corrected_times_la, corrected_curve),
                                color='#17becf', lw=1.8, ls='--', alpha=0.9,
                                label='Realized var to expiry (noise-corrected)', zorder=6)
                    rv_masked_for_ylim.append(corrected_curve[corrected_mask])
            ax_vol.set_xlim(day_start, day_end)

            ylim = _padded_ylim(iv2[mask], *rv_masked_for_ylim)
            if ylim:
                ax_vol.set_ylim(*ylim)
            ax_vol.set_ylabel('Variance (°F²)', fontsize=9)
            ax_vol.tick_params(labelsize=8)
            ax_vol.legend(fontsize=8, loc='upper right')
            ax_vol.grid(True, alpha=0.3)
            plt.setp(ax_vol.get_xticklabels(), visible=False)

            # -- Implied vol vs. rolling realized vol, both scaled to a 1-day equivalent --
            # RV10/30/60 are computed off a 1min grid (via variance_freqs['1min'], the
            # same day_results_1min already fetched for the variance panel above) rather
            # than the primary (1s) spot_ts -- these are rolling-WINDOW estimates, not a
            # per-point fit, so there's no benefit to the finer grid here and it's 60x
            # fewer points to roll over; falls back to the primary spot_ts/times_la if a
            # '1min' entry isn't present in variance_freqs.
            rv_freq_df = _lookup_ticker(variance_freqs.get('1min', []), ticker) if variance_freqs else None
            if rv_freq_df is not None and not rv_freq_df.empty:
                rv_times_la = rv_freq_df['time'].dt.tz_convert(LA_TZ)
                rv_spot_ts = rv_freq_df.set_index('time')['implied_spot']
            else:
                rv_times_la = times_la
                rv_spot_ts = spot_ts
            rv_stride = max(1, int(np.ceil(len(rv_times_la) / _MAX_PLOT_POINTS)))

            rv10 = realized_vol_scaled(rv_spot_ts, '10min').reset_index(drop=True)
            rv30 = realized_vol_scaled(rv_spot_ts, '30min').reset_index(drop=True)
            rv60 = realized_vol_scaled(rv_spot_ts, '60min').reset_index(drop=True)
            # Same masking-by-own-NaN principle as freq_rv above -- rolling().sum()'s
            # default min_periods behavior already tends toward NaN across a gap, but
            # mask explicitly rather than relying on that as an implementation detail.
            rv_notna_mask = rv_spot_ts.notna().to_numpy()
            rv10 = rv10.where(rv_notna_mask)
            rv30 = rv30.where(rv_notna_mask)
            rv60 = rv60.where(rv_notna_mask)

            vol_day_end = max(day_start, day_end - vol_trim_td)
            vol_mask = (times_la >= day_start) & (times_la <= vol_day_end)
            rv_vol_mask = (rv_times_la >= day_start) & (rv_times_la <= vol_day_end)

            ylim_series = [rv10[rv_vol_mask], rv30[rv_vol_mask], rv60[rv_vol_mask]]
            if winddown_curve is not None and winddown_curve[0] is not None:
                wd_grid, wd_pct = winddown_curve
                iv_wd = iv_scaled_to_day_winddown(df, date, wd_grid, wd_pct)
                # Oracle reference (grey dashed): reweights by THIS day's own
                # realized variance_till_expiry path instead of the fitted
                # cross-day curve -- a look-ahead best case, not a linear-time
                # assumption -- see iv_scaled_to_day_realized_winddown's docstring.
                iv_oracle = iv_scaled_to_day_realized_winddown(df, date)
                ax_rv.plot(*_decimate(stride, times_la, iv_oracle), color='#999999', lw=1.0, ls='--',
                           label='IV (oracle winddown)', zorder=3)
                ax_rv.plot(*_decimate(stride, times_la, iv_wd), color='black', lw=1.4,
                           label='IV (winddown-scaled)', zorder=4)
                ylim_series.append(iv_wd[vol_mask])
                ylim_series.append(iv_oracle[vol_mask])
            else:
                iv_scaled = iv_scaled_to_day(df)
                ax_rv.plot(*_decimate(stride, times_la, iv_scaled), color='black', lw=1.3,
                           label='Implied vol (scaled/day)', zorder=4)
                ylim_series.append(iv_scaled[vol_mask])
            ax_rv.plot(*_decimate(rv_stride, rv_times_la, rv10), color='#9467bd', lw=0.8, alpha=0.75, label='RV 10min')
            ax_rv.plot(*_decimate(rv_stride, rv_times_la, rv30), color='#e74c3c', lw=0.9, alpha=0.8, label='RV 30min')
            ax_rv.plot(*_decimate(rv_stride, rv_times_la, rv60), color='#2ca02c', lw=1.0, alpha=0.85, label='RV 60min')
            _shade_gaps(ax_rv, gap_intervals)
            ax_rv.set_xlim(day_start, vol_day_end)

            ylim = _padded_ylim(*ylim_series)
            if ylim:
                ax_rv.set_ylim(*ylim)
            ax_rv.set_ylabel('Vol (°F/√day)', fontsize=9)
            ax_rv.tick_params(labelsize=8)
            ax_rv.legend(fontsize=8, loc='upper left')
            ax_rv.grid(True, alpha=0.3)
            plt.setp(ax_rv.get_xticklabels(), visible=False)

            # -- Wing greeks: symmetric OTM strangle, wing_k implied-sigma units out
            # on each side (delta-neutral at strike -- see wing_greeks_timeseries) --
            wing = wing_greeks_timeseries(df, wing_k=wing_k)

            ax_vega.plot(*_decimate(stride, times_la, wing['wing_vega']), color=color, lw=1.3,
                         label=f'Wing vega ($/vol pt, k={wing_k}σ)')
            ax_vega.axhline(0, color='#999999', lw=0.7, ls='--')
            _shade_gaps(ax_vega, gap_intervals)
            ax_vega.set_xlim(day_start, vol_day_end)
            ylim = _padded_ylim(wing['wing_vega'][vol_mask])
            if ylim:
                ax_vega.set_ylim(*ylim)
            ax_vega.set_ylabel('Vega ($/vol pt)', fontsize=9)
            ax_vega.tick_params(labelsize=8)
            ax_vega.legend(fontsize=8, loc='upper right')
            ax_vega.grid(True, alpha=0.3)
            plt.setp(ax_vega.get_xticklabels(), visible=False)

            ax_gamma.plot(*_decimate(stride, times_la, wing['wing_gamma']), color=color, lw=1.3,
                          label='Wing gamma P&L (1° move)')
            ax_gamma.axhline(0, color='#999999', lw=0.7, ls='--')
            _shade_gaps(ax_gamma, gap_intervals)
            ax_gamma.set_xlim(day_start, vol_day_end)
            ylim = _padded_ylim(wing['wing_gamma'][vol_mask])
            if ylim:
                ax_gamma.set_ylim(*ylim)
            ax_gamma.set_ylabel('Gamma P&L ($)', fontsize=9)
            ax_gamma.tick_params(labelsize=8)
            ax_gamma.legend(fontsize=8, loc='upper right')
            ax_gamma.grid(True, alpha=0.3)
            plt.setp(ax_gamma.get_xticklabels(), visible=False)

            ax_theta.plot(*_decimate(stride, times_la, wing['wing_theta']), color=color, lw=1.3,
                          label='Wing theta (1-day)')
            ax_theta.axhline(0, color='#999999', lw=0.7, ls='--')
            _shade_gaps(ax_theta, gap_intervals)
            ax_theta.set_xlim(day_start, vol_day_end)
            ylim = _padded_ylim(wing['wing_theta'][vol_mask])
            if ylim:
                ax_theta.set_ylim(*ylim)

            ax_theta.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=LA_TZ))
            ax_theta.xaxis.set_major_locator(mdates.HourLocator(interval=3))
            plt.setp(ax_theta.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)
            ax_theta.set_ylabel('Theta ($/day)', fontsize=9)
            ax_theta.set_xlabel('Time (PT)', fontsize=10)
            ax_theta.tick_params(labelsize=8)
            ax_theta.legend(fontsize=8, loc='upper right')
            ax_theta.grid(True, alpha=0.3)

        # hide any unused columns in this group's grid
        for col in range(len(group_items), days_per_row):
            for row in range(6):
                axes[row, col].axis('off')

    fig.suptitle(
        'KXHIGHLAX – Implied Spot, Variance to Expiry, Vol Scaled to 1 Day, and Wing Greeks',
        fontsize=15,
    )
    fig.savefig('implied_spot_history.png', dpi=150, bbox_inches='tight')
    plt.show()


# ---------------------------------------------------------------------------
# Realized-variance winddown shape (feeds the predict-realized-vs-implied
# strategy: how remaining variance should be predicted as a function of time
# left, if not linearly in calendar time)
# ---------------------------------------------------------------------------

def _invert_monotonic(x, y):
    """
    Generic monotonic inverter: given (x, y) with x strictly increasing and y
    non-decreasing in x, return (y_unique, x_at_y) -- y's flat runs (ties)
    collapsed to a single point each, at the MEAN x across that run, so
    y_unique is strictly increasing and usable as np.interp's xp to invert
    "x as a function of y."

    Used twice in plot_variance_winddown: (1) per day, inverting (hours_to_
    expiry, pct_remaining) to get that day's OWN hours-to-expiry as a
    function of %-remaining -- ties there come from a quiet stretch with zero
    implied_spot movement, or the stretch before the day's first tick; (2)
    once more on the resulting landmark MEAN curve itself, to re-express it
    back in (hours_to_expiry -> pct) form for iv_scaled_to_day_winddown.

    np.maximum.accumulate guards against y dipping by float noise across an
    exactly-tied run (e.g. variance_till_expiry's cumsum of non-negative
    terms should be non-decreasing, but isn't guaranteed bit-exact) --
    without it a tiny backward wobble would break the "strictly increasing
    after collapsing ties" contract.
    """
    y = np.maximum.accumulate(y)
    is_new = np.empty(len(y), dtype=bool)
    is_new[0] = True
    is_new[1:] = np.diff(y) > 0
    group_id = np.cumsum(is_new) - 1
    n_groups = group_id[-1] + 1
    x_avg = np.bincount(group_id, weights=x, minlength=n_groups) / np.bincount(group_id, minlength=n_groups)
    return y[is_new], x_avg


def _build_winddown_per_day(day_results):
    """
    Shared by plot_variance_winddown and compute_winddown_curve: for every
    cached day, (hours_to_expiry, pct_remaining) in increasing-hte order --
    pct_remaining normalized to that day's OWN variance_till_expiry value at
    its 11:00-PT-day-before start (see plot_variance_winddown's docstring for
    why).

    A day's own thresholded-out stretches (implied_spot NaN -- see
    fit_normal_to_bucket_bid_ask's lambda_min rejection) get NaN inserted
    into pct at exactly those (still chronologically-ordered) positions,
    rather than being dropped from the array. Two things fall out of that for
    free: a plotted line through (hte, pct) breaks and leaves the stretch
    blank instead of drawing a straight chord across it (matplotlib skips
    NaN automatically -- dropping the points instead, as an earlier version
    of this did, reconnects across possibly-long gaps and reads as a sharp,
    fabricated spike in the per-day line); and np.interp(grid, hte, pct)
    naturally produces NaN for every grid point strictly between the gap's
    bounding real points too, with no separate masking step needed --
    np.nanmean across days then lets other days that DO have real data at
    that grid point still contribute to the aggregate mean ("pretend that
    portion of the day doesn't exist," same semantics as the implied-spot
    gap shading elsewhere, but per-day rather than dropping the whole day).

    variance_till_expiry can produce a real (non-NaN) rv value at a row whose
    OWN implied_spot is NaN (it sums FUTURE increments, so it can "look past"
    a gap and recover once real data resumes) -- so gap detection here uses
    spot_ts's own NaN pattern, not rv/pct's, which would under-detect gaps.

    Returns (per_day, max_hours): per_day is a list of (hte, pct, date,
    gap_pct_intervals). gap_pct_intervals (the %-remaining span each gap
    covers) is needed only by the landmark-mean method in
    plot_variance_winddown, which must feed _invert_monotonic clean,
    NaN-free input and so can't rely on np.interp's automatic NaN
    propagation the way the vertical mean and raw per-day plot lines do.
    max_hours is the largest hte observed across days (the common grid's
    upper bound).
    """
    valid = [(ticker, df, date) for ticker, df, date in day_results if not df.empty]
    per_day = []
    max_hours = 0.0
    for ticker, df, date in valid:
        times_la = df['time'].dt.tz_convert(LA_TZ)
        spot_ts = df.set_index('time')['implied_spot']
        rv = variance_till_expiry(spot_ts).reset_index(drop=True)
        hours_to_expiry = (df['time'].iloc[-1] - df['time']).dt.total_seconds() / 3600.0

        day_ref = pd.Timestamp(date).tz_localize(LA_TZ)
        day_start = day_ref - pd.Timedelta(hours=13)
        day_end = times_la.max()
        mask = (times_la >= day_start) & (times_la <= day_end)
        if not mask.any():
            continue

        is_valid = spot_ts.reset_index(drop=True)[mask.to_numpy()].notna().to_numpy()
        if not is_valid.any():
            continue
        rv_masked = rv[mask].to_numpy()
        total_var = rv_masked[np.flatnonzero(is_valid)[0]]
        if pd.isna(total_var) or total_var <= 0:
            continue
        pct_remaining = rv / total_var * 100.0

        hte = hours_to_expiry[mask].to_numpy()[::-1]      # chronological -> increasing-x
        pct = pct_remaining[mask].to_numpy()[::-1].copy()
        gap_mask = ~is_valid[::-1]                        # aligned with hte/pct

        gap_pct_intervals = _nan_gap_intervals(pct, gap_mask) if gap_mask.any() else []
        pct[gap_mask] = np.nan

        per_day.append((hte, pct, date, gap_pct_intervals))
        max_hours = max(max_hours, hte.max())
    return per_day, max_hours


def _mask_gap_intervals(curve, grid, gap_intervals):
    """NaN out grid points falling within any (start, end) gap interval -- see _build_winddown_per_day."""
    if not gap_intervals:
        return curve
    curve = curve.copy()
    for gstart, gend in gap_intervals:
        curve[(grid >= gstart) & (grid <= gend)] = np.nan
    return curve


def _smooth_curve(values, grid, window_x):
    """
    Centered rolling-mean smoothing over a window `window_x` wide, in the
    same units as `grid` (hours for the vertical winddown curve, percentage
    points for the landmark one). The cross-day mean at any single grid
    point is still just an average of a handful of days' worth of noisy
    per-day curves (see plot_variance_winddown's own goodness-of-fit
    tables -- residuals of several percentage points against even the mean
    curve itself), so the raw np.nanmean curve is jittery point-to-point in
    a way the true underlying winddown shape has no reason to be -- smoothed
    here once, centrally, so every caller (the plotted line, the returned
    curve, iv_scaled_to_day_winddown) sees the same smoothed shape rather
    than each needing its own ad hoc smoothing.

    `grid` is assumed uniformly spaced (true for every np.linspace grid this
    is called on). min_periods=1 lets the window shrink near the edges
    instead of eating into them with NaN, so the curve's boundary values
    (100% at hte=0... err, at hte=grid[0]==0, 0% at %-remaining=0 for the
    landmark curve) stay anchored rather than getting smoothed toward their
    neighbors and away from their true, exactly-known endpoint values.
    """
    if len(grid) < 2:
        return values
    spacing = grid[1] - grid[0]
    window = max(3, int(round(window_x / spacing)))
    if window % 2 == 0:
        window += 1
    return pd.Series(values).rolling(window, center=True, min_periods=1).mean().to_numpy()


def compute_winddown_curve(day_results, grid_points=241, smooth_hours=1.5):
    """
    The vertical-mean empirical winddown curve (see plot_variance_winddown)
    as a standalone (grid_hours, mean_pct) pair, for reuse outside that
    plotting function -- e.g. iv_scaled_to_day_winddown reweighting implied
    vol by this shape instead of assuming a linear one. grid_hours is
    ascending hours-to-expiry from 0 to the longest cached day's span;
    mean_pct is this cohort's mean % of a day's own total variance_till_
    expiry still outstanding at that many hours to expiry (100% at
    grid_hours[-1], 0% at grid_hours[0] == 0), computed via np.nanmean so a
    day's own thresholded-out stretch (see _build_winddown_per_day's NaN-in-
    pct handling) doesn't drag down the mean at that grid point -- other
    days with real data there still contribute -- and then smoothed over a
    `smooth_hours`-wide centered window (see _smooth_curve) since the raw
    cross-day mean is jittery with only a handful of days in the cohort.
    """
    per_day, max_hours = _build_winddown_per_day(day_results)
    if not per_day:
        return np.array([0.0, 1.0]), np.array([0.0, 100.0])  # degenerate fallback: ~linear
    grid = np.linspace(0, max_hours, grid_points)
    curves = np.vstack([np.interp(grid, hte, pct) for hte, pct, _, _ in per_day])
    mean_curve = np.nanmean(curves, axis=0)
    return grid, _smooth_curve(mean_curve, grid, smooth_hours)


def plot_variance_winddown(day_results, figsize=(10, 6.5), smooth_hours=1.5, smooth_pct=5.0):
    """
    Overlays, for every cached day in `day_results`, the % of that day's
    total variance_till_expiry still outstanding as a function of hours-to-
    expiry -- i.e. how "wound down" realized variance actually is at each
    point in the trading day, across as many historical days as are cached.

    Each day is normalized by its OWN variance_till_expiry value at that
    day's start (11:00 PT the day before, same day_start convention as
    plot_implied_spot_history -- not day 1's value or a global constant), so
    every curve starts at 100% regardless of how much total variance that
    particular day actually realized. This plot is about the SHAPE of the
    winddown, not the absolute variance level (which already varies day to
    day for other reasons -- see the spot/variance panels above).

    A straight diagonal from (max hours, 100%) to (0, 0%) is drawn as a
    reference: that's what iv_scaled_to_day's linear-in-calendar-time
    days_remaining scaling implicitly assumes. Systematic deviation of the
    actual (per-day and mean) curves from that diagonal is exactly the
    seasonality a realized-vol PREDICTION model needs to account for, rather
    than assuming remaining variance scales linearly with time left -- see
    the notebook discussion this feeds.

    Draws TWO mean curves, averaged across the same per-day curves but along
    opposite axes -- they answer different questions and can disagree
    whenever the days' curves are shaped similarly but shifted in TIME
    relative to each other:
      - "Mean (vertical)" (black solid): the standard approach -- per-day
        curves linearly interpolated onto a common hours-to-expiry grid, then
        averaged at each grid point (curves.mean(axis=0)). If different days'
        variance resolves in a sharp move at genuinely different times (e.g.
        one day's marine-layer burn-off at hour 5, another's at hour 15),
        vertical averaging blends that into a smooth ramp from 5 to 15 that
        no single day actually had.
      - "Mean (landmark)" (navy dash-dot): inverts each day's curve via
        _invert_monotonic to get that day's OWN hours-to-expiry as a function
        of %-remaining, interpolates THAT onto a common %-remaining grid, and
        averages -- i.e. "the mean TIME at which days cross a given %-
        remaining level," not "the mean %-remaining at a given time." This
        preserves a sharp characteristic event time (e.g. "days typically
        cross 50% remaining around hour 10") instead of smearing it out,
        at the cost of being a odder inversion to reason about directly.

    Both mean curves are smoothed (see _smooth_curve) immediately after the
    cross-day nanmean and before anything else touches them -- goodness-of-
    fit tables, the plotted line, and the returned curves all see the SAME
    smoothed shape, rather than a raw per-grid-point average that's jittery
    with only a handful of days in the cohort. `smooth_hours`/`smooth_pct`
    set each curve's smoothing window in its own native units (hours for the
    vertical curve, percentage points for the landmark curve).

    Prints a goodness-of-fit table for EACH mean curve, in whichever units
    that curve is actually averaged over -- the vertical mean's residuals are
    in percentage points (%-remaining is what's being averaged at fixed
    time), the landmark mean's are in hours (time is what's being averaged at
    fixed %). Both tables compare each day's own curve against its (smoothed)
    cohort mean and against a straight-line baseline in that same space,
    restricted to that day's own observed range (so a shorter/incomplete day
    isn't scored against np.interp's flat extrapolation outside its own
    data) -- plus an overall summary and the fraction of the linear
    baseline's squared error the empirical mean explains. This is the actual
    answer to "is this mean curve worth using instead of assuming linear
    winddown," not just a visual impression from the plot -- and comparing
    the two tables' R² is the actual answer to "which averaging method fits
    the data better."

    Returns (vert_grid, vert_pct, landmark_grid, landmark_pct): the vertical
    mean curve exactly as compute_winddown_curve would return it, and the
    landmark mean re-inverted back into the same (ascending hours -> pct)
    form (via _invert_monotonic on the landmark curve itself) so it's a
    drop-in alternative to the vertical curve for any caller expecting that
    form -- e.g. iv_scaled_to_day_winddown, which doesn't care which
    averaging method produced its (grid, pct) curve.
    """
    valid = [(ticker, df, date) for ticker, df, date in day_results if not df.empty]
    if not valid:
        print('No data to plot.')
        return None, None, None, None

    colors = plt.get_cmap('tab10')(np.linspace(0, 1, len(valid)))
    fig, ax = plt.subplots(figsize=figsize)

    per_day, max_hours = _build_winddown_per_day(day_results)

    for (hte, pct, date, _gap_pct), color in zip(per_day, colors):
        stride = max(1, int(np.ceil(len(hte) / _MAX_PLOT_POINTS)))
        # pct has NaN inserted at this day's own thresholded-out stretches
        # (see _build_winddown_per_day) -- matplotlib breaks the line there
        # instead of drawing a straight chord across the gap.
        ax.plot(hte[::stride], pct[::stride], color=color, lw=1.0, alpha=0.6, label=str(date))

    if per_day:
        grid = np.linspace(0, max_hours, 241)
        # np.interp propagates pct's NaN to every grid point strictly inside
        # a gap on its own -- no separate masking needed here (unlike the
        # landmark curve below, which must invert clean data first).
        curves = np.vstack([np.interp(grid, hte, pct) for hte, pct, _, _ in per_day])
        mean_curve = _smooth_curve(np.nanmean(curves, axis=0), grid, smooth_hours)
        linear_curve = 100.0 * grid / max_hours if max_hours > 0 else np.zeros_like(grid)
        ax.plot(grid, mean_curve, color='black', lw=2.5,
                label=f'Mean (vertical, {len(per_day)} days)', zorder=10)

        # -- Goodness of fit: how well the empirical mean curve explains each
        # day's own shape vs. the naive linear-in-time diagonal, restricted to
        # each day's own observed [hte.min(), hte.max()] range (see docstring)
        # and excluding that day's own gap-masked grid points (NaN in
        # curve_row -- see _build_winddown_per_day).
        print(f"\nMean-curve fit ({len(per_day)} days, RMSE in percentage points of variance remaining):")
        print(f"{'date':<14}{'n_pts':>7}{'rmse_vs_mean':>14}{'rmse_vs_linear':>16}")
        sq_err_mean_total = sq_err_linear_total = n_total = 0
        for (hte, pct, date, _gap_pct), curve_row in zip(per_day, curves):
            in_range = (grid >= hte.min()) & (grid <= hte.max()) & ~np.isnan(curve_row)
            resid_mean = curve_row[in_range] - mean_curve[in_range]
            resid_linear = curve_row[in_range] - linear_curve[in_range]
            rmse_mean = np.sqrt(np.mean(resid_mean ** 2))
            rmse_linear = np.sqrt(np.mean(resid_linear ** 2))
            print(f"{str(date):<14}{in_range.sum():>7}{rmse_mean:>14.2f}{rmse_linear:>16.2f}")
            sq_err_mean_total += np.sum(resid_mean ** 2)
            sq_err_linear_total += np.sum(resid_linear ** 2)
            n_total += in_range.sum()

        overall_rmse_mean = np.sqrt(sq_err_mean_total / n_total)
        overall_rmse_linear = np.sqrt(sq_err_linear_total / n_total)
        print(f"\n{'overall':<14}{n_total:>7}{overall_rmse_mean:>14.2f}{overall_rmse_linear:>16.2f}")
        if sq_err_linear_total > 0:
            r2_vs_linear = 1.0 - sq_err_mean_total / sq_err_linear_total
            print(f"Mean curve explains {r2_vs_linear:.1%} of the linear-winddown assumption's squared error")

        # -- Landmark mean: invert each day (time as a fn of %-remaining),
        # interpolate onto a common %-remaining grid, average, then plot back
        # in (time, %) space -- see docstring for how this differs from the
        # vertical mean above. _invert_monotonic needs clean, NaN-free input,
        # so each day's own gap points are filtered out here (rather than
        # relying on NaN propagation like the vertical mean does); gap_pct
        # then masks the %-remaining range that stretch WOULD have spanned,
        # same nanmean treatment as vertical.
        pct_grid = np.linspace(0.0, 100.0, 201)
        per_day_inverted = []
        for hte, pct, _, _ in per_day:
            keep = ~np.isnan(pct)
            per_day_inverted.append(_invert_monotonic(hte[keep], pct[keep]))  # (pct_unique_i, hte_avg_i)
        landmark_curves = np.vstack([
            _mask_gap_intervals(np.interp(pct_grid, pu, ha), pct_grid, gap_pct)
            for (pu, ha), (_, _, _, gap_pct) in zip(per_day_inverted, per_day)
        ])
        landmark_time = _smooth_curve(np.nanmean(landmark_curves, axis=0), pct_grid, smooth_pct)
        ax.plot(landmark_time, pct_grid, color='navy', lw=2.0, ls='-.',
                label='Mean (landmark: mean time at each %)', zorder=9)

        # -- Goodness of fit for the landmark mean, in HOURS (that's the axis
        # being averaged here, unlike the vertical table above) -- same
        # restrict-to-each-day's-own-observed-range logic as the vertical
        # table, using each day's own inverted pct range this time, and same
        # gap-masked-NaN exclusion.
        linear_time_at_pct = max_hours * pct_grid / 100.0 if max_hours > 0 else np.zeros_like(pct_grid)
        print(f"\nLandmark-mean fit ({len(per_day)} days, RMSE in hours-to-expiry):")
        print(f"{'date':<14}{'n_pts':>7}{'rmse_vs_mean':>14}{'rmse_vs_linear':>16}")
        sq_err_lm_mean_total = sq_err_lm_linear_total = n_lm_total = 0
        for (hte, pct, date, _gap_pct), curve_row, (pu, ha) in zip(per_day, landmark_curves, per_day_inverted):
            in_range = (pct_grid >= pu.min()) & (pct_grid <= pu.max()) & ~np.isnan(curve_row)
            resid_mean = curve_row[in_range] - landmark_time[in_range]
            resid_linear = curve_row[in_range] - linear_time_at_pct[in_range]
            rmse_mean = np.sqrt(np.mean(resid_mean ** 2))
            rmse_linear = np.sqrt(np.mean(resid_linear ** 2))
            print(f"{str(date):<14}{in_range.sum():>7}{rmse_mean:>14.2f}{rmse_linear:>16.2f}")
            sq_err_lm_mean_total += np.sum(resid_mean ** 2)
            sq_err_lm_linear_total += np.sum(resid_linear ** 2)
            n_lm_total += in_range.sum()

        overall_lm_rmse_mean = np.sqrt(sq_err_lm_mean_total / n_lm_total)
        overall_lm_rmse_linear = np.sqrt(sq_err_lm_linear_total / n_lm_total)
        print(f"\n{'overall':<14}{n_lm_total:>7}{overall_lm_rmse_mean:>14.2f}{overall_lm_rmse_linear:>16.2f}")
        if sq_err_lm_linear_total > 0:
            r2_lm_vs_linear = 1.0 - sq_err_lm_mean_total / sq_err_lm_linear_total
            print(f"Landmark mean explains {r2_lm_vs_linear:.1%} of the linear-winddown assumption's squared error")

        # Re-invert the landmark mean back into (ascending hours -> pct) form
        # so it's usable anywhere the vertical curve is (see docstring).
        landmark_grid, landmark_pct = _invert_monotonic(pct_grid, landmark_time)

    ax.plot([max_hours, 0], [100, 0], color='#999999', lw=1.2, ls='--', label='Linear (calendar-time) winddown')
    ax.set_xlim(max_hours, 0)
    ax.set_ylim(0, 105)
    ax.set_xlabel('Hours to expiry')
    ax.set_ylabel('% of variance-to-expiry remaining')
    ax.set_title('KXHIGHLAX -- realized-variance winddown shape across cached days')
    ax.legend(fontsize=8, loc='upper right', ncol=2)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    plt.show()

    if not per_day:
        return None, None, None, None
    return grid, mean_curve, landmark_grid, landmark_pct


# ---------------------------------------------------------------------------
# Diagnostic -- biggest single-second implied-spot jumps
# ---------------------------------------------------------------------------

_WING_COLS = ('implied_wing_z_less', 'implied_wing_sigma_less',
              'implied_wing_z_greater', 'implied_wing_sigma_greater')


def find_biggest_spot_jumps(day_results, top_n=3, warmup='15min'):
    """Largest single-step |implied_spot| moves across all sample days, as
    (ticker, date, t_before, t_after, spot_before, spot_after, sigma_before, sigma_after, jump),
    plus each side's local-sigma smile pillars (wing_{z,sigma}_{less,greater}_{before,after} --
    see black_scholes.fit_local_sigma_smile) when df carries those columns, NaN otherwise (a
    day cached before fit_smile=True existed, or a snapshot with no qualifying wing market --
    both legitimate, not an error). plot_jump_occurrence uses these to overlay the actual
    local-vol smile curve instead of just the flat ATM-sigma one.

    Skips the first `warmup` of each day's series: markets open in a staggered way
    (each of the day's ~6 tickers posts its first quote within a couple minutes of
    each other, not simultaneously), so right at open the fit is working off only
    2-4 strikes instead of the full set, which produces big-but-uninteresting
    implied_spot swings as more strikes come online -- not a real market move, and
    not representative of the per-market bid/ask coverage seen later in the day.
    """
    warmup_td = pd.Timedelta(warmup)
    records = []
    for ticker, df, date in day_results:
        if df.empty or len(df) < 2:
            continue
        has_wings = set(_WING_COLS).issubset(df.columns)
        cutoff = df['time'].iloc[0] + warmup_td
        dspot = df['implied_spot'].diff().abs()
        dspot = dspot[df['time'] >= cutoff]
        for idx in dspot.nlargest(top_n).index:
            if idx == 0 or pd.isna(dspot.loc[idx]):
                continue
            record = {
                'ticker': ticker, 'date': date,
                't_before': df['time'].iloc[idx - 1], 't_after': df['time'].iloc[idx],
                'spot_before': df['implied_spot'].iloc[idx - 1], 'spot_after': df['implied_spot'].iloc[idx],
                'sigma_before': df['implied_sigma'].iloc[idx - 1], 'sigma_after': df['implied_sigma'].iloc[idx],
                'jump': dspot.loc[idx],
            }
            for col in _WING_COLS:
                suffix = col.replace('implied_wing_', '')
                record[f'wing_{suffix}_before'] = df[col].iloc[idx - 1] if has_wings else float('nan')
                record[f'wing_{suffix}_after'] = df[col].iloc[idx] if has_wings else float('nan')
            records.append(record)
    records.sort(key=lambda r: -r['jump'])
    return records[:top_n]


def _fmt_lots(n):
    """Compact lot-size label, e.g. 107315 -> '107K', 950 -> '950'."""
    if pd.isna(n):
        return ''
    n = int(n)
    if n >= 1000:
        return f'{n / 1000:.0f}K'
    return str(n)


_SOURCE_COLORS = {'predexon': '#1f77b4', 'kalshi': '#ff7f0e'}


def _render_snapshot_panel(ax, snap, mu, sigma, wing, title, mispriced_sum_tol=0.002):
    """
    One market snapshot -- every listed market's own bid/ask plus the
    fitted model's own probability curve -- drawn onto a single `ax`. Shared
    by plot_jump_occurrence (two of these side by side, before/after a jump)
    and plot_fit_occurrence (one alone, for a single worst-fitting moment --
    see find_worst_fits). `wing` is (z_less, sigma_less, z_greater,
    sigma_greater), e.g. from a find_biggest_spot_jumps/find_worst_fits
    record's own wing_*_{before,after} or wing_* fields.

    The reference curve ALWAYS goes through the LOCAL-VOL SMILE machinery
    (black_scholes.expand_smile_pillars/local_sigma_at/smile_bucket_prob --
    see their docstrings) -- there is no separate "flat" code path anymore.
    A snapshot with no qualifying wing market on either side (find_biggest_
    spot_jumps's wing_{z,sigma}_{less,greater}_{before,after} fields both NaN
    -- a real per-snapshot occurrence, not stale/pre-smile data, since
    fit_smile=True is baked into every regen_cache.py run now) makes
    expand_smile_pillars degenerate to the single ATM pillar [(0.0, sigma)],
    under which local_sigma_at/smile_bucket_prob return exactly the flat-
    sigma formula -- so "no correction" is the smile machinery's own zero-
    correction case, not a fallback around it.

    The thin grey dashed flat reference is only drawn SEPARATELY when a real
    correction pillar exists (`has_correction`) and therefore actually
    differs from the smile curve -- same convention plot_implied_spot_
    history's winddown overlay uses, so the correction's effect stays
    visible rather than silently replacing it. Otherwise (no correction)
    drawing it too would just overplot an identical line, so only the one
    curve is shown -- no separate "15d/50d/85d" labeling either in that
    case, since nothing was actually fit beyond the flat ATM sigma.

    The reference curve itself is a generic 2°-wide window swept across x --
    it's the actual pricing formula for a 'between' market (whose real bucket
    width IS 2° post continuity-correction), but NOT for a 'greater'/'less'
    tail market, whose real price is a one-sided cumulative CDF value, not a
    window density. Plotting a tail market's real bid/ask at its own true
    strike against this window-shaped curve makes a well-priced market look
    wrong purely from the shape mismatch -- most visibly for a tail market
    pricing in a LARGE probability, since a window's height is inherently
    bounded while a cumulative tail probability isn't.

    So every 'greater'/'less' market is instead plotted at its own TRUE
    strike (x_pos, unchanged), but its bid/ask are displayed as an OFFSET
    from the reference curve's own height at that x, rather than as raw
    probability values on the window-density scale: the offset is (quoted
    bid/ask - the model's own CDF-implied probability for that market,
    smile_bucket_prob at the fitted mu/sigma). A quote that exactly matches
    what the model implies for it therefore sits ON the curve; a quote
    pricing MORE probability than the model gives sits ABOVE it; one pricing
    LESS sits BELOW. This sidesteps the CDF-vs-window shape mismatch without
    borrowing an adjacent bucket's price at all -- it shows agreement or
    disagreement with the fitted distribution directly, in the same y-units
    the curve itself is drawn in, anchored at the market's real strike.
    'between' markets are left untouched (plotted at their own true bid/ask,
    already the same window-probability quantity the curve is).

    mu is None or NaN means no TRUSTWORTHY point fit was available for this
    snapshot (fewer than 2 markets quoted, or fit_normal_to_bucket_bid_ask's
    own lambda_min guard rejected every candidate as too poorly constrained
    -- see its docstring) -- there's no curve to draw or model probability to
    reposition/rescale against, so this just plots the raw BBO at each
    market's true x_pos/bid/ask and returns early. None is what a fresh
    _expand_sample minute-boundary refit returns on rejection; NaN is what a
    find_worst_fits/find_biggest_spot_jumps record's own implied_spot/sigma
    columns hold on the SAME rejection (implied_band_cost can still be a real
    number on such a row even though mu/sigma aren't -- see that column's own
    docstring on compute_implied_spot_timeseries -- which is exactly how a
    lambda_min-rejected row can still surface here at all, e.g. via
    find_worst_fits).

    mispriced_sum_tol GATES black_scholes.find_and_correct_mispriced_market:
    KXHIGHLAX settlement is a discrete integer temp, so the listed markets'
    TRUE probabilities must sum to exactly 1 -- sum((bid+ask)/2) is therefore
    a model-free sanity check, and only when it's off by more than
    mispriced_sum_tol is there actually a market to find/correct. Confirmed
    on a real case (KXHIGHLAX-26JUL23) that firing this unconditionally is
    at best a no-op and at worst misleading: mids there summed to EXACTLY
    1.000 (fully consistent), the "flagged" market's conservation-implied
    fair value landed almost exactly on its own quoted mid, and "correcting"
    it barely moved band_cost -- there was no mispriced market, just a
    symmetric-normal shape limitation (see find_and_correct_mispriced_
    market's own docstring). When the gate DOES fire and a correction is
    found, its (mu, sigma) -- and the wing pillars, refit fresh against it
    for consistency -- become this panel's primary fit from here on, the
    flagged market's own real quote is drawn at reduced alpha (still real
    data, just visually de-emphasized), and its corrected fair value is
    drawn as a separate gold-edged star marker, in the SAME curve-relative
    units as its row's own plot_bid/plot_ask (see above) so it's readable
    directly off the same axes.
    """
    print(f"[{title}]")
    for row in snap.itertuples():
        if row.strike_type == 'less':
            label = f"<{row.floor_strike:g}"
        elif row.strike_type == 'greater':
            label = f">{row.floor_strike:g}"
        else:
            label = f"{row.floor_strike:g}-{row.cap_strike:g}"
        print(f"  {label}: {row.bid:.3f} {row.ask:.3f}")
    print(f"  sum_bid={snap['bid'].sum():.3f}  sum_ask={snap['ask'].sum():.3f}")

    if mu is None or pd.isna(mu):
        for src, color in _SOURCE_COLORS.items():
            sub = snap[snap['source'] == src]
            if sub.empty:
                continue
            ax.vlines(sub['x_pos'], sub['bid'], sub['ask'], color=color, lw=3, alpha=0.5, zorder=2)
            ax.scatter(sub['x_pos'], sub['bid'], marker='v', s=90, color=color, zorder=3, label=f'{src} bid')
            ax.scatter(sub['x_pos'], sub['ask'], marker='^', s=90, color=color, zorder=3, label=f'{src} ask')
        ax.set_title(f"{title}\nΣbid={snap['bid'].sum():.3f}  Σask={snap['ask'].sum():.3f}", fontsize=10)
        ax.set_xlabel('Strike (°F)')
        ax.legend(fontsize=7, loc='upper right')
        ax.grid(True, alpha=0.3)
        return

    snap = snap.copy()

    # Discrete-settlement conservation check (see this function's docstring
    # on mispriced_sum_tol) -- only attempt find_and_correct_mispriced_market
    # when the listed markets' own mids are ALREADY inconsistent by more
    # than mispriced_sum_tol; otherwise there's no mispriced market to find.
    mispriced = None
    sum_mid = ((snap['bid'] + snap['ask']) / 2.0).sum()
    if sigma > 0 and abs(sum_mid - 1.0) > mispriced_sum_tol:
        markets, bids, asks, bidd, askd = _markets_from_snapshot(snap)
        mispriced = black_scholes.find_and_correct_mispriced_market(
            markets, bids, asks, bidd, askd, warm_start=(mu, sigma))
        if mispriced['mu'] is not None:
            mu, sigma = mispriced['mu'], mispriced['sigma']
            wing = black_scholes.fit_local_sigma_smile(mu, sigma, markets, bids, asks, bidd, askd)
        else:
            mispriced = None

    z_less, sigma_less, z_greater, sigma_greater = wing
    # expand_smile_pillars ALWAYS runs (never an optional "if we have a
    # smile" branch) -- fit_smile=True is baked into every regen_cache.py
    # run now, so a missing wing pillar on a given side just means that
    # snapshot had no qualifying 'less'/'greater' market to fit one from
    # (see fit_local_sigma_smile's docstring), not stale/pre-smile data.
    # With both sides NaN, expand_smile_pillars degenerates to the single
    # pillar [(0.0, sigma)] -- and local_sigma_at/smile_bucket_prob then
    # return exactly the flat-sigma formula off that one pillar -- so
    # there's no separate flat code path to maintain; it's the smile
    # machinery's own zero-correction case, not a fallback around it.
    pillars = black_scholes.expand_smile_pillars(sigma, z_less, sigma_less, z_greater, sigma_greater) if sigma > 0 else None
    has_correction = pillars is not None and len(pillars) > 1

    # 'greater'/'less' markets stay at their own TRUE x_pos (no
    # repositioning) -- see this function's docstring for why their bid/ask
    # are instead displaced vertically by (quoted - model CDF prob) from the
    # reference curve's own height at that x, rather than plotted as raw
    # probability. curve_y uses the SAME single-local-sigma-per-point
    # formula as the reference curve drawn below (not smile_bucket_prob's
    # own per-edge blend), so "on the curve" here means literally coincident
    # with the drawn line, not just numerically close. Applied to EVERY
    # market, 'between' included -- not just 'less'/'greater' -- since the
    # drawn line's single-sigma-at-center approximation and the market's own
    # smile_bucket_prob (per-edge blend) formula can diverge meaningfully
    # for a 'between' bucket that itself sits out in the wing ramp (its two
    # edges then have very different local sigma), not just for tail markets
    # -- confirmed on a real case (KXHIGHLAX-26JUL28, B82.5: drawn line 0.086
    # vs its own true smile_bucket_prob 0.024). Plotting a 'between' market's
    # raw bid/ask un-adjusted against that line made a correctly-priced
    # market look off purely from the two formulas disagreeing.
    plot_x = snap['x_pos'].to_numpy(dtype=float)
    plot_bid = snap['bid'].to_numpy(dtype=float)
    plot_ask = snap['ask'].to_numpy(dtype=float)
    if sigma > 0:
        for i, row in enumerate(snap.itertuples()):
            model_p = black_scholes.smile_bucket_prob(
                mu, sigma, pillars, row.floor_strike, row.cap_strike, row.strike_type)
            z = (row.x_pos - mu) / sigma
            local_sigma = black_scholes.local_sigma_at(z, pillars)
            curve_y = _norm.cdf((row.x_pos + 1 - mu) / local_sigma) - _norm.cdf((row.x_pos - 1 - mu) / local_sigma)
            plot_bid[i] = curve_y + (row.bid - model_p)
            plot_ask[i] = curve_y + (row.ask - model_p)
            if row.strike_type == 'less':
                label = f"<{row.floor_strike:g}"
            elif row.strike_type == 'greater':
                label = f">{row.floor_strike:g}"
            else:
                label = f"{row.floor_strike:g}-{row.cap_strike:g}"
            print(f"    {label} plotted: model_p={model_p:.4f} curve_y={curve_y:.4f}"
                  f" -> plot=({plot_bid[i]:.4f}, {plot_ask[i]:.4f})")
    snap['plot_x'] = plot_x
    snap['plot_bid'] = plot_bid
    snap['plot_ask'] = plot_ask

    # The flagged market's corrected fair-value point, in the SAME
    # curve-relative units as its own row's plot_bid/plot_ask above --
    # mispriced_row_label anchors it to snap's own index (stable across the
    # .copy()/column additions above) rather than a positional int, since
    # the vlines loop below filters by label too.
    mispriced_row_label = None
    mispriced_plot_x = mispriced_plot_y = None
    if mispriced is not None:
        mi = mispriced['mispriced_index']
        row = snap.iloc[mi]
        mispriced_row_label = snap.index[mi]
        mispriced_plot_x = plot_x[mi]
        model_p = black_scholes.smile_bucket_prob(
            mu, sigma, pillars, row['floor_strike'], row['cap_strike'], row['strike_type'])
        z = (row['x_pos'] - mu) / sigma
        local_sigma = black_scholes.local_sigma_at(z, pillars)
        curve_y = _norm.cdf((row['x_pos'] + 1 - mu) / local_sigma) - _norm.cdf((row['x_pos'] - 1 - mu) / local_sigma)
        mispriced_plot_y = curve_y + (mispriced['fair_value'] - model_p)

    # BBO colored by data source (Predexon tick feed vs Kalshi 1-min candle
    # fallback) -- see snapshot_market_quotes's docstring. Bid/ask markers
    # themselves (not just the connecting line) carry the source color,
    # distinguished from each other by marker shape (v=bid, ^=ask) instead
    # of color. Plotted at plot_x/plot_bid/plot_ask (curve-relative for a
    # tail market -- see above), not raw x_pos/bid/ask. The flagged
    # market's own row (if any) is drawn at reduced alpha within its
    # source's usual color -- still visible (it's real data), just visually
    # de-emphasized as "not trusted" -- with its corrected fair-value point
    # drawn separately, full-strength, right after this loop.
    for src, color in _SOURCE_COLORS.items():
        sub = snap[snap['source'] == src]
        if sub.empty:
            continue
        if mispriced_row_label is not None:
            is_flagged = sub.index.isin([mispriced_row_label])
        else:
            is_flagged = np.zeros(len(sub), dtype=bool)
        normal, flagged = sub[~is_flagged], sub[is_flagged]
        if not normal.empty:
            ax.vlines(normal['plot_x'], normal['plot_bid'], normal['plot_ask'], color=color, lw=3, alpha=0.5, zorder=2)
            ax.scatter(normal['plot_x'], normal['plot_bid'], marker='v', s=90, color=color, zorder=3,
                       label=f'{src} bid')
            ax.scatter(normal['plot_x'], normal['plot_ask'], marker='^', s=90, color=color, zorder=3,
                       label=f'{src} ask')
        if not flagged.empty:
            ax.vlines(flagged['plot_x'], flagged['plot_bid'], flagged['plot_ask'], color=color, lw=3, alpha=0.15, zorder=2)
            ax.scatter(flagged['plot_x'], flagged['plot_bid'], marker='v', s=90, color=color, alpha=0.3, zorder=3)
            ax.scatter(flagged['plot_x'], flagged['plot_ask'], marker='^', s=90, color=color, alpha=0.3, zorder=3)

    if mispriced is not None:
        ax.scatter([mispriced_plot_x], [mispriced_plot_y], marker='*', s=260, color='black',
                   edgecolors='gold', linewidths=1.3, zorder=6, label='corrected fair value')
        mispriced_ticker = snap.loc[mispriced_row_label, 'ticker'].rsplit('-', 1)[-1]
        side = 'BUY' if mispriced['fair_value'] > mispriced['quoted_ask'] else (
            'SELL' if mispriced['fair_value'] < mispriced['quoted_bid'] else '')
        ax.annotate(
            f"{mispriced_ticker}: quoted {mispriced['quoted_bid']:.2f}-{mispriced['quoted_ask']:.2f}"
            f" -> fair {mispriced['fair_value']:.2f} {side}",
            (mispriced_plot_x, mispriced_plot_y), xytext=(0, -14), textcoords='offset points',
            fontsize=7, ha='center', color='#333333', zorder=6)

    # Smooth reference curve: the fitted normal's probability of landing in a
    # generic 2°-wide window centered at each x (matches the real "between"
    # bucket width, e.g. B78.5 -> continuity-corrected [77.5, 79.5)) -- this is
    # what makes the fit's shape visible as a spline, since each real market's
    # own formula differs (between/greater/less) and isn't a single function
    # of x the way this generic sweep is.
    #
    # sigma == 0 is a genuine point mass (fit_normal_to_bucket_bid_ask's
    # near-certain-outcome short-circuit -- one bucket already priced
    # ~100%), not a numerical edge case to round away: (x-mu)/sigma would
    # divide by zero, so there's no bell curve to draw here at all, just a
    # vertical spike at mu.
    if sigma == 0:
        ax.axvline(mu, color='k', ls='--', lw=1.5, alpha=0.7,
                   label=f'certain @ μ={mu:.2f} (σ=0)')
    else:
        k_range = np.linspace(plot_x.min() - 3, plot_x.max() + 3, 300)
        z_pts = (k_range - mu) / sigma
        local_sigma = np.array([black_scholes.local_sigma_at(z, pillars) for z in z_pts])
        curve_p = _norm.cdf((k_range + 1 - mu) / local_sigma) - _norm.cdf((k_range - 1 - mu) / local_sigma)

        if has_correction:
            # Only worth drawing the flat line SEPARATELY when it
            # actually differs from curve_p -- otherwise (no wing pillar
            # this snapshot) the two are identical by construction and a
            # second overlapping line is just noise, not information.
            curve_p_flat = _norm.cdf((k_range + 1 - mu) / sigma) - _norm.cdf((k_range - 1 - mu) / sigma)
            ax.plot(k_range, curve_p_flat, color='#999999', ls='--', lw=1.0, alpha=0.6,
                    label=f'flat N(μ={mu:.2f}, σ={sigma:.2f})')
            ax.plot(k_range, curve_p, 'k-', lw=1.5, alpha=0.8,
                    label='local-vol smile -- 2°-window P(x)')
        else:
            ax.plot(k_range, curve_p, 'k-', lw=1.5, alpha=0.7,
                    label=f'fitted N(μ={mu:.2f}, σ={sigma:.2f}) -- 2°-window P(x)')

    # lot size (depth) at the best level of each side -- skipped entirely for
    # kalshi-sourced rows (no real book depth from a 1-min candle, so no lot
    # size to show), not just a bid_depth>0 check: a stale-gap row's depth is
    # a NOMINAL 1.0 (see snapshot_market_quotes's docstring), there purely so
    # a refit off this snapshot gets the right hinge-vs-midpoint treatment,
    # not a real size that'd be meaningful to label.
    for _, row in snap.iterrows():
        if row['source'] == 'kalshi':
            continue
        if row['bid_depth'] > 0:
            ax.annotate(_fmt_lots(row['bid_depth']), (row['plot_x'], row['plot_bid']),
                        xytext=(4, -10), textcoords='offset points', fontsize=6.5, color='#555555')
        if row['ask_depth'] > 0:
            ax.annotate(_fmt_lots(row['ask_depth']), (row['plot_x'], row['plot_ask']),
                        xytext=(4, 4), textcoords='offset points', fontsize=6.5, color='#555555')

    title_extra = f"\nΣbid={snap['bid'].sum():.3f}  Σask={snap['ask'].sum():.3f}"
    if mispriced is not None:
        # The figure-level suptitle a caller (plot_fit_occurrence/plot_jump_
        # occurrence) shows is the ORIGINAL record's band_cost -- computed
        # off the raw, uncorrected quotes, before this panel found and
        # substituted the flagged market's conservation-implied fair value.
        # Once that correction is applied, mu/sigma/the curve ALL reflect
        # the corrected fit, so leaving the caller's stale original number
        # as the only band_cost visible would describe a different (worse)
        # fit than the one actually drawn. Shown here, on the panel itself,
        # since the outer suptitle can't know a correction happened inside
        # this specific panel (a multi-panel figure may only correct one).
        title_extra += f"\nband_cost={mispriced['band_cost']:.4f} (corrected; baseline was {mispriced['baseline_band_cost']:.4f})"
    ax.set_title(f"{title}{title_extra}", fontsize=10)
    ax.set_xlabel('Strike (°F)')
    ax.legend(fontsize=7, loc='upper right')
    ax.grid(True, alpha=0.3)


def _markets_from_snapshot(snap):
    """(markets, bids, asks, bid_depths, ask_depths) lists -- the shape
    fit_normal_to_bucket_bid_ask/fit_local_sigma_smile take -- built from a
    snapshot_market_quotes DataFrame. cap_strike is NaN (not None) for a
    'greater'/'less' row (pandas' float column, no per-row object dtype),
    converted back to None here since that's the sentinel _continuity_
    corrected_bounds and friends actually check for."""
    markets = [
        (row.floor_strike, None if pd.isna(row.cap_strike) else row.cap_strike, row.strike_type)
        for row in snap.itertuples()
    ]
    return markets, snap['bid'].tolist(), snap['ask'].tolist(), snap['bid_depth'].tolist(), snap['ask_depth'].tolist()


def _fit_and_smile_from_snapshot(snap):
    """
    Fresh (mu, sigma, wing) for one snapshot, refit directly from its own raw
    bid/ask via fit_normal_to_bucket_bid_ask + fit_local_sigma_smile -- the
    same two functions the primary cache-building pipeline itself calls --
    rather than a cache lookup. Used for _expand_sample's minute-boundary
    panels, which are ad hoc timestamps (a real Kalshi candle close, not
    necessarily anything find_worst_fits/find_biggest_spot_jumps recorded)
    with no pre-computed implied_spot/sigma/wing_* sitting in a df to read.

    Returns (None, None, (None, None, None, None)) if the fit itself
    rejects the snapshot (fewer than 2 markets quoted, or lambda_min too low
    -- see fit_normal_to_bucket_bid_ask's docstring) -- _render_snapshot_
    panel's mu=None path handles that by falling back to a bare-BBO plot.
    """
    markets, bids, asks, bidd, askd = _markets_from_snapshot(snap)
    mu, sigma, _band_cost, _lambda_min = black_scholes.fit_normal_to_bucket_bid_ask(markets, bids, asks, bidd, askd)
    if mu is None:
        return None, None, (None, None, None, None)
    wing = black_scholes.fit_local_sigma_smile(mu, sigma, markets, bids, asks, bidd, askd)
    return mu, sigma, wing


def _expand_sample(long_df, kalshi_backup, t, snap, mu, sigma, wing, title):
    """
    [(snap, mu, sigma, wing, title), ...] for ONE conceptual sample -- just
    the sample itself, unchanged, UNLESS any of its markets' BBO came from
    the Kalshi 1-min-candle fallback (source=='kalshi' -- see snapshot_
    market_quotes's docstring), in which case that market's plotted value
    here is _asof_linear_interp'd between two real candle closes, not an
    observed quote. In that case, also snapshot+refit (via
    _fit_and_smile_from_snapshot -- a fresh fit, not a cache lookup, since
    these ad hoc boundary timestamps have no cached row of their own) the
    two REAL candle-close timestamps bracketing t (t floored/ceiled to the
    minute), so the reader can see the actual observed Kalshi prints this
    interpolation was built from on either side, not just the interpolated
    in-between value alone. Returns them in chronological order around the
    original sample: [minute-start, sample, minute-end] (either boundary
    dropped if it lands exactly on t already -- nothing new to show there).
    """
    if not (snap['source'] == 'kalshi').any():
        return [(snap, mu, sigma, wing, title)]

    t_ts = pd.Timestamp(t)
    before, after = [], []
    for boundary_t, boundary_label, bucket in (
        (t_ts.floor('1min'), 'min start', before),
        (t_ts.ceil('1min'), 'min end', after),
    ):
        if boundary_t == t_ts:
            continue
        b_snap = black_scholes.snapshot_market_quotes(long_df, kalshi_backup, boundary_t)
        b_mu, b_sigma, b_wing = _fit_and_smile_from_snapshot(b_snap)
        b_t_la = boundary_t.tz_convert(LA_TZ)
        suffix = '' if b_mu is not None else ' (no fit)'
        b_title = f'{boundary_label}: {b_t_la.strftime("%H:%M:%S")} PT{suffix}'
        bucket.append((b_snap, b_mu, b_sigma, b_wing, b_title))

    return before + [(snap, mu, sigma, wing, title)] + after


def plot_jump_occurrence(record, long_df, kalshi_backup):
    """
    Before/after snapshot of every market's own bid/ask around one
    implied_spot jump -- see _render_snapshot_panel for what each panel
    actually draws and why. A direct look at whether a big jump reflects a
    real market move or something to dig into.

    Either side (or both) may expand into 3 panels instead of 1 -- see
    _expand_sample -- whenever that side's own snapshot has a market priced
    off the Kalshi-candle fallback, bracketing it with the real candle-close
    snapshots on either side. Panel count is therefore dynamic (2 to 6).
    """
    # Both snapshots go through black_scholes.snapshot_market_quotes -- the
    # exact same Predexon-staleness/mismatch -> Kalshi-BBO fallback (including
    # linear interpolation between Kalshi candles) that
    # compute_implied_spot_timeseries itself used to produce implied_spot/sigma,
    # so this diagnostic shows what the fit actually saw rather than a
    # separately-maintained approximation of it.
    snap_before = black_scholes.snapshot_market_quotes(long_df, kalshi_backup, record['t_before'])
    snap_after = black_scholes.snapshot_market_quotes(long_df, kalshi_backup, record['t_after'])
    t_before_la = pd.Timestamp(record['t_before']).tz_convert(LA_TZ)
    t_after_la = pd.Timestamp(record['t_after']).tz_convert(LA_TZ)

    before_panels = _expand_sample(
        long_df, kalshi_backup, record['t_before'], snap_before,
        record['spot_before'], record['sigma_before'],
        (record.get('wing_z_less_before'), record.get('wing_sigma_less_before'),
         record.get('wing_z_greater_before'), record.get('wing_sigma_greater_before')),
        title=f'before: {t_before_la.strftime("%H:%M:%S")} PT',
    )
    after_panels = _expand_sample(
        long_df, kalshi_backup, record['t_after'], snap_after,
        record['spot_after'], record['sigma_after'],
        (record.get('wing_z_less_after'), record.get('wing_sigma_less_after'),
         record.get('wing_z_greater_after'), record.get('wing_sigma_greater_after')),
        title=f'after: {t_after_la.strftime("%H:%M:%S")} PT',
    )
    all_panels = before_panels + after_panels

    fig, axes = plt.subplots(1, len(all_panels), figsize=(6 * len(all_panels), 5), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, (p_snap, p_mu, p_sigma, p_wing, p_title) in zip(axes, all_panels):
        _render_snapshot_panel(ax, p_snap, p_mu, p_sigma, p_wing, title=p_title)

    axes[0].set_ylabel('Probability')
    fig.suptitle(
        f"{record['ticker']} -- jump of {record['jump']:.2f}°F "
        f"({record['spot_before']:.2f} -> {record['spot_after']:.2f})   "
        f"(labels = lot size at best bid/ask)",
        fontsize=12,
    )
    plt.tight_layout()
    plt.show()


def plot_fit_occurrence(record, long_df, kalshi_backup):
    """
    Single-snapshot counterpart to plot_jump_occurrence -- one market
    snapshot (see _render_snapshot_panel for what it draws and why), not a
    before/after pair, since a bad FIT (see find_worst_fits) is one moment's
    mispricing, not a transition between two moments the way a price jump is.

    May expand into 3 panels instead of 1 -- see _expand_sample -- if the
    snapshot has a market priced off the Kalshi-candle fallback.
    """
    snap = black_scholes.snapshot_market_quotes(long_df, kalshi_backup, record['t'])
    t_la = pd.Timestamp(record['t']).tz_convert(LA_TZ)

    panels = _expand_sample(
        long_df, kalshi_backup, record['t'], snap,
        record['spot'], record['sigma'],
        (record.get('wing_z_less'), record.get('wing_sigma_less'),
         record.get('wing_z_greater'), record.get('wing_sigma_greater')),
        title=f'{t_la.strftime("%H:%M:%S")} PT',
    )

    fig, axes = plt.subplots(1, len(panels), figsize=(6.5 * len(panels), 5), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, (p_snap, p_mu, p_sigma, p_wing, p_title) in zip(axes, panels):
        _render_snapshot_panel(ax, p_snap, p_mu, p_sigma, p_wing, title=p_title)

    axes[0].set_ylabel('Probability')
    fig.suptitle(
        f"{record['ticker']} -- band_cost={record['band_cost']:.4f} "
        f"(μ={record['spot']:.2f}, σ={record['sigma']:.2f})   "
        f"(labels = lot size at best bid/ask)",
        fontsize=12,
    )
    plt.tight_layout()
    plt.show()


def plot_biggest_jumps(day_results, top_n=3, warmup='15min'):
    """
    Find the top_n largest single-second implied_spot jumps across
    day_results (see find_biggest_spot_jumps) and plot each one's before/after
    market snapshot (see plot_jump_occurrence). Prints a one-line summary per
    jump and returns the jump records, in case the caller wants them directly.
    """
    jumps = find_biggest_spot_jumps(day_results, top_n=top_n, warmup=warmup)
    for rank, record in enumerate(jumps, 1):
        print(f"#{rank}: {record['ticker']}  {record['t_before']} -> {record['t_after']}  "
              f"jump={record['jump']:.3f} F  ({record['spot_before']:.2f} -> {record['spot_after']:.2f})")
        long_df = load_predexon_long_df(record['ticker'])
        kalshi_backup = load_kalshi_backup_long_df(record['ticker'])
        plot_jump_occurrence(record, long_df, kalshi_backup)
    return jumps


def find_worst_fits(day_results, top_n=10, warmup='15min', dedupe_tol=0.01, min_gap='2min',
                     mispriced_sum_tol=0.002, correction_pool_mult=50):
    """
    Top_n DISTINCT snapshots (ticker, date, t, spot, sigma, band_cost,
    baseline_band_cost, wing_{z,sigma}_{less,greater} pillar fields) across
    day_results ranked by CORRECTED band_cost DESCENDING -- i.e. the moments
    the model still fits worst even after accounting for a single
    conservation-law-inconsistent market (see find_and_correct_mispriced_
    market's and _render_snapshot_panel's mispriced_sum_tol docstrings) --
    rather than find_biggest_spot_jumps's ranking by |implied_spot| jump
    size. See implied_band_cost's own docstring on compute_implied_spot_
    timeseries for why these are different failure modes from a jump: a big,
    cleanly-explained move ranks high there but not here; a small move the
    model still can't reconcile across its listed markets ranks high here
    but not there.

    RAW vs CORRECTED band_cost -- why rank on the latter: a snapshot whose
    raw band_cost is high purely because ONE market's own quote is
    inconsistent with the rest (Sigma-mid off from 1 by more than
    mispriced_sum_tol) isn't really evidence the MODEL fits badly -- it's
    evidence one quote is bad data, which the conservation law can identify
    and fix directly, no model needed (confirmed on KXHIGHLAX-26JUL27: raw
    band_cost 0.0031, corrected 0.0005, an ~85% drop from fixing just that
    one market). Ranking by the RAW value lets cases like that crowd out
    snapshots the model genuinely can't explain even after that correction
    (e.g. KXHIGHLAX-26JUL23's right-skew case, where mids already summed to
    1 and there was nothing to correct at all). `record['band_cost']` is
    therefore the CORRECTED value (identical to the raw value whenever the
    mispriced_sum_tol gate never fires), with the original preserved
    separately as `record['baseline_band_cost']` for comparison -- matching
    _render_snapshot_panel's own panel-title convention, so a plotted
    panel's title and this function's own ranking always agree.

    Computing the correction requires an actual snapshot_market_quotes call
    per candidate (see the raw-quote dedup pass below, which needs the same
    snapshot anyway and is merged with this step to avoid fetching twice) --
    too expensive to run on every raw candidate this gathers. Instead: sort
    by the CHEAP raw (already-cached) band_cost first, take the top
    `top_n * correction_pool_mult` candidates globally, and only compute the
    correction (then re-sort by the corrected value) within that pool.
    Correction can only ever LOWER a genuinely-inconsistent snapshot's
    band_cost (it's solved specifically to reduce the worst violator, not
    increase it), so a candidate far down the raw ranking is not a
    plausible top_n contender after correction either -- the pool just needs
    to be generous enough that nothing genuinely in the final top_n was
    excluded before correction had a chance to matter, not exhaustive.

    DISTINCT: a single stuck/frozen incident (the same bad quotes sitting
    unchanged for a long stretch) scores the exact same (spot, sigma,
    band_cost) at every second inside it -- confirmed on a real case
    (KXHIGHLAX-26JUL01) recurring, byte-identical, across a ~5 HOUR span, far
    longer than any reasonable time-based exclusion window would catch. A
    naive per-ticker nlargest(top_n) can fill the ENTIRE top_n with seconds
    from that one incident, crowding out every other genuinely different
    worst-fit moment across the whole sample. Dedup is by VALUE, not time:
    a candidate is skipped if its (spot, sigma) is within `dedupe_tol`
    degrees of an ALREADY-SELECTED record for the SAME ticker -- if the fit
    hasn't actually changed, it's the same incident being resampled, no
    matter how far apart in time; if it HAS changed, even the same ticker
    can legitimately contribute a second, distinct bad-fit moment. Gathers a
    generous pool (top_n*20) of per-ticker candidates before dedup/global
    sort, since a single dominant incident can otherwise consume an entire
    day's naive top_n before any deduping even has other candidates to fall
    back on.

    A second, independent dedup pass checks the RAW market quotes, not just
    the derived (spot, sigma) fit: for any candidate that survives the
    value-based check above, its actual snapshot_market_quotes (bid/ask for
    every listed market at that instant) is compared against every already-
    selected candidate for the same ticker. If the raw quotes are identical
    (rounded to 6dp), it's skipped regardless of how much (spot, sigma)
    differ -- e.g. multiple sampling frequencies (1s/10s/1min/10min) can
    independently land on a snapshot where nothing actually ticked since the
    last ACCEPTED sample, and re-fitting unchanged input quotes can still
    perturb (spot, sigma) by more than dedupe_tol purely from optimizer
    numerics (different warm start, different candidate-start ordering),
    which the value-only check alone wouldn't catch. This is the more
    expensive check (one snapshot_market_quotes call per surviving
    candidate, long_df/kalshi_backup cached per ticker for the duration of
    this call) so it only runs after the cheap value check has already
    thinned the pool.

    A third, cheap backstop: a candidate is also skipped if it's within
    `min_gap` of an already-selected candidate for the SAME ticker,
    regardless of how much (spot, sigma) or the raw quotes differ. In
    practice, real-world runs kept surfacing same-ticker candidates mere
    SECONDS apart that were technically distinct under both checks above
    (e.g. book depth churning by a contract or two shifting the WAP anchor
    just enough to move (spot, sigma) past dedupe_tol, or one thin market's
    quote ticking a single cent) but were plainly the same underlying
    incident to look at, not a different one -- the two checks above dedup
    EXACT recurrence, not near-recurrence. min_gap trades away the (rarer)
    case of a genuinely new, distinct incident starting mere seconds after a
    prior one on the same ticker in favor of guaranteeing every selected
    same-ticker candidate is visually a different moment.

    Rows missing implied_band_cost entirely (a day cached before this column
    existed, or a row fit_normal_to_bucket_bid_ask rejected outright -- see
    that function's and implied_band_cost's own docstrings) are skipped
    outright, not treated as 0 -- there's nothing to rank there, not evidence
    of a good fit.

    Skips the first `warmup` of each day's series, same reasoning as
    find_biggest_spot_jumps: markets open staggered, so an early snapshot
    working off only 2-4 of the day's ~6 strikes can score an inflated
    band_cost purely from thin coverage forcing an over-constrained fit, not
    genuine mispricing once the full set is listed.
    """
    warmup_td = pd.Timedelta(warmup)
    min_gap_td = pd.Timedelta(min_gap)
    candidates = []
    for ticker, df, date in day_results:
        if df.empty or 'implied_band_cost' not in df.columns:
            continue
        has_wings = set(_WING_COLS).issubset(df.columns)
        cutoff = df['time'].iloc[0] + warmup_td
        cost = df['implied_band_cost'].where(df['time'] >= cutoff)
        for idx in cost.nlargest(top_n * 20).index:
            if pd.isna(cost.loc[idx]):
                continue
            record = {
                'ticker': ticker, 'date': date,
                't': df['time'].iloc[idx],
                'spot': df['implied_spot'].iloc[idx],
                'sigma': df['implied_sigma'].iloc[idx],
                'band_cost': cost.loc[idx],
                'baseline_band_cost': cost.loc[idx],
            }
            for col in _WING_COLS:
                suffix = col.replace('implied_wing_', '')
                record[f'wing_{suffix}'] = df[col].iloc[idx] if has_wings else float('nan')
            candidates.append(record)
    candidates.sort(key=lambda r: -r['band_cost'])

    # Cheap VALUE dedup across the FULL candidate list, before any pool
    # slicing -- otherwise a single incident whose fit doesn't change for a
    # long stretch (e.g. KXHIGHLAX-26MAR12's unrecoverable garbage quotes,
    # every listed market pinned at bid=0/ask=1 for hundreds of consecutive
    # seconds) consumes the ENTIRE correction_pool_mult-sized slice with
    # near-identical copies of itself -- since they all share the same
    # (spot, sigma), the correction/re-sort step below never even gets a
    # chance to consider any OTHER ticker's distinct incident. No snapshot
    # fetch needed for this pass (just the already-cached spot/sigma), so
    # it's cheap enough to run on the full list regardless of size.
    value_deduped = []
    seen_values_per_ticker = {}
    for r in candidates:
        prior = seen_values_per_ticker.get(r['ticker'], [])
        if any(abs(r['spot'] - pm) < dedupe_tol and abs(r['sigma'] - ps) < dedupe_tol for pm, ps in prior):
            continue
        value_deduped.append(r)
        seen_values_per_ticker.setdefault(r['ticker'], []).append((r['spot'], r['sigma']))

    long_df_cache = {}
    kalshi_backup_cache = {}
    snap_cache = {}

    def _snapshot(ticker, t):
        key = (ticker, t)
        if key not in snap_cache:
            if ticker not in long_df_cache:
                long_df_cache[ticker] = load_predexon_long_df(ticker)
                kalshi_backup_cache[ticker] = load_kalshi_backup_long_df(ticker)
            snap_cache[key] = black_scholes.snapshot_market_quotes(long_df_cache[ticker], kalshi_backup_cache[ticker], t)
        return snap_cache[key]

    def _raw_signature(snap):
        return tuple(sorted(zip(snap['ticker'], snap['bid'].round(6), snap['ask'].round(6))))

    # Correct band_cost within the (cheap-raw-sorted, now value-deduped)
    # pool only -- see docstring on why re-sorting a generous prefix, not
    # every candidate, is both sufficient and necessary for performance.
    pool = value_deduped[:top_n * correction_pool_mult]
    for r in pool:
        snap = _snapshot(r['ticker'], r['t'])
        sum_mid = ((snap['bid'] + snap['ask']) / 2.0).sum()
        if abs(sum_mid - 1.0) > mispriced_sum_tol:
            markets, bids, asks, bidd, askd = _markets_from_snapshot(snap)
            mispriced = black_scholes.find_and_correct_mispriced_market(
                markets, bids, asks, bidd, askd, warm_start=(r['spot'], r['sigma']))
            if mispriced['mu'] is not None:
                r['band_cost'] = mispriced['band_cost']
    pool.sort(key=lambda r: -r['band_cost'])

    # Value dedup already ran (see value_deduped above, applied globally
    # before pool slicing/correction) -- every entry in `pool` already has a
    # distinct (spot, sigma) per ticker, and re-sorting by corrected
    # band_cost doesn't change spot/sigma, so only the min_gap and raw-quote
    # checks are left to do here.
    selected = []
    raw_seen_per_ticker = {}
    times_per_ticker = {}
    for r in pool:
        prior_times = times_per_ticker.get(r['ticker'], [])
        if any(abs(r['t'] - pt) < min_gap_td for pt in prior_times):
            continue
        sig = _raw_signature(_snapshot(r['ticker'], r['t']))
        if sig in raw_seen_per_ticker.get(r['ticker'], []):
            continue
        selected.append(r)
        raw_seen_per_ticker.setdefault(r['ticker'], []).append(sig)
        times_per_ticker.setdefault(r['ticker'], []).append(r['t'])
        if len(selected) == top_n:
            break
    return selected


def plot_worst_fits(day_results, top_n=10, warmup='15min'):
    """
    Find the top_n worst-fitting snapshots (find_worst_fits) and plot each
    one's market snapshot (plot_fit_occurrence). Prints a one-line summary
    per snapshot and returns the records, in case the caller wants them
    directly.
    """
    worst = find_worst_fits(day_results, top_n=top_n, warmup=warmup)
    for rank, record in enumerate(worst, 1):
        print(f"#{rank}: {record['ticker']}  {record['t']}  "
              f"band_cost={record['band_cost']:.4f}  (mu={record['spot']:.2f}, sigma={record['sigma']:.2f})")
        long_df = load_predexon_long_df(record['ticker'])
        kalshi_backup = load_kalshi_backup_long_df(record['ticker'])
        plot_fit_occurrence(record, long_df, kalshi_backup)
    return worst


# ---------------------------------------------------------------------------
# Diagnostic -- symmetric-normal skew check
# ---------------------------------------------------------------------------

_SKEW_TYPE_COLORS = {'less': '#e74c3c', 'between': '#7f8c8d', 'greater': '#2980b9'}
_DEFAULT_SKEW_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "skew_diag")
_SKEW_SCHEMA_VERSION = "v3"  # bump whenever the cached row schema OR gathering method changes


def _skew_cache_key(valid_day_results, max_snapshots_per_day, use_smile=False):
    """
    Stable cache key for plot_vol_skew_diagnostic's expensive per-snapshot
    gather step: a hash of the exact (sorted) day list + max_snapshots_per_day
    it was built from, plus _SKEW_SCHEMA_VERSION (so a code change that adds/
    renames a cached column -- e.g. adding 'date' for the by-month breakdown
    -- naturally invalidates old caches instead of loading a DataFrame
    missing a column newer code expects). Deliberately NOT a function of
    z_range -- z_range is applied to the cached table AFTER loading, cheaply,
    so changing it alone never invalidates the cache -- and NOT a hash of the
    underlying tick data itself: this project's cached implied_ts/raw-tick
    files are treated as immutable once written (see compute_implied_spot_
    timeseries_cached's own docstring on the same assumption), so the day
    list alone identifies a specific run's inputs.

    use_smile IS part of the key (unlike z_range): it changes what gets
    computed and stored into the 'mispricing' column itself (symmetric
    bucket_model_prob_and_grad vs. the local-sigma smile reprice -- see
    plot_vol_skew_diagnostic), not just how an already-computed table gets
    filtered/binned afterward, so the two must never share a cache file.
    """
    dates = sorted(str(date) for _, _, date in valid_day_results)
    key_str = f"{_SKEW_SCHEMA_VERSION}|{max_snapshots_per_day}|smile={use_smile}|" + ",".join(dates)
    return hashlib.md5(key_str.encode()).hexdigest()[:16]


def plot_vol_skew_diagnostic(day_results, max_snapshots_per_day=150, z_bins=12, z_range=5.0,
                              months_per_row=4, panel_size=(4.5, 4.0), use_cache=True, cache_dir=None,
                              use_smile=False):
    """
    Visual check for whether the symmetric-normal model (T ~ N(mu, sigma^2)
    -- the same model fit_normal_to_bucket_bid_ask itself fits) systematically
    mispriced one side of the distribution vs. the other across the cached
    sample, i.e. whether there's a persistent SKEW the symmetric assumption
    can't capture, as opposed to noise that would average out.

    For up to `max_snapshots_per_day` sampled timestamps per day (every
    len(df)//max_snapshots_per_day -th row of the ALREADY-FITTED implied_
    spot/sigma series in day_results), pulls every market's own raw bid/ask
    via a single vectorized backward pd.merge_asof PER MARKET across that
    day's whole sampled-timestamp grid at once (the same trick black_scholes.
    compute_implied_spot_timeseries uses to fit an entire day in one pass,
    rather than black_scholes.snapshot_market_quotes's per-timestamp filter+
    sort, which this used to call in a loop -- see the "why is this slow"
    session note below) and computes:
      - z = (x_pos - mu) / sigma: each market's own representative location
        (bucket center for 'between', threshold for a tail) in the FITTED
        model's own standard-deviation units -- z < 0 is the 'less'/lower
        side, z > 0 is the 'greater'/upper side, z ~ 0 is near-ATM. Plotted
        as delta = Phi(z) (the standard "quote by delta instead of strike"
        convention) rather than raw z -- a monotonic transform onto a 0-1
        probability scale, 0.5 at center, ->0 deep 'less', ->1 deep
        'greater' -- but z is still what z_range filters on (see below).
      - mispricing = model_prob(mu, sigma, that market's own floor/cap/
        strike_type) - mid(bid, ask): positive means the symmetric model
        thinks that market is UNDERPRICED (a buy signal under the model),
        negative means OVERPRICED (a sell signal).

    If the symmetric assumption is fine, mispricing should scatter ~randomly
    around 0 at every z. If there's a persistent skew the symmetric fit can't
    capture, mispricing will show a systematic SLOPE across z -- e.g.
    consistently positive for z>0 and negative for z<0 (or the reverse) --
    which would mean "buy the tail that looks cheap, sell the tail that looks
    expensive" is actually just "buy/sell whichever side the model is
    structurally biased toward," not real edge. The binned mean (black line)
    is what makes that trend visible through the raw scatter's noise.

    NOT the same quantity as the fit's own internal band-violation residual
    (which uses the hinge/anchor/one-sided-midprice logic in fit_normal_to_
    bucket_bid_ask) -- this compares against each market's plain mid price
    instead, a simpler, directly interpretable proxy that doesn't require
    exposing the solver's internals, at the cost of not exactly matching what
    the solver itself optimized against.

    Deliberately simpler than snapshot_market_quotes in one respect: each
    market's quote here is Predexon's own last-known bid/ask via a plain
    backward asof (no staleness threshold, no Kalshi-1min-candle fallback for
    stale/mismatched quotes). That fallback exists in the core fitting
    pipeline because a stale quote can silently corrupt the (mu, sigma) FIT
    itself; here it's the opposite direction (comparing an ALREADY-fitted mu/
    sigma against each market's own quote), so an occasional stale quote just
    adds a little noise to one snapshot's mispricing rather than biasing the
    model inputs -- immaterial at max_snapshots_per_day scale, and worth
    trading away for the large speedup.

    Runtime: ~1 merge_asof call per market per day (a handful per day) instead
    of max_snapshots_per_day x markets/event individual filter+sort calls --
    this used to be the slow part of the notebook (tens of minutes over a
    large day cohort, since black_scholes.snapshot_market_quotes rescans that
    day's ENTIRE raw long_df from scratch on every single call, an approach
    it was designed for occasional one-off lookups, not tens of thousands of
    them); this version should run in seconds to low minutes over the same
    cohort.

    z_range: snapshots with |z| beyond this are dropped entirely (not just
    clipped into the edge bin) before plotting/binning/printing -- deep-OTM
    strikes have the widest, least-trustworthy bid/ask bands (see fit_normal_
    to_bucket_bid_ask's own docstring on one-sided/wide-spread markets), so
    they'd otherwise dominate the tails of this diagnostic with the noisiest
    points.

    Split into ONE panel/table per CALENDAR MONTH present in the data (the
    combined pooled sample within each month -- not split by market
    sidedness), arranged `months_per_row` per row, so a seasonal effect on
    the skew shape is visible directly rather than averaged away by pooling
    every month together. If a pooled analysis shows a skew, that alone
    doesn't say whether it's a stable, persistent property or an artifact of
    a handful of unusual days/regimes dominating specific z-bins in the
    pooled sample -- seeing whether the same shape recurs independently in
    (e.g.) January, April, and July is a much stronger check than one
    combined plot, since those months have genuinely different LA weather
    regimes (winter vs. spring vs. summer heat/marine-layer dynamics).

    Prints the binned-mean table (z-bin center, mean mispricing, n) for each
    month's panel so it can be pasted elsewhere -- the same numbers the black
    lines plot.

    use_cache/cache_dir: the expensive part of this (one snapshot_market_
    quotes call per sampled timestamp per day, each filtering/sorting that
    day's raw long_df) is cached to parquet keyed by the exact day list +
    max_snapshots_per_day (see _skew_cache_key) -- a large day cohort (e.g.
    every locally-cached day) can otherwise take minutes to regenerate on
    every rerun. The UNFILTERED per-snapshot table is what's cached (z_range
    is applied after loading -- see _skew_cache_key), so changing z_range
    alone never triggers a recompute. Treated as immutable once written, same
    assumption compute_implied_spot_timeseries_cached makes for the
    underlying tick data -- delete the file (or pass use_cache=False) to
    force a rebuild.

    use_smile: if True, 'mispricing' is computed against black_scholes.
    smile_bucket_prob (the local-sigma smile: every market priced off its own
    interpolated point on a curve built from the day's own listed 'less'/
    'greater' wing markets, flat at implied_sigma out to z=+-1.3 -- see
    fit_local_sigma_smile/expand_smile_pillars) instead of the plain
    symmetric bucket_model_prob_and_grad. Requires each day's df (in
    day_results) to already carry the implied_wing_z_less/implied_wing_sigma_
    less/implied_wing_z_greater/implied_wing_sigma_greater columns -- i.e.
    the underlying implied_ts cache must have been (re)generated with
    fit_smile=True (see regen_cache.py) -- a day missing those columns is
    silently treated as having no wing correction that day (falls back to
    plain symmetric pricing for that day only, not an error), since a mixed
    cache (some days regenerated, some not) is a perfectly reasonable state
    mid-regen. Unlike the plain path (fully vectorized across a day's sampled
    timestamps via numpy array ops on bucket_model_prob_and_grad), this loops
    per sampled timestamp within each ticker, since the smile's pillars
    genuinely differ row to row -- slower, but still trivial at
    max_snapshots_per_day scale.

    Returns the underlying (z, delta, mispricing, strike_type, two_sided,
    month) DataFrame (already restricted to |z| <= z_range), in case the
    caller wants to inspect or slice it directly instead of just the plot.
    """
    valid = [(ticker, df, date) for ticker, df, date in day_results if not df.empty]
    if not valid:
        print('No data to plot.')
        return None

    if cache_dir is None:
        cache_dir = _DEFAULT_SKEW_CACHE_DIR
    cache_path = os.path.join(cache_dir, f"{_skew_cache_key(valid, max_snapshots_per_day, use_smile)}.parquet")

    if use_cache and os.path.exists(cache_path):
        diag_full = pd.read_parquet(cache_path)
        print(f"[skew cache] loaded {len(diag_full)} snapshots from {cache_path}")
    else:
        frames = []
        for ticker, df, date in valid:
            long_df = load_predexon_long_df(ticker)
            if long_df.empty:
                continue
            stride = max(1, len(df) // max_snapshots_per_day)
            sampled = df.iloc[::stride]
            eval_df = pd.DataFrame({'time': sampled['time'].to_numpy()})
            mu_arr = sampled['implied_spot'].to_numpy()
            sigma_arr = sampled['implied_sigma'].to_numpy()
            sigma_ok = sigma_arr > 0
            month_label = date.strftime('%Y-%m')

            pillars_per_row = None
            if use_smile:
                wing_cols = {'implied_wing_z_less', 'implied_wing_sigma_less',
                             'implied_wing_z_greater', 'implied_wing_sigma_greater'}
                if wing_cols.issubset(sampled.columns):
                    wing_z_less_arr = sampled['implied_wing_z_less'].to_numpy()
                    wing_sigma_less_arr = sampled['implied_wing_sigma_less'].to_numpy()
                    wing_z_greater_arr = sampled['implied_wing_z_greater'].to_numpy()
                    wing_sigma_greater_arr = sampled['implied_wing_sigma_greater'].to_numpy()
                else:
                    # This day's cache predates fit_smile=True -- fall back to
                    # no wing correction for this day only (not an error; a
                    # cache mid-regen can legitimately have a mix).
                    n = len(sampled)
                    wing_z_less_arr = np.full(n, np.nan)
                    wing_sigma_less_arr = np.full(n, np.nan)
                    wing_z_greater_arr = np.full(n, np.nan)
                    wing_sigma_greater_arr = np.full(n, np.nan)
                # Built ONCE per sampled timestamp, reused across every
                # ticker/market below -- mirrors compute_implied_spot_
                # timeseries's own "fit once per snapshot, price every market
                # off it" shape, rather than rebuilding the same curve
                # redundantly inside the per-ticker loop.
                pillars_per_row = [
                    black_scholes.expand_smile_pillars(
                        sigma_arr[j], wing_z_less_arr[j], wing_sigma_less_arr[j],
                        wing_z_greater_arr[j], wing_sigma_greater_arr[j],
                    ) if sigma_ok[j] else None
                    for j in range(len(sampled))
                ]

            for mtk in sorted(long_df['ticker'].unique()):
                sub = long_df.loc[long_df['ticker'] == mtk].sort_values('time')
                floor = float(sub['floor_strike'].iloc[0])
                cap_raw = sub['cap_strike'].iloc[0]
                cap = float(cap_raw) if pd.notna(cap_raw) else None
                strike_type = sub['strike_type'].iloc[0]
                x_pos = (floor + cap) / 2.0 if cap is not None else floor

                # One backward asof per market across the WHOLE sampled grid at
                # once -- same vectorized approach compute_implied_spot_
                # timeseries uses -- instead of a fresh filter+sort per timestamp.
                merged = pd.merge_asof(
                    eval_df,
                    sub[['time', 'bid', 'ask', 'bid_depth', 'ask_depth']].rename(columns={'time': 'quote_time'}),
                    left_on='time', right_on='quote_time', direction='backward',
                )
                bid = merged['bid'].to_numpy()
                ask = merged['ask'].to_numpy()
                bidd = merged['bid_depth'].to_numpy()
                askd = merged['ask_depth'].to_numpy()
                ok = sigma_ok & ~np.isnan(bid) & ~np.isnan(ask)
                if not ok.any():
                    continue

                mu_v, sigma_v = mu_arr[ok], sigma_arr[ok]
                if use_smile:
                    ok_idx = np.nonzero(ok)[0]
                    p = np.array([
                        black_scholes.smile_bucket_prob(mu_arr[j], sigma_arr[j], pillars_per_row[j], floor, cap, strike_type)
                        for j in ok_idx
                    ])
                else:
                    p, _, _ = black_scholes.bucket_model_prob_and_grad(mu_v, sigma_v, floor, cap, strike_type)
                mid = (bid[ok] + ask[ok]) / 2.0
                z = (x_pos - mu_v) / sigma_v
                two_sided = (bidd[ok] > 0) & (askd[ok] > 0)
                frames.append(pd.DataFrame({
                    'z': z, 'mispricing': p - mid, 'strike_type': strike_type,
                    'two_sided': two_sided, 'month': month_label,
                }))

        if not frames:
            print('No market snapshots to plot.')
            return None
        diag_full = pd.concat(frames, ignore_index=True)

        if use_cache:
            os.makedirs(cache_dir, exist_ok=True)
            diag_full.to_parquet(cache_path, index=False)
            print(f"[skew cache] saved {len(diag_full)} snapshots to {cache_path}")

    diag = diag_full[diag_full['z'].abs() <= z_range].reset_index(drop=True)
    if diag.empty:
        print(f'No market snapshots within |z| <= {z_range}.')
        return None
    # delta = Phi(z): the same z-range filter (a distance-from-center cutoff,
    # naturally expressed in sigma units) still applies, but the axis itself
    # is re-expressed on a 0-1 probability scale -- 0.5 at center, ->0 deep on
    # the 'less' side, ->1 deep on the 'greater' side -- the standard
    # "quote by delta instead of strike" convention, and a monotonic
    # transform of z so it preserves the exact same shape/information.
    diag['delta'] = _norm.cdf(diag['z'])

    bins = np.linspace(_norm.cdf(-z_range), _norm.cdf(z_range), z_bins + 1)
    months = sorted(diag['month'].unique())
    n_months = len(months)
    months_per_row = min(months_per_row, n_months)
    n_rows = int(np.ceil(n_months / months_per_row))

    fig, axes = plt.subplots(n_rows, months_per_row,
                              figsize=(panel_size[0] * months_per_row, panel_size[1] * n_rows),
                              sharey=True, squeeze=False)
    axes_flat = axes.flatten()

    for i, month in enumerate(months):
        ax = axes_flat[i]
        sub_panel = diag[diag['month'] == month]
        for strike_type, color in _SKEW_TYPE_COLORS.items():
            sub = sub_panel[sub_panel['strike_type'] == strike_type]
            if sub.empty:
                continue
            ax.scatter(sub['delta'], sub['mispricing'], s=8, alpha=0.15, color=color,
                       label=strike_type, edgecolors='none')

        print(f"\n{month} ({len(sub_panel)} snapshots, |z| <= {z_range}):")
        sub_panel = sub_panel.copy()
        sub_panel['_bin'] = pd.cut(sub_panel['delta'], bins)
        binned = sub_panel.groupby('_bin', observed=True)['mispricing'].agg(['mean', 'count'])
        bin_centers = [iv.mid for iv in binned.index]
        ax.plot(bin_centers, binned['mean'].to_numpy(), color='black', lw=2.0, marker='o', ms=4,
                 label='Binned mean', zorder=10)

        print(f"{'delta_bin_center':>17}{'mean_mispricing':>18}{'n':>8}")
        for center, (mean_val, n) in zip(bin_centers, binned.itertuples(index=False)):
            print(f"{center:>17.3f}{mean_val:>18.4f}{n:>8.0f}")

        ax.axhline(0, color='#999999', lw=1.0, ls='--')
        ax.axvline(0.5, color='#999999', lw=1.0, ls='--')
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.05, 0.05)
        ax.set_xlabel('delta = Φ(z)', fontsize=9)
        ax.set_title(f'{month}, n={len(sub_panel)}', fontsize=10)
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3)

    for i in range(n_months, len(axes_flat)):
        axes_flat[i].axis('off')

    axes_flat[0].legend(fontsize=7, loc='upper right')
    for row in range(n_rows):
        axes[row, 0].set_ylabel('Model prob - mid', fontsize=9)
    fig.suptitle(f'Symmetric-normal skew diagnostic by month ({len(valid)} days total)', fontsize=13)
    fig.tight_layout()
    plt.show()

    return diag
