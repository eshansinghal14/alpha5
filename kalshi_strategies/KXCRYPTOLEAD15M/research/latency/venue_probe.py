"""Trade feeds of the Asian venues, timestamped on receipt, for comparing
where they arrive faster (a Tokyo relay vs direct to us-east-2).

Standalone (only `websockets`), so it runs on a bare probe instance:
    python3 venue_probe.py --seconds 600 --out probe_tokyo.csv
Each row: venue, coin, trade id, exchange time (ms), our receipt (ns).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time

import websockets

COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
rows: list[tuple] = []


async def binance():
    streams = "/".join(f"{c.lower()}usdt@trade" for c in COINS)
    async with websockets.connect(f"wss://fstream.binance.com/stream?streams={streams}", max_size=None) as ws:
        async for raw in ws:
            r = time.time_ns()
            d = json.loads(raw)["data"]
            rows.append(("binance_perp", d["s"][:-4], d["t"], d["T"], r))


async def okx():
    async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public", max_size=None) as ws:
        await ws.send(json.dumps({"op": "subscribe",
                                  "args": [{"channel": "trades", "instId": f"{c}-USDT-SWAP"} for c in COINS]}))
        async for raw in ws:
            r = time.time_ns()
            m = json.loads(raw)
            for t in m.get("data") or ():
                rows.append(("okx_perp", t["instId"].split("-")[0], t["tradeId"], int(t["ts"]), r))


async def hyperliquid():
    async with websockets.connect("wss://api.hyperliquid.xyz/ws", max_size=None) as ws:
        for c in COINS:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
        seen = set()
        async for raw in ws:
            r = time.time_ns()
            m = json.loads(raw)
            if m.get("channel") != "trades":
                continue
            for t in m["data"]:
                if t["coin"] in seen:
                    rows.append(("hyperliquid", t["coin"], t["tid"], t["time"], r))
            if m["data"]:
                seen.add(m["data"][0]["coin"])         # the first message replays history


async def main(seconds: float, out: str):
    tasks = [asyncio.create_task(f()) for f in (binance, okx, hyperliquid)]
    await asyncio.sleep(seconds)
    for t in tasks:
        t.cancel()
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["venue", "coin", "id", "exch_ms", "recv_ns"])
        w.writerows(rows)
    print(f"{len(rows)} rows -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--out", default="probe.csv")
    a = ap.parse_args()
    asyncio.run(main(a.seconds, a.out))
