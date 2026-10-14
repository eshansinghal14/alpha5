"""Who got filled when the far side crossed our resting price? For each sim cross
event (dec0, provably resting 0.5s before), the Kalshi prints within 2s on that
market: did anything print at/through our price on our side? Print-based fills
already capture that; a cross with NO such print is a book event that never
traded (an order that was pulled before matching, or a snapshot artefact)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, load_prints  # noqa: E402

F = pd.read_parquet(os.path.join(D, "sim_calib_fills.parquet"))
f = F[(F.policy == "dec0 cross") & (F.how == "cross")]
rows = []
for ev, g in f.groupby("ev"):
    tr = load_prints(ev)
    if not len(tr):
        continue
    tr["t"] = tr.time.astype("int64") / 1e9
    for r in g.itertuples():
        x = tr[(tr.ticker == r.ticker) & ((tr.t - r.t).abs() <= 2.0)]
        if r.side == "yes":
            x = x[x.taker_side == "no"]
            hit = x[x.yes_price <= r.yes_px + 1e-9]
        else:
            x = x[x.taker_side == "yes"]
            hit = x[x.yes_price >= r.yes_px - 1e-9]
        rows.append(dict(any_print_side=len(x) > 0, print_at_or_through=len(hit) > 0, lots=hit["count"].sum()))
x = pd.DataFrame(rows)
print(len(x), "cross events")
print(x.mean(numeric_only=True).round(3))
