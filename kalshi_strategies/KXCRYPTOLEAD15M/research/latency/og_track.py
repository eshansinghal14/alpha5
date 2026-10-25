"""Are order groups stopping multi-market sweeps? Usage: og_track.py [HOURS] [SERIES]

From the orders journal (og_trip / og_reset / fill / cancel_ack rows) and perf markouts:
  - trips per hour, reset time (trip -> reset ok)
  - per trip: fills in the same event within 5 / 50 / 200 ms AFTER the tripping fill
    (legs the group failed to stop) and their 1s/10s markout
  - cancel acks with reason 'order_group': "gone" (Kalshi had already cancelled) vs "ok" (still resting)
  - multi-fill sweeps (2+ fills in one event within 5 ms) per hour and their markout, vs before
    the group was on (same window length before the first og row)."""
import glob, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/opt/alpha5")
import numpy as np, pandas as pd, pyarrow.parquet as pq
from kalshi_viz import perf
pd.set_option("display.width", 230)
H = float(sys.argv[1]) if len(sys.argv) > 1 else 6
S = sys.argv[2] if len(sys.argv) > 2 else "KXCRYPTOLEAD15M"
NOW = pd.Timestamp.now(tz="UTC")
days = sorted({(NOW - pd.Timedelta(hours=x)).strftime("%Y-%m-%d") for x in (0, H, 2 * H)})
rows = []
LO = (NOW - pd.Timedelta(hours=2 * H)).timestamp()
WANT = ("time", "event", "reason", "status", "group", "exch_t", "ticker", "size", "og_tripped")
for d in days:
    for f in glob.glob(f"/opt/alpha5/data/live/{S}/{d}/journal/orders/*.parquet"):
        if os.path.getmtime(f) < LO:
            continue
        try:
            names = pq.read_schema(f).names
            t = pq.read_table(f, columns=[c for c in WANT if c in names]).to_pandas()
        except Exception:
            continue
        t = t[t.event.isin(["og_trip", "og_reset", "fill", "cancel_ack"])]
        if not t.empty:
            rows.append(t)
O = pd.concat(rows, ignore_index=True)
for c in WANT:
    if c not in O:
        O[c] = np.nan
O["time"] = pd.to_datetime(O.time, utc=True)
og = O[O.event.isin(["og_trip", "og_reset"])].sort_values("time")
if og.empty:
    print("no og_trip / og_reset rows yet"); sys.exit()
t_on = og.time.min()
print(f"order group rows since {t_on:%m-%d %H:%M:%S} UTC; window {H:g} h before that is the comparison")
fl = []
for d in sorted({t_on.strftime("%Y-%m-%d"), NOW.strftime("%Y-%m-%d")}):
    perf._days = lambda x, dd=d: [dd]
    df = perf.Perf(S).refresh(settle_all=False)
    fl.append(df[df.liquidity == "maker"])
F = pd.concat(fl).drop_duplicates(["time", "ticker", "price", "size"]).sort_values("time").reset_index(drop=True)
F = F[F.time >= t_on - (NOW - t_on)]
F["lots"] = F["size"]; F["event"] = F.ticker.str.rsplit("-", n=1).str[0]; F["coin"] = F.ticker.str.rsplit("-", n=1).str[1]
F["t"] = F.time.astype("int64") / 1e9
for c in ("mo1", "mo10"):
    F[f"{c}_$"] = F[c] * F.lots / 100
after = F[F.time >= t_on]
before = F[(F.time < t_on) & (F.time >= t_on - (NOW - t_on))]
hrs = (NOW - t_on).total_seconds() / 3600
trips = og[og.event == "og_trip"]; resets = og[og.event == "og_reset"]
print(f"\ntrips {len(trips)} ({len(trips) / hrs:.1f}/h); resets ok {int((resets.status == 'ok').sum())}, "
      f"errors {int((resets.status != 'ok').sum())}; reasons {trips.reason.astype(str).str[:30].value_counts().head(4).to_dict()}")
tt = trips.time.astype("int64").to_numpy() / 1e9
rt = resets[resets.status == "ok"].time.astype("int64").to_numpy() / 1e9
if len(tt) and len(rt):
    d = [rt[rt > t].min() - t for t in tt if (rt > t).any()]
    print(f"trip -> reset ok: p50 {np.median(d) * 1e3:.0f} ms, p90 {np.percentile(d, 90) * 1e3:.0f} ms")
ca = O[(O.event == "cancel_ack") & (O.reason.astype(str) == "order_group") & (O.time >= t_on)]
print(f"cancel acks for order_group pulls: {ca.status.astype(str).value_counts().to_dict()}  "
      f"('gone' = Kalshi had already cancelled it; 'ok' = it was still resting -> group didn't act first)")


def sweeps(G):
    G = G.sort_values("t").copy()
    G["burst"] = (G.groupby("event").t.diff().fillna(99) > 0.005).cumsum()
    B = G.groupby("burst").agg(n=("t", "size"), mo1=("mo1_$", "sum"), mo10=("mo10_$", "sum"), t0=("time", "first"))
    return B[B.n > 1]


for nm, G, h in (("before (no group)", before, hrs), ("after (group on)", after, hrs)):
    sw = sweeps(G)
    print(f"\n{nm}: {len(G)} fills, 1s ${G['mo1_$'].sum():+.2f}, 10s ${G['mo10_$'].sum():+.2f} | multi-fill sweeps "
          f"(2+ fills in one event within 5 ms): {len(sw)} ({len(sw) / h:.1f}/h), legs {int(sw.n.sum())}, "
          f"1s ${sw.mo1.sum():+.2f}, 10s ${sw.mo10.sum():+.2f}")
# legs after each trip
fr = O[(O.event == "fill") & (O.time >= t_on)].copy()
fr["t"] = fr.exch_t.astype(float)
leak = []
for r in trips.itertuples():
    t0 = float(r.exch_t) if pd.notna(getattr(r, "exch_t", np.nan)) else r.time.timestamp()
    nxt = fr[(fr.t > t0) & (fr.t <= t0 + 0.2)]
    for x in nxt.itertuples():
        leak.append(((x.t - t0) * 1e3, x.ticker, x.size))
L = pd.DataFrame(leak, columns=["ms_after_trip", "ticker", "size"])
print(f"\nfills within 200 ms AFTER a trip (legs the group did not stop): {len(L)}"
      + ("" if L.empty else f"; ms after trip p50 {L.ms_after_trip.median():.1f}; "
         f"within 5 ms {int((L.ms_after_trip <= 5).sum())}, 5-50 ms {int(L.ms_after_trip.between(5, 50).sum())}"))
print(f"fills booked while our side believed the group tripped: {int(O[(O.event == 'fill') & (O.time >= t_on)].get('og_tripped', pd.Series(dtype=bool)).fillna(False).astype(bool).sum())}")
