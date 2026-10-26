"""Order-group limit 10 vs 9 on the race. Usage: og_compare.py [START_UTC_OF_LIMIT_9]

Periods (UTC): off = 10-25 15:15-22:15, limit 10 = 10-25 22:15 -> 10-26 05:30, limit 9 = 05:30 -> now.
Per period, per hour: real maker fills and their 1 s / 10 s / settled $, fills split by time since the
previous full (>= 10 lot) fill in the same event (0-5 ms, 5-50 ms, 50 ms-1 s, >= 1 s), multi-coin
sweeps, order-group trips, and settled PnL per event from run.out.log."""
import glob, os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/opt/alpha5")
import numpy as np, pandas as pd, pyarrow.parquet as pq
from kalshi_viz import perf
S = "KXCRYPTOLEAD15M"
NOW = pd.Timestamp.now(tz="UTC")
T9 = pd.Timestamp(sys.argv[1] if len(sys.argv) > 1 else "2026-10-26 05:30", tz="UTC")
P = [("off", pd.Timestamp("2026-10-25 15:15", tz="UTC"), pd.Timestamp("2026-10-25 22:15", tz="UTC")),
     ("limit 10", pd.Timestamp("2026-10-25 22:15", tz="UTC"), T9),
     ("limit 9", T9, NOW)]
days = sorted({d.strftime("%Y-%m-%d") for d in pd.date_range(P[0][1].normalize(), NOW.normalize(), freq="D")})
fl = []
for d in days:
    perf._days = lambda x, dd=d: [dd]
    df = perf.Perf(S).refresh(settle_all=True)
    fl.append(df[df.liquidity == "maker"])
F = pd.concat(fl).drop_duplicates(["time", "ticker", "price", "size"]).sort_values("time").reset_index(drop=True)
F["lots"] = F["size"]; F["event"] = F.ticker.str.rsplit("-", n=1).str[0]; F["t"] = F.time.astype("int64") / 1e9
for c in ("mo1", "mo10", "settled"):
    F[f"{c}_$"] = F[c] * F.lots / 100
gap = np.full(len(F), np.inf)
for _, g in F.groupby("event", sort=False):
    last = None
    for i, t, q in zip(g.index, g.t, g.lots):
        if last is not None:
            gap[i] = (t - last) * 1e3
        if q >= 9.99:
            last = t
F["gap_ms"] = gap
F["b"] = pd.cut(F.gap_ms, [-1, 5, 50, 1000, np.inf], labels=["0-5ms", "5-50ms", "50ms-1s", ">=1s"])
F["sweep"] = F.groupby("event").t.diff().fillna(99).lt(0.005) | F.groupby("event").t.diff(-1).fillna(-99).gt(-0.005)

# trips from the orders journal (og_trip rows), only the columns needed
tr = []
for d in days:
    for f in glob.glob(f"/opt/alpha5/data/live/{S}/{d}/journal/orders/*.parquet"):
        if os.path.getmtime(f) < P[0][1].timestamp():
            continue
        try:
            t = pq.read_table(f, columns=["time", "event"]).to_pandas()
        except Exception:
            continue
        t = t[t.event == "og_trip"]
        if len(t):
            tr.append(t)
TR = pd.concat(tr) if tr else pd.DataFrame(columns=["time", "event"])
TR["time"] = pd.to_datetime(TR.time, utc=True)

# settled PnL per event from the log: event close (ET) -> UTC
sett = {}
for line in open("/opt/alpha5/data/live/run.out.log", errors="replace"):
    m = re.search(r"KXCRYPTOLEAD15M-(\d{2}[A-Z]{3}\d{2}\d{4}) real PnL settled ([-+][0-9.]+)", line)
    if m:
        T = pd.to_datetime("20" + m.group(1), format="%Y%b%d%H%M").tz_localize("America/New_York").tz_convert("UTC")
        sett[T] = float(m.group(2))
SE = pd.Series(sett).sort_index()

print(f"=== order group limit 10 vs 9 (as of {NOW:%m-%d %H:%M} UTC) ===")
rows = []
for name, a, b in P:
    g = F[(F.time >= a) & (F.time < b)]
    h = max((b - a).total_seconds() / 3600, 1e-9)
    se = SE[(SE.index > a) & (SE.index <= b + pd.Timedelta(minutes=1))]
    r = {"period": name, "hours": round(h, 1), "fills/h": len(g) / h,
         "1s $/h": g["mo1_$"].sum() / h, "10s $/h": g["mo10_$"].sum() / h, "settled(fills) $/h": g["settled_$"].sum() / h,
         "bad1s %": 100 * (g.mo1 <= -3).mean() if len(g) else np.nan,
         "sweep legs/h": g.sweep.sum() / h, "sweep 10s $/h": g[g.sweep]["mo10_$"].sum() / h,
         "trips/h": ((TR.time >= a) & (TR.time < b)).sum() / h,
         "events": len(se), "settled/event": se.mean() if len(se) else np.nan}
    for k in ("0-5ms", "5-50ms", "50ms-1s", ">=1s"):
        gg = g[g.b == k]
        r[f"{k} fills/h"] = len(gg) / h
        r[f"{k} 10s $/h"] = gg["mo10_$"].sum() / h
    rows.append(r)
R = pd.DataFrame(rows).set_index("period").T
pd.set_option("display.width", 200)
print(R.round(2).to_string())
