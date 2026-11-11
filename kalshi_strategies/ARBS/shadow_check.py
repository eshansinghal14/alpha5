"""Daily check that the shadow's fill scoring (replay.score) matches our real
fills, and what every recorded print says about the edge a quote needs.
Writes data/live/ARBS/shadow_check/<day>.json, shown on the ARBS dashboard.

    python -m kalshi_strategies.ARBS.shadow_check [--day YYYY-MM-DD]

- coverage: our finished arbs that began on a quote fill, and how many have
  the print that filled it recorded (the shadow only learns from those).
- calibration: each such arb replayed at its real quote edge and size, vs
  what it really made, per group.
- policy: every print our quote met, scored at each edge for a POLICY_SIZE
  quote, and at the edge the shadow had chosen for that event.
- markout: every recorded print, ours or not: how far the cost of the other
  legs moved 0.1 s and 1 s after it, by group, time to settlement, live and
  basket shape. A maker quote there pays this move whenever it is filled.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import json
import os

import numpy as np
import pandas as pd

from kalshi_infra.config import atomic_write_json
from kalshi_strategies.ARBS import live_params, replay
from kalshi_strategies.ARBS.runner import LIVE_ROOT, QUOTE_GONE_S, SHADOW_LAG_S, series_groups

EDGES = (*live_params.EDGES, 0.15, 0.20)
POLICY_SIZE = 5.0
OUT_DIR = "shadow_check"


def rates(ticker: str) -> tuple[float, float]:
    # A series' own fee rates aren't in the print row; Kalshi's standard ones.
    return 0.07, 0.0


def shape(basket_id: str) -> str:
    kind, _, rest = basket_id.partition(":")
    if kind == "mecnet":
        return "mecnet2" if len(rest.rsplit(":", 1)[-1].split("+")) == 2 else "mecnet3+"
    return kind if kind in ("ladder", "complete_set") else "relational"


def tte_bucket(h: float | None) -> str:
    if h is None:
        return "?"
    return "<6h" if h < 6 else "6-24h" if h < 24 else ">24h"


def _top_cost(books: dict, legs) -> float | None:
    c = 0.0
    for t, s in legs:
        lv = replay.asks(books, t, s)
        if not lv:
            return None
        c += lv[0][0]
    return c


def _pre_cost(pre: dict, legs) -> float | None:
    c = 0.0
    for t, s in legs:
        top = replay.lagged_ask(pre, t, s, SHADOW_LAG_S)
        if top is None:
            return None
        c += top[0]
    return c


def markout(row: dict):
    """(basket_id, edge at the print, move at 0.1 s, move at 1 s) in $ per
    bundle for the row's best basket, or None."""
    best = None
    pmin = min(p for _, p, _ in row["prints"])
    for bid, payout, _ in row["baskets"]:
        legs = replay.legs_of(bid, row["books"])
        if not legs or (row["ticker"], row["side"]) not in legs:
            continue
        others = [l for l in legs if l[0] != row["ticker"]]
        c0 = _pre_cost(row.get("pre") or {}, others)
        if c0 is None:
            continue
        g0 = payout - pmin - c0
        if best is None or g0 > best[1]:
            best = (bid, g0, others, c0)
    if best is None:
        return None
    bid, g0, others, c0 = best
    after = row.get("after") or {}
    m = [None if (c := _top_cost(after.get(k) or {}, others)) is None else c - c0 for k in ("0.1", "1.0")]
    return bid, g0, m[0], m[1]


