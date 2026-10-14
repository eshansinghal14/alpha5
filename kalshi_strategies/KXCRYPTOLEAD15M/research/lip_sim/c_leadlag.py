"""C. Lead-lag: Kalshi repricing jumps vs spot/perp prints and our own fair.

Kalshi jump: a market's live-bbo mid moves >= 4c within 1s and holds (mid 10s
later still >= 3c from the pre-jump mid). t_k = first bbo row where the mid is
>= half the jump away from the pre-jump mid.

For each jump, the coin that moved most (abs return over [t_k-3s, t_k+1s],
binance perp) is the driver. For each venue: half-move time = first 100ms bin
where that coin's price has covered half of its [t_k-3, t_k+1] move. Lead =
t_k - half-move time (positive = venue moved first). Also: our fair (decision
journal, 250ms) half-move time on the jumping market."""
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, DATA  # noqa: E402

EV = os.path.join(D, "ev")
sp = pd.concat([pd.read_parquet(os.path.join(DATA, "spot_prints.parquet")),
                pd.read_parquet(os.path.join(DATA, "perp_prints.parquet"))])
sp["t"] = sp.b / 10.0
venues = sorted(sp.venue.unique())
series = {}
for (v, c), g in sp.groupby(["venue", "coin"]):
    g = g.sort_values("b")
    series[(v, c)] = (g.t.to_numpy(), g.px.to_numpy())
print("venues", venues, flush=True)


def px_at(v, c, ts):
    if (v, c) not in series:
        return np.full(len(ts), np.nan)
    t, p = series[(v, c)]
    k = np.searchsorted(t, ts, side="right") - 1
    out = np.where(k >= 0, p[np.clip(k, 0, None)], np.nan)
    # stale if last print > 2s old
    age = ts - np.where(k >= 0, t[np.clip(k, 0, None)], -1e18)
    return np.where(age <= 2.0, out, np.nan)


rows = []
for f in sorted(glob.glob(os.path.join(EV, "*_bbo.parquet"))):
    ev = os.path.basename(f)[:-12]
    bbo = pd.read_parquet(f)
    dec_p = os.path.join(EV, f"{ev}_dec.parquet")
    dec = pd.read_parquet(dec_p, columns=["time", "ticker", "prob"]) if os.path.exists(dec_p) else None
    for tk, g in bbo.groupby("ticker"):
        g = g.dropna(subset=["bid", "ask"])
        g = g[(g.ask - g.bid) <= 0.10]
        if len(g) < 50:
            continue
        t = g.time.astype("int64").to_numpy() / 1e9
        mid = ((g.bid + g.ask) / 2).to_numpy()
        i = 0
        last_jump = -1e18
        n = len(t)
        j0 = 0
        while i < n:
            # pre-jump mid: mid 1s before
            while j0 < i and t[j0] < t[i] - 1.0:
                j0 += 1
            d = mid[i] - mid[j0]
            if abs(d) >= 0.04 and t[i] - last_jump > 10:
                k10 = np.searchsorted(t, t[i] + 10, side="right") - 1
                if abs(mid[k10] - mid[j0]) >= 0.03 and np.sign(mid[k10] - mid[j0]) == np.sign(d):
                    half = mid[j0] + d / 2
                    seg = np.arange(j0, i + 1)
                    hit = seg[np.sign(d) * (mid[seg] - half) >= 0]
                    tk_ = t[hit[0]] if len(hit) else t[i]
                    rows.append(dict(ev=ev, ticker=tk, t_k=tk_, jump=d, pre=mid[j0]))
                    last_jump = t[i]
            i += 1
J = pd.DataFrame(rows)
print("kalshi jumps", len(J), flush=True)

coins = ["BTC", "ETH", "SOL", "XRP", "HYPE"]
grid_off = np.arange(-3.0, 1.0001, 0.1)
out = []
for r in J.itertuples():
    ts = r.t_k + grid_off
    best, bmove = None, 0.0
    for c in coins:
        p = px_at("binance_perp", c, ts)
        if np.isnan(p[0]) or np.isnan(p[-1]):
            continue
        m = abs(np.log(p[-1] / p[0])) * 1e4
        if m > bmove:
            best, bmove = c, m
    rec = dict(ev=r.ev, ticker=r.ticker, t_k=r.t_k, jump=r.jump, pre=r.pre, driver=best, move_bp=bmove,
               own=r.ticker.rsplit("-", 1)[-1])
    if best is not None:
        for v in venues:
            p = px_at(v, best, ts)
            if np.isnan(p[0]) or np.isnan(p[-1]) or p[-1] == p[0]:
                rec[f"lead_{v}"] = np.nan
                continue
            frac = (p - p[0]) / (p[-1] - p[0])
            h = np.where(frac >= 0.5)[0]
            rec[f"lead_{v}"] = -grid_off[h[0]] if len(h) else np.nan
    if dec is not None:
        g = dec[dec.ticker == r.ticker]
        if len(g):
            tt = g.time.astype("int64").to_numpy() / 1e9
            pp = g.prob.to_numpy()
            k = np.searchsorted(tt, ts, side="right") - 1
            q = np.where(k >= 0, pp[np.clip(k, 0, None)], np.nan)
            if not np.isnan(q[0]) and not np.isnan(q[-1]) and q[-1] != q[0]:
                frac = (q - q[0]) / (q[-1] - q[0])
                h = np.where(frac >= 0.5)[0]
                rec["lead_fair"] = -grid_off[h[0]] if len(h) else np.nan
                rec["fair_move"] = q[-1] - q[0]
    out.append(rec)
X = pd.DataFrame(out)
X.to_parquet(os.path.join(D, "c_jumps.parquet"), index=False)
lc = [c for c in X.columns if c.startswith("lead_")]
print(f"\n{len(X)} Kalshi jumps (>=4c in 1s, holding 10s); driver move median {X.move_bp.median():.1f}bp")
print("lead (s) of each source's half-move over Kalshi's half-move (positive = moved first):")
print(X[lc].describe(percentiles=[.1, .25, .5, .75, .9]).round(2).to_string())
print("\nshare of jumps where source moved >= 0.1/0.2/0.3/0.5s before Kalshi:")
for c in lc:
    s = X[c].dropna()
    print(f"  {c:20s} n={len(s):4d}  " + "  ".join(f">={x}s: {(s >= x).mean():.2f}" for x in (0.1, 0.2, 0.3, 0.5)))
print("\nby driver move size (bp), median lead:")
X["mb"] = pd.cut(X.move_bp, [0, 3, 6, 10, 20, 1000])
print(X.groupby("mb", observed=True)[lc].median().round(2).to_string())
print("\ndriver is the market's own coin:", round((X.driver == X.own).mean(), 2))
