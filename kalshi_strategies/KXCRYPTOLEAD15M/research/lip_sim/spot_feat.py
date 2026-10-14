"""100ms spot grid per coin. Per venue: last trade price (ffill, stale >3s -> NaN),
trailing 0.5s/1s/2s/5s log returns in bp. Per coin: MEDIAN return across venues
(one venue's bad print or bid-ask bounce can't trigger it), and its z vs the
trailing-15min sd of the same median series. Also 1s print notional (all venues).
Output spot_grid.parquet."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import COINS, D, DATA  # noqa: E402

sp = pd.read_parquet(os.path.join(DATA, "spot_prints.parquet"))
pp = pd.read_parquet(os.path.join(DATA, "perp_prints.parquet"))
x = pd.concat([sp, pp], ignore_index=True)
b0 = int(pd.Timestamp("2026-10-11 22:00", tz="UTC").timestamp() * 10)
b1 = int(pd.Timestamp("2026-10-14 08:30", tz="UTC").timestamp() * 10)
x = x[(x.b >= b0) & (x.b < b1)]
n = b1 - b0
out = {"b": np.arange(b0, b1, dtype=np.int64)}
VENUES = ["coinbase", "binance_perp", "okx_perp", "bybit_perp", "hl_perp"]
W = {"05": 5, "1": 10, "2": 20, "5": 50}
for coin in COINS:
    R = {k: [] for k in W}
    usd = np.zeros(n)
    for v in VENUES:
        g = x[(x.venue == v) & (x.coin == coin)]
        if len(g) < 1000:
            continue
        idx = (g.b - b0).to_numpy()
        px = np.full(n, np.nan)
        px[idx] = g.px.to_numpy()
        last = np.full(n, np.nan)
        last[idx] = idx
        last = pd.Series(last).ffill().to_numpy()
        px = pd.Series(px).ffill().to_numpy()
        px[(np.arange(n) - last) > 30] = np.nan
        lp = np.log(px) * 1e4
        u = np.zeros(n)
        u[idx] = g.usd.to_numpy()
        usd += u
        for k, w in W.items():
            r = np.full(n, np.nan)
            r[w:] = lp[w:] - lp[:-w]
            R[k].append(r)
    for k in W:
        with np.errstate(all="ignore"):
            med = np.nanmedian(np.vstack(R[k]), axis=0)
        sd = pd.Series(med).rolling(9000, min_periods=3000).std().shift(1).to_numpy()
        out[f"{coin}_r{k}"] = med.astype(np.float32)
        out[f"{coin}_z{k}"] = (np.abs(med) / np.maximum(sd, 0.05)).astype(np.float32)
    out[f"{coin}_usd1"] = pd.Series(usd).rolling(10, min_periods=1).sum().to_numpy().astype(np.float32)
    print(coin, flush=True)
df = pd.DataFrame(out)
df.to_parquet(os.path.join(D, "spot_grid.parquet"), index=False)
print(df[[c for c in df.columns if "_z" in c or "_r" in c]].describe(percentiles=[.5, .9, .99, .999]).T.round(2).to_string())
