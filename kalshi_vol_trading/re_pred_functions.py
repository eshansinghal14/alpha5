"""
Predicting future noise-corrected realized vol for the LA high-temp market
(KXHIGHLAX) from an EMA of past noise-corrected realized vol -- re_pred.ipynb.
Kept out of the notebook for the same reason plotting.py is (see its own
docstring): the notebook owns which days/horizons to backtest, this module
owns the estimator and the backtest machinery.

Method
------
1. Split each day's finest (1s) implied_spot series into `bar_freq` (default
   1min) bars. Each bar's own realized variance is estimated via
   plotting.two_scale_realized_variance (Zhang/Mykland/Ait-Sahalia TSRV)
   rather than a naive sum of squared 1s diffs -- see that function's
   docstring for why naive fine-grid RV is systematically biased UP by
   LM-fit tick-to-tick noise. This gives one noise-corrected variance
   observation per bar.
2. EMA-smooth that per-bar series (RiskMetrics-style: ema(t) = a*x(t) +
   (1-a)*ema(t-1), holding forward through any NaN/no-data bar rather than
   resetting or interpolating) -- this smoothed series is the actual
   PREDICTOR this notebook backtests; `ema_halflife` controls how
   responsive vs. smooth it is.
3. Convert the EMA'd per-bar variance into a 1-day-equivalent vol RATE (same
   units as plotting.iv_scaled_to_day: degF / sqrt(day)) and scale it to each
   forecast horizon h via sqrt(h in days) -- the same Brownian time-scaling
   used everywhere else in this project (iv_scaled_to_day, realized_vol_scaled).
4. The "actual" side of the backtest is measured the SAME noise-corrected
   way -- also via two_scale_realized_variance, at a horizon-appropriate
   fast/slow split (see TARGET_SLOW_FREQ) -- over the FUTURE (t, t+h] window,
   not a naive realized move. The thing being forecast is "noise-corrected
   realized vol," not raw tick-to-tick wobble, so the target has to be
   measured the same way the predictor is built, or a systematic gap between
   the two would just be measuring the noise correction itself rather than
   forecast skill.
"""
import datetime
import random

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from plotting import two_scale_realized_variance, pt_midnight_utc, LA_TZ, _VARIANCE_FREQ_PALETTE

HORIZONS = ['10s', '1min', '10min', '30min', '1hr']

# Fast/slow split two_scale_realized_variance uses to noise-correct the
# ACTUAL realized vol measured over each forecast horizon -- the slow scale
# must be small enough relative to the horizon to leave room for a genuine
# fast-vs-slow comparison (see two_scale_realized_variance's docstring). 10s
# is deliberately left out: at 1s sampling a 10s window is only ~10 points,
# too few for TSRV to meaningfully separate signal from noise, so it falls
# back to naive (uncorrected) QV instead -- every output this module
# produces keeps that distinction visible via TARGET_SLOW_FREQ.get(h) being
# None for '10s'.
TARGET_SLOW_FREQ = {
    '1min': '10s',
    '10min': '1min',
    '30min': '1min',
    '1hr': '1min',
}

# Below this many valid ticks, two_scale_realized_variance's own low-n
# behavior (0.0, via its n_obs<3 floor, or a near-degenerate estimate just
# above it) means "we didn't have enough data to estimate anything," not
# "genuinely zero vol" -- treated as NaN, not 0, everywhere in this module
# (see _safe_realized_var) so a thin stretch doesn't silently drag a
# prediction or a target toward 0.
MIN_TSRV_OBS = 5

HORIZON_PALETTE = dict(zip(HORIZONS, _VARIANCE_FREQ_PALETTE))


def _safe_realized_var(window, slow_freq=None, min_obs=MIN_TSRV_OBS):
    """
    Realized variance over `window` (an implied_spot Series indexed by
    time), NaN-safe: returns NaN when fewer than `min_obs` valid points are
    present rather than trusting two_scale_realized_variance's own low-n
    floor (0.0) -- see this module's docstring. `slow_freq=None` uses naive
    (uncorrected) sum-of-squared-diffs instead of TSRV -- the '10s'-horizon
    fallback described above.
    """
    valid = window.dropna()
    if len(valid) < min_obs:
        return np.nan
    if slow_freq is None:
        return float(np.sum(np.diff(valid.to_numpy()) ** 2))
    return two_scale_realized_variance(valid, slow_freq=slow_freq)


