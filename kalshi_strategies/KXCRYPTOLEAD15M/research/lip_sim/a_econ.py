"""A. LIP book economics, every market-second (last Predexon snapshot at or before
each second). Per side: own-side best, reference price, ticks ref behind best,
depth to target, competition score, and our share for N lots at candidate
levels (best, ref, ref-1, ref-2, best+1 if not crossing) under both scoring
rules. Output: a_econ.parquet (one row per market-second-side)."""
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, TICK, events, ladder, load_books, results, score_side  # noqa: E402

NS = [25, 50, 100, 200]


def side_rows(bids, far_own, ev, tk, t, side, tau, res):
    if not bids:
        return None
    best = max(bids)
    o0, a0, q, ref = score_side(bids)
    row = dict(ev=ev, ticker=tk, t=t, side=side, tau=tau, best=best, ref=ref, qual=q,
               depth=sum(bids.values()), best_sz=bids[best], far=far_own, res=res,
               comp=a0)
    if ref is not None:
        row["ref_gap"] = int(round((best - ref) / TICK))
    cands = {"best": best, "ref": ref,
             "ref1": None if ref is None else round(ref - TICK, 2),
             "ref2": None if ref is None else round(ref - 2 * TICK, 2),
             "imp": round(best + TICK, 2) if (far_own is None or best + TICK < far_own - 1e-9) else None}
    for lab, p in cands.items():
        for n in NS:
            if p is None or p < TICK - 1e-9:
                row[f"s_{lab}_{n}"] = 0.0
                row[f"sb_{lab}_{n}"] = 0.0
                continue
            o, a, qq, _ = score_side(bids, p, n, "whole")
            row[f"s_{lab}_{n}"] = o / a if qq and a > 0 else 0.0
            o, a, qq, _ = score_side(bids, p, n, "back")
            row[f"sb_{lab}_{n}"] = o / a if qq and a > 0 else 0.0
        # contracts ahead of us at that price (others' size at better prices + same price)
        row[f"ahead_{lab}"] = np.nan if p is None else sum(s for px, s in bids.items() if px >= p - 1e-9)
    return row


def run(args):
    ev, close = args
    try:
        ob = load_books(ev)
    except Exception:
        return []
    res = results(ev)
    out = []
    t0 = close - pd.Timedelta(seconds=900)
    # 3s grid: 40 scorings per side-snapshot is too slow every second, and the
    # book rarely changes the answer within 3s.
    grid = pd.date_range(t0 + pd.Timedelta(seconds=1), close - pd.Timedelta(seconds=1), freq="3s")
    for tk, g in ob.groupby("ticker"):
        g = g.drop_duplicates("time", keep="last").set_index("time").sort_index()
        idx = g.index.searchsorted(grid, side="right") - 1
        last = -2
        cache = None
        for t, k in zip(grid, idx):
            if k < 0:
                continue
            if k != last:
                r = g.iloc[k]
                yb, nb = ladder(r.yes_bids), ladder(r.yes_asks, flip=True)
                last = k
                cache = (yb, nb)
            yb, nb = cache
            tau = (close - t).total_seconds()
            fy = round(1 - max(nb), 2) if nb else None    # far touch for yes bids (yes ask), own terms
            fn = round(1 - max(yb), 2) if yb else None
            for side, b, far in (("yes", yb, fy), ("no", nb, fn)):
                row = side_rows(b, far, ev, tk, t, side, tau, res.get(tk))
                if row is not None:
                    out.append(row)
    return out


if __name__ == "__main__":
    evs = events()
    print(len(evs), "events", evs[0][0], evs[-1][0], flush=True)
    with Pool(11) as p:
        rows = []
        for i, r in enumerate(p.imap_unordered(run, evs, chunksize=2)):
            rows += r
            if i % 25 == 0:
                print(i, len(rows), flush=True)
    df = pd.DataFrame(rows)
    df.to_parquet(os.path.join(D, "a_econ.parquet"), index=False)
    print("rows", len(df))
