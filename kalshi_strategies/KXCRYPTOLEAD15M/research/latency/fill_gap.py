"""Why are fills losing? Per real maker fill, classify by what we knew when:

  RACE  we had already decided to cancel this order (orders journal fill row:
        cancel_decided_t set) but the fill beat the cancel -> pure latency.
        decided_before_ms = fill exchange time - cancel decision time.
  STALE theo had already moved against the resting quote (beyond the quote
        price) before the fill, but no cancel was decided yet -> our reaction.
        stale_ms = fill time - first moment theo crossed the quote.
  BLIND theo never crossed the quote before the fill -> information we
        don't have (latency can't fix these; edge/selection must).
Then markouts (c/lot, fee incl.) per class, so the $ lost to latency is
separate from the $ lost to not knowing. Race and single-coin series.
Clocks: fill / decision times are our box (decisions, fills, cancel_decided_t
local mono->wall); exch_t is Kalshi's. Both reported."""
import glob, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/opt/alpha5")
import numpy as np, pandas as pd, pyarrow.parquet as pq
from kalshi_viz import perf
pd.set_option("display.width", 260); pd.set_option("display.max_rows", 200)
S = sys.argv[1]
HOURS = float(sys.argv[2]) if len(sys.argv) > 2 else 10
NOW = pd.Timestamp.now(tz="UTC")
LO = NOW - pd.Timedelta(hours=HOURS)
DAY = NOW.strftime("%Y-%m-%d")
J = f"/opt/alpha5/data/live/{S}/{DAY}/journal/"


def load(stream, cols, need=("time",), thin=None):
    out = []
    for f in glob.glob(J + stream + "/*.parquet"):
        if os.path.getmtime(f) < LO.timestamp() - 60:
            continue
        try:
            names = pq.read_schema(f).names
            if not set(need) <= set(names):
                continue
            t = pq.read_table(f, columns=[c for c in cols if c in names]).to_pandas()
        except Exception:
            continue
        if t.empty:
            continue
        t["time"] = pd.to_datetime(t.time, utc=True).astype("datetime64[ns, UTC]")
        t = t[t.time >= LO - pd.Timedelta(seconds=30)]
        if thin is not None and len(t):
            t = t.groupby([t.ticker, t.time.dt.floor(thin)], sort=False).tail(1)
        out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=cols)


perf._days = lambda x: [DAY]
p = perf.Perf(S)
df = p.refresh(settle_all=True)
F = df[(df.time >= LO) & (df.liquidity == "maker")].reset_index(drop=True)
import pyarrow.compute as pc
fr_parts = []
for f in glob.glob(J + "orders/*.parquet"):
    if os.path.getmtime(f) < LO.timestamp() - 60:
        continue
    try:
        names = pq.read_schema(f).names
        if "trade_id" not in names:
            continue
        cols = [c for c in ("time", "ticker", "trade_id", "exch_t", "state", "cancel_decided_t", "cancel_reason") if c in names]
        t = pq.read_table(f, columns=cols)
        t = t.filter(pc.is_valid(t["trade_id"]))
    except Exception:
        continue
    if t.num_rows:
        fr_parts.append(t.to_pandas())
fr = pd.concat(fr_parts, ignore_index=True) if fr_parts else pd.DataFrame(columns=["trade_id", "cancel_decided_t", "cancel_reason", "exch_t", "state"])
for c in ("cancel_decided_t", "cancel_reason", "exch_t", "state"):
    if c not in fr:
        fr[c] = np.nan
print(f"{S}: {len(F)} real maker fills ({F['size'].sum():.1f} lots) since {LO:%H:%M} UTC; "
      f"orders-journal fill rows {len(fr)}; with cancel_decided_t {int(fr.cancel_decided_t.notna().sum())}")
if len(fr):
    print("orders fill-row state:", fr.state.astype(str).value_counts().head(6).to_dict(),
          "| cancel_reason:", fr.cancel_reason.astype(str).value_counts().head(6).to_dict())

# join orders-journal fill rows to perf fills by trade id (strings)
fr["trade_id"] = fr.trade_id.astype(str)
key = df[["time", "ticker"]].copy()
fills_raw = load("fills", ["time", "ticker", "trade_id", "mode", "liquidity", "size", "price", "side"], need=("time", "mode"))
fills_raw = fills_raw[(fills_raw["mode"] == "real") & (fills_raw.liquidity == "maker")]
fills_raw["trade_id"] = fills_raw.trade_id.astype(str)
m = fills_raw.merge(fr[["trade_id", "cancel_decided_t", "cancel_reason", "exch_t", "state"]].drop_duplicates("trade_id"),
                    on="trade_id", how="left")
