"""Per-event slices of spot_grid (event window -60s .. close+5s)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, events  # noqa: E402

g = pd.read_parquet(os.path.join(D, "spot_grid.parquet"))
b = g.b.to_numpy()
OUT = os.path.join(D, "ev")
n = 0
for ev, close in events():
    c = int(close.timestamp() * 10)
    lo, hi = c - 9600, c + 50
    i0, i1 = np.searchsorted(b, lo), np.searchsorted(b, hi)
    if i1 - i0 < 9000:
        continue
    g.iloc[i0:i1].to_parquet(os.path.join(OUT, f"{ev}_spot.parquet"), index=False)
    n += 1
print("spot slices", n)
