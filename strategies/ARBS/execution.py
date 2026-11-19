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

from backtest.core import Order
from infra.kalshi.orders import KalshiOrderClient, http_status
from infra.polymarket.orders import PM_SHARD, PolymarketOrderClient

PM_CANCEL_WAIT_S = 2.0
# Polymarket cancels that may wait PM_CANCEL_WAIT_S for the bucket: the quote
# still has its edge (dropped from the hot list, another basket's duplicate).
# These came in the bursts that drew the 429s (up to 93 not_hot in one
# second, 11-15). Every other cancel leaves a price we no longer want and
# goes now: broken-basket cancels waited the whole 2 s (p50 2,087 ms, 11-15).
PM_ROUTINE_CANCELS = frozenset({"not_hot", "duplicate", "deep_off", "excluded", "venue", "whole"})
# An urgent cancel may overdraw the bucket by this much: 5 requests, the gap
# between our 20/s and Polymarket's documented 25/s per IP.
PM_URGENT_OVERDRAW = 50.0
# A Kalshi cancel is forced past the shard's bucket only this far (5 cancels);
# beyond it, it waits for refill (~7 ms a cancel at 300/s). Always forced, three
# processes' pulls drew 4,486 cancel 429s (11-16 06:51-15:50): a 429'd cancel
# cancels nothing, is retried at once, and keeps Kalshi's bucket empty for
# quotes and completions too.
KALSHI_CANCEL_OVERDRAW = 10.0


class Frozen(RuntimeError):
    """A write refused because this process is not the active one (a
    standby, or one handing over): nothing went to the exchange."""


