"""
Kalshi WS feed for one KXCRYPTOLEAD15M event's 5 markets.

Variant of kalshi_latency_arbs/live/live_kalshi_ws.py's LiveKalshiFeed:
same WS protocol, same auth, same sequence-gap recovery, same book/
trade conventions -- but it accepts an explicit `tickers` list rather
than discovering markets via `_kxinx_event_markets` (which is
KXINX-specific).

Kept as its own file (not a subclass) because the construction site
differs enough that a parameter would just be awkward, and the
duplicated protocol section is small.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import sys
import time

import pandas as pd
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_INFRA_DIR = os.path.join(_PROJECT, "kalshi_infra")
_BACKTEST_DIR = os.path.join(_PROJECT, "kalshi_backtest")
for _p in (_INFRA_DIR, _BACKTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api_functions import API_KEY
from core import Trade
from book import L2Book, BookView
from data import MAX_SNAPSHOT_AGE_S

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
# Mirror live_kalshi_ws.py's key resolution: the key sits in
# kalshi_btc_hedge/eshan-ws-key.txt in this repo (also used by the SPX
# live stack).
WS_KEY_PATH = os.path.join(_PROJECT, "kalshi_btc_hedge", "eshan-ws-key.txt")
RECONNECT_DELAY_S = 3.0
RESYNC_INTERVAL_S = 900.0


def _ws_auth_headers():
    with open(WS_KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + "GET" + "/trade-api/ws/v2").encode()
    sig = key.sign(
        msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
    )
    return [
        ("KALSHI-ACCESS-KEY", API_KEY),
        ("KALSHI-ACCESS-SIGNATURE", base64.b64encode(sig).decode()),
        ("KALSHI-ACCESS-TIMESTAMP", ts),
    ]


class LiveCryptoKalshiFeed:
    """One WS connection, subscribed to orderbook_delta + trade for the
    five KXCRYPTOLEAD15M markets of this event."""

    def __init__(self, event_ticker: str, tickers: list[str],
                 on_quote, on_trade):
        self.event_ticker = event_ticker
        self._tickers = list(tickers)
        self._on_quote = on_quote
        self._on_trade = on_trade
        self._books: dict[str, L2Book] = {t: L2Book(t) for t in self._tickers}

    def book(self, ticker: str) -> BookView | None:
        b = self._books.get(ticker)
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        return BookView.from_quote(
            b.to_quote(pd.Timestamp.now(tz="UTC"), max_snapshot_age_s=MAX_SNAPSHOT_AGE_S), ladder=b)

    async def run(self):
        if not self._tickers:
            raise ValueError(f"no Kalshi markets for {self.event_ticker}")
        sub_id = 1
        while True:
            try:
                async with websockets.connect(WS_URL, additional_headers=_ws_auth_headers(), open_timeout=15) as ws:
                    await ws.send(json.dumps({
                        "id": sub_id, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": self._tickers},
                    }))
                    sub_id += 1
                    await ws.send(json.dumps({
                        "id": sub_id, "cmd": "subscribe",
                        "params": {"channels": ["trade"], "market_tickers": self._tickers},
                    }))
                    sub_id += 1
                    resync_at = time.monotonic() + RESYNC_INTERVAL_S
                    last_seq = None
                    while True:
                        if time.monotonic() > resync_at:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            continue
                        d = json.loads(raw)
                        seq = d.get("seq") if d.get("type", "").startswith("orderbook") else None
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                print(f"[kalshi-ws/crypto] seq gap {last_seq} -> {seq} -- reconnecting")
                                break
                            last_seq = seq
                        self._handle_message(d)
            except (websockets.ConnectionClosed, OSError) as e:
                print(f"[kalshi-ws/crypto] connection error ({e!r}); reconnecting")
                await asyncio.sleep(RECONNECT_DELAY_S)

    def _handle_message(self, d: dict):
        mtype = d.get("type")
        msg = d.get("msg", {})
        if mtype == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            book = self._books.get(ticker)
            if book is None:
                return
            now = pd.Timestamp.now(tz="UTC")
            for side_key, side in (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no")):
                levels = msg.get(side_key) or []
                book.apply_snapshot(side, [(float(p), float(q)) for p, q in levels], time=now)
            self._on_quote(BookView.from_quote(
                book.to_quote(now, max_snapshot_age_s=MAX_SNAPSHOT_AGE_S), ladder=book))
        elif mtype == "orderbook_delta":
            ticker = msg.get("market_ticker")
            book = self._books.get(ticker)
            if book is None:
                return
            book.apply_delta(msg.get("side"), float(msg["price_dollars"]), float(msg["delta_fp"]))
            now = pd.Timestamp.now(tz="UTC")
            self._on_quote(BookView.from_quote(
                book.to_quote(now, max_snapshot_age_s=MAX_SNAPSHOT_AGE_S), ladder=book))
        elif mtype == "trade":
            ticker = msg.get("market_ticker")
            if ticker not in self._books:
                return
            now = pd.Timestamp.now(tz="UTC")
            taker_side = msg.get("taker_side")
            yes_price = float(msg["yes_price_dollars"])
            no_price = float(msg["no_price_dollars"])
            if taker_side == "yes":
                maker_prices = [no_price]
            elif taker_side == "no":
                maker_prices = [yes_price]
            else:
                maker_prices = [yes_price, no_price]
            for price in maker_prices:
                self._on_trade(ticker, Trade(ticker=ticker, time=now, price=price,
                                             size=float(msg["count_fp"]), taker_side=taker_side))


__all__ = ["LiveCryptoKalshiFeed"]
