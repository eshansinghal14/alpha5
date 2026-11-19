"""Quote size from the completion depth a fill is expected to leave.

A fill big enough to matter usually takes much of the other legs' depth with
it, and how much survives is predictable before the fill: from the thinnest
other leg's depth near its top relative to the size, and how far under our
leg's touch the quote rests (a quote 5c under fills only on a 5c+ sweep).
Every Kalshi print that fills a quote (priced as replay.score prices it) is
scored at each SIZES size: the cheaper of completing or unwinding on the 0.1 s
books. The table is the mean P&L of each size per CELL; the runner cuts a live
quote to the best size at or under the one it would rest.

Replay 11-15..19 (Kalshi prints, fit on 3 days, tested on the other 2, both
ways round): on top of the exit model's live size held to the depth size, as
live quotes rest, the cut took live 5c from -$188 to +$943 (11-18..19) and
+$1,005 to +$1,524 (11-15..16), live 10c +$770 to +$1,416 and +$1,660 to +$1,724,
on about 30% fewer lots. Pre-game was mixed (worse at 10c on 11-15..16): live only.

Per-day statistics go to <root>/size_stats/<day>.json (the daily shadow check),
the table to data/live/ARBS/size_table.json from the TRAIN_DAYS before.
    python -m strategies.ARBS.size_table --day YYYY-MM-DD [--backfill N]
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import os

from infra.config import atomic_write_json
from strategies.ARBS import replay

SIZES = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 3000)
MULT_EDGES = (0.5, 1, 2, 5, 20)           # thinnest other leg's depth within SLACK of its top / size
DIST_EDGES = (0.5, 2, 5, 10, 20)          # our price under our leg's touch, c
SLACK = 0.02
LAG = 0.02                                # the quote rests on the books this long before the print
EDGES = {False: (0.03, 0.10), True: (0.05, 0.10)}   # pre-game / live: config edge, and 10c
SHRINK = 20.0                             # a cell's mean is pulled to 0 as if it held this many more zero fills
TRAIN_DAYS = 14
STATS_DIR = "size_stats"
TABLE_FILE = "size_table.json"
_OTHER = {"yes": "no", "no": "yes"}


def _bucket(x: float | None, edges) -> int:
    if x is None:
        return -1
    for i, e in enumerate(edges):
        if x <= e:
            return i
    return len(edges)


def cell(i: int, live: bool, dmin: float, dist: float | None, stable: bool) -> str:
    """The table key for size SIZES[i]."""
    return f"{i}|{int(live)}|{_bucket(dmin / SIZES[i], MULT_EDGES)}|{_bucket(dist, DIST_EDGES)}|{int(stable)}"


def _touch_bid(pre: dict, t: str, side: str) -> float | None:
    for r in pre.get(t) or ():
        if abs(r[0] - LAG) < 1e-9:
            if side == "yes":
                return r[1]
            return None if r[3] is None else round(1.0 - r[3], 4)
    return None


def events(row: dict):
    """(live, cell inputs, P&L per SIZES size) for each edge at which `row`
    fills a quote on its best basket, as replay.score picks and prices it."""
    pre, books, after = row.get("pre") or {}, row["books"], row.get("after") or {}
    end = after.get("0.1") or after.get("0.03")
    if not end or not pre:
        return
    late = after.get("1.0") or end
    tk, side, pr = row["ticker"], row["side"], row["prints"]
    pmin = min(p for _, p, _ in pr)
    best = None
    for bid, payout, live in row.get("baskets") or ():
        legs = replay.legs_of(bid, books)
        if not legs or (tk, side) not in legs:
            continue
        others = [x for x in legs if x[0] != tk]
        tops = [replay.lagged_ask(pre, t, s, LAG) for t, s in others]
        if not others or any(x is None for x in tops):
            continue
        s0 = sum(p + replay._fee(p, 1, 0.07) for p, _ in tops)
        if best is None or payout - pmin - s0 > best[0]:
            best = (payout - pmin - s0, payout, bool(live), others, tops, s0)
    if best is None:
        return
    _, payout, live, others, tops, s0 = best
    dmin, stable = math.inf, True
    for (t, s), top in zip(others, tops):
        dmin = min(dmin, sum(q for p, q in replay.asks(books, t, s) or [] if p <= top[0] + SLACK + 1e-9))
        t25 = replay.lagged_ask(pre, t, s, 0.25)
        stable = stable and t25 is not None and abs(t25[0] - top[0]) < 1e-9 and top[1] >= 0.8 * t25[1]
    tb = _touch_bid(pre, tk, side)
    for e in EDGES[live]:
        x = math.floor((payout - s0 - e) / replay.TICK + 1e-6) * replay.TICK
        if not 0 < x < 1:
            continue
        avail = sum(q for _, p, q in pr if p < x - 1e-9) + 0.5 * sum(q for _, p, q in pr if abs(p - x) < 1e-9)
        if avail <= 0:
            continue
        pnl = []
        for k in SIZES:
            n = min(k, avail)
            comp = n * payout - n * x - sum(replay._take(end, late, t, s, n, 0.07) for t, s in others)
            unw = n - n * x - replay._take(end, late, tk, _OTHER[side], n, 0.07)
            pnl.append(max(comp, unw))
        yield live, dmin, None if tb is None else 100 * (tb - x), stable, pnl


def day_stats(root: str, day: str) -> dict[str, list[float]] | None:
    """cell -> [P&L sum, fills] from one process's print log; None without one."""
    path = os.path.join(root, "prints", f"{day}.jsonl.gz")
    if not os.path.exists(path):
        return None
    out: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
    try:
        import orjson
        loads = orjson.loads
    except ImportError:
        loads = json.loads
    try:
        with gzip.open(path, "rb") as fh:
            for line in fh:
                try:
                    row = loads(line)
                except ValueError:
                    continue
                for live, dmin, dist, stable, pnl in events(row):
                    for i, v in enumerate(pnl):
                        c = out[cell(i, live, dmin, dist, stable)]
                        c[0] += v
                        c[1] += 1
    except (OSError, EOFError):
        pass                              # a print log cut off mid-write: what was read counts
    return {k: [round(v[0], 3), v[1]] for k, v in out.items()}


