#!/usr/bin/env bash
# Hot handoff deploy: asks the running alpha5-arbs parent to move each ARBS
# series onto a fresh process running the code now on disk, handing over its
# resting quotes and positions instead of cancelling them (infra/runtime/handoff.py).
#
#   deploy/arbs_handoff.sh                 # every series
#   deploy/arbs_handoff.sh ARBS3 ARBSPM    # just these
#
# Copy the new files to the box first; this script only triggers and watches.
# A change to strategies/run.py itself, to infra/runtime/handoff.py's Driver,
# or to the systemd unit still needs a full `systemctl restart alpha5-arbs`:
# the parent process keeps running its old code.
set -euo pipefail
cd /opt/alpha5
PY=.venv/bin/python
ROOT="$($PY -c 'from infra.runtime.persist import LIVE_ROOT; print(LIVE_ROOT)')"
ID="$(date -u +%Y%m%dT%H%M%SZ)"
SERIES="${*:-all}"

$PY - "$ROOT" "$ID" "$SERIES" <<'EOF'
import sys, time
from infra.config import atomic_write_json
root, hid, series = sys.argv[1:]
atomic_write_json(f"{root}/HANDOFF.json",
                  {"id": hid, "series": "all" if series == "all" else series.split(), "at": time.time()})
EOF
echo "handoff $ID requested for: $SERIES"

# Up to ~10 min warm-up per series, one series at a time.
deadline=$(( $(date +%s) + 3600 ))
last=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 2
  out="$($PY - "$ROOT" "$ID" <<'EOF'
import json, sys
root, hid = sys.argv[1:]
try:
    s = json.load(open(f"{root}/HANDOFF.status.json"))
except (OSError, ValueError):
    print("waiting")
    sys.exit()
if s.get("id") != hid:
    print("waiting")
    sys.exit()
lines = [f"{n}: {v.get('result') or v.get('phase')}" for n, v in (s.get("series") or {}).items()]
print(s.get("state", "?") + ("; " + s["error"] if s.get("error") else ""))
print("\n".join(lines))
EOF
)"
  if [ "$out" != "$last" ]; then
    echo "---- $(date -u +%H:%M:%S) UTC"
    echo "$out"
    last="$out"
  fi
  case "$(head -n1 <<<"$out")" in
    done*) exit 0 ;;
    failed*) exit 1 ;;
  esac
done
echo "timed out waiting for handoff $ID" >&2
exit 2
