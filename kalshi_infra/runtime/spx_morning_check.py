"""
Scheduled health checks of the S&P shadow session while nobody is watching:
at each time in --at (ET), run a headless opencode agent that reviews the
running KXINXU / KXINXDUD session and appends a short report to
data/live/KXINX/check_<date>.md. Read-only: the agent never edits code,
config or control files and never touches processes.

    python -m kalshi_infra.runtime.spx_morning_check --at 09:20 10:00 11:30 13:30 15:30
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import time

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

OPENCODE = os.path.expandvars(r"%APPDATA%\npm\node_modules\opencode-ai\bin\opencode.exe")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = "America/New_York"
LOG = os.path.join(LIVE_ROOT, "spx_check.log")

PROMPT = """READ-ONLY health check of the live S&P SHADOW session (no real orders) on this machine, at {now} ET.
Series KXINXU, KXINXDUD run in one process (write group spx-daily) with the shared pricing hub
(kalshi_strategies/KXINX/hub.py). Do NOT edit any file except the report below, do NOT touch config.json,
trading_mode.json, STOP.json or any process, do not place or cancel orders, and do not start long jobs
(nothing over ~2 minutes; read journals with a column subset and a time filter).

Data: data/live/<SERIES>/{date}/summary.json (current state), data/live/<SERIES>/{date}/journal/
(decisions, bbo, fills parquet parts), processes via Get-CimInstance Win32_Process.
Earlier checks today are in {report} -- read it first and focus on what changed since.

Check and report:
1. Running: launcher, hub, ONE process for the three series; summaries updating (last_update < 15 s old);
   IB Gateway port 4001 open; extras.feed_problem; signal_age_s vs risk.max_signal_age_s; share of
   decisions blocked stale_signal since the last check.
2. Shared write bucket: summary quotes.write_group (members, reserve), live quote counts by series,
   counts of create_deferred:reserve -- quotes must actually be resting now (the 2026-10-13 bug was zero quotes).
3. Fair vs Kalshi since the last check, per series: median (fair - mid), share of time inside the touch,
   split by spread width (1-2c vs 3c+). Flag KXINXDUD separately (it was 4-11c off Kalshi on 10-13 while
   matching KXINXU's ladder; compare it to KXINXU's interpolated levels again).
4. Shadow fills: count, maker/taker, 10s/60s markout vs Kalshi mid, positions vs max_position,
   extras.combined_risk (worst_usd vs limit).
5. Errors: Tracebacks / WARN / reconnect lines in the run output if you can find it, noise_dropped,
   hub line count (extras.hub_lines, must stay < 100).

Append to {report} (create it if missing) a section exactly like:

## {now} ET
- Status: OK / NEEDS ATTENTION -- one line
- Pricing: ...
- Quoting: ...
- Fair vs Kalshi: ...
- Fills/risk: ...
- Changes needed: numbered list of concrete code/config changes with file:line, or "none".

Keep it under 25 lines. Be specific and numeric; say "unknown" rather than guess.
"""


def log(msg: str):
    line = f"{pd.Timestamp.now(tz='UTC').isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run_check(day: datetime.date, now_et: pd.Timestamp):
    report = os.path.join(LIVE_ROOT, "KXINX", f"check_{day.isoformat()}.md")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    prompt = PROMPT.format(now=now_et.strftime("%H:%M"), date=day.isoformat(), report=report)
    log(f"check {now_et:%H:%M} ET -> {report}")
    try:
        r = subprocess.run([OPENCODE, "run", "--title", f"spx check {now_et:%H:%M}", prompt], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
        log(f"exit {r.returncode}")
        if r.returncode != 0:
            log(r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        log("agent timed out")
        with open(report, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {now_et:%H:%M} ET\n- NOT CHECKED: agent timed out after 20 min.\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", nargs="+", required=True, help="HH:MM ET check times, today.")
    args = ap.parse_args(argv)
    today = pd.Timestamp.now(tz=ET).date()
    times = sorted(pd.Timestamp(datetime.datetime.combine(today, datetime.time(*map(int, t.split(":")))), tz=ET)
                   for t in args.at)
    log(f"scheduled {', '.join(t.strftime('%H:%M') for t in times)} ET on {today}")
    for t in times:
        wait = (t - pd.Timestamp.now(tz=ET)).total_seconds()
        if wait < -600:
            log(f"skipping {t:%H:%M} ET (already past)")
            continue
        if wait > 0:
            time.sleep(wait)
        run_check(today, pd.Timestamp.now(tz=ET))
    log("done")


if __name__ == "__main__":
    main()
