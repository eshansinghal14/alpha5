"""Where does feed delay come from on the live box?

A bare listener (no parsing on the hot path, normal priority) receives the same
streams as the live processes for DUR s and stamps each raw message on arrival:
  Binance USDT perps trade, OKX swap trades, Coinbase Exchange matches.
Afterwards, per trade id, compare with what the live processes stamped:
  perp_prints/ recv_ts   (venue process, 2 copies: race + KXSOL15M)
  prints/ recv_ts        (Coinbase, the race process's PrintLog)
live - bare = time the live process held the message before stamping it, i.e.
queueing behind its own work. Also: the same by sub-second phase (does it spike
right after each wall second -> the 1 s recorder sample) and by second within
the minute (-> the 60 s parquet flush)."""
import asyncio, glob, json, os, sys, time
import numpy as np, pandas as pd, websockets
import pyarrow.parquet as pq, pyarrow.compute as pc
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 180
COINS = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt", "HYPE": "hypeusdt"}
OKX = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "HYPE": "HYPE-USDT-SWAP"}
CB = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "HYPE-USD"]
raw = {"binance": [], "okx": [], "coinbase": []}
T0 = time.time()


async def listen(name, url, subs=(), ping=None):
    end = T0 + DUR
    while time.time() < end:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                for s in subs:
                    await ws.send(json.dumps(s))
                async def pinger():
                    while True:
                        await asyncio.sleep(20)
                        await ws.send(ping)
                pt = asyncio.ensure_future(pinger()) if ping else None
                try:
                    while (left := end - time.time()) > 0:
                        try:
                            m = await asyncio.wait_for(ws.recv(), timeout=left)
                        except asyncio.TimeoutError:
                            return
                        raw[name].append((time.time(), m))
                finally:
                    if pt:
                        pt.cancel()
        except Exception as e:
            print(name, "reconnect", repr(e)[:80], flush=True)
            await asyncio.sleep(0.5)


async def main():
    await asyncio.gather(
        listen("binance", "wss://fstream.binance.com/stream?streams=" + "/".join(f"{s}@trade" for s in COINS.values())),
        listen("okx", "wss://ws.okx.com:8443/ws/v5/public",
               [{"op": "subscribe", "args": [{"channel": "trades", "instId": i} for i in OKX.values()]}], ping="ping"),
        listen("coinbase", "wss://ws-feed.exchange.coinbase.com",
               [{"type": "subscribe", "product_ids": CB, "channels": ["matches"]}]))
asyncio.run(main())
T1 = time.time()
print(f"recorded {T1 - T0:.0f}s:", {k: len(v) for k, v in raw.items()}, flush=True)

bare = []
sym = {v: k for k, v in COINS.items()}
okx = {v: k for k, v in OKX.items()}
for rt, m in raw["binance"]:
    d = json.loads(m).get("data") or {}
    if d.get("e") == "trade":
        bare.append(("binance_perp", sym.get(d["s"].lower()), str(d["t"]), rt, d["T"] / 1e3))
for rt, m in raw["okx"]:
    if m == "pong":
        continue
    d = json.loads(m)
    if (d.get("arg") or {}).get("channel") == "trades":
        for t in d.get("data", []):
            bare.append(("okx_perp", okx.get(t["instId"]), str(t["tradeId"]), rt, int(t["ts"]) / 1e3))
for rt, m in raw["coinbase"]:
    d = json.loads(m)
    if d.get("type") == "match":
        bare.append(("coinbase", d["product_id"].split("-")[0], str(d["trade_id"]), rt, None))
B = pd.DataFrame(bare, columns=["venue", "coin", "trade_id", "bare_recv", "exch_ts"])
del raw

print("waiting 70 s for the live processes to flush their print files...", flush=True)
time.sleep(70)
day = pd.Timestamp(T0, unit="s", tz="UTC").strftime("%Y-%m-%d")
live = []
for f in glob.glob(f"/opt/alpha5/data/kalshi_crypto/perp_prints/{day}/*.parquet"):
    if os.path.getmtime(f) < T0:
        continue
    try:
        t = pq.read_table(f, columns=["recv_ts", "venue", "trade_id"]).to_pandas()
    except Exception:
        continue
    live.append(t[t.recv_ts.between(T0 - 1, T1 + 1)])
for f in glob.glob(f"/opt/alpha5/data/kalshi_crypto/prints/*_{day}_*.parquet"):
    if os.path.getmtime(f) < T0:
        continue
    try:
        t = pq.read_table(f, columns=["recv_ts", "venue", "trade_id"]).to_pandas()
    except Exception:
        continue
    t = t[t.venue == "coinbase"]
    live.append(t[t.recv_ts.between(T0 - 1, T1 + 1)])
L = pd.concat(live)
L["trade_id"] = L.trade_id.astype(str)
L = L.rename(columns={"recv_ts": "live_recv"})
# perp_prints has one row per venue-process copy (race + SOL): keep each copy
M = B.merge(L, on=["venue", "trade_id"], how="inner")
M["hold_ms"] = (M.live_recv - M.bare_recv) * 1e3
M["net_ms"] = np.where(M.exch_ts.notna(), (M.bare_recv - M.exch_ts) * 1e3, np.nan)
q = lambda s: pd.Series({"n": len(s), "p10": s.quantile(.1), "p50": s.median(), "p90": s.quantile(.9),
                         "p99": s.quantile(.99), "max": s.max()})
pd.set_option("display.width", 250)
print("\n=== bare listener: exchange time -> receipt (ms), the network part ===")
print(B.assign(ms=(B.bare_recv - B.exch_ts) * 1e3).dropna().groupby("venue").ms.apply(q).unstack().round(1).to_string())
print("\n=== live process stamp - bare receipt (ms) = time held inside our process ===")
print(M.groupby("venue").hold_ms.apply(q).unstack().round(1).to_string())
M["phase_ms"] = (M.bare_recv % 1.0) * 1000
M["phase"] = pd.cut(M.phase_ms, [0, 50, 100, 200, 500, 1000], right=False)
M["min_s"] = (M.bare_recv % 60).astype(int)
M["min_b"] = pd.cut(M.min_s, [0, 2, 5, 30, 55, 60], right=False)
print("\n=== hold (ms) by phase within the wall second (recorder samples at each second) ===")
print(M.groupby(["venue", "phase"], observed=True).hold_ms.agg(["size", "median", lambda s: s.quantile(.9), lambda s: s.quantile(.99)]).round(1).to_string())
print("\n=== hold (ms) by second within the minute (60 s flushes) ===")
print(M.groupby(["venue", "min_b"], observed=True).hold_ms.agg(["size", "median", lambda s: s.quantile(.9), lambda s: s.quantile(.99)]).round(1).to_string())
# burstiness: hold vs message rate in the surrounding 100 ms
B2 = B[B.venue == "binance_perp"].sort_values("bare_recv")
rate = pd.Series(1, index=pd.to_datetime(B2.bare_recv, unit="s")).rolling("100ms").sum()
Mb = M[M.venue == "binance_perp"].copy()
Mb["rate100"] = rate.reindex(pd.to_datetime(Mb.bare_recv, unit="s"), method="nearest").to_numpy()
Mb["rb"] = pd.qcut(Mb.rate100, 4, duplicates="drop")
print("\n=== Binance hold (ms) by message burst size (prints in the last 100 ms) ===")
print(Mb.groupby("rb", observed=True).hold_ms.agg(["size", "median", lambda s: s.quantile(.9), lambda s: s.quantile(.99)]).round(1).to_string())
M.to_parquet("/tmp/qprobe.parquet")
