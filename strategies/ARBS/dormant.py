"""Series that spend Kalshi shard 0's write budget without finishing arbs.

A series is dormant when shard 0's processes spent at least MIN_TOKENS write
tokens quoting it over the DAYS days before and finished no arb on it. With
trading.dormant_skip the runner stops quoting a dormant series' shard 0
baskets until a print through the price it would quote wakes the event
(runner.DORMANT_WAKE_S); off, it only reports what it would skip.

Measured on 11-16..21 in two 3-day halves, both ways round: dropping these
series freed 15.6% / 16.0% of the next 3 days' tokens for $6 / $1 of their
P&L, while dropping series with P&L <= 0 gave up $55 / $105.

A listed series stays listed until it finishes an arb, or for MAX_DAYS, then
has to qualify again: skipped, it spends too little to qualify.

Per-day counts go to <root>/series_tokens/<day>.json, the list to
data/live/ARBS/dormant.json (and dormant/<day>.json for the record).
    python -m strategies.ARBS.dormant --day YYYY-MM-DD    (shadow_check runs it nightly)
"""
from __future__ import annotations

import argparse
import collections
import json
import os

import pandas as pd

from infra.config import atomic_write_json

ROOTS = ("ARBS", "ARBS0B", "ARBS0C")        # shard 0's processes: one write bucket
DAYS = 3
MIN_TOKENS = 100_000
MAX_DAYS = 7
COST = {"quote": 10, "move": 10, "cancel": 2}   # Kalshi endpoint_costs, 11-21
LIST_FILE = "dormant.json"
OUT_DIR = "series_tokens"


def day_stats(root: str, day: str) -> dict[str, list] | None:
    """series -> [write tokens, finished arbs, their P&L] from the day's
    journal, None without one."""
    path = os.path.join(root, "journal", f"{day}.jsonl")
    if not os.path.exists(path):
        return None
    out: dict[str, list] = collections.defaultdict(lambda: [0, 0, 0.0])
    with open(path, "rb") as fh:
        for line in fh:
            i, off = line.find(b'"kind": "'), 9
            if i < 0:
                i, off = line.find(b'"kind":"'), 8
                if i < 0:
                    continue
            kind = line[i + off:line.find(b'"', i + off)].decode()
            if kind in COST:
                j, off = line.find(b'"ticker": "'), 11
                if j < 0:
                    j, off = line.find(b'"ticker":"'), 10
                if j >= 0:
                    out[line[j + off:line.find(b'"', j + off)].decode().split("-")[0]][0] += COST[kind]
            elif kind == "size_shadow":
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                s = out[r.get("series") or "?"]
                s[1] += 1
                s[2] += float(r.get("pnl") or 0.0)
    return dict(out)


def write_day(root: str, day: str) -> dict | None:
    stats = day_stats(root, day)
    if stats is not None:
        os.makedirs(os.path.join(root, OUT_DIR), exist_ok=True)
        atomic_write_json(os.path.join(root, OUT_DIR, f"{day}.json"), stats, indent=None)
    return stats


def _day(root: str, day: str) -> dict | None:
    try:
        with open(os.path.join(root, OUT_DIR, f"{day}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return write_day(root, day)


def build(days: dict[str, list[dict]], nxt: str, prev: dict | None) -> dict:
    """The list for day `nxt` from each of the DAYS days' per-root stats and
    the previous list."""
    tot: dict[str, list] = collections.defaultdict(lambda: [0, 0, 0.0])
    for per_root in days.values():
        for stats in per_root:
            for s, (t, n, p) in stats.items():
                tot[s][0] += t
                tot[s][1] += n
                tot[s][2] += p
    end = pd.Timestamp(nxt)
    held = (prev or {}).get("series") or {}
    out = {}
    for s, (t, n, p) in tot.items():
        if n == 0 and t >= MIN_TOKENS:
            out[s] = {"since": nxt, "tokens": t}
    for s, v in held.items():
        if tot.get(s, (0, 0))[1] == 0 and end - pd.Timestamp(v["since"]) < pd.Timedelta(days=MAX_DAYS):
            out[s] = {"since": v["since"], "tokens": tot.get(s, (0,))[0]}
    return {"for": nxt, "days": sorted(days), "min_tokens": MIN_TOKENS, "series": dict(sorted(out.items()))}


def write_list(live_root: str, nxt: str) -> dict:
    roots = [os.path.join(live_root, s) for s in ROOTS if os.path.isdir(os.path.join(live_root, s))]
    days = {}
    for d in pd.date_range(pd.Timestamp(nxt) - pd.Timedelta(days=DAYS), periods=DAYS, freq="D"):
        got = [x for r in roots if (x := _day(r, f"{d:%Y-%m-%d}")) is not None]
        if got:
            days[f"{d:%Y-%m-%d}"] = got
    path = os.path.join(live_root, "ARBS", LIST_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        prev = None
    doc = build(days, nxt, prev)
    atomic_write_json(path, doc, indent=None)
    os.makedirs(os.path.join(live_root, "ARBS", "dormant"), exist_ok=True)
    atomic_write_json(os.path.join(live_root, "ARBS", "dormant", f"{nxt}.json"), doc, indent=None)
    return doc


def main():
    from strategies.ARBS.runner import LIVE_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    help="the last full day; the list is for the day after")
    day = ap.parse_args().day
    for s in ROOTS:
        write_day(os.path.join(LIVE_ROOT, s), day)
    nxt = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    doc = write_list(LIVE_ROOT, nxt)
    print(json.dumps({"for": nxt, "days": doc["days"], "series": len(doc["series"])}))


if __name__ == "__main__":
    main()
