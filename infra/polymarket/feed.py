"""Polymarket US websockets for a trading process: market books and trades
on one connection, our own order executions on the private one.

Books come in the Kalshi feed's shape (backtest.book.L2Book: YES bids and NO
bids) so ARBS prices both venues alike: Polymarket's offers to sell YES at p
are NO bids at 1 - p. Every market message is the whole book, so each one is
a snapshot and there is nothing to drift.
"""
from __future__ import annotations

import asyncio
import json
import time

import pandas as pd
import websockets

from backtest.book import BookView, L2Book
from backtest.core import Trade
from backtest.data import MAX_SNAPSHOT_AGE_S
from infra.polymarket import WS, auth
from infra.polymarket.orders import parse_execution

MARKETS_PATH = "/v1/ws/markets"
PRIVATE_PATH = "/v1/ws/private"
OPEN_TIMEOUT_S = 15.0
RECONNECT_DELAY_S = 2.0
RECV_TIMEOUT_S = 5.0
# A book that hasn't changed resends nothing; resubscribing a batch brings
# fresh books, well inside BookView's MAX_SNAPSHOT_AGE_S staleness guard.
RESYNC_INTERVAL_S = 600.0
SUBSCRIBE_BATCH = 500
assert RESYNC_INTERVAL_S < MAX_SNAPSHOT_AGE_S
# Executions that end an order without our cancel being the reason we care:
# a post-only cross is REJECTED, an unfunded order is cancelled unsolicited.
GONE = ("EXECUTION_TYPE_REJECTED", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_EXPIRED",
        "EXECUTION_TYPE_DONE_FOR_DAY")


