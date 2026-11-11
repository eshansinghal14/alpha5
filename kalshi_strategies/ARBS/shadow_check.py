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
- rules: the exit-cost model (exit_cost.py) fitted on the earlier days' saved
  statistics only (every ARBS process's, pooled: the model is the same for any
  market), scored on this day's prints our quote met against the fixed edges:
  the narrowest edge whose predicted net clears RULE_MARGIN_C; the edge with
  the best fill odds x predicted net for a POLICY_SIZE quote; and the edge and
  size (RULE_SIZES) with the best fill odds x size x predicted net, where the
  predicted exit cost grows as the size outgrows the exit legs' depth.

Run for each ARBS process (ARBS, ARBS3), each into its own folder.
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
from kalshi_strategies.ARBS import exit_cost, live_params, replay
from kalshi_strategies.ARBS.runner import LIVE_ROOT, QUOTE_GONE_S, SHADOW_LAG_S, series_groups

EDGES = (*live_params.EDGES, 0.15, 0.20)
POLICY_SIZE = 5.0
OUT_DIR = "shadow_check"
RULE_MARGIN_C = 1.0
TRAIN_DAYS = 14
RULE_SIZES = (5.0, 20.0, 50.0, 100.0)


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
    """(basket_id, edge at the print, move at 0.1 s, move at 1 s, payout) in $
    per bundle for the row's best basket, or None."""
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
            best = (bid, g0, others, c0, payout)
    if best is None:
        return None
    bid, g0, others, c0, payout = best
    after = row.get("after") or {}
    m = [None if (c := _top_cost(after.get(k) or {}, others)) is None else c - c0 for k in ("0.1", "1.0")]
    return bid, g0, m[0], m[1], payout


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
    stats, rule_rows = exit_cost.Stats(), []
    path = os.path.join(root, "prints", f"{day}.jsonl.gz")
    for r in _rows(path) if os.path.exists(path) else ():
        g = grp(r["ticker"])
        mk = markout(r)
        if mk is not None:
            bid, g0, m01, m1, payout = mk
            live = next((lv for b, _, lv in r["baskets"] if b == bid), False)
            key = (g, tte_bucket((r.get("tte_h") or {}).get(bid)), bool(live), shape(bid))
            marks[key].append((g0, m01, m1))
            if g0 >= -0.01:
                n = max(1.0, min(sum(q for _, _, q in r["prints"]), 100.0))
                f = exit_cost.features(r, bid, payout, n)
                if f is not None:
                    stats.add(f, exit_cost.loss(r, f))
                    stats.add_reach(bool(live), f["reach_c"])
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
        if any(s["fills"]):
            by_size = {}
            for n in RULE_SIZES:
                fn = exit_cost.features(r, bk[0][0], bk[0][1], n)
                sn = s if n == POLICY_SIZE else replay.score(row, rates, EDGES, n, lag=SHADOW_LAG_S)
                if fn is not None and sn is not None:
                    by_size[n] = (fn, [0.0] * len(EDGES) if ratio is not None and ratio < 0.5 else sn["pnl"])
            if POLICY_SIZE in by_size:
                rule_rows.append((bool(bk[0][2]), by_size))
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
    return {"exit_stats": stats.to_json(), "rules": rules(root, day, rule_rows),
            "day": day, "generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "edges": list(EDGES),
            "policy_size": POLICY_SIZE,
            "coverage": {"arbs": len(arbs), "matched": len(match)},
            "calibration": cal_rows,
            "policy": [{"group": g, "live": lv, **{k: (v if k != "edges" else [round(x, 2) for x in v])
                                                    for k, v in p.items()}}
                       for (g, lv), p in sorted(policy.items(), key=lambda kv: -kv[1]["prints"])],
            "markout": mk_rows}


def family_roots(root: str) -> list[str]:
    """`root` and the other ARBS processes' folders beside it."""
    from kalshi_strategies.ARBS import SERIES
    out = [root]
    for s in SERIES:
        p = os.path.join(os.path.dirname(root), s)
        if os.path.normpath(p) != os.path.normpath(root) and os.path.isdir(p):
            out.append(p)
    return out


