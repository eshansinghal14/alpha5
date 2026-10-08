"""
upload_completed_days_to_s3.py -- manual, run-by-hand upload of local raw
orderbook/trade parquet parts to S3, for events whose trading day has
already finished (i.e. their event_ticker is no longer in Kalshi's
currently-open-markets list).

The live collector now also runs this same disk-vs-open-list sweep on its
own at every startup (see run() in ws_raw_ob_collector.py), so this script
is mostly a manual/on-demand backstop these days -- e.g. running it from a
laptop, or between collector restarts without waiting for the next one.

Idempotent: skips any event already present in S3, so it's safe to re-run
whenever you want to push newly-completed days up (or sweep for orphans).

Usage:
    python upload_completed_days_to_s3.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ws_raw_ob_collector import Collector, _get_open_markets, _local_events_with_data, _setup_logging, logger


def main():
    _setup_logging()
    collector = Collector()

    open_events = {m["event_ticker"] for m in _get_open_markets()}

    uploaded = skipped_open = skipped_existing = 0
    for kind in ("raw_ob_ticks", "trades"):
        candidates = _local_events_with_data(kind)
        logger.info(f"[{kind}] found {len(candidates)} event(s) with local parts; {len(open_events)} event(s) currently open.")
        for event_ticker in candidates:
            if event_ticker in open_events:
                skipped_open += 1
                continue
            if collector.finalize_event_to_s3(event_ticker, kind=kind, skip_if_exists=True, delete_local_after_upload=True):
                uploaded += 1
            else:
                skipped_existing += 1

    logger.info(
        f"Done. Uploaded {uploaded}, skipped {skipped_open} still-open, "
        f"skipped {skipped_existing} already in S3."
    )


if __name__ == "__main__":
    main()
