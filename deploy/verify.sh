#!/usr/bin/env bash
# Pre-start checks on the server: secrets load, Kalshi auth works, tests pass.
set -euo pipefail
cd /opt/alpha5
PY=.venv/bin/python
$PY - <<'EOF'
from kalshi_infra.kalshi.auth import load_private_key
load_private_key()
print("kalshi private key loads")
from kalshi_infra.kalshi.orders import KalshiOrderClient
print("kalshi authenticated, balance:", KalshiOrderClient.from_account().balance())
EOF
$PY -m pytest kalshi_infra kalshi_strategies kalshi_backtest -q -p no:cacheprovider 2>&1 | tail -3