def training_stats(root: str, day: str) -> tuple[exit_cost.Stats, list[str]]:
    """Every ARBS process's saved statistics from up to TRAIN_DAYS days before `day`."""
    prev, used = exit_cost.Stats(), []
    for r in family_roots(root):
        d = os.path.join(r, OUT_DIR)
        days = sorted((f[:-5] for f in os.listdir(d) if f.endswith(".json") and f[:-5] < day)
                      if os.path.isdir(d) else [])[-TRAIN_DAYS:]
        for dd in days:
            try:
                with open(os.path.join(d, f"{dd}.json"), encoding="utf-8") as fh:
                    st = json.load(fh).get("exit_stats")
            except (OSError, ValueError):
                continue
            if st and st.get("n"):
                prev.merge(exit_cost.Stats.from_json(st))
                used.append(f"{os.path.basename(r)}:{dd}")
    return prev, used


def rules(root: str, day: str, rows: list) -> dict:
    """The exit-cost rules on this day's filled prints, fitted on earlier
    days only. rows: (live, {size: (features, replay pnl per edge)})."""
    prev, used = training_stats(root, day)
    sizes = sorted({n for _, by in rows for n in by})
    out = {"trained_on": sorted(used), "prints": len(rows), "fixed": [0.0] * len(EDGES), "narrowest": None,
           "best_ev": None, "best_ev_quoted": None, "best_ev_edges": None,
           "fixed_by_size": {str(int(n)): [0.0] * len(EDGES) for n in sizes}, "sizes": [int(n) for n in sizes],
           "best_ev_size": None, "best_ev_size_picks": None, "best_ev_size_lots": None}
    for _, by in rows:
        out["fixed"] = [a + b for a, b in zip(out["fixed"], by[POLICY_SIZE][1])]
        for n, (_, pnl) in by.items():
            k = str(int(n))
            out["fixed_by_size"][k] = [a + b for a, b in zip(out["fixed_by_size"][k], pnl)]
    out["fixed"] = [round(v, 2) for v in out["fixed"]]
    out["fixed_by_size"] = {k: [round(v, 2) for v in vs] for k, vs in out["fixed_by_size"].items()}
    if not used or not rows:
        return out
    w = prev.fit()
    narrow = ev = ev_size = lots = 0.0
    quoted, picked = 0, [0] * len(EDGES)
    size_picks = collections.Counter()
    for live, by in rows:
        f, pnl = by[POLICY_SIZE]
        nets, evs = [], []
        for e in EDGES:
            x = exit_cost.quote_price(f, e)
            net = 100 * e - exit_cost.predict(w, exit_cost.at_price(f, x))
            nets.append(net)
            evs.append(prev.fill_prob(live, 100 * (f["touch"] - x)) * net)
        k = next((k for k, v in enumerate(nets) if v >= RULE_MARGIN_C), None)
        narrow += pnl[k] if k is not None else 0.0
        k = int(np.argmax(evs))
        if evs[k] > 0:
            ev += pnl[k]
            quoted += 1
            picked[k] += 1
        best = None
        for n, (fn, pn) in by.items():
            for k, e in enumerate(EDGES):
                x = exit_cost.quote_price(fn, e)
                v = prev.fill_prob(live, 100 * (fn["touch"] - x)) * n * \
                    (100 * e - exit_cost.predict(w, exit_cost.at_price(fn, x)))
                if best is None or v > best[0]:
                    best = (v, n, k, pn[k])
        if best is not None and best[0] > 0:
            ev_size += best[3]
            lots += best[1]
            size_picks[str(int(best[1]))] += 1
    out.update(narrowest=round(narrow, 2), best_ev=round(ev, 2), best_ev_quoted=round(quoted / len(rows), 3),
               best_ev_edges=picked, best_ev_size=round(ev_size, 2), best_ev_size_picks=dict(size_picks),
               best_ev_size_lots=lots)
    return out


def _c(usd: float, lots: float) -> float | None:
    return round(100 * usd / lots, 2) if lots > 1e-9 else None


def _r(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    day = ap.parse_args().day
    from kalshi_strategies.ARBS import SERIES
    groups = series_groups()
    for s in SERIES:
        root = os.path.join(LIVE_ROOT, s)
        if not os.path.exists(os.path.join(root, "journal", f"{day}.jsonl")):
            continue
        doc = run(day, root, groups)
        os.makedirs(os.path.join(root, OUT_DIR), exist_ok=True)
        atomic_write_json(os.path.join(root, OUT_DIR, f"{day}.json"), doc, indent=None)
        print(json.dumps({"series": s, "day": day, "coverage": doc["coverage"], "groups": len(doc["calibration"])}))


if __name__ == "__main__":
    main()
