"""Split the live bbo / decision journals and spot prints into per-event files."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, DATA, events  # noqa: E402

OUT = os.path.join(D, "ev")
os.makedirs(OUT, exist_ok=True)
evs = events()
bbo = pd.concat([pd.read_parquet(os.path.join(DATA, f"bbo_2026-10-1{d}.parquet"),
                                 columns=["time", "ticker", "bid", "ask", "bid_depth", "ask_depth"]) for d in (1, 2, 3, 4)])
bbo["time"] = pd.to_datetime(bbo.time, utc=True)
bbo["ev"] = bbo.ticker.str.rsplit("-", n=1).str[0]
for ev, g in bbo.groupby("ev"):
    g.drop(columns="ev").sort_values("time").to_parquet(os.path.join(OUT, f"{ev}_bbo.parquet"), index=False)
print("bbo split", bbo.ev.nunique(), flush=True)
del bbo
cols = ["time", "ticker", "market_bid", "market_ask", "prob", "prob_lo", "prob_hi", "quote_bid_reservation",
        "quote_ask_reservation", "quote_bid", "quote_ask", "n_levels_bid", "n_levels_ask", "block_bid", "block_ask", "x_tau", "x_theo_vol",
        "net_position", "constant_edge"]
for d in (1, 2, 3, 4):
    x = pd.read_parquet(os.path.join(DATA, f"dec_2026-10-1{d}.parquet"), columns=cols)
    x["time"] = pd.to_datetime(x.time, utc=True)
    x["ev"] = x.ticker.str.rsplit("-", n=1).str[0]
    for ev, g in x.groupby("ev"):
        p = os.path.join(OUT, f"{ev}_dec.parquet")
        g = g.drop(columns="ev")
        if os.path.exists(p):
            g = pd.concat([pd.read_parquet(p), g])
        g.sort_values("time").to_parquet(p, index=False)
    print("dec", d, flush=True)
