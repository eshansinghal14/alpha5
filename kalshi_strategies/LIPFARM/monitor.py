"""LIPFARM watchdog, every 5 min on the box (alpha5-lipfarm-monitor.timer).

Prints what changed since the last run to data/live/lipfarm_monitor.log. It
disarms the farm (as the dashboard button does: every farm order pulled,
positions kept) only on evidence checked twice:
  - P&L is computed from Kalshi's own fills of our orders, not our journal,
    and marked at mid (settled markets at their result), so a wide spread
    or a journal bug can't fake a loss; a journal that disagrees with Kalshi
    is reported, not acted on.
  - a rule must hold on two runs in a row (5 min apart); only a loss past
    LOSS_HARD disarms at once.
Shutdown rules: fill P&L since monitoring began under LOSS_TOTAL; exposure over
MAX_EXPOSURE; ARBS halted or its audit disagreeing with Kalshi; more than
MAX_TRACEBACKS new tracebacks; writes over WRITE_RUNAWAY x the farm's cap
(a retry loop, as RTOR 11-05). One market's fills marked under LOSS_ONE only
alerts: one bad fill isn't a reason to stop.
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
from kalshi_infra.kalshi.orders import FILLS_PATH, _count, _dollars
from kalshi_infra.runtime.persist import LIVE_ROOT

ROOT = os.path.join(LIVE_ROOT, "LIPFARM")
MON = os.path.join(ROOT, "monitor.json")
LOSS_TOTAL, LOSS_HARD, LOSS_ONE = -75.0, -200.0, -40.0
MAX_EXPOSURE, MAX_TRACEBACKS, WRITE_RUNAWAY = 1500.0, 20, 2.0
TOPIC_FILE = "/etc/alpha5/report-topic-arn"


def _market(ticker: str, cache: dict) -> dict:
    if ticker not in cache:
        cache[ticker] = kalshi_get(f"/trade-api/v2/markets/{ticker}").get("market", {})
    return cache[ticker]


def mark(m: dict, side: str, how: str) -> float:
    """What one `side` contract is worth: its result once settled, else the
    bid (how="bid") or the bid/ask mid (how="mid")."""
    if m.get("result") in ("yes", "no"):
        return 1.0 if m["result"] == side else 0.0
    other = "no" if side == "yes" else "yes"
    bid = float(m.get(f"{side}_bid_dollars") or 0.0)
    if how == "bid":
        return bid
    other_bid = float(m.get(f"{other}_bid_dollars") or 0.0)
    ask = 1.0 - other_bid if other_bid > 0 else bid
    return (bid + ask) / 2


def marked_pnl(fills: list[dict], value) -> float:
    """Net P&L of `fills`: yes+no pairs at $1, the rest at value(ticker, side), less cost and fees."""
    pos: dict = {}
    for f in fills:
        p = pos.setdefault((f["ticker"], f["side"]), [0.0, 0.0])
        p[0] += f["size"]
        p[1] += f["size"] * f["price"] + (f.get("fee") or 0.0)
    out = 0.0
    for (t, side), (q, cost) in pos.items():
        paired = min(q, pos.get((t, "no" if side == "yes" else "yes"), [0.0])[0])
        out += paired * 0.5 + (q - paired) * value(t, side) - cost
    return out


def kalshi_fills(since: float, oids: set) -> list[dict]:
    """Kalshi's record of fills of `oids` since `since` (unix s)."""
    out, cursor = [], None
    for _ in range(20):
        params = {"limit": 1000, "min_ts": int(since)}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get(FILLS_PATH, params=params, auth=True)
        for f in data.get("fills", []):
            side = f.get("outcome_side")
            if f.get("order_id") not in oids or side not in ("yes", "no"):
                continue
            out.append({"ticker": f.get("ticker") or f.get("market_ticker"), "side": side,
                        "price": _dollars(f, "yes_price" if side == "yes" else "no_price") or 0.0,
                        "size": _count(f, "count") or 0.0, "fee": float(f.get("fee_cost") or 0.0),
                        "order_id": f.get("order_id")})
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def decide(cur: dict, pending: dict) -> tuple[list[str], list[str], dict]:
    """(shutdown reasons, alerts, rules pending for next run) from this run's
    checked figures `cur` and the rules that held last run (`pending`)."""
    hits, alerts = {}, []
    if cur["mode"] != "LIVE":
        return [], [], {}
    if cur["pnl"] < LOSS_TOTAL:
        hits["loss"] = f"fill P&L since monitoring began ${cur['pnl']:.2f} < ${LOSS_TOTAL:.0f} (Kalshi's fills, mid marks)"
    if cur["exposure"] > MAX_EXPOSURE:
        hits["exposure"] = f"exposure ${cur['exposure']:.0f} > ${MAX_EXPOSURE:.0f}"
    if cur["arbs_bad"]:
        hits["arbs"] = f"ARBS {cur['arbs_bad']}"
    if cur["new_tb"] > MAX_TRACEBACKS:
        hits["tracebacks"] = f"{cur['new_tb']} new tracebacks"
    if cur["write_cap"] and cur["write_tps"] > WRITE_RUNAWAY * cur["write_cap"]:
        hits["writes"] = f"writes {cur['write_tps']:.0f}/s > {WRITE_RUNAWAY:g}x the {cur['write_cap']:.0f}/s cap"
    if cur["worst_market"] < LOSS_ONE:
        alerts.append(f"{cur['worst_name']} marked ${cur['worst_market']:.2f} (one market; not a shutdown)")
    if cur["journal_gap"]:
        alerts.append(f"journal disagrees with Kalshi: {cur['journal_gap']}")
    stop = [why for k, why in hits.items() if k in pending]
    if cur["pnl"] < LOSS_HARD and "loss" not in pending:
        stop.append(hits["loss"] + f", past ${LOSS_HARD:.0f}: at once")
    return stop, alerts, {k: pending.get(k, time.time()) for k in hits}


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
    since = [f for f in fills if f.get("trade_id") not in base]
    at_bid = lambda t, side: mark(_market(t, cache), side, "bid")   # noqa: E731
    at_mid = lambda t, side: mark(_market(t, cache), side, "mid")   # noqa: E731

    # Kalshi's fills of the orders our journal knows, since monitoring began.
    oids = {f.get("order_id") for f in fills if f.get("order_id")}
    try:
        kf = kalshi_fills(mon["started"], oids)
    except Exception as e:
        kf = None
        print(f"Kalshi fills unavailable ({e}); using the journal's")
    gap = ""
    if kf is not None:
        by = lambda fs: {t: round(sum(f["size"] for f in fs if f["ticker"] == t), 2)   # noqa: E731
                         for t in {f["ticker"] for f in fs}}
        j, k = by(since), by(kf)
        diff = {t: (j.get(t, 0.0), k.get(t, 0.0)) for t in set(j) | set(k) if abs(j.get(t, 0.0) - k.get(t, 0.0)) > 0.01}
        gap = ", ".join(f"{t} journal {a:g} vs Kalshi {b:g}" for t, (a, b) in sorted(diff.items()))
    src = kf if kf is not None else since
    pnl, pnl_bid, pnl_journal = marked_pnl(src, at_mid), marked_pnl(src, at_bid), marked_pnl(since, at_mid)

    lines, worst, worst_name = [], 0.0, ""
    for t in sorted({f["ticker"] for f in new}):
        # A market's change, not each fill alone: an exit marked by itself looked like
        # a $138 loss when, netted with the fill it closed, it was $14 (TJAC 11-05).
        mine = [f for f in since if f["ticker"] == t]
        one = marked_pnl(mine, at_mid) - marked_pnl([f for f in mine if f.get("trade_id") in seen], at_mid)
        if one < worst:
            worst, worst_name = one, t
        lines.append(f"  {t}: {one:+.2f} at mid")
        for f in new:
            if f["ticker"] == t:
                lines.append(f"    {str(f.get('time'))[11:19]} {f['side']} @{f['price']} x{f['size']:g} "
                             f"{f.get('liquidity')} fee {f.get('fee') or 0:.3f}")
    err_path = os.path.join(LIVE_ROOT, "lipfarm.err.log")
    tb = open(err_path).read().count("Traceback") if os.path.exists(err_path) else 0
    a = json.load(open(max(glob.glob(os.path.join(LIVE_ROOT, "ARBS", "*", "summary.json")), key=os.path.getmtime)))
    arbs_bad = (f"halt={a.get('halt_reason')} audit={a.get('audit_diff')}"
                if a.get("halt_reason") or a.get("audit_diff") else "")
    cur = {"mode": s["mode"], "pnl": pnl, "exposure": s.get("exposure_usd", 0), "arbs_bad": arbs_bad,
           "new_tb": tb - (mon["tb"] if mon["tb"] is not None else tb), "write_tps": s.get("write_tps_10m", 0),
           "write_cap": s.get("write_tps_cap", 0), "worst_market": worst, "worst_name": worst_name,
           "journal_gap": gap}

    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z LIPFARM {s['mode']} | status "
          f"{time.time() - s['updated_at']:.0f}s old | quoting {s['quoting']} | ${s['our_usd_day_now']:.0f}/day | "
          f"exp LIP ${s['exp_lip_usd']:.2f} (locked ${s.get('lip_locked_usd', 0):.2f}) | exposure "
          f"${cur['exposure']:.0f} | writes {cur['write_tps']:.1f}/s | ws {'up' if s.get('ws_up') else 'DOWN'}")
    print(f"monitoring {(time.time() - mon['started']) / 3600:.1f} h | fill P&L since: ${pnl:+.2f} at mid "
          f"(${pnl_bid:+.2f} at bid; journal ${pnl_journal:+.2f}; source {'Kalshi' if kf is not None else 'journal'}) "
          f"| new fills {len(new)} | new tracebacks {cur['new_tb']} | ARBS live {a.get('live_trading')} "
          f"halt {a.get('halt_reason')} audit {a.get('audit_diff')}")
    for line in lines:
        print(line)

    if gap and gap == mon.get("gap"):
        cur["journal_gap"] = ""                      # alerted on already
    mon["gap"] = gap
    stop, alerts, pending = decide(cur, mon.get("pending", {}))
    for k in pending:
        if k not in mon.get("pending", {}):
            print(f"WATCH: {k} rule hit; disarms if it still holds next run")
    for x in alerts:
        print(f"ALERT: {x}")
    if alerts:
        alert("LIPFARM monitor alert (still trading)", "\n".join(alerts + [""] + lines))
    if stop:
        from kalshi_infra.runtime.control import request_trading_mode
        reason = "; ".join(stop)
        request_trading_mode("LIPFARM", False, source=f"lipfarm monitor: {reason}")
        print(f"SHUTDOWN: disarmed LIPFARM: {reason}")
        alert("LIPFARM disarmed by its monitor", reason + "\n\n" + "\n".join(lines))
        pending = {}
    mon["pending"] = pending
    mon["seen"] = sorted(seen | {f.get("trade_id") for f in new})
    mon["tb"] = tb
    json.dump(mon, open(MON, "w"))


if __name__ == "__main__":
    main()
