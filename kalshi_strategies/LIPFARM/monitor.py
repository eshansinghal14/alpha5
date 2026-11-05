"""LIPFARM watchdog, every 5 min on the box (alpha5-lipfarm-monitor.timer).

Prints what changed since the last run to data/live/lipfarm_monitor.log and
disarms the farm (as the dashboard button does: every farm order pulled,
positions kept) when a rule hits, with an SNS alert:
  - fill P&L since monitoring began, marked at current bids, under LOSS_TOTAL
  - one market's new fills, netted with its earlier ones, marked under LOSS_ONE
  - exposure over MAX_EXPOSURE
  - ARBS halted, or its position audit disagrees with Kalshi
  - more than MAX_TRACEBACKS new tracebacks in the farm's error log
State between runs: data/live/LIPFARM/monitor.json (delete it to restart the P&L baseline).

    python -m kalshi_strategies.LIPFARM.monitor
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.runtime.control import request_trading_mode
from kalshi_infra.runtime.persist import LIVE_ROOT

ROOT = os.path.join(LIVE_ROOT, "LIPFARM")
MON = os.path.join(ROOT, "monitor.json")
LOSS_TOTAL, LOSS_ONE, MAX_EXPOSURE, MAX_TRACEBACKS = -75.0, -40.0, 1500.0, 20
TOPIC_FILE = "/etc/alpha5/report-topic-arn"


def _bid(ticker: str, side: str, cache: dict) -> float:
    if ticker not in cache:
        cache[ticker] = kalshi_get(f"/trade-api/v2/markets/{ticker}").get("market", {})
    return float(cache[ticker].get(f"{side}_bid_dollars") or 0.0)


def marked_pnl(fills: list[dict], cache: dict) -> float:
    """Net P&L of `fills`: yes+no pairs at $1, the rest at the current bid, less cost and fees."""
    pos: dict = {}
    for f in fills:
        p = pos.setdefault((f["ticker"], f["side"]), [0.0, 0.0])
        p[0] += f["size"]
        p[1] += f["size"] * f["price"] + (f.get("fee") or 0.0)
    out = 0.0
    for (t, side), (q, cost) in pos.items():
        paired = min(q, pos.get((t, "no" if side == "yes" else "yes"), [0.0])[0])
        out += paired * 0.5 + (q - paired) * _bid(t, side, cache) - cost
    return out


def alert(subject: str, body: str):
    try:
        topic = open(TOPIC_FILE).read().strip()
        subprocess.run(["aws", "sns", "publish", "--region", "us-east-2", "--topic-arn", topic, "--subject",
                        subject[:99], "--message", body], timeout=30, capture_output=True)
    except Exception as e:
        print(f"alert not sent: {e}")


def main():
    st = json.load(open(os.path.join(ROOT, "live", "state.json")))
    s = json.load(open(os.path.join(ROOT, "status.json")))["summary"]
    fills = st["fills"]
    mon = json.load(open(MON)) if os.path.exists(MON) else {
        "started": time.time(), "base": [f.get("trade_id") for f in fills], "seen": [f.get("trade_id") for f in fills],
        "tb": None}
    base, seen, cache = set(mon["base"]), set(mon["seen"]), {}
    new = [f for f in fills if f.get("trade_id") not in seen]
    pnl = marked_pnl([f for f in fills if f.get("trade_id") not in base], cache)
    lines, worst = [], 0.0
    since = [f for f in fills if f.get("trade_id") not in base]
    for t in sorted({f["ticker"] for f in new}):
        # A market's change, not each fill alone: an exit marked by itself looked like
        # a $138 loss when, netted with the fill it closed, it was $14 (TJAC 11-05).
        mine = [f for f in since if f["ticker"] == t]
        one = marked_pnl(mine, cache) - marked_pnl([f for f in mine if f.get("trade_id") in seen], cache)
        worst = min(worst, one)
        lines.append(f"  {t}: {one:+.2f} at today's bids")
        for f in new:
            if f["ticker"] == t:
                lines.append(f"    {str(f.get('time'))[11:19]} {f['side']} @{f['price']} x{f['size']:g} "
                             f"{f.get('liquidity')} fee {f.get('fee') or 0:.3f}")
    err_path = os.path.join(LIVE_ROOT, "lipfarm.err.log")
    tb = open(err_path).read().count("Traceback") if os.path.exists(err_path) else 0
    a = json.load(open(max(glob.glob(os.path.join(LIVE_ROOT, "ARBS", "*", "summary.json")), key=os.path.getmtime)))

    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z LIPFARM {s['mode']} | status "
          f"{time.time() - s['updated_at']:.0f}s old | quoting {s['quoting']} | ${s['our_usd_day_now']:.0f}/day | "
          f"exp LIP ${s['exp_lip_usd']:.2f} (locked ${s.get('lip_locked_usd', 0):.2f}) | exposure "
          f"${s.get('exposure_usd', 0):.0f} | writes {s.get('write_tps_10m', 0):.1f}/s | "
          f"ws {'up' if s.get('ws_up') else 'DOWN'}")
    print(f"monitoring {(time.time() - mon['started']) / 3600:.1f} h | fill P&L since: ${pnl:+.2f} | new fills {len(new)} | "
          f"new tracebacks {tb - (mon['tb'] if mon['tb'] is not None else tb)} | ARBS live {a.get('live_trading')} "
          f"halt {a.get('halt_reason')} audit {a.get('audit_diff')}")
    for line in lines:
        print(line)

    why = []
    if s["mode"] == "LIVE":
        if pnl < LOSS_TOTAL:
            why.append(f"fill P&L since monitoring began ${pnl:.2f} < ${LOSS_TOTAL:.0f}")
        if worst < LOSS_ONE:
            why.append(f"one market's new fills marked ${worst:.2f} (< ${LOSS_ONE:.0f})")
        if s.get("exposure_usd", 0) > MAX_EXPOSURE:
            why.append(f"exposure ${s['exposure_usd']:.0f} > ${MAX_EXPOSURE:.0f}")
        if a.get("halt_reason") or a.get("audit_diff"):
            why.append(f"ARBS halt={a.get('halt_reason')} audit={a.get('audit_diff')}")
        if mon["tb"] is not None and tb - mon["tb"] > MAX_TRACEBACKS:
            why.append(f"{tb - mon['tb']} new tracebacks")
    if why:
        reason = "; ".join(why)
        request_trading_mode("LIPFARM", False, source=f"lipfarm monitor: {reason}")
        print(f"SHUTDOWN: disarmed LIPFARM: {reason}")
        alert("LIPFARM disarmed by its monitor", reason + "\n\n" + "\n".join(lines))
    mon["seen"] = sorted(seen | {f.get("trade_id") for f in new})
    mon["tb"] = tb
    json.dump(mon, open(MON, "w"))


if __name__ == "__main__":
    main()
