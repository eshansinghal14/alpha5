"""Score stale-vol replacements against the next live implied reading.

Race decision rows (x_vol_src, x_vol_bp, x_har_vol_bp, x_anch_vol_bp,
x_implied_age_s; shadow fields from model._shadow_vols). For every second a
coin is NOT on live implied, three candidates are scored by |ln(v / v_next)|,
v_next = that coin's next live implied vol:
  hold     the last live implied value, unchanged
  har      the raw HAR (realized) rate
  anchored last implied x HAR now / HAR at that reading
by age of the last implied reading. Also the "too far from market" trigger:
share of held-row fills / time with |fair - Kalshi mid| over 2/3/5c, and those
fills' 10s markouts vs the rest (perf fills).

Run on the box:  deploy/review_py.sh <this file> [hours=24]
"""
import glob, os, sys, time, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, pyarrow.parquet as pq

pd.set_option("display.width", 220)
S = "KXCRYPTOLEAD15M"
R = f"/opt/alpha5/data/live/{S}"
H = float(sys.argv[1]) if len(sys.argv) > 1 else 24
LO = time.time() - H * 3600
days = sorted({time.strftime("%Y-%m-%d", time.gmtime(t)) for t in (LO, time.time())})
cols = ["time", "ticker", "prob", "market_bid", "market_ask", "x_vol_src", "x_vol_bp", "x_har_vol_bp",
        "x_anch_vol_bp", "x_implied_age_s"]
parts = []
for day in days:
    for f in glob.glob(f"{R}/{day}/journal/decisions/*.parquet"):
        if os.path.getmtime(f) < LO - 60:
            continue
        try:
            names = pq.read_schema(f).names
            if "x_anch_vol_bp" not in names:
                continue
            t = pq.read_table(f, columns=[c for c in cols if c in names]).to_pandas()
        except Exception:
            continue
        t["s"] = pd.to_datetime(t.time, utc=True).astype("int64") // 10**9
        t = t[t.s >= LO].drop(columns="time")
        t["ticker"], t["x_vol_src"] = t.ticker.astype(str), t.x_vol_src.astype(str)
        parts.append(t.groupby(["ticker", "s"], sort=False).last().reset_index())
if not parts:
    sys.exit("no decision rows with the shadow fields yet")
d = pd.concat(parts).groupby(["ticker", "s"]).last().reset_index()
d["coin"] = d.ticker.str.rsplit("-", n=1).str[1]
print(f"{len(d)} ticker-seconds over the last {H:g}h; vol_src {d.x_vol_src.value_counts(normalize=True).round(3).to_dict()}")

rows = []
for c, g in d.groupby("coin"):
    g = g.sort_values("s").drop_duplicates("s")
    imp = g[g.x_vol_src == "implied"]
    s_imp, v_imp = imp.s.to_numpy(), imp.x_vol_bp.to_numpy()
    o = g[(g.x_vol_src != "implied") & g.x_anch_vol_bp.notna()].copy()
    i_prev = np.searchsorted(s_imp, o.s.to_numpy(), side="left") - 1
    i_next = np.searchsorted(s_imp, o.s.to_numpy(), side="right")
    ok = (i_prev >= 0) & (i_next < len(s_imp))
    o = o[ok]
    o["v_hold"], o["v_next"] = v_imp[i_prev[ok]], v_imp[i_next[ok]]
    rows.append(o)
r = pd.concat(rows)
for k, col in (("hold", "v_hold"), ("har", "x_har_vol_bp"), ("anchored", "x_anch_vol_bp")):
    r[k] = np.abs(np.log(r[col] / r.v_next))
r["age"] = pd.cut(r.x_implied_age_s, [0, 60, 180, 300, 420, 600, 900, 3600])
print("\nmedian |ln error| vs the next live implied reading, by age of the last one:")
print(r.groupby("age", observed=True).agg(n=("hold", "size"), hold=("hold", "median"), har=("har", "median"),
                                          anchored=("anchored", "median")).round(3).to_string())
print("\nby coin (all ages):")
print(r.groupby("coin").agg(n=("hold", "size"), hold=("hold", "median"), har=("har", "median"),
                            anchored=("anchored", "median")).round(3).to_string())

# Distance trigger: held seconds with the fair far from Kalshi's mid.
q = d.dropna(subset=["prob", "market_bid", "market_ask"]).copy()
q = q[(q.market_ask - q.market_bid) <= 0.05]
q["gap"] = (q.prob - (q.market_bid + q.market_ask) / 2).abs() * 100
held = q[q.x_vol_src == "held"]
print("\nheld ticker-seconds with |fair - mid| over 2/3/5c:",
      {k: round((held.gap > k).mean(), 3) for k in (2, 3, 5)}, "| all:", {k: round((q.gap > k).mean(), 3) for k in (2, 3, 5)})
try:
    F = pd.read_parquet(f"{R}/perf/fills.parquet")
    F["time"] = pd.to_datetime(F.time, utc=True)
    F = F[(F.liquidity == "maker") & (F.time.astype("int64") // 10**9 >= LO)].copy()
    F["s"] = F.time.astype("int64") // 10**9
    F = F.merge(q[["ticker", "s", "x_vol_src", "gap"]], on=["ticker", "s"], how="inner")
    w = lambda x: np.average(x.mo10, weights=x["size"]) if len(x) else np.nan
    for k in (2, 3, 5):
        far = F.gap > k
        print(f"  fills |gap|>{k}c: {far.sum()} ({far.mean():.0%}) mo10 {w(F[far]):+.2f}c vs rest {w(F[~far]):+.2f}c; "
              f"held only: {(far & (F.x_vol_src == 'held')).sum()} mo10 {w(F[far & (F.x_vol_src == 'held')]):+.2f}c")
except Exception as e:
    print("fills section skipped:", repr(e))
