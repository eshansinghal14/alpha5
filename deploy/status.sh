#!/usr/bin/env bash
# Quick health readout of the live service.
cd /opt/alpha5
for u in alpha5-crypto alpha5-spx alpha5-ibgateway alpha5-dashboard alpha5-perf alpha5-watchdog; do
  printf '%-18s %s\n' "$u" "$(systemctl is-active $u)"
done
printf '%-18s %s\n' "ib api :4001" "$(timeout 2 bash -c '</dev/tcp/127.0.0.1/4001' 2>/dev/null && echo open || echo CLOSED)"
free -m | sed -n '1,2p'
.venv/bin/python - <<'EOF'
import glob, json, os
p = max(glob.glob("data/live/KXCRYPTOLEAD15M/*/summary.json"), key=os.path.getmtime)
s = json.load(open(p))
e = s["extras"]
print(s["last_update"], s["event_ticker"], "live:", s["live_trading"], "signal_age:", round(s.get("signal_age_s") or -1, 3))
print("venues_in_mid:", e.get("venues_in_mid"))
print("venue_process:", e.get("venue_process"))
print("quote blocks:", s.get("quote_block_counts"), "| loop lag:", s.get("timings", {}).get("loop_lag"))
for series in ("KXINXU", "KXINXDUD"):
    ps = glob.glob(f"data/live/{series}/*/summary.json")
    if not ps:
        print(series, "no summary yet")
        continue
    s = json.load(open(max(ps, key=os.path.getmtime)))
    print(series, s.get("last_update"), s.get("event_ticker"), "live:", s.get("live_trading"),
          "signal_age:", round(s.get("signal_age_s") or -1, 3), "feed_problem:", s.get("extras", {}).get("feed_problem"),
          "combined_risk:", s.get("extras", {}).get("combined_risk"))
EOF
top -bn1 | sed -n '3p;8,12p'

.venv/bin/python -c 'import glob,os,json; p=max(glob.glob("data/live/KXCRYPTOLEAD15M/*/summary.json"),key=os.path.getmtime); print("bursts:", json.load(open(p))["extras"]["spot_burst"])'
