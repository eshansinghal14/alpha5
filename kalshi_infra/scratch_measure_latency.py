"""Temp diagnostic: measure real-world latency against Kalshi for the
KXHIGHLAX (LA high temp) market.

Two independent measurements:

1. WS market-data latency (`measure_ws_latency`): for every orderbook_delta
   message on today's open KXHIGHLAX markets, compares Kalshi's own exchange
   timestamp (`msg["ts"]`, ISO 8601) against local receive time. Read-only,
   no risk -- safe to run anytime.

2. Order round-trip latency (`measure_order_roundtrip`): times POST .../orders
   (send -> resting) and DELETE .../orders/{id} (cancel send -> confirmed)
   against Kalshi's REST API. THIS PLACES REAL ORDERS -- see the big warning
   in that function's docstring before running it.

Usage:
    python -m kalshi_infra.scratch_measure_latency ws [duration_seconds]
    python -m kalshi_infra.scratch_measure_latency order [n_trials]
"""
import base64
import datetime
import json
import sys
import time
import uuid

import numpy as np
import requests
import websockets
import asyncio
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import os

from kalshi_infra import api_functions as af
from kalshi_infra.api_functions import event_ticker_from_date, API_KEY, BASE_URL
from kalshi_infra.kalshi.auth import private_key_path

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_KEY_PATH = private_key_path()


def _ws_auth_headers():
    with open(WS_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + "GET" + "/trade-api/ws/v2").encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return [
        ("KALSHI-ACCESS-KEY", API_KEY),
        ("KALSHI-ACCESS-SIGNATURE", base64.b64encode(sig).decode()),
        ("KALSHI-ACCESS-TIMESTAMP", ts),
    ]


# ---------------------------------------------------------------------------
# 1. WS market-data latency -- read-only, safe
# ---------------------------------------------------------------------------

async def _collect_ws_latencies(event_ticker: str, duration_s: float) -> list[float]:
    markets = af.kalshi_get(
        "/trade-api/v2/markets", params={"event_ticker": event_ticker, "status": "open"}, auth=True
    ).get("markets", [])
    tickers = [m["ticker"] for m in markets]
    if not tickers:
        print(f"No open markets for {event_ticker}")
        return []
    print(f"Subscribing to {len(tickers)} markets in {event_ticker} for {duration_s:.0f}s...")

    latencies_ms = []
    async with websockets.connect(WS_URL, additional_headers=_ws_auth_headers(), open_timeout=15) as ws:
        await ws.send(json.dumps({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers},
        }))
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            receive_time = datetime.datetime.now(datetime.timezone.utc)
            d = json.loads(raw)
            msg = d.get("msg", {})
            ts_str = msg.get("ts")
            if not ts_str:
                continue
            exchange_time = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            latency_ms = (receive_time - exchange_time).total_seconds() * 1000.0
            latencies_ms.append(latency_ms)
            if len(latencies_ms) % 100 == 0:
                print(f"  [{len(latencies_ms)} msgs] last={latency_ms:.1f}ms")
    return latencies_ms


def measure_ws_latency(duration_s: float = 60):
    today = datetime.date.today()
    event_ticker = event_ticker_from_date(today)
    latencies = asyncio.run(_collect_ws_latencies(event_ticker, duration_s))
    if not latencies:
        print("No messages received -- market may be quiet right now, try a longer duration.")
        return
    arr = np.array(latencies)
    print(f"\n=== WS latency ({len(arr)} messages, {event_ticker}) ===")
    print(f"  mean={arr.mean():.1f}ms  median={np.median(arr):.1f}ms  "
          f"p95={np.percentile(arr, 95):.1f}ms  p99={np.percentile(arr, 99):.1f}ms  "
          f"min={arr.min():.1f}ms  max={arr.max():.1f}ms")


# ---------------------------------------------------------------------------
# 2. Order round-trip latency -- PLACES REAL ORDERS, see warning below
# ---------------------------------------------------------------------------

