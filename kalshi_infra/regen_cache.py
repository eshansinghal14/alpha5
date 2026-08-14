"""
Regenerate the implied_spot/sigma cache for a set of days, in parallel.

The per-tick LM solve within a single day is inherently SEQUENTIAL now (each
point's fit is warm-started from the previous point's own result -- see
black_scholes.compute_implied_spot_timeseries's docstring on why), so it
can't be vectorized across time within a day. But different days -- and
different freq grids for the same day -- don't depend on each other at all,
so that's where the real speedup is: run them concurrently across processes
instead of one at a time in a Python loop.

Also always deletes the skew_diag cache first: its cache key is a hash of the
day list + max_snapshots_per_day, with no way to detect that the underlying
implied_ts data it was built from just changed underneath it (see
plotting._skew_cache_key's docstring) -- a stale skew_diag file would
silently keep serving results computed from the old, pre-regen fits.

Always passes fit_smile=True (see black_scholes.compute_implied_spot_
timeseries's docstring) so every regenerated file carries the implied_wing_*
columns the local-sigma smile needs -- cheap to compute unconditionally (it
reuses the same per-row bid/ask/depth matrices the primary fit already
builds), and it's simpler for every cache file to consistently have those
columns than to track which ones do.

Usage: edit DAYS/FREQS below and run `python regen_cache.py`.
"""
import os
import glob
import datetime
from datetime import timezone, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

import api_functions
import black_scholes
import predexon_functions

SKEW_DIAG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kalshi_vol_trading', 'data', 'skew_diag')


def _day_window_ts(date):
    import pytz
    la_tz = pytz.timezone('America/Los_Angeles')
    event_start_pt = la_tz.localize(datetime.datetime(date.year, date.month, date.day))
    start = event_start_pt - timedelta(days=1)
    end = event_start_pt + timedelta(days=1)
    now = int(datetime.datetime.now(timezone.utc).timestamp())
    return int(start.timestamp()), min(int(end.timestamp()), now)


def _regen_one(date, freq):
    """
    Runs in a worker process (Windows uses spawn, so this re-imports its own
    module-level deps fresh -- no shared state with the parent or other
    workers) -- deletes then recomputes exactly one (date, freq)'s cache
    file. Returns summary stats rather than the DataFrame itself, so workers
    don't have to ship a multi-MB result back over IPC just to be printed.
    """
    event_ticker = api_functions.event_ticker_from_date(date)
    cache_path = black_scholes.implied_spot_cache_path(event_ticker, freq)
    if os.path.exists(cache_path):
        os.remove(cache_path)
    start_ts, end_ts = _day_window_ts(date)
    df = black_scholes.compute_implied_spot_timeseries_cached(
        lambda: predexon_functions.load_predexon_long_df(event_ticker),
        start_ts, end_ts, event_ticker, freq=freq, fit_smile=True,
    )
    if df.empty:
        return event_ticker, freq, 0, float('nan'), float('nan')
    max_sigma = float(df['implied_sigma'].max())
    max_jump = float(df['implied_spot'].diff().abs().max())
    return event_ticker, freq, len(df), max_sigma, max_jump


def regenerate(dates, freqs=('1s', '1min'), max_workers=None, clear_skew_cache=True):
    """
    Delete + eagerly regenerate every (date, freq) cache file, in parallel.
    Always includes '1min' by default (feeds the winddown/skew diagnostics)
    and '1s' (feeds the 3-day-sample detailed plots) -- both grids, not just
    one, so a regen never leaves one of them stale relative to the other.
    """
    if clear_skew_cache:
        skew_files = glob.glob(os.path.join(SKEW_DIAG_DIR, '*.parquet'))
        for f in skew_files:
            os.remove(f)
        print(f'removed {len(skew_files)} stale skew_diag cache file(s)')

    # Pre-populate the event_markets cache SEQUENTIALLY in this (single) main
    # process first -- predexon_get's own rate limiter is a per-process lock,
    # so it only actually throttles correctly within one process. Once every
    # ticker's market listing is cached on disk, the parallel workers below
    # never need to call it at all, so this is the only place the real
    # network throttling has to hold.
    tickers = sorted({api_functions.event_ticker_from_date(d) for d in dates})
    print(f'pre-fetching event markets for {len(tickers)} tickers (sequential, rate-limited)...')
    for tk in tickers:
        predexon_functions.get_event_markets(tk)
    print('event markets cached.')

    jobs = [(d, f) for d in dates for f in freqs]
    max_workers = max_workers or max(1, (os.cpu_count() or 4) - 1)
    print(f'regenerating {len(jobs)} (day, freq) cache files across up to {max_workers} worker processes...')

    results = []
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_regen_one, d, f): (d, f) for d, f in jobs}
        for fut in as_completed(futures):
            ticker, freq, n, max_sigma, max_jump = fut.result()
            print(f'{ticker} ({freq}): {n} pts, max_sigma={max_sigma:.3f}, max_jump={max_jump:.3f}')
            results.append((ticker, freq, n, max_sigma, max_jump))
    return results


if __name__ == '__main__':
    # Full regen: every day already cached at each freq, not a fixed recent
    # window -- picking up both the certain-outcome mu fix (see fit_normal_
    # to_bucket_bid_ask's docstring on always holding warm_start's mu) and
    # the new implied_wing_* smile columns across the whole historical cache.
    # Different freqs generally have different day lists (1min covers nearly
    # every cached day; 1s/10s/10min only cover a small detailed-plot
    # subset), so each freq is regenerated against its OWN existing day list
    # rather than forcing one shared list across all of them.
    skew_files = glob.glob(os.path.join(SKEW_DIAG_DIR, '*.parquet'))
    for f in skew_files:
        os.remove(f)
    print(f'removed {len(skew_files)} stale skew_diag cache file(s)')

    for freq in ('1min', '1s', '10s', '10min'):
        days = black_scholes.list_cached_days(freq=freq)
        if not days:
            continue
        print(f'--- regenerating {len(days)} days at freq={freq} ---')
        regenerate(days, freqs=(freq,), clear_skew_cache=False)
    print('REGEN DONE')
