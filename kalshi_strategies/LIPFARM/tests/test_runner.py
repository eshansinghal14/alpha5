import asyncio
import dataclasses
import threading
import time
from types import SimpleNamespace

import pytest

from kalshi_backtest.core import Trade
from kalshi_strategies.LIPFARM import farm, runner
from kalshi_strategies.LIPFARM.tests.test_farm import CFG

import pandas as pd


class FakeClient:
    """A shard client: records every write in order, answers at once."""
    tokens_per_s = 300.0
    COST = {"order": 10, "amend": 10, "cancel": 2}

    def __init__(self):
        self.calls, self.lock, self.n = [], threading.Lock(), 0
        self.ioc_fill = None                     # contracts an IOC fills (None: all)

    def _log(self, *row):
        with self.lock:
            self.calls.append((time.perf_counter(),) + row)

    def allow_now(self, kind, force=False, reason=""):
        return True

    def can_afford(self, kind, reserve=0.0):
        return True

    def tokens_now(self):
        return 300.0

    def note_rate_limited(self, reason):
        pass

    def place_resting(self, order, order_group_id=None):
        self.n += 1
        self._log("rest", order.ticker, order.side, order.price, order.size, order_group_id)
        return SimpleNamespace(exchange_order_id=f"r{self.n}", fill=None, parse_error=None)

    def place_ioc(self, order):
        self.n += 1
        self._log("ioc", order.ticker, order.side, order.price, order.size)
        q = order.size if self.ioc_fill is None else self.ioc_fill
        return SimpleNamespace(exchange_order_id=f"i{self.n}", price=order.price, size=q, fee=0.0)

    def cancel(self, oid, ticker=None):
        self._log("cancel", ticker, oid)
        return True

    def create_order_group(self, limit):
        self.n += 1
        self._log("group", limit)
        return f"g{self.n}"

    def reset_order_group(self, gid):
        self._log("reset", gid)

    def delete_order_group(self, gid):
        self._log("delete", gid)


class FakeFeed:
    def __init__(self, books):
        self.books, self.up, self.tickers = books, True, list(books)

    def ladder(self, t):
        b = self.books.get(t)
        return None if b is None else SimpleNamespace(yes_bids=b["yes"], no_bids=b["no"])

    def add(self, tickers):
        self.tickers += [t for t in tickers if t not in self.tickers]


def make(tmp_path, monkeypatch, books, live=True):
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days: {"trades_day": 0.0, "sweeps_day": 0.0, "days": days,
                                                             "at": time.time()})
    monkeypatch.setattr(farm, "CLAIMS_PATH", str(tmp_path / "live" / "claimed.json"))
    r = runner.Runner(root=str(tmp_path))
    if live:
        r._open_mode(True)
    c = FakeClient()
    r.gw.clients = {0: c}
    r.gw.shard_of = {t: 0 for t in books}
    r.feed = FakeFeed(books)
    r.meta = {t: {"event_ticker": farm.event_of(t), "exchange_index": 0, "status": "active"} for t in books}
    r.programs = {t: {"id": "p" + t, "end_date": "2099-01-01T00:00:00Z", "target_size_fp": "1000",
                      "discount_factor_bps": 5000, "period_reward": 1800000, "start_date": "2026-11-04T00:00:00Z"}
                  for t in books}
    r.taker_rates = {farm.event_of(t): 0.07 for t in books}
    r.cfg = CFG
    r.cfg_adverse = CFG.adverse_lots
    return r, c


async def boot(r):
    r.wake = asyncio.Event()
    r.urgent_set, r.dirty = runner._Dirty(r.wake), runner._Dirty(r.wake)
    return asyncio.get_running_loop().create_task(r.eval_loop())


async def settle(r, s=0.2):
    end = time.time() + s
    while time.time() < end:
        await asyncio.sleep(0.01)
        if not r.tasks and not r.urgent_set and not r.dirty:
            await asyncio.sleep(0.02)
            if not r.tasks:
                return


def _fill(t, side, price, size, oid, tid):
    return {"ticker": t, "side": side, "price": price, "size": size, "fee": 0.0, "order_id": oid,
            "trade_id": tid, "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker"}


