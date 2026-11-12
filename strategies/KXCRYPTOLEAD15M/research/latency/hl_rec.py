"""Record Hyperliquid trades and best bid/offer with our receive times.

For two questions about HYPE: how late Hyperliquid's feed reaches the box
(its block time vs our receipt), and whether it leads the moves that pick off
KXCRYPTOLEAD15M's HYPE quotes (Binance's perp mostly doesn't: 11-05..07).

    python -m strategies.KXCRYPTOLEAD15M.research.latency.hl_rec [--coins HYPE,BTC]

Writes data/kalshi_crypto/hl_rec/{day}/{ms}.parquet every FLUSH_S.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import pandas as pd
import websockets

from infra import DATA_ROOT

URL = "wss://api.hyperliquid.xyz/ws"
FLUSH_S = 60.0


def _flush(rows: list[dict]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    day = pd.Timestamp(df.recv_ns.iloc[0], unit="ns", tz="UTC").strftime("%Y-%m-%d")
    d = os.path.join(DATA_ROOT, "kalshi_crypto", "hl_rec", day)
    os.makedirs(d, exist_ok=True)
    df.to_parquet(os.path.join(d, f"{int(time.time() * 1000)}.parquet"), compression="zstd", index=False)


async def run(coins: list[str]):
    rows: list[dict] = []
    last_flush = time.monotonic()
    while True:
        try:
            async with websockets.connect(URL, max_size=None, ping_interval=20) as ws:
                for c in coins:
                    for typ in ("trades", "bbo"):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": typ, "coin": c}}))
                seen_trades: set[str] = set()
                async for raw in ws:
                    recv = time.time_ns()
                    m = json.loads(raw)
                    ch, data = m.get("channel"), m.get("data")
                    if ch == "trades":
                        for t in data:
                            # The first message per feed replays recent trades.
                            if t["coin"] not in seen_trades:
                                continue
                            rows.append({"recv_ns": recv, "kind": "trade", "coin": t["coin"], "exch_ms": t["time"],
                                         "px": float(t["px"]), "sz": float(t["sz"]), "side": t["side"],
                                         "bid": None, "ask": None})
                        if data:
                            seen_trades.add(data[0]["coin"])
                    elif ch == "bbo":
                        b, a = data["bbo"]
                        rows.append({"recv_ns": recv, "kind": "bbo", "coin": data["coin"], "exch_ms": data["time"],
                                     "px": None, "sz": None, "side": None,
                                     "bid": float(b["px"]) if b else None, "ask": float(a["px"]) if a else None})
                    if time.monotonic() - last_flush >= FLUSH_S:
                        _flush(rows)
                        rows, last_flush = [], time.monotonic()
        except Exception as e:
            print(f"[hl_rec] {e!r}; reconnecting", flush=True)
            _flush(rows)
            rows = []
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="HYPE,BTC")
    asyncio.run(run(ap.parse_args().coins.split(",")))
