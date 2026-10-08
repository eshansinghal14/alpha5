"""One-off: for every event we've pulled Predexon orderbook history for
(data/predexon/{series}/orderbooks/*.parquet), also backfill the matching
Kalshi 1-minute-candle backup (data/kalshi_1min_historical/{series}/) so
black_scholes.compute_implied_spot_timeseries has something to fall back to
when Predexon is stale for that day.

Deliberately does NOT go through predexon_functions.load_kalshi_backup_long_df,
since that also calls Predexon's own /kalshi/markets endpoint for open/close
times - which would compete with scratch_backfill_year.py for Predexon's
quota. Instead, each ticker's time window is derived straight from the
already-cached Predexon parquet (min/max observed tick time, padded 60s), so
this script only talks to Kalshi's own API and can safely run concurrently
with the Predexon backfill.

Safe to re-run: skips any event ticker already cached.
"""
import glob
import os
import time

import pandas as pd

from kalshi_infra import api_functions as af
from kalshi_infra import predexon_functions as pf
MAX_CONSECUTIVE_ERRORS = 5

event_tickers = pf.all_cached_predexon_event_tickers()
print(f"{len(event_tickers)} Predexon-cached events found.")

n_skipped = n_fetched = n_empty = n_errors = 0
consecutive_errors = 0
cols = ["ticker", "time", "bid", "ask"]

for i, et in enumerate(event_tickers):
    backup_dir = pf.kalshi_1min_historical_dir(et)
    os.makedirs(backup_dir, exist_ok=True)
    out_path = os.path.join(backup_dir, f"{et}.parquet")
    if os.path.exists(out_path):
        n_skipped += 1
        continue

    try:
        raw = pd.read_parquet(os.path.join(pf.predexon_orderbooks_dir(et), f"{et}.parquet"))
        if raw.empty:
            n_empty += 1
            consecutive_errors = 0
            continue

        frames = []
        for ticker, sub in raw.groupby("ticker"):
            start_ts = int(sub["time"].min().timestamp()) - 60
            end_ts = int(sub["time"].max().timestamp()) + 60
            candles = af.get_candlesticks(ticker, start_ts, end_ts, period_interval=1)
            time.sleep(0.05)
            if candles.empty:
                continue
            candles = candles[["time", "bid", "ask"]].copy()
            candles["ticker"] = ticker
            frames.append(candles)

        df = (
            pd.concat(frames, ignore_index=True).sort_values(["ticker", "time"]).reset_index(drop=True)[cols]
            if frames else pd.DataFrame(columns=cols)
        )
        df.to_parquet(out_path, index=False)
        if df.empty:
            n_empty += 1
        else:
            n_fetched += 1
        consecutive_errors = 0
    except Exception as e:
        print(f"  [warn] {et}: {e}")
        n_errors += 1
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[STOP] {consecutive_errors} consecutive failures - giving up "
                  f"({i+1}/{len(event_tickers)} events attempted).")
            break

print(
    f"\nDone (or stopped). fetched={n_fetched}  already_cached_skipped={n_skipped}  "
    f"empty={n_empty}  errors={n_errors}  total_cached_now={n_skipped + n_fetched}/{len(event_tickers)}"
)
