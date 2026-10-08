"""
Dedicated single-event live Kalshi WebSocket feed. NOT a reuse of
ws_raw_ob_collector.py (which is built for a ~42-series multi-market
sweep); this is the trading-side connection, one event's handful of
markets, no sharding or S3. Protocol pieces (URL, RSA-PSS auth,
subscribe shape) duplicated rather than imported.
"""
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

_INFRA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "kalshi_infra")
_BACKTEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "kalshi_backtest")
for _p in (_INFRA_DIR, _BACKTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api_functions import API_KEY
from databento_functions import _kxinx_event_markets
from core import Trade
from book import L2Book, BookView
from data import MAX_SNAPSHOT_AGE_S

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_KEY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "kalshi_btc_hedge", "eshan-ws-key.txt")
RECONNECT_DELAY_S = 3.0
# Kalshi sends orderbook_snapshot only on SUBSCRIBE; a long-lived
# connection accumulates delta-only state and drifts (44+ min observed,
# see kalshi_backtest/book.py). to_quote() uses max_snapshot_age_s=1800s,
# so a timed reconnect every 15min re-supplies fresh snapshots before the
# guard blanks the book.
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


class LiveKalshiFeed:
    """One WS connection, subscribed to orderbook_delta + trade for just
    this event's own markets.     `on_quote`/`on_trade` fire synchronously on
    the same asyncio task this coroutine runs on -- no cross-thread
    hand-off needed."""

    def __init__(self, event_ticker: str, on_quote, on_trade):
        self.event_ticker = event_ticker
        self._on_quote = on_quote
        self._on_trade = on_trade
        self._books: dict[str, L2Book] = {}
        markets = _kxinx_event_markets(event_ticker)
        self._tickers = [m["ticker"] for m in markets]
        for t in self._tickers:
            self._books[t] = L2Book(t)

    def book(self, ticker: str) -> BookView | None:
        b = self._books.get(ticker)
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        return BookView.from_quote(
            b.to_quote(pd.Timestamp.now(tz="UTC"), max_snapshot_age_s=MAX_SNAPSHOT_AGE_S), ladder=b)

    async def run(self):
        if not self._tickers:
            raise ValueError(f"no Kalshi markets found for {self.event_ticker}")
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
                    # Per-connection counter shared across this event's
                    # tickers; a jump means Kalshi dropped a message and the
                    # ladders are now unverifiable, so take the same recovery
                    # as the collector: reconnect for fresh snapshots.
                    last_seq = None
                    while True:
                        if time.monotonic() > resync_at:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            continue
                        d = json.loads(raw)
                        # Only orderbook messages carry the counter this
                        # tracks -- this connection also carries "trade",
                        # which the collector deliberately keeps on a
                        # separate connection, so interleaving it here would
                        # otherwise read as a gap on every trade.
                        seq = d.get("seq") if d.get("type", "").startswith("orderbook") else None
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                print(f"[kalshi-ws] seq gap {last_seq} -> {seq} "
                                      f"({seq - last_seq - 1} missed) -- reconnecting to resync")
                                break
                            last_seq = seq
                        self._handle_message(d)
            except (websockets.ConnectionClosed, OSError):
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
            # Priced in the MAKER's side terms, matching data.load_trades (see
            # its docstring) -- an aggressive yes buy depletes the resting NO
            # queue, at no_price. QueueTracker matches a resting order to a
            # print by plain equality against the order's own side price, so
            # sending the yes price unconditionally (as this did) meant a
            # no-side quote could never match one and shadow quoting recorded
            # no maker fills on that entire side.
            taker_side = msg.get("taker_side")
            yes_price, no_price = float(msg["yes_price_dollars"]), float(msg["no_price_dollars"])
            if taker_side == "yes":
                maker_prices = [no_price]
            elif taker_side == "no":
                maker_prices = [yes_price]
            else:
                maker_prices = [yes_price, no_price]
            for price in maker_prices:
                self._on_trade(ticker, Trade(ticker=ticker, time=now, price=price,
                                             size=float(msg["count_fp"]), taker_side=taker_side))