def test_an_informed_fill_is_sold_from_the_socket_book_before_any_cancel(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.96: 300.0, 0.95: 500.0}, "no": {0.02: 5000.0}},
             "Z-1-B": {"yes": {0.40: 900.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["groups"] = {"Z-1": {"id": "g1", "tripped": False, "fills": [], "used": time.time()}}
    r.state["orders"] = {
        "Z-1-A|yes": {"oid": "o1", "side": "yes", "price": 0.97, "size": 1000, "remaining": 1000,
                      "placed": time.time(), "group": "g1"},
        "Z-1-B|no": {"oid": "o2", "side": "no", "price": 0.30, "size": 1000, "remaining": 1000,
                     "placed": time.time(), "group": "g1"}}
    r.state["lip_oid_size"] = {"o1": 1000, "o2": 1000}
    r.state["hedged_oids"] = ["o1", "o2"]
    r.state["all_oids"] = ["o1", "o2"]

    async def go():
        ev = await boot(r)
        r._on_fill(_fill("Z-1-A", "yes", 0.97, 300.0, "o1", "t1"))
        await settle(r)
        ev.cancel()
    asyncio.run(go())
    ops = [x[1:] for x in c.calls]
    # Sold 300 into the 0.96 bids (buy no at 0.04) first; then the event's bids, pulled by the trip, confirmed.
    assert ops[0] == ("ioc", "Z-1-A", "no", 0.04, 300.0)
    assert {o[1] for o in ops[1:] if o[0] == "cancel"} == {"Z-1-A", "Z-1-B"}
    assert r.state["cooldown"]["Z-1-B"] > time.time()
    row = next(x for x in r.rows if x.get("op") == "ioc")
    assert row["fill_seen"] is not None and row["sent"] - row["fill_seen"] < 0.05
    assert abs(r.free_positions().get("Z-1-A", 0.0)) < 1e-9


def test_a_fill_that_beats_its_create_ack_is_replayed(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 2000.0, 0.39: 300.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": {"oid": None, "side": "yes", "price": 0.40, "size": 1000,
                                       "remaining": 1000, "placed": time.time(), "group": "g1"}}

    async def go():
        ev = await boot(r)
        r._on_fill(_fill("Z-1-A", "yes", 0.40, 10.0, "late", "t1"))
        assert not r.state["fills"] and r.orphans
        r.state["orders"]["Z-1-A|yes"]["oid"] = "late"
        r.state["all_oids"] = ["late"]
        r.replay_orphans()
        await settle(r)
        ev.cancel()
    asyncio.run(go())
    assert [f["trade_id"] for f in r.state["fills"] if f["liquidity"] == "maker"] == ["t1"]


def test_sentinel_goes_right_after_its_bid_in_the_events_group(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 50.0, 0.39: 300.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    ws = {"Z-1-A|yes": (0.39, 1000, True), "Z-1-A|yes|s": (0.40, farm.SENTINEL_LOTS, True)}
    r.wanted = dict(ws)

    async def go():
        await boot(r)
        await r.place_side("Z-1-A", "yes", ws, 0.0)
    asyncio.run(go())
    ops = [x[1:] for x in c.calls]
    assert ops[0] == ("group", 1)
    assert ops[1][:5] == ("rest", "Z-1-A", "yes", 0.39, 1000) and ops[2][:5] == ("rest", "Z-1-A", "yes", 0.40, 1.1)
    assert ops[1][5] == ops[2][5] == "g1"
    assert r.state["sentinel_oids"] == {"r3": "Z-1-A|yes"}


def test_a_bid_cancelled_while_its_create_is_in_flight_is_cancelled_on_ack(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 2000.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["groups"] = {"Z-1": {"id": "g1", "tripped": False, "fills": [], "used": time.time()}}

    async def go():
        await boot(r)
        task = asyncio.get_running_loop().create_task(r.place("Z-1-A", "yes", 0.40, 1000))
        await asyncio.sleep(0)
        r.cancel("Z-1-A|yes")
        assert await task is False
        await settle(r)
    asyncio.run(go())
    assert [x[1] for x in c.calls] == ["rest", "cancel"] and not r.state["orders"]


def test_shadow_fills_on_a_print_through_our_bid_and_exits_in_memory(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.88: 100.0}, "no": {0.10: 300.0, 0.08: 2000.0}}}
    r, c = make(tmp_path, monkeypatch, books, live=False)
    r.state["orders"] = {"Z-1-A|no": {"oid": "shadow-1", "side": "no", "price": 0.11, "size": 800,
                                      "remaining": 800, "placed": time.time(), "group": "shadow-Z-1"}}
    r.state["lip_oid_size"] = {"shadow-1": 800}
    r.state["hedged_oids"] = ["shadow-1"]
    r.state["all_oids"] = ["shadow-1"]

    async def go():
        ev = await boot(r)
        # A yes buy printing at no 0.10: the 0.11 level (ours) was swept.
        r._on_trade("Z-1-A", Trade(ticker="Z-1-A", time=pd.Timestamp.now(tz="UTC"), price=0.10, size=500.0,
                                   taker_side="yes"))
        await settle(r)
        ev.cancel()
    asyncio.run(go())
    assert not c.calls                                                  # nothing sent in shadow
    maker = [f for f in r.state["fills"] if f["liquidity"] == "maker"]
    taker = [f for f in r.state["fills"] if f["liquidity"] == "taker"]
    assert maker[0]["size"] == 800 and maker[0]["side"] == "no"
    # Informed (800 >= 300): sold at once down to 2 ticks under 0.11 -- the 300 at 0.10; the rest rests.
    assert taker and taker[0]["side"] == "yes" and taker[0]["size"] == 300.0
    assert any(farm.is_hedge(k) for k in r.state["orders"])            # an exit bid for the other 500


def test_a_cycle_plans_off_the_loop_and_places_after_a_passing_flow_read(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.97: 2000.0}, "no": {0.02: 5000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    ob = {"yes_dollars": [["0.97", "2000"]], "no_dollars": [["0.02", "5000"]]}
    monkeypatch.setattr(farm, "fetch_book", lambda t: ob)
    monkeypatch.setattr(farm, "arbs_held_events", lambda: set())
    monkeypatch.setattr(runner, "CLAIM_SETTLE_S", 0.0)          # ARBS's stand-aside wait

    async def go():
        await boot(r)
        await r.cycle(CFG)                      # flow not read yet: nothing placed
        assert not r.state["orders"]
        r.flow_futs["Z-1-A"].result()
        await r.cycle(CFG)
        await settle(r)
    asyncio.run(go())
    rests = [x[1:] for x in c.calls if x[1] == "rest"]
    assert rests and rests[0][1:4] == ("Z-1-A", "yes", 0.97) and rests[0][5] is not None   # grouped


def test_a_fill_on_a_busy_market_is_handled_as_soon_as_its_task_ends(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.96: 300.0, 0.95: 500.0}, "no": {0.02: 5000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": {"oid": "o1", "side": "yes", "price": 0.97, "size": 1000,
                                       "remaining": 1000, "placed": time.time()}}
    r.state["lip_oid_size"] = {"o1": 1000}
    r.state["hedged_oids"] = ["o1"]
    r.state["all_oids"] = ["o1"]

    async def go():
        ev = await boot(r)
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
        r.spawn(slow(), "Z-1-A")
        r._on_fill(_fill("Z-1-A", "yes", 0.97, 300.0, "o1", "t1"))
        await asyncio.sleep(0.05)
        assert not [x for x in c.calls if x[1] == "ioc"]          # busy: waits
        gate.set()
        await settle(r)
        ev.cancel()
    asyncio.run(go())
    assert [x[1:3] for x in c.calls if x[1] == "ioc"] == [("ioc", "Z-1-A")]


def test_a_short_informed_sell_is_retried_on_the_next_book_update(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.96: 300.0, 0.95: 500.0}, "no": {0.02: 5000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    c.ioc_fill = 100.0                                            # the socket book was stale: 100 of 300
    r.state["orders"] = {"Z-1-A|yes": {"oid": "o1", "side": "yes", "price": 0.97, "size": 1000,
                                       "remaining": 1000, "placed": time.time()}}
    r.state["lip_oid_size"] = {"o1": 1000}
    r.state["hedged_oids"] = ["o1"]
    r.state["all_oids"] = ["o1"]

    async def go():
        ev = await boot(r)
        r._on_fill(_fill("Z-1-A", "yes", 0.97, 300.0, "o1", "t1"))
        await settle(r)
        c.ioc_fill = None
        books["Z-1-A"]["yes"] = {0.95: 500.0}
        r._on_quote(SimpleNamespace(ladder=SimpleNamespace(ticker="Z-1-A")))     # the book update
        await settle(r)
        ev.cancel()
    asyncio.run(go())
    iocs = [x[1:] for x in c.calls if x[1] == "ioc"]
    assert iocs[0] == ("ioc", "Z-1-A", "no", 0.04, 300.0) and iocs[1] == ("ioc", "Z-1-A", "no", 0.05, 200.0)
    assert abs(r.free_positions().get("Z-1-A", 0.0)) < 1e-9
