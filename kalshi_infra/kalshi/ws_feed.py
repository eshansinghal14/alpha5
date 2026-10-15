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

# Kalshi sends orderbook_snapshot only on subscribe, so a long-lived
# connection accumulates delta-only state and drifts (44+ min observed, see
# kalshi_backtest/book.py). A timed reconnect re-supplies snapshots before
# BookView's MAX_SNAPSHOT_AGE_S guard blanks the book, so this must stay below it.
RESYNC_INTERVAL_S = 900.0
RECONNECT_DELAY_S = 1.0
RECV_TIMEOUT_S = 5.0     # only bounds how late the resync timer is checked
OPEN_TIMEOUT_S = 15.0
assert RESYNC_INTERVAL_S < MAX_SNAPSHOT_AGE_S


def parse_ws_fill(msg: dict) -> dict | None:
    """A `fill` channel message as KalshiOrderClient.fills' dict, or None if
    it can't be read. Price is in `outcome_side` terms (what the fill left us
    holding), the same convention the REST fills use; the WS message carries
    only the yes price, so a no fill is 1 - yes."""
    try:
        side = msg["outcome_side"]
        yes = float(msg["yes_price_dollars"])
        size = float(msg["count_fp"])
    except (KeyError, TypeError, ValueError):
        return None
    if side not in ("yes", "no") or size <= 0:
        return None
    ts = msg.get("ts_ms")
    return {"ticker": msg.get("market_ticker"), "side": side,
            "price": yes if side == "yes" else round(1.0 - yes, 4), "size": size,
            "fee": float(msg.get("fee_cost") or 0.0),
            "time": None if ts is None else pd.Timestamp(int(ts), unit="ms", tz="UTC"),
            "liquidity": "taker" if msg.get("is_taker") else "maker",
            "order_id": msg.get("order_id"), "trade_id": msg.get("trade_id")}


def parse_ws_print(msg: dict, received: pd.Timestamp) -> dict | None:
    """A public `trade` message as one row: yes price, size, taker side and
    both clocks. `time` is Kalshi's ts_ms when present (the clock our WS
    fills carry), else `received`."""
    try:
        yes = float(msg["yes_price_dollars"])
        size = float(msg["count_fp"])
    except (KeyError, TypeError, ValueError):
        return None
    ts = msg.get("ts_ms")
    return {"time": received if ts is None else pd.Timestamp(int(ts), unit="ms", tz="UTC"),
            "received": received, "ticker": msg.get("market_ticker"), "trade_id": msg.get("trade_id"),
            "yes_price": yes, "size": size, "taker_side": msg.get("taker_side")}


class LiveKalshiFeed:
    """`on_quote(BookView)` / `on_trade(ticker, Trade)` fire synchronously
    on this coroutine's task."""

    def __init__(self, tickers: list[str], on_quote, on_trade, *, label: str, on_fill=None, on_print=None):
        """`on_fill(dict)` (optional): OUR fills from the authenticated `fill`
        channel, in KalshiOrderClient.fills' dict shape. Only fills on these
        tickers are delivered. `on_print(dict)` (optional): every public
        trade once, in yes terms (see parse_ws_print)."""
        self._tickers = list(tickers)
        self._on_quote = on_quote
        self._on_trade = on_trade
        self._on_fill = on_fill
        self._on_print = on_print
        self.n_fills = 0
        self._books: dict[str, L2Book] = {t: L2Book(t) for t in self._tickers}
        self.label = label
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
                                              open_timeout=OPEN_TIMEOUT_S) as ws:
                    channels = ("orderbook_delta", "trade") + (("fill",) if self._on_fill else ())
                    for channel in channels:
                        await ws.send(json.dumps({
                            "id": sub_id, "cmd": "subscribe",
                            "params": {"channels": [channel], "market_tickers": self._tickers},
                        }))
                        sub_id += 1
                    resync_at = time.monotonic() + RESYNC_INTERVAL_S
                    # Per-connection counter shared across tickers; a jump means
                    # a dropped message and unverifiable ladders, so reconnect
                    # for fresh snapshots.
                    last_seq = None
                    while time.monotonic() <= resync_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
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
                await asyncio.sleep(RECONNECT_DELAY_S)
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
        if mtype == "fill":
            f = parse_ws_fill(msg)
            if f is not None and self._on_fill is not None:
                self.n_fills += 1
                self._on_fill(f)
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
            if self._on_print is not None:
                row = parse_ws_print(msg, now)
                if row is not None:
                    self._on_print(row)
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