def ema_series(values, halflife_periods):
    """
    RiskMetrics-style EMA over `values` (array-like, one observation per
    regularly-spaced bar), holding the last valid EMA forward through any
    NaN bar instead of resetting or interpolating through it -- the same
    "unknown stays unknown, don't invent a value" convention plotting.py's
    gap handling uses elsewhere in this project (see variance_till_expiry's
    docstring). Returns NaN for every bar before the first valid observation.
    """
    alpha = 1.0 - 0.5 ** (1.0 / halflife_periods)
    out = np.full(len(values), np.nan)
    ema = np.nan
    for i, v in enumerate(values):
        if np.isnan(v):
            out[i] = ema
            continue
        ema = v if np.isnan(ema) else alpha * v + (1 - alpha) * ema
        out[i] = ema
    return out


def day_window(date):
    """
    (day_start, day_end) for `date`'s own settlement day: 11:00 PT the day
    before -> PT midnight ending `date`'s calendar day -- same convention
    plot_implied_spot_history/iv_scaled_to_day_realized_winddown use in
    plotting.py, rather than the full 48h fetch window fetch_day_timeseries
    pulls (which starts a full day earlier, before the market has any real
    liquidity, and would otherwise bias early bars toward thin-data NaNs).
    """
    day_start = pt_midnight_utc(date) - pd.Timedelta(hours=13)
    day_end = pt_midnight_utc(date) + pd.Timedelta(hours=24)
    return day_start, day_end


def build_bar_variance(spot_1s, day_start, day_end, bar_freq='1min', predictor_slow_freq='10s'):
    """
    One noise-corrected realized-variance observation per `bar_freq` bar
    (via _safe_realized_var/TSRV on that bar's own 1s ticks) spanning
    [day_start, day_end). Returned Series is indexed by each bar's END
    timestamp -- the point in time the bar's variance is actually KNOWN as
    of, which is what build_predictions treats as "now" when forecasting
    forward from it.
    """
    edges = pd.date_range(day_start, day_end, freq=bar_freq)
    spot_1s = spot_1s.sort_index()
    var_bar = np.full(len(edges) - 1, np.nan)
    for i in range(len(edges) - 1):
        window = spot_1s.loc[edges[i]:edges[i + 1]]
        var_bar[i] = _safe_realized_var(window, slow_freq=predictor_slow_freq)
    return pd.Series(var_bar, index=edges[1:])


