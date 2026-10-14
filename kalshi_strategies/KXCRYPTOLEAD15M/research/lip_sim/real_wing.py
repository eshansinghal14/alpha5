"""Independent check on REAL maker fills (edge quotes, 2026-10-11..14): markout and
settled PnL per lot by own-side price bucket and time left. If the sim's wing
pattern is real, our own fills should show it too."""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, DATA, MKT, close_of  # noqa: E402

pd.set_option("display.width", 250)
R = pd.concat([pd.read_parquet(p) for p in glob.glob(os.path.join(DATA, "fills_*.parquet"))])
R = R[(R["mode"] == "real") & ~R.seeded.fillna(False).astype(bool)].copy().reset_index(drop=True)
R["time"] = pd.to_datetime(R.time, utc=True)
R["t"] = R.time.astype("int64") / 1e9
R["ev"] = R.ticker.str.rsplit("-", n=1).str[0]
res = {}
for p in glob.glob(os.path.join(MKT, "*.json")):
    for m in json.load(open(p)):
        res[m["ticker"]] = m.get("result")
R["result"] = R.ticker.map(res)
R["yes_px"] = np.where(R.side == "yes", R.price, 1 - R.price)
d = np.where(R.side == "yes", 1.0, -1.0)
R["settled_pl"] = np.where(R.result.isin(["yes", "no"]), d * ((R.result == "yes").astype(float) - R.yes_px), np.nan)
R["tau"] = [(close_of(e) - t).total_seconds() for e, t in zip(R.ev, R.time)]
mo = np.full(len(R), np.nan)
for ev, idx in R.groupby("ev").groups.items():
    p = os.path.join(D, "ev", f"{ev}_bbo.parquet")
    if not os.path.exists(p):
        continue
    b = pd.read_parquet(p)
    b["t"] = b.time.astype("int64") / 1e9
    b["mid"] = (b.bid + b.ask) / 2
    for i in idx:
        r = R.loc[i]
        bb = b[b.ticker == r.ticker]
        k = np.searchsorted(bb.t.to_numpy(), r.t + 10, side="right") - 1
        if k >= 0:
            mo[R.index.get_loc(i)] = (1 if r.side == "yes" else -1) * (bb.mid.iloc[k] - r.yes_px)
R = R.reset_index(drop=True)
R["mo10_pl"] = mo
for liq in ("maker", "taker"):
    x = R[R.liquidity == liq]
    print(f"\n==== REAL {liq} fills: {len(x)}, lots {x['size'].sum():.0f}")
    g = x.groupby(pd.cut(x.price, [0, .1, .2, .35, .5, .65, .8, .9, 1.0]), observed=True)
    t = g.apply(lambda s: pd.Series(dict(fills=len(s), lots=s["size"].sum(),
                                         mo10_c=100 * np.nansum(s.mo10_pl * s["size"]) / s["size"].sum(),
                                         settled_c=100 * np.nansum(s.settled_pl * s["size"]) / s["size"].sum())))
    print("by own-side price (c per lot):"); print(t.round(2).to_string())
    g = x.groupby(pd.cut(x.tau, [0, 60, 120, 180, 300, 600, 900]), observed=True)
    t = g.apply(lambda s: pd.Series(dict(fills=len(s), lots=s["size"].sum(),
                                         mo10_c=100 * np.nansum(s.mo10_pl * s["size"]) / s["size"].sum(),
                                         settled_c=100 * np.nansum(s.settled_pl * s["size"]) / s["size"].sum())))
    print("by time left:"); print(t.round(2).to_string())
