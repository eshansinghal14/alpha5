"""Summarise a sim run: per policy LIP paid, fills, markouts, settled, net; t-stat of net per event."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 200)
SET = sys.argv[1]
P = pd.read_parquet(os.path.join(D, f"sim_{SET}_periods.parquet"))
F = pd.read_parquet(os.path.join(D, f"sim_{SET}_fills.parquet"))
nev = P.ev.nunique()


def summ(P, F):
    rows = []
    for pol, p in P.groupby("policy", sort=False):
        f = F[F.policy == pol]
        ev_lip = p.groupby("ev").paid.sum()
        ev_set = f.groupby("ev").settled.sum().reindex(ev_lip.index, fill_value=0.0)
        ev_mo = f.groupby("ev").mo10.sum().reindex(ev_lip.index, fill_value=0.0)
        net_s = ev_lip + ev_set
        net_m = ev_lip + ev_mo
        rows.append(dict(policy=pol, lip_paid=p.paid.sum(), lip_gross=p.gross.sum(),
                         pct_paid=(p.paid > 0).mean(), fills=len(f), lots=f["size"].sum(),
                         mo1=f.mo1.sum(), mo10=f.mo10.sum(), mo60=f.mo60.sum(), settled=f.settled.sum(),
                         net_mo10=net_m.sum(), net_settled=net_s.sum(),
                         t_net_mo10=net_m.mean() / (net_m.std(ddof=1) / np.sqrt(len(net_m))),
                         worst_ev=net_s.min(),
                         c_per_lot_mo10=100 * f.mo10.sum() / max(f["size"].sum(), 1)))
    return pd.DataFrame(rows).set_index("policy")


S = summ(P, F)
print(f"{nev} events, {P.ticker.nunique()} market-periods")
print(S.round(2).to_string())
