"""Run a set of LIP policies over every event. Usage: run_sim.py <policyset> [n_events]"""
import importlib
import os
import sys
import time
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, events  # noqa: E402
import sim  # noqa: E402

SET = sys.argv[1]
POL = importlib.import_module(f"pol_{SET}").POLICIES


def work(args):
    ev, close = args
    try:
        ed = sim.EventData(ev, close)
    except Exception as e:
        return ev, None, repr(e)
    if not ed.dec or ed.spot is None:
        return ev, None, "no dec/spot"
    out = {}
    for name, pol in POL.items():
        r = sim.simulate(ed, pol)
        if r is not None:
            out[name] = r
    return ev, out, None


if __name__ == "__main__":
    evs = [x for x in events() if os.path.exists(os.path.join(D, "ev", x[0] + "_spot.parquet"))
           and os.path.exists(os.path.join(D, "ev", x[0] + "_dec.parquet"))]
    if len(sys.argv) > 2:
        evs = evs[: int(sys.argv[2])]
    t0 = time.time()
    P, F, A, skipped = [], [], [], []
    with Pool(11) as p:
        for i, (ev, out, err) in enumerate(p.imap_unordered(work, evs)):
            if out is None:
                skipped.append((ev, err))
                continue
            for name, r in out.items():
                P += [dict(policy=name, **x) for x in r["periods"]]
                F += [dict(policy=name, **x) for x in r["fills"]]
                A += [dict(policy=name, **x) for x in r.get("acc", [])]
            if i % 25 == 0:
                print(i, f"{time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(P).to_parquet(os.path.join(D, f"sim_{SET}_periods.parquet"), index=False)
    pd.DataFrame(F).to_parquet(os.path.join(D, f"sim_{SET}_fills.parquet"), index=False)
    pd.DataFrame(A).to_parquet(os.path.join(D, f"sim_{SET}_acc.parquet"), index=False)
    print("skipped", len(skipped), skipped[:5])
    print("done", f"{time.time() - t0:.0f}s")
