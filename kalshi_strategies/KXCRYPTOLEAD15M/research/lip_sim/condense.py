"""Condense live journals + spot prints for offline LIP research. Runs on EC2 under review_py.sh."""
import glob, os, sys
import numpy as np, pandas as pd, pyarrow.parquet as pq
R = "/opt/alpha5/data/live/KXCRYPTOLEAD15M"
OUT = "/tmp/lipr"; os.makedirs(OUT, exist_ok=True)
DC = ["time","ticker","market_bid","market_ask","prob","prob_lo","prob_hi","quote_bid_reservation","quote_ask_reservation",
      "quote_bid","quote_ask","n_levels_bid","n_levels_ask","block_bid","block_ask","x_tau","x_theo_vol","net_position",
      "constant_edge","signal_age_s","lip_accrued_usd"]
what = sys.argv[1]
if what == "dec":
    for d in ("2026-10-11","2026-10-12","2026-10-13","2026-10-14"):
        outs = []
        for p in sorted(glob.glob(f"{R}/{d}/journal/decisions/part-*.parquet")):
            try:
                names = pq.read_schema(p).names
                t = pq.read_table(p, columns=[c for c in DC if c in names]).to_pandas()
            except Exception:
                continue
            if t.empty: continue
            t["time"] = pd.to_datetime(t.time, utc=True)
            t["b"] = t.time.dt.floor("250ms")
            for c in ("block_bid","block_ask"):
                if c in t: t[c] = t[c].astype(object).where(t[c].notna(), None).astype("string")
            g = t.groupby(["ticker","b"], sort=False).last().reset_index()
            outs.append(g)
        if outs:
            df = pd.concat(outs, ignore_index=True)
            for c in ("block_bid","block_ask"):
                if c in df: df[c] = df[c].astype("string")
            df.to_parquet(f"{OUT}/dec_{d}.parquet", index=False)
            print(d, len(df), flush=True)
elif what == "fills":
    for d in ("2026-10-11","2026-10-12","2026-10-13","2026-10-14"):
        fs = []
        for st in ("fills","bbo"):
            ps = sorted(glob.glob(f"{R}/{d}/journal/{st}/part-*.parquet"))
            fr = []
            for p in ps:
                try: fr.append(pd.read_parquet(p))
                except Exception: pass
            if fr:
                df = pd.concat(fr, ignore_index=True)
                for c in df.columns:
                    if df[c].dtype == object and c != "time": df[c] = df[c].astype("string")
                df.to_parquet(f"{OUT}/{st}_{d}.parquet", index=False); print(st, d, len(df), flush=True)
elif what == "prints":
    rows = []
    for p in sorted(glob.glob("/opt/alpha5/data/kalshi_crypto/prints/*_2026-10-1[1-4]_*.parquet")):
        x = pd.read_parquet(p, columns=["coin","price","size","side","recv_ts","venue"])
        x["b"] = (x.recv_ts * 10).astype(np.int64)
        x["usd"] = x.price * x["size"]
        x["sgn"] = np.where(x.side.astype(str).str.lower().isin(["buy","b"]), 1.0, -1.0) * x.usd
        g = x.groupby(["venue","coin","b"]).agg(px=("price","last"), usd=("usd","sum"), susd=("sgn","sum"), n=("price","size")).reset_index()
        rows.append(g)
    df = pd.concat(rows); df.to_parquet(f"{OUT}/spot_prints.parquet", index=False); print("spot", len(df))
    rows = []
    for d in ("2026-10-11","2026-10-12","2026-10-13","2026-10-14"):
        for p in sorted(glob.glob(f"/opt/alpha5/data/kalshi_crypto/perp_prints/{d}/*.parquet")):
            try: x = pd.read_parquet(p, columns=["coin","price","size","side","recv_ts","venue"])
            except Exception: continue
            x["b"] = (x.recv_ts * 10).astype(np.int64)
            x["usd"] = x.price * x["size"]
            x["sgn"] = np.where(x.side.astype(str).str.lower().isin(["buy","b"]), 1.0, -1.0) * x.usd
            rows.append(x.groupby(["venue","coin","b"]).agg(px=("price","last"), usd=("usd","sum"), susd=("sgn","sum"), n=("price","size")).reset_index())
    df = pd.concat(rows); df.to_parquet(f"{OUT}/perp_prints.parquet", index=False); print("perp", len(df))
elif what == "comp":
    fr = [pd.read_parquet(p, columns=["coin","ts","price","fallback_mid"]) for p in sorted(glob.glob("/opt/alpha5/data/kalshi_crypto/composite/*_2026-10-1[1-4]_*.parquet"))]
    df = pd.concat(fr); df.to_parquet(f"{OUT}/composite.parquet", index=False); print("comp", len(df))
