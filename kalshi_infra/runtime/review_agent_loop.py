"""
Every 15 min, after each crypto event settles, run a headless opencode agent
that reads that event's real fills and appends a summary of the bad trades to
data/live/{series}/trade_journal.md.

    python -m kalshi_infra.runtime.review_agent_loop
"""
from __future__ import annotations

import os
import subprocess
import time

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

SERIES = "KXCRYPTOLEAD15M"
OPENCODE = os.path.expandvars(r"%APPDATA%\npm\node_modules\opencode-ai\bin\opencode.exe")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOURNAL = os.path.join(LIVE_ROOT, SERIES, "trade_journal.md")
LOG = os.path.join(LIVE_ROOT, "review_agent.log")
# HYPE 0345 was finalized ~3 min after its 07:45Z close (2026-10-11); 4 min gives margin.
SETTLE_WAIT_S = 240

PROMPT = """You are reviewing ONE just-finished Kalshi {series} event: {event}. READ-ONLY job.
Do NOT edit code, do NOT touch data/live/STOP.json, trading_mode.json, or any process. Do not place or cancel orders.

1. Run: python -m kalshi_infra.runtime.trade_review --series {series} --event {event}
   That prints every real fill with model prob, book at fill, mid 10s later (markout), time-to-close (x_tau), settlement result, settled pnl, and auto-flags.
2. If you need more context, read the journal (kalshi_infra.runtime.journal.read_journal for streams bbo/decisions) around specific fills.
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


def review(close_utc: pd.Timestamp):
    event = event_for(close_utc)
    if already_done(event):
        return
    prompt = PROMPT.format(series=SERIES, event=event, journal=JOURNAL, close=close_utc.strftime("%H:%M"))
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