def write_day(root: str, day: str) -> bool:
    st = day_stats(root, day)
    if st is None:
        return False
    os.makedirs(os.path.join(root, STATS_DIR), exist_ok=True)
    atomic_write_json(os.path.join(root, STATS_DIR, f"{day}.json"), {"day": day, "cells": st}, indent=None)
    return True


def write_table(roots: list[str], out_root: str, before: str):
    """size_table.json in `out_root` from every root's stats of the TRAIN_DAYS before `before`."""
    cells: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
    used = set()
    for r in roots:
        d = os.path.join(r, STATS_DIR)
        days = sorted(f[:10] for f in os.listdir(d) if f.endswith(".json") and f[:10] < before) if os.path.isdir(d) else []
        for day in days[-TRAIN_DAYS:]:
            try:
                with open(os.path.join(d, f"{day}.json"), encoding="utf-8") as fh:
                    st = json.load(fh)["cells"]
            except (OSError, ValueError, KeyError):
                continue
            used.add(f"{os.path.basename(r)}:{day}")
            for k, (s, n) in st.items():
                c = cells[k]
                c[0] += s
                c[1] += n
    if not used:
        return
    atomic_write_json(os.path.join(out_root, TABLE_FILE),
                      {"sizes": list(SIZES), "trained_on": sorted(used),
                       "cells": {k: [round(v[0], 3), v[1]] for k, v in cells.items()}}, indent=None)


class Table:
    def __init__(self, d: dict):
        self.cells = d.get("cells") or {}
        self.ok = list(d.get("sizes") or ()) == list(SIZES)

    def value(self, key: str) -> float:
        s, n = self.cells.get(key, (0.0, 0))
        return s / (n + SHRINK)

    def size(self, size: float, live: bool, dmin: float, dist: float | None, stable: bool) -> float:
        """The best SIZES size at or under `size`; `size` itself when that is
        the largest one under it (the table only knows the grid)."""
        allowed = [i for i, k in enumerate(SIZES) if k <= size + 1e-9]
        if not self.ok or not allowed or dmin <= 0:
            return size
        best = max(allowed, key=lambda i: self.value(cell(i, live, dmin, dist, stable)))
        return size if best == allowed[-1] else float(SIZES[best])


def main():
    import pandas as pd
    from infra.runtime.persist import LIVE_ROOT
    from strategies.ARBS import SERIES
    from strategies.ARBS.shadow_check import PM_ROOTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--backfill", type=int, default=0, help="also the N days before --day")
    a = ap.parse_args()
    roots = [os.path.join(LIVE_ROOT, s) for s in SERIES if s not in PM_ROOTS]
    days = [(pd.Timestamp(a.day) - pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in range(a.backfill, -1, -1)]
    for r in roots:
        for d in days:
            if write_day(r, d):
                print(json.dumps({"root": os.path.basename(r), "day": d}))
    nxt = (pd.Timestamp(a.day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    write_table(roots, os.path.join(LIVE_ROOT, "ARBS"), nxt)


if __name__ == "__main__":
    main()
