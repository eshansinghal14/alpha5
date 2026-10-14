"""Robustness of the leading rules: split by day / half, by coin, worst events,
collateral. Also an honest out-of-sample bucket test: choose the profitable price
buckets on the FIRST half of events from the unrestricted run, score the rule on
the SECOND half (and vice versa)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, close_of  # noqa: E402

pd.set_option("display.width", 250)


def per_event(P, F, pol):
    p = P[P.policy == pol]
    f = F[F.policy == pol]
    e = p.groupby("ev").agg(lip=("paid", "sum"), col=("max_col", "max"))
    e["mo10"] = f.groupby("ev").mo10.sum().reindex(e.index, fill_value=0)
    e["settled"] = f.groupby("ev").settled.sum().reindex(e.index, fill_value=0)
    e["lots"] = f.groupby("ev")["size"].sum().reindex(e.index, fill_value=0)
    e["net_mo10"] = e.lip + e.mo10
    e["net_set"] = e.lip + e.settled
    e["close"] = [close_of(x) for x in e.index]
    return e.sort_values("close")


P = pd.read_parquet(os.path.join(D, "sim_v4_periods.parquet"))
F = pd.read_parquet(os.path.join(D, "sim_v4_fills.parquet"))
for pol in ("best N200 band[1-20+80-99] t180+swp", "best N300 band[1-20+80-99] t180+swp"):
    for fm in ("", " |full", " |x20"):
        e = per_event(P, F, pol + fm)
        e["day"] = e.close.dt.strftime("%m-%d")
        print(f"\n=== {pol}{fm}")
        d = e.groupby("day").agg(events=("lip", "size"), lip=("lip", "sum"), mo10=("mo10", "sum"),
                                 settled=("settled", "sum"), net_mo10=("net_mo10", "sum"), net_set=("net_set", "sum"))
        print(d.round(0).to_string())
        h = len(e) // 2
        for nm, x in (("1st half", e.iloc[:h]), ("2nd half", e.iloc[h:])):
            t = x.net_mo10.mean() / (x.net_mo10.std() / np.sqrt(len(x)))
            print(f"  {nm}: net_mo10 ${x.net_mo10.sum():.0f} (t {t:.1f}), net_settled ${x.net_set.sum():.0f}")
        print(f"  per event: LIP ${e.lip.mean():.2f}, net_mo10 ${e.net_mo10.mean():.2f}, net_settled ${e.net_set.mean():.2f};"
              f" worst event net_settled ${e.net_set.min():.0f}, p5 ${e.net_set.quantile(.05):.0f};"
              f" max collateral ${e.col.max():.0f}, p95 ${e.col.quantile(.95):.0f}, median ${e.col.median():.0f}")
        if fm == "":
            f = F[F.policy == pol]
            f = f.assign(coin=f.ticker.str.rsplit("-", n=1).str[-1], wing=np.where(f.price <= 0.5, "cheap", "rich"))
            print(f.groupby("coin")[["size", "mo10", "settled"]].sum().round(0).to_string())
            print(f.groupby("wing")[["size", "mo10", "settled"]].sum().round(0).to_string())
            print("worst 5 events:")
            print(e.nsmallest(5, "net_set")[["lip", "mo10", "settled", "net_set", "lots"]].round(1).to_string())

# ---- out-of-sample bucket choice
A = pd.read_parquet(os.path.join(D, "sim_bucket_acc.parquet"))
FB = pd.read_parquet(os.path.join(D, "sim_bucket_fills.parquet"))
evs = sorted(A.ev.unique(), key=close_of)
h = len(evs) // 2
halves = {"A": set(evs[:h]), "B": set(evs[h:])}
PB = np.round(np.arange(0, 1.0001, 0.05), 2)
print("\n=== out-of-sample price-bucket choice (unrestricted best N200 swp, tau>=180s; 5c buckets)")
for fm in ("", " |full", " |x20"):
    pol = "best N200 swp" + fm
    a = A[(A.policy == pol) & (A.tau_min >= 3)].copy()
    f = FB[(FB.policy == pol) & (FB.tau >= 180)].copy()
    a["pb"] = pd.cut(a.price, PB)
    f["pb"] = pd.cut(f.price, PB)
    for train, test in (("A", "B"), ("B", "A")):
        def tab(evset):
            l = a[a.ev.isin(evset)].groupby("pb", observed=False).lip_gross.sum()
            m = f[f.ev.isin(evset)].groupby("pb", observed=False).mo10.sum()
            return (l + m.reindex(l.index, fill_value=0)).fillna(0)
        tr, te = tab(halves[train]), tab(halves[test])
        keep = tr[tr > 0].index
        print(f"  {fm or 'prints':6s} train {train} -> test {test}: keep {len(keep)}/20 buckets "
              f"{[f'{iv.left:.2f}' for iv in keep]}; test net_mo10 (gross LIP) ${te[keep].sum():.0f} "
              f"vs all buckets ${te.sum():.0f}")