def build_predictions(spot_1s, date, horizons=HORIZONS, bar_freq='1min',
                       ema_halflife='20min', eval_stride='5min',
                       predictor_slow_freq='10s'):
    """
    One day's (predicted, actual) noise-corrected realized-vol pairs, one
    row per (evaluation time, horizon) -- see this module's docstring for
    the full method. predicted_vol/actual_vol are both raw degF (stdev of
    price change over that horizon), directly comparable to each other;
    predicted_var/actual_var are their squares (the scale accuracy should
    actually be scored in -- see compute_metrics's docstring).

    Evaluation times are `bar_freq` bars subsampled every `eval_stride`
    (should be an exact multiple of bar_freq): backtesting every single bar
    would mean adjacent evaluation windows for the SAME horizon share almost
    all of their underlying ticks, adding runtime without adding real
    statistical information.

    A (time, horizon) pair is dropped if either side isn't computable: the
    EMA predictor hasn't seen a valid bar yet (still NaN), the predictor is
    still in its warm-up window (fewer than one `ema_halflife`'s worth of
    real bars behind it -- see the warm-up note below), the actual side's
    forward window runs past `spot_1s`'s own last tick (not enough future
    data yet to know what actually happened), or too few ticks landed in
    the actual window to trust (_safe_realized_var's min_obs floor). This
    unavoidably excludes the last `max(horizons)` of every day from the
    backtest -- there's no way to know the "actual" 1-hour-forward vol at
    a point less than an hour from the day's last tick.

    Warm-up: a day's market opens thin (few markets quoted, per-bar TSRV
    itself noisy off a handful of ticks -- the same opening-thinness effect
    plotting.realized_vs_implied_at_day_start's docstring documents for
    implied vol), so the EMA's first few bars can sit at a near-zero,
    not-yet-informative level regardless of what actually happens right
    after. Confirmed directly (2026-02-08's opening bar): predicted_var
    exactly 0.0 immediately followed by an actual 1min realized variance of
    0.024 -- a single cold-start row that on its own inflated that day's
    mean QLIKE by 6 orders of magnitude. Evaluation points are therefore
    skipped until the EMA has had at least one full `ema_halflife` of
    ACTUAL (non-NaN) bars behind it, not just calendar time since day start.
    """
    day_start, day_end = day_window(date)
    spot_1s = spot_1s.set_index('time')['implied_spot'].sort_index() if 'time' in spot_1s.columns else spot_1s

    bar_var = build_bar_variance(spot_1s, day_start, day_end, bar_freq=bar_freq,
                                  predictor_slow_freq=predictor_slow_freq)

    bar_td = pd.Timedelta(bar_freq)
    halflife_bars = pd.Timedelta(ema_halflife) / bar_td
    ema_var = ema_series(bar_var.to_numpy(), halflife_bars)
    bar_days = bar_td / pd.Timedelta(days=1)
    vol_rate = pd.Series(np.sqrt(np.clip(ema_var, 0, None) / bar_days), index=bar_var.index)
    valid_bars_seen = pd.Series(bar_var.notna().to_numpy().cumsum(), index=bar_var.index)

    stride_bars = max(1, int(round(pd.Timedelta(eval_stride) / bar_td)))
    eval_times = vol_rate.index[::stride_bars]

    last_tick = spot_1s.index.max()
    rows = []
    for t in eval_times:
        rate = vol_rate.loc[t]
        if np.isnan(rate) or valid_bars_seen.loc[t] < halflife_bars:
            continue
        for h in horizons:
            h_td = pd.Timedelta(h)
            h_days = h_td / pd.Timedelta(days=1)
            t_end = t + h_td
            if t_end > last_tick:
                continue
            window = spot_1s.loc[t:t_end]
            actual_var = _safe_realized_var(window, slow_freq=TARGET_SLOW_FREQ.get(h))
            if np.isnan(actual_var):
                continue
            predicted_var = (rate ** 2) * h_days
            rows.append({
                'date': date, 'time': t, 'horizon': h,
                'predicted_var': predicted_var, 'actual_var': actual_var,
                'predicted_vol': np.sqrt(predicted_var), 'actual_vol': np.sqrt(actual_var),
            })
    return pd.DataFrame(rows, columns=['date', 'time', 'horizon', 'predicted_var', 'actual_var',
                                        'predicted_vol', 'actual_vol'])


def sample_backtest_days(cached_days, n, months_back=6, as_of=None, gap_check=None, seed=42):
    """
    Random sample of up to `n` days from `cached_days`, restricted to the
    trailing `months_back` months of `as_of` (default: today) -- "random
    days over the past 6 months," not a most-recent-N walk. `gap_check`
    (e.g. a wrapper around plotting._gap_fraction), if given, is applied
    AFTER the random draw and any day it rejects is simply dropped rather
    than replaced from outside the window -- padding back out to `n` by
    reaching further back would defeat the point of sampling a fixed
    trailing window, so this can return fewer than `n` days.
    """
    if as_of is None:
        as_of = datetime.date.today()
    cutoff = as_of - datetime.timedelta(days=30 * months_back)
    pool = [d for d in cached_days if cutoff <= d <= as_of]
    rng = random.Random(seed)
    draw = rng.sample(pool, min(n, len(pool)))
    if gap_check is None:
        return sorted(draw)
    return sorted(d for d in draw if gap_check(d))