def _rows(path: str):
    with gzip.open(path, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            for k in ("books", "after", "pre"):
                if isinstance(r.get(k), str):
                    r[k] = json.loads(r[k])
            yield r


def run(day: str, root: str | None = None, groups: dict | None = None) -> dict:
    root = root or os.path.join(LIVE_ROOT, "ARBS")
    groups = series_groups() if groups is None else groups
    grp = lambda ticker: groups.get(ticker.split("-", 1)[0], "unknown")
    arbs, choices = [], []
    with open(os.path.join(root, "journal", f"{day}.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if '"size_shadow"' in line or '"live_params"' in line:
                r = json.loads(line)
                if r["kind"] == "size_shadow" and (r.get("entry") or [None])[0] == "quote":
                    arbs.append(r)
                elif r["kind"] == "live_params":
                    choices.append((r["t"], r.get("events") or {}))
    want = collections.defaultdict(list)
    for i, a in enumerate(arbs):
        want[(a["entry"][1], a["entry"][2], a["basket_id"])].append(i)
    match: dict[int, tuple[float, dict]] = {}
    choices.sort(key=lambda x: x[0])
    cts = [t for t, _ in choices]
    policy = collections.defaultdict(lambda: {"prints": 0, "edges": [0.0] * len(EDGES), "shadow_usd": 0.0,
                                              "shadow_prints": 0})
    marks = collections.defaultdict(list)
    path = os.path.join(root, "prints", f"{day}.jsonl.gz")
    for r in _rows(path) if os.path.exists(path) else ():
        g = grp(r["ticker"])
        mk = markout(r)
        if mk is not None:
            bid, g0, m01, m1 = mk
            live = next((lv for b, _, lv in r["baskets"] if b == bid), False)
            key = (g, tte_bucket((r.get("tte_h") or {}).get(bid)), bool(live), shape(bid))
            marks[key].append((g0, m01, m1))
        q = r.get("quote")
        if q is None or (q.get("tripped") and (q.get("tripped_s") or 0.0) > QUOTE_GONE_S):
            continue
        bk = [b for b in r["baskets"] if b[0] == q["basket_id"]]
        if not bk:
            continue
        for i in want.get((r["ticker"], r["side"], q["basket_id"]), ()):
            dt = arbs[i]["t0"] - r["t"]
            if -0.5 <= dt <= 1.5 and (i not in match or abs(dt) < abs(match[i][0])):
                match[i] = (dt, {**r, "baskets": bk, "quote": q})
        row = {**r, "baskets": bk}
        s = replay.score(row, rates, EDGES, POLICY_SIZE, lag=SHADOW_LAG_S)
        if s is None:
            continue
        ratio = (r.get("pull") or {}).get("ratio")
        pnl = [0.0] * len(EDGES) if ratio is not None and ratio < 0.5 else s["pnl"]
        p = policy[(g, bool(bk[0][2]))]
        p["prints"] += 1
        p["edges"] = [a + b for a, b in zip(p["edges"], pnl)]
        j = bisect.bisect_right(cts, r["t"]) - 1
        ev = r["ticker"].rsplit("-", 1)[0]
        if j >= 0 and r["t"] - cts[j] < 900 and ev in choices[j][1]:
            e = choices[j][1][ev]
            p["shadow_prints"] += 1
            if e not in (None, "none"):
                k = min(range(len(EDGES)), key=lambda k: abs(EDGES[k] - float(e)))
                p["shadow_usd"] += pnl[k]
    calib = collections.defaultdict(lambda: {"arbs": 0, "matched": 0, "lots": 0.0, "real_usd": 0.0,
                                             "filled": 0, "f_lots": 0.0, "f_real_usd": 0.0, "f_shadow_lots": 0.0,
                                             "f_shadow_usd": 0.0})
    for i, a in enumerate(arbs):
        c = calib[a["group"]]
        c["arbs"] += 1
        c["lots"] += a["lots"]
        c["real_usd"] += a["pnl"]
        if i not in match:
            continue
        c["matched"] += 1
        row = match[i][1]
        s = replay.score(row, rates, [row["quote"].get("edge") or 0.0], a["lots"], lag=SHADOW_LAG_S)
        if s is None or not s["fills"][0]:
            continue
        c["filled"] += 1
        c["f_lots"] += a["lots"]
        c["f_real_usd"] += a["pnl"]
        c["f_shadow_lots"] += s["fills"][0]
        c["f_shadow_usd"] += s["pnl"][0]
    cal_rows = []
    for g, c in sorted(calib.items(), key=lambda kv: -kv[1]["lots"]):
        cal_rows.append({"group": g, **{k: round(v, 4) for k, v in c.items()},
                         "real_c": _c(c["f_real_usd"], c["f_lots"]),
                         "shadow_c": _c(c["f_shadow_usd"], c["f_shadow_lots"])})
    mk_rows = []
    for (g, b, live, sh), v in marks.items():
        x = np.array([[np.nan if y is None else y for y in t] for t in v], dtype=float)
        mk_rows.append({"group": g, "tte": b, "live": live, "shape": sh, "n": len(v),
                        "edge_c": _r(100 * np.nanmean(x[:, 0])), "move_01_c": _r(100 * np.nanmean(x[:, 1])),
                        "move_1_c": _r(100 * np.nanmean(x[:, 2])),
                        "move_1_p90_c": _r(100 * np.nanpercentile(x[:, 2], 90)) if np.isfinite(x[:, 2]).any() else None})
    mk_rows.sort(key=lambda r: -r["n"])
    return {"day": day, "generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "edges": list(EDGES),
            "policy_size": POLICY_SIZE,
            "coverage": {"arbs": len(arbs), "matched": len(match)},
            "calibration": cal_rows,
            "policy": [{"group": g, "live": lv, **{k: (v if k != "edges" else [round(x, 2) for x in v])
                                                    for k, v in p.items()}}
                       for (g, lv), p in sorted(policy.items(), key=lambda kv: -kv[1]["prints"])],
            "markout": mk_rows}


def _c(usd: float, lots: float) -> float | None:
    return round(100 * usd / lots, 2) if lots > 1e-9 else None


def _r(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    day = ap.parse_args().day
    root = os.path.join(LIVE_ROOT, "ARBS")
    doc = run(day, root)
    os.makedirs(os.path.join(root, OUT_DIR), exist_ok=True)
    atomic_write_json(os.path.join(root, OUT_DIR, f"{day}.json"), doc, indent=None)
    print(json.dumps({"day": day, "coverage": doc["coverage"], "groups": len(doc["calibration"])}))


if __name__ == "__main__":
    main()
