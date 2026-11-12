#!/bin/bash
# Scheduled Claude review of live trading, emailed as HTML with charts through
# SES (kalshi_infra.runtime.report_email); plain text through SNS if that fails.
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
EMAIL=$(cat /etc/alpha5/report-email 2>/dev/null)
mkdir -p "$OUT"
cd "$REPO"

# One review at a time: two Claude runs plus their python would crowd trading.
exec 9>"$STATE/run.lock"
if [ "$MODE" = hourly ]; then
  flock -n 9 || { echo "another review is running; skipped"; exit 0; }
  HOURS=1 CHART_HOURS=6 MODEL=sonnet TURNS=15 LIMIT=12m
else
  flock -w 900 9 || { echo "lock wait timed out"; exit 1; }
  HOURS=24 CHART_HOURS=24 MODEL=opus TURNS=60 LIMIT=32m
fi

send() {  # subject, body file
  local subj
  subj=$(printf '%s' "$1" | tr -cd '[:print:]' | cut -c1-99)
  aws sns publish --region "$REGION" --topic-arn "$TOPIC" --subject "$subj" \
      --message "file://$2" --output text >/dev/null
}

send_html() {  # subject, Markdown body file, [proposals json]; falls back to plain text
  if [ -n "$EMAIL" ] && $REPO/.venv/bin/python -W ignore -m kalshi_infra.runtime.report_email \
      --subject "$1" --body "$2" --hours "$CHART_HOURS" --status "$STATUS" --sender "$EMAIL" --to "$EMAIL" \
      ${3:+--proposals "$3"} 2> "$OUT/email.err"; then
    return 0
  fi
  send "$1" "$2"
}

$REPO/.venv/bin/python -W ignore -m kalshi_infra.runtime.trading_digest --hours "$HOURS" \
    --log-state "$OUT/logpos.json" > "$OUT/digest.txt" 2> "$OUT/digest.err"

{
  cat deploy/claude_report/common.md deploy/claude_report/$MODE.md
  if [ "$MODE" = hourly ]; then
    echo; echo "Earlier alerts:"; tail -n 10 "$OUT/alerts.log" 2>/dev/null || echo "(none)"
  fi
} > "$OUT/prompt.txt"

# Daily: the strategies' handoffs, live configs and recent commits go in ahead of the
# digest so suggestions build on what was already tried, and the last few reports
# after it so ideas aren't repeated. Stdin, not the prompt argument: the handoffs
# alone are ~80 KB, near Linux's 128 KB cap on a single argument.
{
  if [ "$MODE" = daily ]; then
    for s in "ARBS:kalshi_strategies/ARBS" "KXCRYPTOLEAD15M (coin race):kalshi_strategies/KXCRYPTOLEAD15M"; do
      name=${s%%:*} dir=${s#*:}
      echo "===== $name: HANDOFF ====="
      cat "$dir/HANDOFF.md" "$dir/research/HANDOFF.md" 2>/dev/null
      echo; echo "===== $name: live config.json ====="; cat "$dir/config.json"
      echo; echo "===== $name: commits, last 14 days ====="
      git -c safe.directory=$REPO log --since=14.days --format='%h %ad %s' --date=short -- "$dir" | head -40
      echo
    done
  fi
  echo "===== DIGEST ====="; cat "$OUT/digest.txt"
  if [ "$MODE" = daily ]; then
    for f in $(ls -t "$OUT"/reply-*.txt 2>/dev/null | head -3); do
      echo; echo "===== earlier report $(basename "$f" .txt) ====="; cat "$f"
    done
    echo; echo "===== owner's decisions on earlier ideas (replies to the email; newest last) ====="
    tail -n 40 /var/lib/alpha5-replies/decisions.jsonl 2>/dev/null || echo "(none yet)"
  fi
} > "$OUT/stdin.txt"

# The OS user is the real boundary (no secrets, no write access to anything
# trading reads); this list just keeps Claude to reading.
TOOLS=(Read Grep Glob "Bash(/opt/alpha5/.venv/bin/python:*)" "Bash(.venv/bin/python:*)" "Bash(cd:*)"
       "Bash(grep:*)" "Bash(tail:*)" "Bash(head:*)" "Bash(cut:*)" "Bash(sort:*)" "Bash(uniq:*)" "Bash(echo:*)"
       "Bash(ls:*)" "Bash(wc:*)" "Bash(cat:*)" "Bash(date:*)" "Bash(journalctl:*)" "Bash(systemctl status:*)"
       "Bash(systemctl show:*)" "Bash(git -c safe.directory=/opt/alpha5 log:*)"
       "Bash(git -c safe.directory=/opt/alpha5 show:*)" "Bash(git -c safe.directory=/opt/alpha5 diff:*)"
       "Bash(free:*)" "Bash(df:*)")
CLAUDE_CODE_OAUTH_TOKEN=$(tr -d '\r\n ' < /etc/alpha5/claude-token) timeout "$LIMIT" \
  "$HOME/.local/bin/claude" -p "$(cat "$OUT/prompt.txt")" --model "$MODEL" --max-turns "$TURNS" \
    --allowedTools "${TOOLS[@]}" --disallowedTools Write Edit NotebookEdit WebFetch WebSearch \
    < "$OUT/stdin.txt" > "$OUT/reply.txt" 2> "$OUT/claude.err"
rc=$?

# Searched for, not read off line 1: Claude sometimes writes a sentence first (11-09).
SL=$(grep -n -m1 '^STATUS:' "$OUT/reply.txt" | cut -d: -f1)
SL=${SL:-1}
STATUS=$(sed -n "${SL}s/^STATUS: *//p" "$OUT/reply.txt" | tr -d '\r ')
SUBJECT=$(sed -n "$((SL + 1))s/^SUBJECT: *//p" "$OUT/reply.txt" | tr -d '\r')
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
tail -n +$((SL + 3)) "$OUT/reply.txt" > "$OUT/body.txt"
cp "$OUT/reply.txt" "$OUT/reply-$(date -u +%Y%m%dT%H%M).txt"
find "$OUT" -name 'reply-*.txt' -mtime +14 -delete

if [ "$MODE" = daily ]; then
  tag=$([ "$STATUS" = ALERT ] && echo "[alpha5 ALERT]" || echo "[alpha5]")
  # The ```proposals block becomes /var/lib/alpha5-proposals/<day>.json, which
  # owner replies approve (report_replies poll); it isn't shown in the email.
  PROPS=/var/lib/alpha5-proposals/$DAY.json
  $REPO/.venv/bin/python -W ignore -m kalshi_infra.runtime.report_replies extract --reply "$OUT/body.txt" \
      --out "$OUT/body.txt" --day "$DAY" > "$OUT/proposals.log" 2>&1 || PROPS=
  send_html "$tag Daily $DAY: $SUBJECT" "$OUT/body.txt" "$PROPS"
elif [ "$STATUS" = ALERT ]; then
  echo "$NOW $SUBJECT" >> "$OUT/alerts.log"
  send_html "[alpha5 ALERT] $SUBJECT" "$OUT/body.txt"
fi
echo "$MODE review done: $STATUS $SUBJECT"