def compute_metrics(results, space='var'):
    """
    Per-horizon accuracy table over `results` (the concatenation of
    build_predictions across every backtested day): MSE/RMSE/MAE/correlation/
    R^2 in `space` ('var' or 'vol'), plus QLIKE when space='var' -- the
    standard heteroskedasticity-robust loss for evaluating a VARIANCE
    forecast (actual/pred - log(actual/pred) - 1): unlike MSE, it penalizes
    an overconfident LOW forecast that actual vol blew past far more
    heavily than an equally-sized overcautious HIGH one, which is usually
    the failure mode that actually matters for a vol forecast feeding a
    trading decision. QLIKE is undefined in vol space (it's specifically a
    variance-forecast loss) so is NaN there.

    'var' is the more statistically appropriate space to score accuracy in
    (predicted_var/actual_var are the estimator's natural, roughly-symmetric-
    error scale); 'vol' (raw degF, sqrt of the above) is more legible for a
    quick read of "how many degrees off," at the cost of compressing large
    misses via the sqrt -- compute both rather than picking one.

    Both `qlike` (mean) and `qlike_median` are reported: QLIKE is
    unbounded above as predicted_var -> 0 (a quiet stretch the EMA has
    decayed toward near-zero over, immediately followed by a real move --
    a genuine, recurring failure mode for this predictor on a market that
    spends long stretches flat between jumps, not a numerical artifact),
    so its MEAN can be dominated by a handful of such rows even after
    build_predictions' own EMA warm-up filtering -- confirmed directly: a
    single row on 2026-02-08 (predicted_var==0.0 immediately followed by
    actual_var==0.024) alone moved a full day's mean QLIKE by 6 orders of
    magnitude. The median is the more representative "typical case" number;
    the mean is kept alongside specifically because that tail IS the risk a
    vol forecast feeding a trading decision actually cares about, not noise
    to average away.
    """
    pred_col, act_col = f'predicted_{space}', f'actual_{space}'
    rows = []
    for h in HORIZONS:
        sub = results[results['horizon'] == h]
        if sub.empty:
            continue
        pred, act = sub[pred_col].to_numpy(), sub[act_col].to_numpy()
        err = pred - act
        ss_res = np.sum(err ** 2)
        ss_tot = np.sum((act - act.mean()) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
        corr = np.corrcoef(pred, act)[0, 1] if len(pred) > 1 else np.nan

        qlike, qlike_median = np.nan, np.nan
        if space == 'var':
            pv, av = sub['predicted_var'].to_numpy(), sub['actual_var'].to_numpy()
            eps = np.median(av[av > 0]) * 1e-6 if np.any(av > 0) else 1e-12
            ratio = (av + eps) / (pv + eps)
            qlike_vals = ratio - np.log(ratio) - 1
            qlike = float(np.mean(qlike_vals))
            qlike_median = float(np.median(qlike_vals))

        rows.append({
            'horizon': h, 'n': len(sub),
            'mse': float(np.mean(err ** 2)), 'rmse': float(np.sqrt(np.mean(err ** 2))),
            'mae': float(np.mean(np.abs(err))), 'r2': r2, 'corr': corr,
            'qlike': qlike, 'qlike_median': qlike_median,
            'mean_predicted': float(pred.mean()), 'mean_actual': float(act.mean()),
        })
    out = pd.DataFrame(rows).set_index('horizon')
    present = [h for h in HORIZONS if h in out.index]
    return out.loc[present]


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_example_day(spot_1s, date, bar_freq='1min', ema_halflife='20min',
                      predictor_slow_freq='10s'):
    """
    One day's implied spot alongside the EMA noise-corrected vol-rate signal
    (predicted_var's own sqrt(ema_var/bar_days) input, in degF/sqrt(day) --
    same units as plotting.iv_scaled_to_day) that build_predictions scales
    into each horizon's forecast. Two stacked panels sharing one time axis
    (never a dual-axis single panel -- spot and vol rate are different units
    on very different scales) so the vol signal's actual shape -- when it
    reacts, how much it smooths -- is visible against the price path that
    produced it.
    """
    day_start, day_end = day_window(date)
    spot_1s = spot_1s.set_index('time')['implied_spot'].sort_index() if 'time' in spot_1s.columns else spot_1s
    bar_var = build_bar_variance(spot_1s, day_start, day_end, bar_freq=bar_freq,
                                  predictor_slow_freq=predictor_slow_freq)
    bar_td = pd.Timedelta(bar_freq)
    halflife_bars = pd.Timedelta(ema_halflife) / bar_td
    ema_var = ema_series(bar_var.to_numpy(), halflife_bars)
    bar_days = bar_td / pd.Timedelta(days=1)
    vol_rate = pd.Series(np.sqrt(np.clip(ema_var, 0, None) / bar_days), index=bar_var.index)

    spot_window = spot_1s.loc[day_start:day_end]
    times_la = spot_window.index.tz_convert(LA_TZ)
    vol_times_la = vol_rate.index.tz_convert(LA_TZ)

    fig, (ax_spot, ax_vol) = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True,
                                           gridspec_kw={'height_ratios': [1, 1], 'hspace': 0.08})
    ax_spot.plot(times_la, spot_window.to_numpy(), color='#2c3e50', lw=1.2)
    ax_spot.set_ylabel('Implied high (degF)', fontsize=9)
    ax_spot.set_title(f'{date} -- implied spot and EMA noise-corrected vol rate', fontsize=11)
    ax_spot.grid(True, alpha=0.3)
    ax_spot.tick_params(labelsize=8)

    ax_vol.plot(vol_times_la, vol_rate.to_numpy(), color='#17becf', lw=1.4)
    ax_vol.set_ylabel('EMA vol rate\n(degF / sqrt(day))', fontsize=9)
    ax_vol.grid(True, alpha=0.3)
    ax_vol.tick_params(labelsize=8)
    fig.autofmt_xdate()
    return fig