def _kalshi_request(method: str, path: str, body: dict = None) -> tuple[float, dict]:
    headers = af._auth_headers(method, path)
    headers["Content-Type"] = "application/json"
    t0 = time.perf_counter()
    resp = requests.request(method, BASE_URL + path, headers=headers, json=body, timeout=10)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    return elapsed_ms, resp.json()


def measure_order_roundtrip(n_trials: int = 5):
    """
    !!! THIS PLACES REAL LIVE ORDERS ON YOUR KALSHI ACCOUNT !!!

    For each trial: places a 1-contract YES limit buy at $0.01 (the minimum
    possible price/size) on today's most out-of-the-money open KXHIGHLAX
    market -- chosen specifically because it's the least likely strike to
    trade near 1c and fill during the ~1s window before cancellation -- then
    immediately cancels it. Times both legs separately:
      - POST .../orders  (send -> order acknowledged/resting)
      - DELETE .../orders/{id}  (cancel send -> confirmed)

    Worst-case financial exposure per trial if a fill somehow occurs anyway:
    $0.01 (1 contract at 1 cent). Do not run this unless you've confirmed
    you want real orders touching your live account.
    """
    today = datetime.date.today()
    event_ticker = event_ticker_from_date(today)
    markets = af.kalshi_get(
        "/trade-api/v2/markets", params={"event_ticker": event_ticker, "status": "open"}, auth=True
    ).get("markets", [])
    if not markets:
        print(f"No open markets for {event_ticker}")
        return

    # Furthest tail market = least likely to trade near 1c in the next second.
    tail_markets = [m for m in markets if m["ticker"].rsplit("-", 1)[-1].startswith("T")]
    target = (tail_markets or markets)[0]
    ticker = target["ticker"]
    print(f"Using {ticker} for order round-trip test ({n_trials} trials).")
    print("!!! This places real $0.01 limit orders and immediately cancels them. !!!")

    post_ms, cancel_ms = [], []
    for i in range(n_trials):
        client_order_id = str(uuid.uuid4())
        body = {
            "action": "buy", "side": "yes", "ticker": ticker,
            "type": "limit", "yes_price": 1, "count": 1,
            "client_order_id": client_order_id,
        }
        try:
            elapsed, resp = _kalshi_request("POST", "/trade-api/v2/portfolio/orders", body)
        except requests.exceptions.HTTPError as e:
            print(f"  [trial {i+1}] POST failed: {e.response.status_code} {e.response.text[:200]}")
            continue
        post_ms.append(elapsed)
        order_id = resp.get("order", {}).get("order_id")
        print(f"  [trial {i+1}] POST order: {elapsed:.1f}ms  order_id={order_id}")

        if order_id:
            try:
                elapsed, _ = _kalshi_request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")
                cancel_ms.append(elapsed)
                print(f"  [trial {i+1}] DELETE cancel: {elapsed:.1f}ms")
            except requests.exceptions.HTTPError as e:
                print(f"  [trial {i+1}] cancel failed (may have filled!): "
                      f"{e.response.status_code} {e.response.text[:200]}")
        time.sleep(1)

    if post_ms:
        arr = np.array(post_ms)
        print(f"\n=== Order placement (POST) latency, n={len(arr)} ===")
        print(f"  mean={arr.mean():.1f}ms  median={np.median(arr):.1f}ms  min={arr.min():.1f}ms  max={arr.max():.1f}ms")
    if cancel_ms:
        arr = np.array(cancel_ms)
        print(f"\n=== Order cancel (DELETE) latency, n={len(arr)} ===")
        print(f"  mean={arr.mean():.1f}ms  median={np.median(arr):.1f}ms  min={arr.min():.1f}ms  max={arr.max():.1f}ms")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "ws"
    if mode == "ws":
        duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60
        measure_ws_latency(duration)
    elif mode == "order":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        measure_order_roundtrip(n)
    else:
        print("Usage: python -m kalshi_infra.scratch_measure_latency [ws|order] [param]")
