#!/bin/bash
# Scheduled Claude review of live trading, emailed through SNS.
#   daily:  8am ET report, always emailed (alpha5-report-daily.timer)
#   hourly: health check, emailed only on STATUS: ALERT (alpha5-report-hourly.timer)
# Runs as alpha5-report (deploy/setup_report.sh): it can read the repo and data/
# but not secrets/ (no Kalshi key) and can write nothing trading reads.
# Claude Code is signed in to the owner's account by a token in
# /etc/alpha5/claude-token (claude setup-token), not an API key.
set -uo pipefail
MODE=${1:?daily|hourly}
REPO=/opt/alpha5
STATE=/var/lib/alpha5-report
OUT=$STATE/$MODE
REGION=us-east-2
TOPIC=$(cat /etc/alpha5/report-topic-arn)
mkdir -p "$OUT"
cd "$REPO"

# One review at a time: two Claude runs plus their python would crowd trading.
exec 9>"$STATE/run.lock"
if [ "$MODE" = hourly ]; then
  flock -n 9 || { echo "another review is running; skipped"; exit 0; }
  HOURS=1 MODEL=sonnet TURNS=15 LIMIT=12m
else
  flock -w 900 9 || { echo "lock wait timed out"; exit 1; }
  HOURS=24 MODEL=opus TURNS=40 LIMIT=25m
fi

send() {  # subject, body file
  local subj
  subj=$(printf '%s' "$1" | tr -cd '[:print:]' | cut -c1-99)
  aws sns publish --region "$REGION" --topic-arn "$TOPIC" --subject "$subj" \
      --message "file://$2" --output text >/dev/null
}

$REPO/.venv/bin/python -W ignore -m kalshi_infra.runtime.trading_digest --hours "$HOURS" \
    --log-state "$OUT/logpos.json" > "$OUT/digest.txt" 2> "$OUT/digest.err"

{
  cat deploy/claude_report/common.md deploy/claude_report/$MODE.md
  if [ "$MODE" = hourly ]; then
    echo; echo "Earlier alerts:"; tail -n 10 "$OUT/alerts.log" 2>/dev/null || echo "(none)"
  fi
} > "$OUT/prompt.txt"

TOOLS=(Read Grep Glob "Bash(/opt/alpha5/.venv/bin/python:*)" "Bash(grep:*)" "Bash(tail:*)" "Bash(head:*)"
       "Bash(ls:*)" "Bash(wc:*)" "Bash(cat:*)" "Bash(date:*)" "Bash(journalctl:*)" "Bash(systemctl status:*)"
       "Bash(free:*)" "Bash(df:*)")
CLAUDE_CODE_OAUTH_TOKEN=$(tr -d '\r\n ' < /etc/alpha5/claude-token) timeout "$LIMIT" \
  "$HOME/.local/bin/claude" -p "$(cat "$OUT/prompt.txt")" --model "$MODEL" --max-turns "$TURNS" \
    --allowedTools "${TOOLS[@]}" --disallowedTools Write Edit NotebookEdit WebFetch WebSearch \
    < "$OUT/digest.txt" > "$OUT/reply.txt" 2> "$OUT/claude.err"
rc=$?

STATUS=$(sed -n '1s/^STATUS: *//p' "$OUT/reply.txt" | tr -d '\r ')
SUBJECT=$(sed -n '2s/^SUBJECT: *//p' "$OUT/reply.txt" | tr -d '\r')
DAY=$(TZ=America/New_York date +%m-%d)
NOW=$(TZ=America/New_York date '+%m-%d %H:%M ET')

if [ $rc -ne 0 ] || [ -z "$STATUS" ]; then
  # Claude failed (timeout, usage limit, expired token): send the digest itself.
  fails=$(( $(cat "$OUT/fails" 2>/dev/null || echo 0) + 1 ))
  echo $fails > "$OUT/fails"
  { echo "Claude review failed (exit $rc). Last error output:"; tail -n 20 "$OUT/claude.err"
    tail -n 20 "$OUT/reply.txt"; echo; echo "Raw digest:"; cat "$OUT/digest.txt"; } > "$OUT/body.txt"
  # Hourly: on the 2nd failure in a row, then every 12th, so a dead token is noticed without an hourly flood.
  if [ "$MODE" = daily ] || [ $fails -eq 2 ] || [ $(( fails % 12 )) -eq 2 ]; then
    send "[alpha5] $MODE review FAILED $NOW (digest attached)" "$OUT/body.txt"
  fi
  exit 1
fi
echo 0 > "$OUT/fails"
tail -n +4 "$OUT/reply.txt" > "$OUT/body.txt"
cp "$OUT/reply.txt" "$OUT/reply-$(date -u +%Y%m%dT%H%M).txt"
find "$OUT" -name 'reply-*.txt' -mtime +14 -delete

if [ "$MODE" = daily ]; then
  tag=$([ "$STATUS" = ALERT ] && echo "[alpha5 ALERT]" || echo "[alpha5]")
  send "$tag Daily $DAY: $SUBJECT" "$OUT/body.txt"
elif [ "$STATUS" = ALERT ]; then
  echo "$NOW $SUBJECT" >> "$OUT/alerts.log"
  send "[alpha5 ALERT] $SUBJECT" "$OUT/body.txt"
fi
echo "$MODE review done: $STATUS $SUBJECT"
