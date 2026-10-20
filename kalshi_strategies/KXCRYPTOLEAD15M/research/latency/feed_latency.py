"""Per-coin latency on the live box, last WIN minutes:
  1. feed delay: exchange trade time -> receipt on this box (prints)
       Coinbase (Exchange matches, prints/), Binance/OKX/Bybit/Hyperliquid
       (venue_prints/). Kraken etc. don't send per-trade timestamps we record.
  2. our pipeline, from the race's decision rows: signal age (oldest coin's
       newest book at decision time) and per-coin quote_block = stale share.
  3. Kalshi print delay to us, from the race trades journal (received - ts).
  4. orders: create / cancel round trip from the orders journal."""
import glob, os, sys, time, json, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, pyarrow.parquet as pq, pyarrow.compute as pc
pd.set_option("display.width", 250)
WIN = float(sys.argv[1]) if len(sys.argv) > 1 else 30
NOW = time.time()
LO = NOW - WIN * 60
R = "/opt/alpha5/data/kalshi_crypto/"
day = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
q = lambda s: pd.Series({"n": len(s), "p10": s.quantile(.1), "p50": s.median(), "p90": s.quantile(.9), "p99": s.quantile(.99)})

rows = []
for f in glob.glob(R + f"venue_prints/{day}/*.parquet"):
    if os.path.getmtime(f) < LO:
        continue
    try:
        t = pq.read_table(f, columns=["recv_ts", "exch_ts", "venue", "coin"])
    except Exception:
        continue
    rows.append(t.to_pandas())
for f in glob.glob(R + f"prints/*_{day}_*.parquet"):
    if os.path.getmtime(f) < LO:
        continue
    try:
        t = pq.read_table(f, columns=["recv_ts", "exch_ts", "venue", "coin"]).to_pandas()
    except Exception:
        continue
    rows.append(t[t.venue == "coinbase"])
P = pd.concat(rows)
P = P[(P.recv_ts >= LO) & P.exch_ts.notna()]
P["ms"] = (P.recv_ts - P.exch_ts) * 1e3
print(f"=== 1. feed delay, exchange trade time -> receipt (ms), last {WIN:.0f} min ===")
T = P.groupby(["venue", "coin"]).ms.apply(q).unstack().round(1)
print(T[T.n >= 20].to_string())

J = f"/opt/alpha5/data/live/KXCRYPTOLEAD15M/{day}/journal/"
dec = []
for f in glob.glob(J + "decisions/*.parquet"):
    if os.path.getmtime(f) < LO:
        continue
    try:
        names = pq.read_schema(f).names
        x = pq.read_table(f, columns=[c for c in ("time", "ticker", "signal_age_s", "quote_block", "live") if c in names]).to_pandas()
    except Exception:
        continue
    dec.append(x)
D = pd.concat(dec)
D = D[pd.to_datetime(D.time, utc=True).astype("int64") / 1e9 >= LO]
D["coin"] = D.ticker.str.rsplit("-", n=1).str[1]
D["ms"] = D.signal_age_s * 1e3
D["stale"] = D.quote_block.astype(str).str.contains("stale")
print(f"\n=== 2. race decisions, last {WIN:.0f} min: signal age (ms) and stale-blocked share ===")
print(D.groupby("coin").apply(lambda g: pd.concat([q(g.ms.dropna()), pd.Series({"over_30ms_%": 100 * (g.ms > 30).mean(),
                                                                                  "stale_%": 100 * g.stale.mean()})])).round(2).to_string())

tr = []
for f in glob.glob(J + "trades/*.parquet"):
    if os.path.getmtime(f) < LO:
        continue
    try:
        x = pq.read_table(f, columns=["time", "received", "ticker"]).to_pandas()
    except Exception:
        continue
    tr.append(x)
K = pd.concat(tr)
K["ms"] = (pd.to_datetime(K.received, utc=True) - pd.to_datetime(K.time, utc=True)).dt.total_seconds() * 1e3
K = K[pd.to_datetime(K.time, utc=True).astype("int64") / 1e9 >= LO]
K["coin"] = K.ticker.str.rsplit("-", n=1).str[1]
print(f"\n=== 3. Kalshi race prints, Kalshi trade time -> our receipt (ms) ===")
print(K.groupby("coin").ms.apply(q).unstack().round(1).to_string())

od = []
for f in glob.glob(J + "orders/*.parquet"):
    if os.path.getmtime(f) < LO:
        continue
    try:
        names = pq.read_schema(f).names
        x = pq.read_table(f, columns=[c for c in ("time", "live", "ticker", "status", "rtt_s", "reason", "queued_s") if c in names]).to_pandas()
    except Exception:
        continue
    od.append(x)
O = pd.concat(od)
O = O[O.live.fillna(False).astype(bool) & (O.status == "ok") & O.rtt_s.notna()]
O["kind"] = np.where(O.reason.astype(str).isin(["<NA>", "nan", "None"]), "create", "cancel")
O["ms"] = O.rtt_s * 1e3
print(f"\n=== 4. real order round trips to Kalshi (ms) ===")
print(O.groupby("kind").ms.apply(q).unstack().round(1).to_string())
s = json.load(open(f"/opt/alpha5/data/live/KXCRYPTOLEAD15M/{day}/summary.json"))
t = s.get("timings") or {}
print("\nrace loop timings (ms):", {k: {kk: v.get(kk) for kk in ("median_ms", "p95_ms", "max_ms")} for k, v in t.items() if isinstance(v, dict)})