F = F.merge(m[["time", "ticker", "price", "size", "cancel_decided_t", "cancel_reason", "exch_t", "state"]],
            on=["time", "ticker", "price", "size"], how="left")

# theo path around each fill: when did theo first cross the quote price?
# only the 3 s before each fill, from decision parts whose time range covers one
win = pd.DataFrame({"a": F.time - pd.Timedelta(seconds=3.2), "b": F.time})
dparts = []
for f in glob.glob(J + "decisions/*.parquet"):
    if os.path.getmtime(f) < LO.timestamp() - 60:
        continue
    try:
        md = pq.ParquetFile(f).metadata
        i = md.schema.to_arrow_schema().get_field_index("time")
        lo_ = min(pd.Timestamp(md.row_group(g).column(i).statistics.min) for g in range(md.num_row_groups))
        hi_ = max(pd.Timestamp(md.row_group(g).column(i).statistics.max) for g in range(md.num_row_groups))
        lo_ = lo_.tz_localize("UTC") if lo_.tzinfo is None else lo_
        hi_ = hi_.tz_localize("UTC") if hi_.tzinfo is None else hi_
    except Exception:
        continue
    if not ((win.a <= hi_) & (win.b >= lo_)).any():
        continue
    try:
        t = pq.read_table(f, columns=["time", "ticker", "prob"]).to_pandas()
    except Exception:
        continue
    t["time"] = pd.to_datetime(t.time, utc=True).astype("datetime64[ns, UTC]")
    keep = np.zeros(len(t), bool)
    tt = t.time.astype("int64").to_numpy()
    for a, b in zip(win.a.astype("int64"), win.b.astype("int64")):
        keep |= (tt >= a) & (tt <= b)
    dparts.append(t[keep & t.prob.notna()])
dec = pd.concat(dparts, ignore_index=True).sort_values("time") if dparts else pd.DataFrame(columns=["time", "ticker", "prob"])
print(f"decision rows in fill windows: {len(dec)}")
ft = F.time.astype("int64").to_numpy() / 1e9
st = []
for tk, g in F.groupby("ticker"):
    d = dec[dec.ticker == tk]
    if d.empty:
        continue
    dt = d.time.astype("int64").to_numpy() / 1e9
    dp = d.prob.to_numpy(float)
    for i in g.index:
        t, px, dr = ft[i], F.yes_px[i], F.dir[i]
        lo_i, hi_i = np.searchsorted(dt, t - 3.0), np.searchsorted(dt, t, side="right")
        seg_t, seg_p = dt[lo_i:hi_i], dp[lo_i:hi_i]
        # we bought yes at px (dir +1): bad if theo fell below px; sold yes: bad if theo rose above px
        crossed = (seg_p < px) if dr > 0 else (seg_p > px)
        if crossed.any():
            # first moment of the LAST continuous crossed run before the fill
            j = len(crossed) - 1
            if crossed[j]:
                while j > 0 and crossed[j - 1]:
                    j -= 1
                st.append((i, (t - seg_t[j]) * 1e3, True))
                continue
        st.append((i, np.nan, False))
S2 = pd.DataFrame(st, columns=["i", "stale_ms", "crossed_at_fill"]).set_index("i")
F = F.join(S2)
cdt = pd.to_datetime(F.cancel_decided_t, unit="s", utc=True, errors="coerce") if F.cancel_decided_t.notna().any() else pd.Series(pd.NaT, index=F.index)
F["decided_before_ms"] = (F.time - cdt).dt.total_seconds() * 1e3
F["cls"] = np.where(F.cancel_decided_t.notna(), "RACE (cancel decided, lost)",
                    np.where(F.crossed_at_fill.fillna(False), "STALE (theo crossed, no cancel yet)", "BLIND (theo not past quote)"))

w = lambda g, c: np.average(g[c].dropna(), weights=g.loc[g[c].notna(), "size"]) if g[c].notna().any() else np.nan
def summ(g):
    return pd.Series({"fills": len(g), "lots": g["size"].sum(), "share_%": 100 * len(g) / len(F),
                      "at_fill": w(g, "mo0"), "mo1": w(g, "mo1"), "mo10": w(g, "mo10"), "mo60": w(g, "mo60"),
                      "mo10_$": (g.mo10 * g["size"] / 100).sum(), "settled_$": (g.settled * g["size"] / 100).sum()})
print("\n=== maker fills by what we knew (c/lot; $ = total) ===")
T = F.groupby("cls").apply(summ)
print(T.round(2).to_string())
tot = (F.mo10 * F["size"] / 100).sum()
print(f"total mo10 $ {tot:+.2f}")

