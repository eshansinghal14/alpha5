"""Order gateway for ARBS: one KalshiOrderClient per exchange shard, every
write pinned to its market's shard, every blocking call on a worker thread.

Each method returns (result, exc, sent). sent=False means our own token
bucket refused it and nothing went to Kalshi.
"""
from __future__ import annotations

import asyncio
import collections
import functools
import itertools
import uuid

import pandas as pd

from kalshi_backtest.core import Order
from kalshi_infra.kalshi.orders import KalshiOrderClient, http_status


class Gateway:
    def __init__(self, pool, write_pool=None):
        """`pool` runs reads (fills, positions, balance); `write_pool` the
        order writes, so a write never queues behind them."""
        self.pool = pool
        self.write_pool = write_pool or pool
        self.clients: dict[int, KalshiOrderClient] = {}
        self.shard_of: dict[str, int] = {}
        self._ids = itertools.count(1)
        # Exchange ids of our IOCs: their fills are booked from the create
        # response, so the same fill arriving on the websocket is ignored.
        self._ioc_order = collections.deque(maxlen=20000)
        self.ioc_ids: set[str] = set()

    def setup(self, shard_of: dict[str, int], cap_fn) -> None:
        """Blocking (from_account reads /account/limits)."""
        self.shard_of = dict(shard_of)
        for s in sorted(set(shard_of.values())):
            if s not in self.clients:
                c = KalshiOrderClient.from_account(shard=s)
                c.cap_fn = cap_fn
                c.price_dp = 4
                self.clients[s] = c

    def client(self, ticker: str) -> KalshiOrderClient | None:
        s = self.shard_of.get(ticker)
        return None if s is None else self.clients.get(s)

    def any_client(self) -> KalshiOrderClient | None:
        return next(iter(self.clients.values()), None)

    def can_afford(self, ticker: str, kind: str, reserve: float) -> bool:
        c = self.client(ticker)
        return c is not None and c.can_afford(kind, reserve=reserve)

    def _order(self, ticker, side, price, size, intent, cid=None) -> Order:
        o = Order(order_id=next(self._ids), ticker=ticker, side=side, order_type="limit",
                  price=round(float(price), 4), size=float(size), decision_time=pd.Timestamp.now(tz="UTC"),
                  intent=intent)
        o.client_order_id = cid or str(uuid.uuid4())
        return o

    async def call(self, fn, *a, **kw):
        return await asyncio.get_running_loop().run_in_executor(self.pool, functools.partial(fn, *a, **kw))

    async def write(self, fn, *a, **kw):
        return await asyncio.get_running_loop().run_in_executor(self.write_pool, functools.partial(fn, *a, **kw))

    def _note_ioc(self, oid: str | None):
        if not oid:
            return
        if len(self._ioc_order) == self._ioc_order.maxlen:
            self.ioc_ids.discard(self._ioc_order[0])
        self._ioc_order.append(oid)
        self.ioc_ids.add(oid)

    async def ioc(self, ticker, side, price, size, *, force: bool, reason: str):
        c = self.client(ticker)
        if c is None or not c.allow_now("order", force=force, reason=reason):
            return None, None, False
        o = self._order(ticker, side, price, size, "take")
        try:
            fill = await self.write(c.place_ioc, o)
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited(reason)
            return None, e, True
        if fill is not None:
            self._note_ioc(fill.exchange_order_id)
        return fill, None, True

    async def rest(self, ticker, side, price, size, cid: str):
        c = self.client(ticker)
        if c is None or not c.allow_now("order", reason="quote"):
            return None, None, False
        o = self._order(ticker, side, price, size, "quote", cid=cid)
        try:
            return await self.write(c.place_resting, o), None, True
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited("quote")
            return None, e, True

    async def amend(self, ticker, side, exchange_id, price, remaining, filled):
        c = self.client(ticker)
        if c is None or not c.allow_now("amend", reason="move"):
            return None, None, False
        o = self._order(ticker, side, price, filled + remaining, "quote")
        try:
            return await self.write(c.amend, exchange_id, o, remaining, filled, price=round(price, 4)), None, True
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited("move")
            return None, e, True

    async def cancel(self, ticker, exchange_id, reason: str):
        """Always sent (force): a refused cancel leaves risk on the book."""
        c = self.client(ticker)
        if c is None:
            return None, RuntimeError(f"no order client for {ticker}"), False
        c.allow_now("cancel", force=True, reason=reason)
        try:
            return await self.write(c.cancel, exchange_id, ticker), None, True
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited("cancel")
            return None, e, True

    def usage(self) -> dict:
        return {str(s): {"tokens_now": round(c.tokens_now(), 1), "tokens_per_s": c.tokens_per_s,
                         "http_429": (u := c.usage()).get("http_429"), "refused": u.get("refused")}
                for s, c in self.clients.items()}