class Gateway:
    # No order writes at all while set (a standby, or a process handing over):
    # creates, moves and IOCs answer "not sent", cancels and group calls fail
    # with Frozen.
    frozen = False

    def __init__(self, pool, write_pool=None):
        """`pool` runs reads (fills, positions, balance); `write_pool` the
        order writes, so a write never queues behind them."""
        self.pool = pool
        self.write_pool = write_pool or pool
        self.clients: dict[int, KalshiOrderClient] = {}
        self.shard_of: dict[str, int] = {}
        self.cancel_wait: dict[str, list] = {}         # PM cancel reason -> [held, total s, max s]
        self._ids = itertools.count(1)
        # Exchange ids of our IOCs: their fills are booked from the create
        # response, so the same fill arriving on the websocket is ignored.
        self._ioc_order = collections.deque(maxlen=20000)
        self.ioc_ids: set[str] = set()

    def setup(self, shard_of: dict[str, int], cap_fn) -> None:
        """Blocking (from_account reads /account/limits). Adds to shard_of,
        never forgets: a market's shard doesn't change, and a quote still
        resting on a market the scan dropped must stay cancellable (one sat
        on SENATEIN-28-R with every sweep failing "no order client", 11-17)."""
        self.shard_of.update(shard_of)
        for s in sorted(set(shard_of.values())):
            if s not in self.clients:
                # Kalshi's whole bucket (900 tokens at Advanced): holding one
                # second, bursts refused 4-5% of quote creates and moves at 88%
                # average use (11-15). Every ARBS process passes it, so the
                # shard's shared bucket keeps one capacity.
                c = PolymarketOrderClient() if s == PM_SHARD else KalshiOrderClient.from_account(shard=s, full_burst=True)
                c.cap_fn = cap_fn
                c.price_dp = 4
                self.clients[s] = c

    def burst_s(self) -> float:
        """Seconds of refill the Kalshi buckets hold (1 before setup)."""
        return max((c.budget.capacity / c.budget.rate for c in self.clients.values()
                    if isinstance(c, KalshiOrderClient) and c.budget.rate), default=1.0)

    def client(self, ticker: str) -> KalshiOrderClient | None:
        s = self.shard_of.get(ticker)
        return None if s is None else self.clients.get(s)

    def any_client(self) -> KalshiOrderClient | None:
        return self.kalshi_client() or next(iter(self.clients.values()), None)

    def kalshi_client(self) -> KalshiOrderClient | None:
        """Any Kalshi shard's client: account-wide reads (fills, positions, balance)."""
        return next((c for s, c in self.clients.items() if s != PM_SHARD), None)

    def venue_clients(self) -> list:
        """One client per exchange, for account-wide reads."""
        return [c for c in (self.kalshi_client(), self.clients.get(PM_SHARD)) if c is not None]

    def balances(self, reserve: float) -> tuple[dict[int, float], float]:
        """Blocking. (cash by shard, positions' value) over both exchanges."""
        cash, value = {}, 0.0
        for c in self.venue_clients():
            got, v = c.shard_balances(reserve)
            cash.update(got)
            value += v
        return cash, value

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
        if self.frozen:
            raise Frozen("order writes frozen")
        return await asyncio.get_running_loop().run_in_executor(self.write_pool, functools.partial(fn, *a, **kw))

    def _note_ioc(self, oid: str | None):
        if not oid:
            return
        if len(self._ioc_order) == self._ioc_order.maxlen:
            self.ioc_ids.discard(self._ioc_order[0])
        self._ioc_order.append(oid)
        self.ioc_ids.add(oid)

    async def ioc(self, ticker, side, price, size, *, force: bool, reason: str):
        if self.frozen:
            return None, None, False
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

    async def rest(self, ticker, side, price, size, cid: str, group: str | None = None):
        if self.frozen:
            return None, None, False
        c = self.client(ticker)
        if c is None or not c.allow_now("order", reason="quote"):
            return None, None, False
        o = self._order(ticker, side, price, size, "quote", cid=cid)
        try:
            return await self.write(c.place_resting, o, order_group_id=group), None, True
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited("quote")
            return None, e, True

    async def amend(self, ticker, side, exchange_id, price, remaining, filled):
        if self.frozen:
            return None, None, False
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

    async def group_call(self, ticker, method: str, *args):
        """(result, exc) of an order-group create/reset/delete on ticker's shard."""
        if self.frozen:
            return None, Frozen("order writes frozen")
        c = self.client(ticker)
        if c is None:
            return None, RuntimeError(f"no order client for {ticker}")
        c.allow_now("order", force=True, reason="order_group")
        try:
            return await self.write(getattr(c, method), *args), None
        except Exception as e:
            return None, e

    async def cancel(self, ticker, exchange_id, reason: str):
        """Always sent, forced past the bucket if it must, but only while the
        overdraw allows (KALSHI_CANCEL_OVERDRAW, PM_URGENT_OVERDRAW): a refused
        cancel leaves risk on the book, and a 429'd one cancels nothing."""
        if self.frozen:
            return None, Frozen("order writes frozen"), False
        c = self.client(ticker)
        if c is None:
            return None, RuntimeError(f"no order client for {ticker}"), False
        if c.shard == PM_SHARD:
            # Paced on Polymarket's per-IP bucket: pulling a game's quotes at
            # once sent dozens of cancels in a second and 2,023 drew 429s
            # (11-15 19:40-19:56). Never forced past PM_URGENT_OVERDRAW: a
            # 2,000-quote stop forced each after its wait, drew 15,708 429s and
            # left the bucket at -64k, blocking quotes and the scan ~30 min
            # (11-16 05:55). A 429'd cancel cancels nothing anyway.
            routine = reason in PM_ROUTINE_CANCELS
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            end = t0 + PM_CANCEL_WAIT_S
            if not c.allow_now("cancel", reason=reason):        # counted refused once, not per check
                while True:
                    # An urgent cancel is held only by the per-IP bucket, not by
                    # this process's quoting cap: with the cap spent, pulls waited
                    # while Polymarket's own limit had room (11-18).
                    if routine and loop.time() < end:
                        if c.can_afford("cancel"):
                            c.allow_now("cancel", force=True, reason=reason)
                            break
                    elif (c.tokens_now() if routine else c.budget.tokens_now()) > -PM_URGENT_OVERDRAW:
                        c.allow_now("cancel", force=True, reason=reason)
                        break
                    await asyncio.sleep(0.05)
                w = self.cancel_wait.setdefault(reason, [0, 0.0, 0.0])
                dt = loop.time() - t0
                w[0] += 1
                w[1] += dt
                w[2] = max(w[2], dt)
        elif not c.allow_now("cancel", reason=reason):
            # Counted refused once, not per check: per check, the waits showed
            # as 300k+ "refused" cancels a process in a few hours (11-17).
            while c.tokens_now() <= -KALSHI_CANCEL_OVERDRAW:
                await asyncio.sleep(0.005)
            c.allow_now("cancel", force=True, reason=reason)
        try:
            return await self.write(c.cancel, exchange_id, ticker), None, True
        except Exception as e:
            if http_status(e) == 429:
                c.note_rate_limited("cancel")
            return None, e, True

    def usage(self) -> dict:
        out = {str(s): {"tokens_now": round(c.tokens_now(), 1), "tokens_per_s": c.tokens_per_s,
                        "http_429": (u := c.usage()).get("http_429"), "refused": u.get("refused")}
               for s, c in self.clients.items()}
        if self.cancel_wait and str(PM_SHARD) in out:
            out[str(PM_SHARD)]["cancel_wait"] = {r: {"n": n, "mean_ms": round(1000 * tot / n, 1),
                                                    "max_ms": round(1000 * mx, 1)}
                                                for r, (n, tot, mx) in self.cancel_wait.items()}
        return out
