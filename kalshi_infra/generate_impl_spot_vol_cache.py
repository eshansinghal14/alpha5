"""One-off: for every event we've pulled Predexon orderbook history for
(data/predexon/{series}/orderbooks/*.parquet), compute + cache the implied spot/vol
timeseries (black_scholes.compute_implied_spot_timeseries_cached) at the
grid resolutions analysis.ipynb uses -- 1s (primary spot/variance/
vol-scaled series) plus 10s/1min (comparison grids for the variance panel,
see plot_implied_spot_history's variance_freqs param) -- plus 10min for a
coarser comparison point beyond what the notebook currently plots.

long_df is loaded once per event, not once per freq, and threaded into each
freq's cache call via a closure that just returns it -- so this makes at most
one live Predexon call (/kalshi/markets, for floor/cap/strike_type; the raw
ticks themselves are already local) per event, not one per freq.

start_ts/end_ts are passed wide open (epoch..2100) since
compute_implied_spot_timeseries clips the eval grid to the long_df's own
actual data range regardless -- no need to duplicate day_window_ts's PT
midnight math here.

Safe to re-run: compute_implied_spot_timeseries_cached skips any
(event_ticker, freq) already on disk.
"""
import os

from kalshi_infra import black_scholes as bs
from kalshi_infra import predexon_functions as pf
FREQS = ["1s", "10s", "1min", "10min"]
MAX_CONSECUTIVE_ERRORS = 5
WIDE_START_TS = 0
WIDE_END_TS = 4102444800  # 2100-01-01

event_tickers = pf.all_cached_predexon_event_tickers()
print(f"{len(event_tickers)} Predexon-cached events found.")

n_done = n_skipped = n_empty = n_errors = 0
consecutive_errors = 0

for i, et in enumerate(event_tickers):
    if all(os.path.exists(bs.implied_spot_cache_path(et, freq=f)) for f in FREQS):
        n_skipped += 1
        continue

    try:
        long_df = pf.load_predexon_long_df(et)
        if long_df.empty:
            print(f"  [warn] {et}: empty long_df, skipping")
            n_empty += 1
            consecutive_errors = 0
            continue

        for freq in FREQS:
            ts_df = bs.compute_implied_spot_timeseries_cached(
                lambda: long_df, WIDE_START_TS, WIDE_END_TS, et, freq=freq,
            )
            print(f"  {et} ({freq}): {len(ts_df)} points")
        n_done += 1
        consecutive_errors = 0
    except Exception as e:
        print(f"  [warn] {et}: {e}")
        n_errors += 1
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[STOP] {consecutive_errors} consecutive failures - giving up "
                  f"({i + 1}/{len(event_tickers)} events attempted).")
            break

print(
    f"\nDone (or stopped). computed={n_done}  already_cached_skipped={n_skipped}  "
    f"empty={n_empty}  errors={n_errors}  total_done_now={n_skipped + n_done}/{len(event_tickers)}"
)
