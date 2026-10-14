"""LIP $ earned vs adverse $ lost, by bucket of (own-side price, time left), from the
unrestricted run. Ratio > 1 means that bucket pays for its own pickoffs. Gross LIP is
pre-floor (the $1 floor is applied per market-period later)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D  # noqa: E402

pd.set_option("display.width", 250)
A = pd.read_parquet(os.path.join(D, "sim_bucket_acc.parquet"))
F = pd.read_parquet(os.path.join(D, "sim_bucket_fills.parquet"))
PB = [0, .1, .2, .35, .5, .65, .8, .9, 1.0]
TB = [-1, 1, 2, 3, 5, 8, 11, 15]
TL = ["<1m", "1-2m", "2-3m", "3-5m", "5-8m", "8-11m", "11-15m"]
A["pb"] = pd.cut(A.price, PB)
A["tb"] = pd.cut(A.tau_min, TB, labels=TL)
F["pb"] = pd.cut(F.price, PB)
F["tb"] = pd.cut(F.tau / 60, [-1, 1, 2, 3, 5, 8, 11, 15], labels=TL)
for pol in ("best N200", "best N200 swp"):
    for fm in ("", " |full", " |x20"):
        p = pol + fm
        a = A[A.policy == pol]       # LIP accrual: same across fill models up to fills reducing size; use main
        a = A[A.policy == p]
        f = F[F.policy == p]
        print(f"\n######## {p}: LIP gross ${a.lip_gross.sum():.0f}, settled ${f.settled.sum():.0f}, mo10 ${f.mo10.sum():.0f}")
        lip = a.groupby("pb", observed=True).lip_gross.sum()
        st = f.groupby("pb", observed=True).agg(settled=("settled", "sum"), mo10=("mo10", "sum"), lots=("size", "sum"))
        t = pd.concat([lip, st], axis=1).fillna(0)
        t["net_set"] = t.lip_gross + t.settled
        t["net_mo10"] = t.lip_gross + t.mo10
        print("by own-side price:"); print(t.round(0).to_string())
        lip = a.groupby("tb", observed=True).lip_gross.sum()
        st = f.groupby("tb", observed=True).agg(settled=("settled", "sum"), mo10=("mo10", "sum"), lots=("size", "sum"))
        t = pd.concat([lip, st], axis=1).fillna(0)
        t["net_set"] = t.lip_gross + t.settled
        t["net_mo10"] = t.lip_gross + t.mo10
        print("by time left:"); print(t.round(0).to_string())
