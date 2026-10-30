#!/bin/bash
# Moves recorded data older than KEEP_DAYS off the live box to S3, one tar per
# folder and day, keyed by its path under data/: live/KXINXU/2026-10-14/journal.tar,
# kalshi_crypto/books/2026-10-14.tar. The 60 GB disk was 95% full on 2026-10-30
# at ~3.5 GB/day. Only day journals and the crypto recorders move; summaries,
# *_df parquets and perf/ stay for the dashboard.
#
# Restore a day into the same layout, from any machine with access:
#   aws s3 cp s3://alpha5-archive-992536799271/live/KXINXU/2026-10-14/journal.tar - | tar -x -C data
# A day restored on the box is moved again by the next run.
set -uo pipefail
export AWS_CONFIG_FILE=/opt/alpha5/deploy/aws-archive.config
BUCKET=alpha5-archive-992536799271
KEEP_DAYS=${KEEP_DAYS:-5}
cutoff=$(date -u -d "-$KEEP_DAYS days" +%F)
cd /opt/alpha5/data || exit 1

# Deletes the paths only once S3 holds at least their file bytes (a tar is
# never smaller). Each step is checked explicitly: errexit is off inside a
# function called from `||`, so set -e would not stop the rm.
ship() {
  local key=$1; shift
  local want got
  want=$(find "$@" -type f -printf '%s\n' | awk '{s += $1} END {print s + 0}')
  if ! tar -cf - "$@" | aws s3 cp - "s3://$BUCKET/$key" --only-show-errors; then
    echo "$key: upload failed, kept" >&2; return 1
  fi
  if ! got=$(aws s3api head-object --bucket "$BUCKET" --key "$key" --query ContentLength --output text) \
      || (( got < want )); then
    echo "$key: S3 has ${got:-?} bytes < $want on disk, kept" >&2; return 1
  fi
  rm -rf -- "$@"
  echo "$key $((got / 1000000)) MB"
}

fail=0
for j in live/*/20??-??-??/journal; do
  day=$(basename "$(dirname "$j")")
  if [ -d "$j" ] && [[ "$day" < "$cutoff" ]]; then
    ship "$j.tar" "$j" || fail=1
  fi
done
# Recorders keep either a folder per day (books/2026-10-14/) or files with the
# day in the name (prints/BTC_2026-10-14_16.parquet).
for dir in kalshi_crypto/*/; do
  dir=${dir%/}
  for day in $(ls "$dir" | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' | sort -u); do
    [[ "$day" < "$cutoff" ]] || continue
    if [ -d "$dir/$day" ]; then
      ship "$dir/$day.tar" "$dir/$day" || fail=1
    else
      ship "$dir/$day.tar" "$dir"/*"$day"*.parquet || fail=1
    fi
  done
done
df -h / | tail -1
exit $fail
