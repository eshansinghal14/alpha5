"""One-off repair pass, run after the initial pipeline (run_full_pipeline.py)
completed: 429 rate-limiting during the Kalshi 1-min backup fetch (both
scratch_backfill_kalshi_backup.py and predexon_functions.load_kalshi_backup_
long_df's live-fallback path -- neither has real 429 backoff, see
api_functions.get_candlesticks / kalshi_get) left many event-level backup
parquets missing one or more individual tickers' candles. Because both of
those code paths key off event-level file existence (not per-ticker
completeness), those gaps are permanent until repaired directly here.

Step 1: for every event with cached Predexon raw ticks, diff Predexon's
ticker set against the cached Kalshi backup's ticker set and re-fetch
whatever's missing, this time with real Retry-After-aware backoff.

Step 2: any event whose backup file actually changed had its implied spot/
vol cache (data/implied_ts/) originally computed against the incomplete
backup -- delete that event's cached implied_ts files (all freqs) so
generate_impl_spot_vol_cache.py recomputes them against the repaired data on
its next run (invoked automatically at the end of this script).

Safe to re-run: an event with nothing missing in step 1 is a no-op, and step
2 only deletes+invalidates cache entries for events step 1 actually touched.
"""
import os
import subprocess
import sys
import time

import pandas as pd
import requests

from kalshi_infra import api_functions as af
from kalshi_infra import black_scholes as bs
from kalshi_infra import predexon_functions as pf
FREQS = ["1s", "10s", "1min", "10min"]
MAX_RETRIES = 5
cols = ["ticker", "time", "bid", "ask"]


def get_candlesticks_with_retry(ticker, start_ts, end_ts, period_interval=1):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return af.get_candlesticks(ticker, start_ts, end_ts, period_interval=period_interval)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 and attempt < MAX_RETRIES:
                retry_after = float(e.response.headers.get("Retry-After", 2 ** attempt))
                print(f"    [429] {ticker} - retrying in {retry_after:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})", flush=True)
                time.sleep(retry_after)
                continue
            raise


event_tickers = pf.all_cached_predexon_event_tickers()
print(f"{len(event_tickers)} Predexon-cached events to check.", flush=True)

n_clean = n_repaired = n_errors = 0
repaired_events = []

for et in event_tickers:
    raw = pd.read_parquet(os.path.join(pf.predexon_orderbooks_dir(et), f"{et}.parquet"))
    if raw.empty:
        continue
    predexon_tickers = set(raw["ticker"].unique())

    backup_dir = pf.kalshi_1min_historical_dir(et)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"{et}.parquet")
    backup = pd.read_parquet(backup_path) if os.path.exists(backup_path) else pd.DataFrame(columns=cols)
    backup_tickers = set(backup["ticker"].unique()) if not backup.empty else set()

    missing = sorted(predexon_tickers - backup_tickers)
    if not missing:
        n_clean += 1
        continue

    print(f"{et}: missing {len(missing)}/{len(predexon_tickers)} tickers -> {missing}", flush=True)
    new_frames = []
    for ticker in missing:
        sub = raw.loc[raw["ticker"] == ticker]
        start_ts = int(sub["time"].min().timestamp()) - 60
        end_ts = int(sub["time"].max().timestamp()) + 60
        try:
            candles = get_candlesticks_with_retry(ticker, start_ts, end_ts, period_interval=1)
        except Exception as e:
            print(f"  [warn] {ticker}: {e}", flush=True)
            n_errors += 1
            continue
        time.sleep(0.3)
        if candles.empty:
            continue
        candles = candles[["time", "bid", "ask"]].copy()
        candles["ticker"] = ticker
        new_frames.append(candles)

    if new_frames:
        combined = pd.concat([backup] + new_frames, ignore_index=True)
        combined = combined.sort_values(["ticker", "time"]).reset_index(drop=True)[cols]
        combined.to_parquet(backup_path, index=False)
        n_repaired += 1
        repaired_events.append(et)

print(
    f"\nStep 1 done. clean={n_clean}  repaired={n_repaired}  errors={n_errors}  total={len(event_tickers)}",
    flush=True,
)

n_invalidated = 0
for et in repaired_events:
    for freq in FREQS:
        path = bs.implied_spot_cache_path(et, freq=freq)
        if os.path.exists(path):
            os.remove(path)
            n_invalidated += 1

print(
    f"Invalidated {n_invalidated} implied_ts cache files across {len(repaired_events)} repaired events.",
    flush=True,
)

print("\nRe-running generate_impl_spot_vol_cache.py to recompute invalidated entries...", flush=True)
result = subprocess.run(
    [sys.executable, "-m", "kalshi_infra.generate_impl_spot_vol_cache"],
    cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
sys.exit(result.returncode)
