#!/bin/bash
# Hourly, between the nightly archives (deploy/archive_s3.sh, KEEP_DAYS 5):
# rotates oversized service logs and, while the disk has under TARGET_GB free,
# ships recorded days to S3 early, oldest first.
# The box writes ~10 GB/day (11-23: race journal 2.9, PM books 1.7, crypto
# recorders 1.5, ARBS files 1.5) to a 58 GB disk, so keeping five days left it
# at recording's 3 GB floor (infra/runtime/disk.py) before most nightly runs.
#   disk_guard.sh logs      as root: systemd opens the services' logs as root
#   disk_guard.sh archive   as ubuntu, like the nightly run
set -uo pipefail
DATA=/opt/alpha5/data
TARGET_GB=${TARGET_GB:-8}
LOG_MAX_MB=${LOG_MAX_MB:-500}
OLD=$DATA/live/log_rotated

free_mb() { df --output=avail -BM "$DATA" | tail -1 | tr -dc 0-9; }

case "${1:-}" in
logs)
  # A traceback flood grew arbs.out.log to 6.7 GB in a day (11-23). Every unit
  # writes its log with append:, so truncating it in place is safe for the writer.
  mkdir -p "$OLD"
  for f in "$DATA"/live/*.log; do
    [ -f "$f" ] || continue
    (( $(stat -c %s "$f") > LOG_MAX_MB * 1000000 )) || continue
    gzip -1 -c "$f" > "$OLD/$(basename "$f").$(date -u +%Y%m%dT%H%M).gz" && truncate -s 0 "$f"
    echo "$f was over $LOG_MAX_MB MB: gzipped to $OLD and emptied"
  done
  for base in $(ls "$OLD" | sed -E 's/\.[0-9T]+\.gz$//' | sort -u); do
    ls -1t "$OLD/$base".*.gz | tail -n +4 | xargs -r rm -f --
  done
  ;;
archive)
  for keep in 4 3 2 1; do
    (( $(free_mb) >= TARGET_GB * 1024 )) && exit 0
    echo "$(free_mb) MB free, under $TARGET_GB GB: shipping days older than $keep to S3"
    KEEP_DAYS=$keep RACE_KEEP_DAYS=$(( keep < 2 ? keep : 2 )) bash /opt/alpha5/deploy/archive_s3.sh
  done
  (( $(free_mb) >= TARGET_GB * 1024 )) || echo "still under $TARGET_GB GB keeping one day: the disk needs to grow" >&2
  ;;
*)
  echo "usage: $0 logs|archive" >&2
  exit 2
  ;;
esac
