"""
Trading-side Kalshi websocket: orderbook_delta + trade for one event's
markets on one connection. Separate from ws_raw_ob_collector, which is a
multi-series sweep with sharding and S3.
"""
import asyncio
import json
import time

import pandas as pd
import websockets

from kalshi_backtest.book import BookView, L2Book
from kalshi_backtest.core import Trade
from kalshi_backtest.data import MAX_SNAPSHOT_AGE_S
from kalshi_infra.kalshi import auth

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


class LiveKalshiFeed:
    """`on_quote(BookView)` / `on_trade(ticker, Trade)` fire synchronously
    on this coroutine's task.

    `resync_interval_s`: Kalshi sends orderbook_snapshot only on subscribe,
    so a long-lived connection accumulates delta-only state and drifts (44+
    min observed, see kalshi_backtest/book.py). A timed reconnect re-supplies
    snapshots before BookView's MAX_SNAPSHOT_AGE_S guard blanks the book, so
    this must stay below it.
    """

    def __init__(self, tickers: list[str], on_quote, on_trade, *, label: str,
                 resync_interval_s: float, reconnect_delay_s: float, recv_timeout_s: float,
                 open_timeout_s: float):
        if resync_interval_s >= MAX_SNAPSHOT_AGE_S:
            raise ValueError(f"resync_interval_s {resync_interval_s} must be < "
                             f"MAX_SNAPSHOT_AGE_S {MAX_SNAPSHOT_AGE_S}")
        self._tickers = list(tickers)
        self._on_quote = on_quote
        self._on_trade = on_trade
        self._books: dict[str, L2Book] = {t: L2Book(t) for t in self._tickers}
        self.label = label
        self.resync_interval_s = resync_interval_s
        self.reconnect_delay_s = reconnect_delay_s
        self.recv_timeout_s = recv_timeout_s
        self.open_timeout_s = open_timeout_s
        self.n_reconnects = 0
        self.n_seq_gaps = 0
        self.last_msg_mono: float | None = None

    @property
    def tickers(self) -> list[str]:
        return list(self._tickers)

    def book(self, ticker: str) -> BookView | None:
        b = self._books.get(ticker)
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        return BookView.from_quote(
            b.to_quote(pd.Timestamp.now(tz="UTC"), max_snapshot_age_s=MAX_SNAPSHOT_AGE_S), ladder=b)

    def seconds_since_message(self) -> float | None:
        return None if self.last_msg_mono is None else time.monotonic() - self.last_msg_mono

    async def run(self):
        if not self._tickers:
            raise ValueError(f"[{self.label}] no Kalshi markets to subscribe to")
        sub_id = 1
        while True:
            try:
                async with websockets.connect(WS_URL, additional_headers=auth.ws_headers(),
                                              open_timeout=self.open_timeout_s) as ws:
                    for channel in ("orderbook_delta", "trade"):
                        await ws.send(json.dumps({
                            "id": sub_id, "cmd": "subscribe",
                            "params": {"channels": [channel], "market_tickers": self._tickers},
                        }))
                        sub_id += 1
                    resync_at = time.monotonic() + self.resync_interval_s
                    # Per-connection counter shared across tickers; a jump means
                    # a dropped message and unverifiable ladders, so reconnect
                    # for fresh snapshots.
                    last_seq = None
                    while time.monotonic() <= resync_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout_s)
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_mono = time.monotonic()
                        d = json.loads(raw)
                        # Only orderbook messages carry this counter; trade
                        # messages on the same connection would otherwise read
                        # as a gap every time.
                        seq = d.get("seq") if d.get("type", "").startswith("orderbook") else None
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                self.n_seq_gaps += 1
                                print(f"[{self.label}] kalshi ws seq gap {last_seq} -> {seq}; reconnecting")
                                break
                            last_seq = seq
                        self._handle_message(d)
            except (websockets.ConnectionClosed, OSError) as e:
                print(f"[{self.label}] kalshi ws connection error ({e!r}); reconnecting")
                await asyncio.sleep(self.reconnect_delay_s)
            self.n_reconnects += 1

    def _emit_book(self, book: L2Book):
        now = pd.Timestamp.now(tz="UTC")
        self._on_quote(BookView.from_quote(book.to_quote(now, max_snapshot_age_s=MAX_SNAPSHOT_AGE_S),
                                           ladder=book))

    def _handle_message(self, d: dict):
        mtype = d.get("type")
        msg = d.get("msg", {})
        ticker = msg.get("market_ticker")
        book = self._books.get(ticker)
        if book is None:
            return
        if mtype == "orderbook_snapshot":
            now = pd.Timestamp.now(tz="UTC")
            for side_key, side in (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no")):
                levels = msg.get(side_key) or []
                book.apply_snapshot(side, [(float(p), float(q)) for p, q in levels], time=now)
            self._emit_book(book)
        elif mtype == "orderbook_delta":
            book.apply_delta(msg.get("side"), float(msg["price_dollars"]), float(msg["delta_fp"]))
            self._emit_book(book)
        elif mtype == "trade":
            now = pd.Timestamp.now(tz="UTC")
            # Priced in the MAKER's side terms (see core.Trade): an aggressive
            # yes buy depletes the resting NO queue at no_price. Sending the
            # yes price unconditionally meant no-side quotes never matched a
            # print and shadow recorded no maker fills on that side.
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