def _f(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def apply_book(book: L2Book, d: dict, now: pd.Timestamp):
    """A marketData message (YES bids and offers) onto an L2Book."""
    yes = [(_f(x.get("px")), _f(x.get("qty"))) for x in d.get("bids") or []]
    no = [(round(1.0 - p, 4), q) for p, q in ((_f(x.get("px")), _f(x.get("qty"))) for x in d.get("offers") or [])
          if p is not None]
    book.apply_snapshot("yes", [(p, q) for p, q in yes if p is not None and q], time=now)
    book.apply_snapshot("no", [(p, q) for p, q in no if q], time=now)


def trade_of(t: dict, now: pd.Timestamp) -> list[Trade]:
    """A public trade as core.Trade rows priced in the MAKER's side terms, as
    the Kalshi feed sends them: a taker buying YES at p filled a NO bid at 1 - p."""
    slug, p, q = t.get("marketSlug"), _f(t.get("price")), _f(t.get("quantity"))
    if not slug or p is None or not q:
        return []
    taker = t.get("taker") or {}
    intent = taker.get("intent") or ""
    bought_yes = intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT")
    if intent:
        return [Trade(ticker=slug, time=now, price=round(1.0 - p, 4) if bought_yes else p, size=q,
                      taker_side="yes" if bought_yes else "no")]
    return [Trade(ticker=slug, time=now, price=p, size=q, taker_side=None),
            Trade(ticker=slug, time=now, price=round(1.0 - p, 4), size=q, taker_side=None)]


class PolyFeed:
    def __init__(self, tickers, on_quote, on_trade, *, label, on_fill=None, on_gone=None, on_error=None):
        self._tickers = list(tickers)
        self._books: dict[str, L2Book] = {t: L2Book(t, 4) for t in self._tickers}
        self._on_quote, self._on_trade = on_quote, on_trade
        self._on_fill, self._on_gone, self._on_error = on_fill, on_gone, on_error
        self.label = label
        self._new: list[str] = []
        self.up = False
        self.down_since: float | None = time.monotonic()
        self.private_up = False
        self.n_reconnects = self.n_seq_gaps = self.n_errors = self.n_refreshes = self.n_fills = 0
        self.last_msg_mono: float | None = None

    @property
    def tickers(self) -> list[str]:
        return list(self._tickers)

    def add(self, tickers):
        new = [t for t in tickers if t not in self._books]
        for t in new:
            self._books[t] = L2Book(t, 4)
        self._tickers += new
        self._new += new

    def ladder(self, ticker: str):
        return self._books.get(ticker)

    def book(self, ticker: str):
        b = self._books.get(ticker)
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        return BookView.from_quote(b.to_quote(pd.Timestamp.now(tz="UTC"), max_snapshot_age_s=MAX_SNAPSHOT_AGE_S),
                                   ladder=b)

    def seconds_since_message(self) -> float | None:
        return None if self.last_msg_mono is None else time.monotonic() - self.last_msg_mono

    def _set_up(self, up: bool):
        if up and not self.up:
            self.down_since = None
        elif not up and self.up:
            self.down_since = time.monotonic()
        self.up = up

    async def run(self):
        await asyncio.gather(self._markets(), self._private())

    async def _subscribe(self, ws, slugs, tag: str):
        for i in range(0, len(slugs), SUBSCRIBE_BATCH):
            batch = slugs[i:i + SUBSCRIBE_BATCH]
            for typ in ("SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"):
                await ws.send(json.dumps({"subscribe": {"requestId": f"{tag}-{typ[18:].lower()}-{i}",
                                                        "subscriptionType": typ, "marketSlugs": batch}}))

    async def _markets(self):
        while not self._tickers:
            await asyncio.sleep(1.0)
        while True:
            try:
                async with websockets.connect(WS + MARKETS_PATH, additional_headers=auth.headers("GET", MARKETS_PATH),
                                              open_timeout=OPEN_TIMEOUT_S, max_size=None, ping_interval=20) as ws:
                    self._new = []
                    n_sub = 0
                    await self._subscribe(ws, list(self._tickers), f"s{n_sub}")
                    self._set_up(True)
                    resync_at = time.monotonic() + RESYNC_INTERVAL_S
                    while True:
                        if self._new:
                            new, self._new = self._new, []
                            n_sub += 1
                            await self._subscribe(ws, new, f"s{n_sub}")
                        if time.monotonic() >= resync_at:
                            # A repeat subscription answers with every book again; the
                            # server allows 10 per connection, so reconnect instead.
                            self.n_refreshes += 1
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_mono = time.monotonic()
                        self.handle_market(json.loads(raw))
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                self._set_up(False)
                print(f"[{self.label}] polymarket ws error ({e!r}); reconnecting", flush=True)
                await asyncio.sleep(RECONNECT_DELAY_S)
            self.n_reconnects += 1

    def handle_market(self, m: dict):
        now = pd.Timestamp.now(tz="UTC")
        if (d := m.get("marketData")) is not None:
            book = self._books.get(d.get("marketSlug"))
            if book is None:
                return
            apply_book(book, d, now)
            self._on_quote(BookView.from_quote(book.to_quote(now, max_snapshot_age_s=MAX_SNAPSHOT_AGE_S),
                                               ladder=book))
        elif (t := m.get("trade")) is not None:
            if t.get("marketSlug") in self._books:
                for tr in trade_of(t, now):
                    self._on_trade(tr.ticker, tr)
        elif "error" in m:
            self.n_errors += 1
            if self._on_error:
                self._on_error(f"polymarket ws error {m.get('requestId')}: {m['error']}")

    async def _private(self):
        if self._on_fill is None:
            return
        while not self._tickers:
            await asyncio.sleep(1.0)
        while True:
            try:
                async with websockets.connect(WS + PRIVATE_PATH, additional_headers=auth.headers("GET", PRIVATE_PATH),
                                              open_timeout=OPEN_TIMEOUT_S, max_size=None, ping_interval=20) as ws:
                    await ws.send(json.dumps({"subscribe": {"requestId": "orders",
                                                            "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
                                                            "marketSlugs": []}}))
                    self.private_up = True
                    async for raw in ws:
                        self.handle_private(json.loads(raw))
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"[{self.label}] polymarket private ws error ({e!r}); reconnecting", flush=True)
            self.private_up = False
            await asyncio.sleep(RECONNECT_DELAY_S)

    def handle_private(self, m: dict):
        ex = (m.get("orderSubscriptionUpdate") or {}).get("execution")
        if not ex:
            if "error" in m and self._on_error:
                self._on_error(f"polymarket private ws error: {m['error']}")
            return
        f = parse_execution(ex)
        if f is not None:
            self.n_fills += 1
            self._on_fill(f)
            return
        if ex.get("type") in GONE and self._on_gone is not None:
            o = ex.get("order") or {}
            if o.get("id"):
                self._on_gone(o["id"], ex.get("type"),
                              ex.get("orderRejectReason") or ex.get("unsolicitedCancelReason") or ex.get("text"))
