#!/bin/bash
# Moves recorded data older than KEEP_DAYS off the live box to S3, one tar per
# folder and day, keyed by its path under data/: live/KXINXU/2026-10-14/journal.tar,
# kalshi_crypto/books/2026-10-14.tar. The 60 GB disk was 95% full on 2026-10-30
# at ~3.5 GB/day. Only day journals and the crypto and Polymarket recorders move; summaries,
# *_df parquets and perf/ stay for the dashboard.
#
# Restore a day into the same layout, from any machine with access:
#   aws s3 cp s3://alpha5-archive-992536799271/live/KXINXU/2026-10-14/journal.tar - | tar -x -C data
# A day restored on the box is moved again by the next run (an ARBS journal
# once its .archived marker is deleted).
set -uo pipefail
export AWS_CONFIG_FILE=/opt/alpha5/deploy/aws-archive.config
BUCKET=alpha5-archive-992536799271
KEEP_DAYS=${KEEP_DAYS:-5}
cutoff=$(date -u -d "-$KEEP_DAYS days" +%F)
cd /opt/alpha5/data || exit 1

# Succeeds only once S3 holds at least the paths' file bytes (a tar is never
# smaller). Each step is checked explicitly: errexit is off inside a function
# called from `||`, so set -e would not stop what follows it.
upload() {
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
  echo "$key $((got / 1000000)) MB"
}

ship() {
  upload "$@" || return 1
  shift
  rm -rf -- "$@"
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
# Polymarket US books are ~1.5 GB/day (2026-11-12) and are read off the box,
# so they leave after one day instead of KEEP_DAYS.
pm_cutoff=$(date -u -d "-1 days" +%F)
for dir in polymarket_us/*/; do
  dir=${dir%/}
  for day in $(ls "$dir" | grep -oE '^20[0-9]{2}-[0-9]{2}-[0-9]{2}$'); do
    [[ "$day" < "$pm_cutoff" ]] || continue
    ship "$dir/$day.tar" "$dir/$day" || fail=1
  done
done
# ARBS processes keep a file per day instead of a folder: journal/2026-11-05.jsonl,
# prints/, liquidity/ (plus orders_<day>), fill_context/ -- ~1 GB/day for the two
# Kalshi shards (11-14), never matched above. The dashboard's P&L chart reads
# every journal day (viz/arbs_pnl.py), so a shipped journal stays as its rows
# other than quotes, moves, cancels, pulls and model picks (~1% of it), and an
# .archived marker keeps the next run from overwriting S3's full copy with that.
for j in live/ARBS*/journal/20??-??-??.jsonl; do
  day=$(basename "$j" .jsonl)
  [[ "$day" < "$cutoff" ]] && [ ! -e "$j.archived" ] || continue
  upload "$(dirname "$j")/$day.tar" "$j" || { fail=1; continue; }
  grep -v -E '"kind": "(quote|move|cancel|pull|model_quote)"' "$j" > "$j.slim"
  if (( $? > 1 )); then
    rm -f -- "$j.slim"; echo "$j: slimming failed, kept whole" >&2; fail=1; continue
  fi
  mv -- "$j.slim" "$j" && touch -- "$j.archived" || fail=1
done
for dir in live/ARBS*/prints live/ARBS*/liquidity live/ARBS*/fill_context; do
  [ -d "$dir" ] || continue
  for day in $(ls "$dir" | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' | sort -u); do
    [[ "$day" < "$cutoff" ]] || continue
    ship "$dir/$day.tar" "$dir"/*"$day"* || fail=1
  done
done
df -h / | tail -1
exit $fail
