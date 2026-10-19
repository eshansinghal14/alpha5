"""
Every 15 min, after each crypto event settles, run a headless opencode agent
that reviews that event's real fills and appends the bad trades to
data/live/{series}/trade_journal.md on this machine.

Trading runs on EC2, so the fills and journal live there: the fill table is
pulled over SSH before the agent starts, and the agent reads more context
over SSH. Runs here because opencode is only signed in on this machine.

    python -m kalshi_infra.runtime.review_agent_loop
"""
from __future__ import annotations

import os
import subprocess
import time
import urllib.request

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

SERIES = "KXCRYPTOLEAD15M"
OPENCODE = os.path.expandvars(r"%APPDATA%\npm\node_modules\opencode-ai\bin\opencode.exe")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOURNAL = os.path.join(LIVE_ROOT, SERIES, "trade_journal.md")
FILLS_DIR = os.path.join(LIVE_ROOT, SERIES, "review_fills")
LOG = os.path.join(LIVE_ROOT, "review_agent.log")
# HYPE 0345 was finalized ~3 min after its 07:45Z close (2026-10-11); 4 min gives margin.
SETTLE_WAIT_S = 240

EC2_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "alpha5-live-key.pem")
# us-east-2 since 2026-10-19, next to Kalshi's API (external-api.kalshi.com is
# an us-east-2 ELB): order RTT p50 19ms -> 8ms vs us-east-1.
EC2_REGION, EC2_SG, EC2_NAME = "us-east-2", "sg-0216e64cd58ee302c", "alpha5-live"
# Fallback only: the box has no Elastic IP, so its address changes on every stop/start.
EC2_IP_FALLBACK = "3.131.100.179"


def ec2_host() -> str:
    """ubuntu@<current public IP> of the live box, looked up by Name tag."""
    try:
        r = subprocess.run(["aws", "ec2", "describe-instances", "--region", EC2_REGION,
                            "--filters", f"Name=tag:Name,Values={EC2_NAME}", "Name=instance-state-name,Values=running",
                            "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text"],
                           capture_output=True, text=True, timeout=60)
        ip = r.stdout.strip()
        if r.returncode == 0 and ip and ip != "None":
            return f"ubuntu@{ip}"
    except Exception:
        pass
    return f"ubuntu@{EC2_IP_FALLBACK}"


