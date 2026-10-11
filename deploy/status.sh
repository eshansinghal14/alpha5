#!/usr/bin/env bash
# Quick health readout of the live service.
cd /opt/alpha5
systemctl is-active alpha5-crypto alpha5-dashboard
.venv/bin/python - <<'EOF'
import glob, json, os
p = max(glob.glob("data/live/KXCRYPTOLEAD15M/*/summary.json"), key=os.path.getmtime)
s = json.load(open(p))
e = s["extras"]
print(s["last_update"], s["event_ticker"], "live:", s["live_trading"], "signal_age:", round(s.get("signal_age_s") or -1, 3))
print("venues_in_mid:", e.get("venues_in_mid"))
print("venue_process:", e.get("venue_process"))
print("quote blocks:", s.get("quote_block_counts"), "| loop lag:", s.get("timings", {}).get("loop_lag"))
EOF
top -bn1 | sed -n '3p;8,12p'

.venv/bin/python -c 'import glob,os,json; p=max(glob.glob("data/live/KXCRYPTOLEAD15M/*/summary.json"),key=os.path.getmtime); print("bursts:", json.load(open(p))["extras"]["spot_burst"])'
