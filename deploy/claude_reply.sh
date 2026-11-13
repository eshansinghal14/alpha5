#!/bin/bash
# Claude's reading of one owner reply to the daily review: prompt on stdin,
# JSON schema as $1, Claude Code's --output-format json result on stdout.
# Run by infra.runtime.report_replies (as ubuntu) through sudo -u alpha5-report,
# which holds the Claude token. No tools: it only classifies the text.
set -uo pipefail
export HOME=/var/lib/alpha5-report
cd "$HOME"
CLAUDE_CODE_OAUTH_TOKEN=$(tr -d '\r\n ' < /etc/alpha5/claude-token) timeout 180 \
  "$HOME/.local/bin/claude" -p "Follow the instructions on stdin." --model sonnet --tools "" \
    --output-format json --json-schema "${1:?schema}"
