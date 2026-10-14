"""Fill-model calibration: our real maker fills vs the sim's replay of the same quotes.
Real fills are fractional lots (0.5-1.5); compare counts of fill EVENTS and their
10s markout per lot, by sim fill type."""
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, DATA  # noqa: E402

F = pd.read_parquet(os.path.join(D, "sim_calib_fills.parquet"))
evs = set(F.ev.unique())
R = pd.concat([pd.read_parquet(p) for p in glob.glob(os.path.join(DATA, "fills_*.parquet"))])
R = R[(R["mode"] == "real") & (R.liquidity == "maker") & ~R.seeded.fillna(False).astype(bool)]
R["time"] = pd.to_datetime(R.time, utc=True)
R["ev"] = R.ticker.str.rsplit("-", n=1).str[0]
R = R[R.ev.isin(evs)]
R["t"] = R.time.astype("int64") / 1e9
R["yes_px"] = np.where(R.side == "yes", R.price, 1 - R.price)
# real markout from bbo journal
mo = []
for ev, g in R.groupby("ev"):
    b = pd.read_parquet(os.path.join(D, "ev", f"{ev}_bbo.parquet"))
    b["t"] = b.time.astype("int64") / 1e9
    b["mid"] = (b.bid + b.ask) / 2
    for r in g.itertuples():
        bb = b[b.ticker == r.ticker]
        k = np.searchsorted(bb.t.to_numpy(), r.t + 10, side="right") - 1
        m = bb.mid.iloc[k] if k >= 0 else np.nan
        d = 1 if r.side == "yes" else -1
        mo.append(d * (m - r.yes_px))
R["mo10_per_lot"] = mo
# Treat fills of the same order within 1s as one fill event
R = R.sort_values("t")
R["grp"] = (R.groupby(["ticker", "side"]).t.diff().fillna(99) > 1.0).cumsum()
RE = R.groupby("grp").agg(ev=("ev", "first"), ticker=("ticker", "first"), side=("side", "first"), t=("t", "first"),
                          lots=("size", "sum"), mo=("mo10_per_lot", "mean"))
print(f"REAL maker fill events: {len(RE)} over {RE.ev.nunique()} events, lots {RE.lots.sum():.0f}, "
      f"10s markout per lot {RE.mo.mean() * 100:.2f}c (median {RE.mo.median() * 100:.1f}c)")
for pol, f in F.groupby("policy"):
    f = f.sort_values("t").copy()
    f["grp"] = (f.groupby(["ticker", "side"]).t.diff().fillna(99) > 1.0).cumsum()
    fe = f.groupby("grp").agg(ev=("ev", "first"), how=("how", "first"), lots=("size", "sum"),
                              mo=("mo10", "sum"))
    fe["mo_pl"] = fe.mo / fe.lots
    print(f"\nSIM {pol}: fill events {len(fe)}; 10s markout/lot {fe.mo_pl.mean() * 100:.2f}c")
    print(fe.groupby("how").agg(n=("lots", "size"), mo_c=("mo_pl", lambda s: 100 * s.mean())).round(2).to_string())

# match: real fill events vs sim dec0 fills within 2s same ticker/side
f0 = F[F.policy == "dec0 cross"]
f1 = F[F.policy == "dec1 cross"]
fa = pd.concat([f0, f1])
hit = []
for r in RE.itertuples():
    s = fa[(fa.ticker == r.ticker) & (fa.side == r.side) & ((fa.t - r.t).abs() <= 2.0)]
    hit.append(s.how.iloc[0] if len(s) else None)
RE["sim_match"] = hit
print("\nreal fill events matched by a sim fill within 2s:", round(RE.sim_match.notna().mean(), 3))
print(RE.groupby(RE.sim_match.fillna("unmatched")).agg(n=("lots", "size"), mo_c=("mo", lambda s: 100 * s.mean())).round(2))

print("\nfraction of SIM fill events (dec0) with a REAL fill on same ticker/side within 2s:")
f = F[F.policy == "dec0 cross"].sort_values("t").copy()
f["grp"] = (f.groupby(["ticker", "side"]).t.diff().fillna(99) > 1.0).cumsum()
fe = f.groupby("grp").agg(ticker=("ticker", "first"), side=("side", "first"), t=("t", "first"), how=("how", "first"),
                          lots=("size", "sum"), mo=("mo10", "sum"))
m = []
for r in fe.itertuples():
    s = RE[(RE.ticker == r.ticker) & (RE.side == r.side) & ((RE.t - r.t).abs() <= 2.0)]
    m.append(len(s) > 0)
fe["real"] = m
print(fe.groupby("how").agg(n=("real", "size"), realized=("real", "mean"),
                            mo_c=("mo", lambda s: 100 * s.mean())).round(3).to_string())
print(fe.groupby(["how", "real"]).mo.mean().mul(100).round(2).to_string())