def ssh_cmd() -> list[str]:
    return ["ssh", "-i", EC2_KEY, "-o", "ConnectTimeout=30", "-o", "ConnectionAttempts=3",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", ec2_host()]
# Every python the review runs on the live box goes through this 1 GiB memory
# cap (deploy/review_py.sh), so a heavy query can't take trading down with it.
EC2_PY = "/opt/alpha5/deploy/review_py.sh"

PROMPT = """You are reviewing ONE just-finished Kalshi {series} event: {event}. READ-ONLY job.
Do NOT edit code, do NOT touch STOP.json, trading_mode.json, or any process (here or on EC2). Do not place or cancel orders.

Live trading runs on an EC2 box, not this machine; this machine's data/live has no live fills.
1. The event's fill table, already pulled from EC2, is in {fills_file}.
   Every real fill with model prob, book at fill, mid 10s later (markout), time-to-close (x_tau), settlement result, settled pnl, and auto-flags.
2. For more context, run commands on EC2 over SSH (read-only), e.g.
     {ssh} "{ec2_py} -m kalshi_infra.runtime.trade_review --series {series} --event {event}"
     {ssh} "grep -v '^\\[record\\]' /opt/alpha5/data/live/run.out.log | tail -n 200"
   The decision/bbo journal is under /opt/alpha5/data/live/{series}/<date>/journal/ on EC2
   (kalshi_infra.runtime.journal.read_journal). Write any helper script to a temp dir, scp it over, and run it with {ec2_py}.
   MEMORY: live trading shares EC2's 3.8 GB. ALWAYS run python there through {ec2_py} (it is capped at 1 GB and
   killed past that; never call .venv/bin/python directly). A full day of decisions is ~2.5 GB, so ALWAYS pass
   read_journal(..., ticker_prefix="{event}", columns=[only what you need]).
3. Judge each fill / burst of fills manually. Only call a trade BAD when it is definitively a bad decision
   (e.g. traded on stale fair after a spot jump, quote left resting through an obvious move, repeated takes into a
   running market, size far above normal, trading against own model, fees > edge). A loser that was +EV at the time is NOT bad.
4. Append (do not overwrite) to {journal} a section exactly like:

## {event}  (close {close} UTC)
- Fills: N real (taker/maker), settled PnL $X
- Bad trades:
  - HH:MM:SS ticker side qty@price -- what happened and why it was a bad decision (1-2 sentences)
- Pattern: one line on the root cause, or "none" if no bad trades.

Keep it short. If there were no real fills, append just the header and "- No real fills.".
"""

# Appended when the fill table couldn't be pulled, so the morning log shows the gap.
UNREACHABLE = "## {event}  (close {close} UTC)\n- NOT REVIEWED: could not read fills from EC2 ({why}).\n\n"


def log(msg: str):
    line = f"{pd.Timestamp.now(tz='UTC').isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def event_for(close_utc: pd.Timestamp) -> str:
    # Tickers are stamped in New York time: close 08:00Z -> ...-26OCT110400.
    et = close_utc.tz_convert("America/New_York")
    return f"{SERIES}-{et.strftime('%y%b%d%H%M').upper()}"


def already_done(event: str) -> bool:
    return os.path.exists(JOURNAL) and f"## {event} " in open(JOURNAL, encoding="utf-8").read()


def allow_my_ip():
    """This machine's IP keeps changing overnight, and the security group only
    admits listed IPs, so SSH silently times out until it's added."""
    try:
        ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
        r = subprocess.run(["aws", "ec2", "authorize-security-group-ingress", "--region", EC2_REGION,
                            "--group-id", EC2_SG, "--protocol", "tcp", "--port", "22", "--cidr", f"{ip}/32"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log(f"added SSH rule for new IP {ip}")
        elif "Duplicate" not in r.stderr:
            log(f"could not add SSH rule for {ip}: {r.stderr.strip()[-300:]}")
    except Exception as e:
        log(f"IP check failed: {e!r}")


def pull_fills(event: str) -> tuple[str | None, str]:
    """Fill table for `event` from EC2, saved locally. (path, "") or (None, why)."""
    allow_my_ip()
    cmd = f"{EC2_PY} -m kalshi_infra.runtime.trade_review --series {SERIES} --event {event}"
    for attempt in range(3):
        try:
            r = subprocess.run(ssh_cmd() + [cmd], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300)
        except subprocess.TimeoutExpired:
            why = "ssh timed out"
        else:
            if r.returncode == 0:
                os.makedirs(FILLS_DIR, exist_ok=True)
                path = os.path.join(FILLS_DIR, f"{event}.txt")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(r.stdout)
                return path, ""
            why = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:200]
        log(f"{event}: pull attempt {attempt + 1} failed: {why}")
        time.sleep(30)
    return None, why


def review(close_utc: pd.Timestamp):
    event = event_for(close_utc)
    if already_done(event):
        return
    close = close_utc.strftime("%H:%M")
    fills_file, why = pull_fills(event)
    if fills_file is None:
        with open(JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(UNREACHABLE.format(event=event, close=close, why=why))
        return
    ssh = " ".join(f'"{a}"' if " " in a else a for a in ssh_cmd())
    prompt = PROMPT.format(series=SERIES, event=event, journal=JOURNAL, close=close,
                           fills_file=fills_file, ssh=ssh, ec2_py=EC2_PY)
    log(f"reviewing {event}")
    try:
        r = subprocess.run([OPENCODE, "run", "--title", f"review {event}", prompt], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=780)
        log(f"{event}: exit {r.returncode}")
        if r.returncode != 0:
            log(r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        log(f"{event}: agent timed out")
    if not already_done(event):
        log(f"{event}: agent did not write a section")


def main():
    log("started")
    while True:
        now = pd.Timestamp.now(tz="UTC")
        last_close = now.floor("15min")
        if (now - last_close).total_seconds() >= SETTLE_WAIT_S:
            review(last_close)
            nxt = last_close + pd.Timedelta(minutes=15, seconds=SETTLE_WAIT_S)
        else:
            nxt = last_close + pd.Timedelta(seconds=SETTLE_WAIT_S)
        time.sleep(max(5.0, (nxt - pd.Timestamp.now(tz="UTC")).total_seconds()))


if __name__ == "__main__":
    main()
