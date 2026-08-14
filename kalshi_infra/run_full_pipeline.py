"""Orchestrator: chains the three backfill/compute stages so the whole
pipeline can run unattended from wherever it's currently at.

  1. scratch_backfill_year.py   - Predexon sub-cent orderbook history
  2. scratch_backfill_kalshi_backup.py - Kalshi 1-min candle backup (only
     needs Kalshi's own API, so only depends on stage 1 having *some*
     events cached, not all of them -- but is re-run per retry loop below so
     it keeps picking up newly-arrived stage-1 events too)
  3. generate_impl_spot_vol_cache.py - implied spot/sigma cache at 1s/10s/1min

Each of stages 1-2 is a one-off script that intentionally stops itself early
on real trouble (Predexon quota/rate-limit exhaustion, or a run of
consecutive errors -- e.g. a transient network/DNS blip). Both are documented
as safe to re-run (they skip whatever's already cached on disk), so this
wrapper just re-invokes a stage, with backoff, as long as that stage's own
stdout shows it stopped early (contains "[STOP]") rather than having run to
the end of its date/event list naturally. This is how a transient blip (DNS
failure, single 429) gets retried automatically instead of requiring someone
to notice and re-launch it by hand.

Does NOT loop forever: caps retries per stage. If Predexon's quota is a
longer-lived (e.g. daily) cap rather than a transient one, this will exhaust
its retry budget within the backoff schedule and move on with whatever
fraction got cached -- re-run this script later to pick up the rest.
"""
import subprocess
import sys
import time

MAX_RETRIES = 12
BACKOFF_SCHEDULE = [30, 60, 120, 180, 300]  # seconds; last value repeats


def backoff(attempt: int) -> int:
    return BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]


def run_stage_with_retries(script: str, max_retries: int = MAX_RETRIES) -> bool:
    """Returns True if the stage completed a full pass without hitting [STOP]."""
    for attempt in range(max_retries):
        print(f"\n===== [{script}] attempt {attempt + 1}/{max_retries} =====", flush=True)
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        stopped_early = "[STOP]" in proc.stdout or proc.returncode != 0
        if not stopped_early:
            print(f"===== [{script}] completed a full pass, moving on =====", flush=True)
            return True
        wait = backoff(attempt)
        print(f"===== [{script}] stopped early (attempt {attempt + 1}/{max_retries}), "
              f"retrying in {wait}s =====", flush=True)
        time.sleep(wait)
    print(f"===== [{script}] exhausted retry budget, moving on with partial data =====", flush=True)
    return False


print("########## STAGE 1: Predexon orderbook backfill ##########", flush=True)
run_stage_with_retries("scratch_backfill_year.py")

print("\n########## STAGE 2: Kalshi 1-min candle backup backfill ##########", flush=True)
run_stage_with_retries("scratch_backfill_kalshi_backup.py")

print("\n########## STAGE 3: implied spot/vol cache generation ##########", flush=True)
run_stage_with_retries("generate_impl_spot_vol_cache.py")

print("\n########## PIPELINE COMPLETE ##########", flush=True)
