"""
upload_completed_days_to_s3.py -- manual, run-by-hand upload of local raw
orderbook parquet files to S3, for events whose trading day has already
finished (i.e. their event_ticker is no longer in Kalshi's
currently-open-markets list).

Idempotent: skips any event already present in S3, so it's safe to re-run
whenever you want to push newly-completed days up.

Usage:
    python upload_completed_days_to_s3.py
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ws_raw_ob_collector import OUT_DIR_ROOT, Collector, _get_open_markets, _setup_logging, logger


def main():
    _setup_logging()
    collector = Collector()

    open_events = {m["event_ticker"] for m in _get_open_markets()}
    local_files = glob.glob(os.path.join(OUT_DIR_ROOT, "*", "raw_ob_ticks", "*.parquet"))
    logger.info(f"Found {len(local_files)} local event file(s); {len(open_events)} event(s) currently open.")

    uploaded = skipped_open = skipped_existing = 0
    for path in local_files:
        event_ticker = os.path.splitext(os.path.basename(path))[0]
        if event_ticker in open_events:
            skipped_open += 1
            continue
        if collector.finalize_event_to_s3(event_ticker, skip_if_exists=True):
            uploaded += 1
        else:
            skipped_existing += 1

    logger.info(
        f"Done. Uploaded {uploaded}, skipped {skipped_open} still-open, "
        f"skipped {skipped_existing} already in S3."
    )


if __name__ == "__main__":
    main()