r = F[F.cls.str.startswith("RACE")]
if len(r):
    print(f"\nRACE: cancel decided {r.decided_before_ms.median():.1f} ms before the fill (p10 {r.decided_before_ms.quantile(.1):.1f}, "
          f"p90 {r.decided_before_ms.quantile(.9):.1f}); reasons {r.cancel_reason.astype(str).value_counts().head(5).to_dict()}")
s = F[F.cls.str.startswith("STALE")]
if len(s):
    print(f"STALE: theo had crossed the quote {s.stale_ms.median():.0f} ms before the fill "
          f"(p10 {s.stale_ms.quantile(.1):.0f}, p25 {s.stale_ms.quantile(.25):.0f}, p75 {s.stale_ms.quantile(.75):.0f}, p90 {s.stale_ms.quantile(.9):.0f})")
    s = s.assign(b=pd.cut(s.stale_ms, [0, 10, 25, 50, 100, 250, 1000, 3001]))
    print(s.groupby("b", observed=True).apply(summ)[["fills", "mo10", "mo10_$"]].round(2).to_string())
# --- the latency gap itself: for each fill, the pull trip nearest before it on
# that coin (burst_trips: exchange time of the triggering print, our receipt)
coin = F.ticker.str.rsplit("-", n=1).str[1] if S == "KXCRYPTOLEAD15M" else pd.Series(S.replace("KX", "").replace("15M", ""), index=F.index)
F["coin"] = coin.values
tr = []
for f in glob.glob(f"/opt/alpha5/data/kalshi_crypto/burst_trips/{DAY}/*.parquet"):
    if os.path.getmtime(f) < LO.timestamp() - 60:
        continue
    try:
        tr.append(pq.read_table(f, columns=["recv_ts", "exch_ts", "kind", "source", "coin", "pulls"]).to_pandas())
    except Exception:
        pass
TR = pd.concat(tr, ignore_index=True) if tr else pd.DataFrame(columns=["recv_ts", "exch_ts", "kind", "source", "coin", "pulls"])
TR = TR[TR.pulls.fillna(False).astype(bool)]
ftw = F.time.astype("int64").to_numpy() / 1e9
near = []
for i in F.index:
    t = TR[(TR.coin == F.coin[i]) & (TR.recv_ts > ftw[i] - 1.0) & (TR.recv_ts < ftw[i] + 0.3)]
    if t.empty:
        near.append((np.nan, np.nan, None)); continue
    j = (t.recv_ts - ftw[i]).abs().idxmin()
    near.append(((t.recv_ts[j] - ftw[i]) * 1e3, (t.exch_ts[j] - ftw[i]) * 1e3 if pd.notna(t.exch_ts[j]) else np.nan, t.source[j]))
F[["trip_recv_vs_fill_ms", "trip_exch_vs_fill_ms", "trip_src"]] = pd.DataFrame(near, index=F.index)
print("\n=== pull trips within [-1s, +0.3s] of a fill, same coin (ms vs the Kalshi fill time; - = before) ===")
h = F[F.trip_src.notna()]
print(f"{len(h)} of {len(F)} fills had one; sources {h.trip_src.value_counts().to_dict()}")
if len(h):
    print("  trip print EXCHANGE time vs fill: p10/p50/p90", np.nanpercentile(h.trip_exch_vs_fill_ms, [10, 50, 90]).round(1).tolist())
    print("  trip RECEIPT on our box vs fill:  p10/p50/p90", np.nanpercentile(h.trip_recv_vs_fill_ms, [10, 50, 90]).round(1).tolist())
    h = h.assign(when=np.where(h.trip_recv_vs_fill_ms < 0, "we saw it before the fill", "we saw it after the fill"))
    print(h.groupby("when").apply(summ)[["fills", "lots", "mo1", "mo10", "mo60", "mo10_$", "settled_$"]].round(2).to_string())
    nh = F[F.trip_src.isna()]
    print("no trip near:", summ(nh)[["fills", "mo1", "mo10", "mo60", "mo10_$", "settled_$"]].round(2).to_dict())
r = F[F.cancel_decided_t.notna()].assign(b=lambda x: pd.cut(x.decided_before_ms, [-1e9, -50, -20, -10, 0, 10, 20, 50, 1e9]))
if len(r):
    print("\n=== fills with a cancel already decided: decision time vs Kalshi fill time (ms; - = decided AFTER the fill) ===")
    print(r.groupby("b", observed=True).apply(summ)[["fills", "lots", "mo1", "mo10", "mo60", "mo10_$"]].round(2).to_string())
F.to_parquet(f"/tmp/gap_{S}.parquet")