def plot_predicted_vs_actual(results, space='vol', max_points_per_horizon=4000, seed=0):
    """
    One scatter panel per horizon (predicted vs. actual, `space` in {'vol',
    'var'}), sharing both axes so panels are visually comparable, with a
    y=x reference line -- a well-calibrated forecast clusters along it; a
    cloud consistently above/below it is a systematic over/under-forecast at
    that horizon. Colored by horizon using the same fixed HORIZON_PALETTE
    order plotting.py's own variance-frequency panel uses (categorical
    identity, not a cycled/rainbow map) -- one legend-worthy color per
    horizon, consistent across every figure this module draws.

    Each horizon is subsampled to at most `max_points_per_horizon` (a fixed
    seed, so the plot is reproducible) purely to keep the scatter render
    fast and legible -- compute_metrics still runs on the FULL `results`,
    this only affects what gets drawn.
    """
    pred_col, act_col = f'predicted_{space}', f'actual_{space}'
    horizons_present = [h for h in HORIZONS if h in results['horizon'].unique()]
    n = len(horizons_present)
    fig, axes = plt.subplots(1, n, figsize=(3.1 * n, 3.3), sharex=True, sharey=True)
    axes = np.atleast_1d(axes)

    all_vals = pd.concat([results[pred_col], results[act_col]]).replace([np.inf, -np.inf], np.nan).dropna()
    lo, hi = (0.0, all_vals.quantile(0.995)) if not all_vals.empty else (0.0, 1.0)

    rng = np.random.RandomState(seed)
    for ax, h in zip(axes, horizons_present):
        sub = results[results['horizon'] == h]
        if len(sub) > max_points_per_horizon:
            idx = rng.choice(len(sub), size=max_points_per_horizon, replace=False)
            sub = sub.iloc[idx]
        ax.plot([lo, hi], [lo, hi], color='#999999', lw=1.0, ls='--', zorder=1)
        ax.scatter(sub[pred_col], sub[act_col], s=6, alpha=0.25, color=HORIZON_PALETTE[h],
                   edgecolors='none', zorder=2)
        ax.set_title(h, fontsize=10)
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_xlabel(f'predicted {space}', fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=7)
    axes[0].set_ylabel(f'actual {space}', fontsize=8)
    fig.suptitle(f'Predicted vs. actual noise-corrected realized {space} by horizon', fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return fig


def plot_metrics_bar(metrics, metric='rmse'):
    """
    Single bar per horizon for one `metric` column of compute_metrics'
    output, in HORIZONS' own (short -> long) order -- one measure, one axis,
    no dual-axis mixing e.g. rmse and r2 on the same panel. Bars share
    HORIZON_PALETTE so a horizon's color stays consistent with
    plot_predicted_vs_actual's scatter panels.
    """
    horizons_present = [h for h in HORIZONS if h in metrics.index]
    vals = metrics.loc[horizons_present, metric]
    colors = [HORIZON_PALETTE[h] for h in horizons_present]

    fig, ax = plt.subplots(figsize=(5.5, 3.3))
    bars = ax.bar(horizons_present, vals, color=colors, width=0.6)
    ax.set_ylabel(metric, fontsize=9)
    ax.set_title(f'{metric} by forecast horizon', fontsize=11)
    ax.grid(True, axis='y', alpha=0.3)
    ax.tick_params(labelsize=9)
    for rect, v in zip(bars, vals):
        ax.annotate(f'{v:.3g}', (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                    xytext=(0, 3), textcoords='offset points', ha='center', fontsize=8, color='#2c3e50')
    fig.tight_layout()
    return fig
