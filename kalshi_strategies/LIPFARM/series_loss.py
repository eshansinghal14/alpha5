"""Adverse-sweep loss per trade by series and by distance from 50c, from the tape.

`build` turns per-series trade tapes (series_collect.py's pickles: {ticker: (meta,
[(ts, taker_side, yes_price, count)])}) into series_loss.json, which farm.loss_mult
reads. Strikes and weeks are pooled by distance from 50c, not by name: "T205" is a
different bet each week. Run locally (bulk reads stay off the box), then copy the
file to data/live/LIPFARM/ on the box.

    python -m kalshi_strategies.LIPFARM.series_loss TAPE_DIR OUT_JSON
"""
from __future__ import annotations

import bisect
import collections
import datetime
import glob
import json
import os
import pickle
import sys
import time

# Distance of a price from 50c, as bucket upper bounds.
BUCKETS = (0.10, 0.25, 0.40, 0.51)
MIN_LOTS = 50                 # a sweep: 50+ lots sold into one side within 1 s, at 2+ prices
MAX_TRADES_DAY = 50.0         # markets the farm could quote
PRIOR_TRADES = 300.0          # shrink each series toward its bucket's all-market rate
MIN_CLOSE_S = 2 * 86400       # the farm's min_close_h: trades nearer close aren't ones we'd be quoting into
TICK = 0.01


def bucket(price: float) -> int:
    d = abs(price - 0.5)
    for i, hi in enumerate(BUCKETS):
        if d < hi:
            return i
    return len(BUCKETS) - 1


def adverse_losses(trades: list[tuple]) -> list[tuple[float, float]]:
    """(price the sweep started at, its loss to a 1000-lot bid there) per adverse sweep.
    Adverse: no sell into that side's bids within a tick of the start in 30 min, and
    the last one by 2 h at least 2 ticks under it (the definitions loss_per_trade was
    fitted with)."""
    tr = sorted(trades)
    out = []
    for side in ("yes", "no"):
        hit = "no" if side == "yes" else "yes"                  # a taker buying no sells into yes bids
        idx = [i for i, x in enumerate(tr) if x[1] == hit]
        if len(idx) < 2:
            continue
        px = [tr[i][2] if side == "yes" else 1.0 - tr[i][2] for i in idx]
        tm = [tr[i][0] for i in idx]
        end = tr[-1][0]
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and tm[j + 1] - tm[i] <= 1.0:
                j += 1
            seg = px[i:j + 1]
            lots = sum(tr[idx[q]][3] for q in range(i, j + 1))
            if max(seg) - min(seg) > 1e-9 and lots >= MIN_LOTS and tm[j] + 7200 <= end:
                p0 = max(seg)
                k30 = bisect.bisect_right(tm, tm[j] + 1800)
                back = any(p >= p0 - TICK - 1e-9 for p in px[j + 1:k30])
                k2 = bisect.bisect_right(tm, tm[j] + 7200) - 1
                p2 = px[k2] if k2 > j else min(seg)
                if not back and p2 <= p0 - 2 * TICK + 1e-9:
                    out.append((p0, min(lots, 1000.0) * (p0 - p2)))
            i = j + 1
    return out


def _close_ts(meta: dict) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(meta["close_time"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def build(tape_dir: str) -> dict:
    """Series multipliers are relative to their own bucket's all-market rate: the
    bucket's own level stays in loss_per_trade (fitted without buckets), so a series
    with no tape, or one that behaves like the rest, is 1."""
    loss = collections.defaultdict(lambda: [0.0] * len(BUCKETS))
    trades = collections.defaultdict(lambda: [0.0] * len(BUCKETS))
    for path in glob.glob(os.path.join(tape_dir, "*.pkl")):
        s = os.path.basename(path)[:-4]
        for _, (meta, tr) in pickle.load(open(path, "rb")).items():
            close = _close_ts(meta)
            if close is not None:
                tr = [x for x in tr if x[0] <= close - MIN_CLOSE_S]
            if len(tr) < 2:
                continue
            days = max((max(x[0] for x in tr) - min(x[0] for x in tr)) / 86400, 1.0)
            if len(tr) / days > MAX_TRADES_DAY:
                continue
            for x in tr:
                trades[s][bucket(x[2])] += 1
            for p0, lo in adverse_losses(tr):
                loss[s][bucket(p0)] += lo
    g_loss = [sum(loss[s][b] for s in loss) for b in range(len(BUCKETS))]
    g_tr = [sum(trades[s][b] for s in trades) for b in range(len(BUCKETS))]
    g_rate = [g_loss[b] / max(g_tr[b], 1.0) for b in range(len(BUCKETS))]
    overall = sum(g_loss) / max(sum(g_tr), 1.0)
    series = {}
    for s in trades:
        series[s] = [round((loss[s][b] + PRIOR_TRADES * g_rate[b]) / (trades[s][b] + PRIOR_TRADES)
                           / max(g_rate[b], 1e-9), 3) for b in range(len(BUCKETS))]
    return {"built_at": time.time(), "buckets": list(BUCKETS), "overall_usd_per_trade": overall,
            "bucket_usd_per_trade": [round(r, 4) for r in g_rate],
            "bucket_mult": [1.0] * len(BUCKETS), "series": series,
            "trades": {s: [int(x) for x in v] for s, v in trades.items()}}


if __name__ == "__main__":
    tab = build(sys.argv[1])
    with open(sys.argv[2], "w") as f:
        json.dump(tab, f)
    print(f"{len(tab['series'])} series; overall ${tab['overall_usd_per_trade']:.2f} per trade; "
          f"$/trade by bucket {tab['bucket_usd_per_trade']}")
