"""One-off: backfill KXHIGHLAX Predexon orderbook history into
data/predexon/{series}/orderbooks/, from Predexon's own coverage start (~Jan 2026 per
their docs - confirmed empirically too, pre-2026 dates come back empty) through
yesterday. Dates are visited in random order (not chronological) so that if we
get cut off by Predexon's rate limit/quota partway through, what's already
cached is a random cross-section of the window rather than just the first N
days. Safe to re-run - already-cached event tickers are skipped, so a run that
gets cut off can just be re-invoked later to keep filling in the sample.
"""
import datetime
import os
import random

import api_functions
import predexon_functions as pf

COVERAGE_START = datetime.date(2026, 1, 1)
yesterday = datetime.date.today() - datetime.timedelta(days=1)  # exclude today (event still open)
N_DAYS = (yesterday - COVERAGE_START).days + 1
dates = [COVERAGE_START + datetime.timedelta(days=i) for i in range(N_DAYS)]
random.shuffle(dates)

MAX_CONSECUTIVE_ERRORS = 5  # give up if pulls keep failing back-to-back, not just once

n_skipped = n_fetched = n_empty = n_errors = 0
consecutive_errors = 0

for i, d in enumerate(dates):
    et = api_functions.event_ticker_from_date(d)
    out_path = os.path.join(pf.predexon_orderbooks_dir(et), f"{et}.parquet")
    if os.path.exists(out_path):
        n_skipped += 1
        continue

    try:
        df = pf.fetch_event_orderbooks(et)
        if df.empty:
            n_empty += 1
        else:
            n_fetched += 1
        consecutive_errors = 0
    except pf.PredexonQuotaExceeded as e:
        print(f"\n[STOP] Hit quota/rate limit at {et} ({i+1}/{N_DAYS} dates attempted): {e}")
        break
    except Exception as e:
        print(f"  [warn] {et}: {e}")
        n_errors += 1
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[STOP] {consecutive_errors} consecutive failures - giving up "
                  f"({i+1}/{N_DAYS} dates attempted).")
            break

print(
    f"\nDone (or stopped). fetched={n_fetched}  already_cached_skipped={n_skipped}  "
    f"empty(no markets)={n_empty}  errors={n_errors}  "
    f"total_cached_now={n_skipped + n_fetched}/{N_DAYS}"
)
