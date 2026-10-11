"""
Sparse-cancel budget: replay a day's decision journal through the REAL
OrderManager (ladder of quote_levels_behind+1 ticks from the top, every
level acked instantly) and count the cancels normal quoting needs per second
-- edge moved through a level -- across all quoted sides, for several
quote_keep_inside values ($). Pulls (stale/blocked sides) are counted
separately: those get the whole bucket and aren't what the reserve is for.

    python -m kalshi_infra.research.cancel_reserve [--date 2026-10-10] [--levels 4]

Approximations: every create/move is assumed to land at once (no in-flight
time), and the top is taken from the logged quote_bid/quote_ask (the old
quoter's top), so ladders follow the same touch the session saw.
"""
from __future__ import annotations

import argparse
import collections

import numpy as np
import pandas as pd

from kalshi_infra.runtime.journal import read_journal
from kalshi_infra.trading import order_manager as om

TICK = 0.01


class _Ex:
    def __init__(self):
        self.n = 0
        self.cancels = []

    def next_order_id(self):
        self.n += 1
        return self.n

    def create(self, q):
        return True

    def amend(self, q, price, remaining):
        return True

    def cancel(self, q, reason):
        self.cancels.append(reason)

    def bucket_tokens(self):
        return None


def _edge(res, side):
    if pd.isna(res):
        return None
    return (round(np.floor(res / TICK + 1e-9) * TICK, 2) if side == "yes"
            else round(1.0 - np.ceil(res / TICK - 1e-9) * TICK, 2))


def replay(d: pd.DataFrame, levels: int, keep: float) -> dict:
    clock = {"t": 0.0}
    ex = _Ex()
    m = om.OrderManager(ex, cancels_token=0.0, keep_inside=keep, clock=lambda: clock["t"])
    per_sec_sparse = collections.Counter()
    per_sec_pull = collections.Counter()
    for r in d.itertuples(index=False):
        clock["t"] = r.t
        for side, top_col, res_col, blk_col in (("yes", "quote_bid", "quote_bid_reservation", "block_bid"),
                                                ("no", "quote_ask", "quote_ask_reservation", "block_ask")):
            edge = _edge(getattr(r, res_col), side)
            top = getattr(r, top_col)
            blocked = isinstance(getattr(r, blk_col), str)
            before = len(ex.cancels)
            if edge is None or blocked:
                m.set_desired(r.ticker, side, None)
                per_sec_pull[int(r.t)] += len(ex.cancels) - before
                continue
            if pd.isna(top):
                prices = []
            else:
                top = round(top if side == "yes" else 1.0 - top, 2)
                prices = [round(top - i * TICK, 2) for i in range(levels)]
            m.set_desired(r.ticker, side, om.Desired(prices, 1.0, edge, float(levels)))
            per_sec_sparse[int(r.t)] += len(ex.cancels) - before
        m.pump()
        for q in list(m.quotes.values()):
            if q.state == om.CREATING:
                m.on_created(q.order_id, f"x{q.order_id}")
            elif q.state == om.AMENDING:
                m.on_amended(q.order_id, ok=True)
            elif q.state == om.CANCELLING:
                m.on_cancel_done(q.order_id)
    t0, t1 = int(d.t.min()), int(d.t.max())
    secs = np.array([per_sec_sparse.get(s, 0) for s in range(t0, t1 + 1)])
    return {"seconds": len(secs), "mean": secs.mean(),
            **{f"p{q}": float(np.quantile(secs, q / 100)) for q in (50, 90, 99, 99.9)},
            "max": int(secs.max()), "creates": m.counts.get("create", 0), "moves": m.counts.get("move", 0),
            "sparse_total": int(secs.sum()), "pull_total": int(sum(per_sec_pull.values()))}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXCRYPTOLEAD15M")
    ap.add_argument("--date", default="2026-10-10")
    ap.add_argument("--levels", type=int, default=4)
    ap.add_argument("--keep", default="0,0.005,0.01,0.02")
    a = ap.parse_args(argv)
    d = read_journal(a.series, a.date, "decisions")
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.sort_values("time", kind="stable")
    d["t"] = d.time.astype("int64") / 1e9
    d["event"] = d.ticker.str.rsplit("-", n=1).str[0]
    rows = []
    for k in [float(x) for x in a.keep.split(",")]:
        agg = collections.defaultdict(list)
        for _, g in d.groupby("event", sort=False):
            r = replay(g, a.levels, k)
            for kk, v in r.items():
                agg[kk].append(v)
        n = sum(agg["seconds"])
        rows.append({"keep_inside": k, "seconds": n,
                     "sparse/s mean": round(sum(agg["sparse_total"]) / n, 2),
                     "p50": np.median(agg["p50"]), "p90": max(agg["p90"]), "p99": max(agg["p99"]),
                     "p99.9": max(agg["p99.9"]), "max": max(agg["max"]),
                     "creates/s": round(sum(agg["creates"]) / n, 2), "moves/s": round(sum(agg["moves"]) / n, 2),
                     "pull cancels": sum(agg["pull_total"])})
    out = pd.DataFrame(rows)
    print(f"{a.series} {a.date}: {a.levels} levels/side, {d.ticker.nunique()} markets")
    print(out.to_string(index=False))
    print("\ntokens/s needed for sparse cancels = 2 x the per-second count; "
          "p90/p99/p99.9/max are the WORST event's quantiles.")


if __name__ == "__main__":
    main()
