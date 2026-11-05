import asyncio
import json
import dataclasses
import threading
import time
from types import SimpleNamespace


from kalshi_backtest.core import Trade
from kalshi_strategies.LIPFARM import farm, runner
from kalshi_strategies.LIPFARM.farm import unkey
from kalshi_strategies.LIPFARM.tests.test_farm import CFG

import pandas as pd
import pytest


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
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days, opened=None: {"trades_day": 0.0, "days": days,
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


def test_a_resting_sentinel_doesnt_get_its_new_main_bid_pulled(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 50.0, 0.39: 300.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["groups"] = {"Z-1": {"id": "g1", "tripped": False, "fills": [], "used": time.time()}}
    r.state["orders"] = {"Z-1-A|yes|s": {"oid": "s1", "side": "yes", "price": 0.40, "size": 1.1, "remaining": 1.1,
                                         "placed": time.time(), "group": "g1"}}
    ws = {"Z-1-A|yes": (0.39, 1000, True), "Z-1-A|yes|s": (0.40, farm.SENTINEL_LOTS, True)}
    r.wanted = dict(ws)

    async def go():
        await boot(r)
        await r.place_side("Z-1-A", "yes", ws, 0.0)
        await settle(r)
    asyncio.run(go())
    assert [x[1] for x in c.calls] == ["rest"] and "Z-1-A|yes" in r.state["orders"]


# ---- ported from the synchronous farm's tests ----

def _plan(t, mode="decided", legs=(("yes", 0.97),), rate=1e-3, capital=970.0, lots=1000, layout=None, big=None):
    return farm.Plan(t, mode, legs, rate, capital, 0.002, lots, layout or {s: "touch" for s, _ in legs},
                     big or {s: p for s, p in legs})


def _order(side, price, size=1000, oid="o1", group="g1", placed=None, remaining=None):
    return {"oid": oid, "side": side, "price": price, "size": size,
            "remaining": size if remaining is None else remaining, "placed": placed or time.time(), "group": group}


def _held(r, t, side, price, size, oid="f1"):
    r.state.setdefault("hedged_oids", []).append(oid)
    r.state["fills"].append({"ticker": t, "side": side, "price": price, "size": size, "fee": 0.0, "order_id": oid,
                             "trade_id": "x" + oid, "time": "2026-11-05T00:00:00", "liquidity": "maker"})


def test_choose_ranks_by_net_lip_per_day_and_caps_by_exposure(tmp_path, monkeypatch):
    books = {t: {"yes": {}, "no": {}} for t in ("A-1-X", "B-1-X", "C-1-X")}
    r, _ = make(tmp_path, monkeypatch, books)
    r.plans = {"A-1-X": _plan("A-1-X", rate=2e-3), "C-1-X": _plan("C-1-X", legs=(("no", 0.98),), rate=5e-4, capital=980.0),
               "B-1-X": _plan("B-1-X", "wide", (("yes", 0.1), ("no", 0.4)), capital=500.0)}
    r.plans["A-1-X"].loss_rate = 1.7e-3                                   # A pays most, but nets least
    assert list(r.choose(dataclasses.replace(CFG, max_capital=1000))) == ["B-1-X", "C-1-X", "A-1-X"]
    monkeypatch.setattr(r, "exposure", lambda: 500.0)
    assert list(r.choose(dataclasses.replace(CFG, max_capital=1000))) == ["B-1-X"]


def test_quoted_past_one_dollar_first_then_under_then_new(tmp_path, monkeypatch):
    books = {t: {"yes": {}, "no": {}} for t in ("PAST-1-A", "UNDER-1-A", "NEW-1-A")}
    r, _ = make(tmp_path, monkeypatch, books)
    r.plans = {t: _plan(t, rate=rate) for t, rate in (("PAST-1-A", 1e-4), ("UNDER-1-A", 2e-4), ("NEW-1-A", 9e-4))}
    r.state["orders"] = {"PAST-1-A|yes": _order("yes", 0.97), "UNDER-1-A|yes": _order("yes", 0.97, oid="o2")}
    r.state["period_accrued"] = {"pPAST-1-A": 1.5, "pUNDER-1-A": 0.4}
    assert list(r.choose(dataclasses.replace(CFG, num_markets=3))) == ["PAST-1-A", "UNDER-1-A", "NEW-1-A"]
    assert list(r.choose(dataclasses.replace(CFG, num_markets=1))) == ["PAST-1-A"]


def test_candidates_rank_by_what_we_earned_and_rotate_through_the_rest(tmp_path, monkeypatch):
    books = {f"M-1-{i}": {"yes": {}, "no": {}} for i in range(6)}
    r, _ = make(tmp_path, monkeypatch, books)
    r.pool_rate = {t: float(i) for i, t in enumerate(sorted(books))}
    r.est = {"M-1-0": 5.0}
    cfg = dataclasses.replace(CFG, scan_markets=2, scan_rotate=2)
    first = r.candidates(cfg)
    assert first[:2] == ["M-1-0", "M-1-5"] and len(first) == 4
    assert set(r.candidates(cfg)[2:]) != set(first[2:])               # the rotation moved on


def test_old_journal_keys_migrate(tmp_path, monkeypatch):
    (tmp_path / "shadow").mkdir()
    (tmp_path / "shadow" / "state.json").write_text(json.dumps(
        {"t0": 0, "orders": {"Q-1-A": {"oid": "o", "side": "no", "price": 0.1, "size": 5, "remaining": 5,
                                       "placed": 0}}, "fills": [], "trade_ids": [], "accrued": {}}))
    r = runner.Runner(root=str(tmp_path))
    assert list(r.state["orders"]) == ["Q-1-A|no"]


def test_our_bucket_limits_lip_bids_but_never_exits(tmp_path, monkeypatch):
    books = {t: {"yes": {}, "no": {}} for t in ("A-1-X", "A-1-Y", "A-1-Z", "D-1-X")}
    r, c = make(tmp_path, monkeypatch, books)
    r.bucket = farm.TokenBucket(rate=0.0, capacity=35.0)               # the event's group and two orders

    async def go():
        await boot(r)
        assert await r.place("A-1-X", "yes", 0.1, 1000) and await r.place("A-1-Y", "yes", 0.1, 1000)
        assert not await r.place("A-1-Z", "yes", 0.1, 1000)             # spent: ARBS's budget untouched
        assert await r.place("D-1-X", "no", 0.9, 300, hedge=True)       # an exit goes anyway
    asyncio.run(go())
    rests = [x[1:] for x in c.calls if x[1] == "rest"]
    assert [x[1] for x in rests] == ["A-1-X", "A-1-Y", "D-1-X"] and r.bucket.tokens < 0
    assert rests[0][5] == rests[1][5] is not None and rests[2][5] is None   # one group per event, exits outside


def test_guard_pulls_a_side_its_plan_dropped_and_cools_down(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.60: 5000.0}, "no": {0.30: 5000.0}}}     # 10c spread, no exit depth: no plan
    r, c = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.60)}

    async def go():
        await boot(r)
        r.guard_market("Z-1-A", r.ws_others("Z-1-A"), CFG)
        await settle(r)
    asyncio.run(go())
    assert not r.state["orders"] and [x[1] for x in c.calls] == ["cancel"]
    assert r.state["cooldown"]["Z-1-A"] > time.time() + farm.GUARD_COOLDOWN_S - 5


def test_stale_bids_are_replaced_and_disarm_pulls_everything(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.11: 2000.0}, "no": {0.37: 2000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    monkeypatch.setattr(runner, "CLAIM_SETTLE_S", 0.0)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.11, size=400)}            # the plan now wants 1000
    r.plans = {"Z-1-A": _plan("Z-1-A", "wide", (("yes", 0.11), ("no", 0.37)), capital=480.0)}

    async def go():
        await boot(r)
        await r.reconcile(CFG)
        await settle(r)
        assert sorted(unkey(k)[1] for k in r.state["orders"]) == ["no", "yes"]
        await r.cancel_all()
    asyncio.run(go())
    assert [x[1] for x in c.calls].count("cancel") == 3 and not r.state["orders"]


def test_fill_rests_an_exit_one_tick_over_the_other_touch_and_steps_up_only_when_outbid(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 2000.0}, "no": {0.50: 2000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    _held(r, "Z-1-A", "yes", 0.40, 100.0)

    async def go():
        await boot(r)
        r.dirty.add("Z-1-A")
        await settle(r)
        assert r.state["orders"]["Z-1-A|no|h"]["price"] == 0.51           # a pair at 0.91: under $1
        books["Z-1-A"]["no"] = {0.52: 2000.0}                            # outbid
        r.dirty.add("Z-1-A")
        await settle(r)
        assert r.state["orders"]["Z-1-A|no|h"]["price"] == 0.51           # not before EXIT_REPRICE_S
        r.state["orders"]["Z-1-A|no|h"]["placed"] -= farm.EXIT_REPRICE_S
        r.dirty.add("Z-1-A")
        await settle(r)
        await settle(r)
    asyncio.run(go())
    assert r.state["orders"]["Z-1-A|no|h"]["price"] == 0.53
    assert not [x for x in c.calls if x[1] == "ioc"]                      # under adverse_lots: no sell into the book


def test_dust_is_sold_into_the_book_exactly_once(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 2000.0, 0.30: 100.0}, "no": {0.50: 2000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    _held(r, "Z-1-A", "yes", 0.42, 10.69)

    async def go():
        await boot(r)
        r.dirty.add("Z-1-A")
        await settle(r)
        for _ in range(3):
            r.dirty.add("Z-1-A")
            await settle(r)
    asyncio.run(go())
    assert [x[1:] for x in c.calls if x[1] == "ioc"] == [("ioc", "Z-1-A", "no", 0.6, 10.69)]
    assert abs(r.free_positions().get("Z-1-A", 0.0)) < 1e-9


def test_an_informed_fill_takes_the_pair_when_it_costs_under_a_dollar(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.95: 300.0}, "no": {0.02: 5000.0}}}           # no asks 1 - 0.95 = 0.05
    r, c = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.90, oid="o1", group=None)}
    r.state.update(lip_oid_size={"o1": 1000}, hedged_oids=["o1"], all_oids=["o1"])

    async def go():
        await boot(r)
        r._on_fill(_fill("Z-1-A", "yes", 0.90, 300.0, "o1", "t1"))       # informed: 300 >= adverse_lots
        await settle(r)
    asyncio.run(go())
    # 0.90 + 0.05 + fee < $1: locked by buying the other side, not sold.
    assert [x[1:] for x in c.calls if x[1] == "ioc"][0] == ("ioc", "Z-1-A", "no", 0.05, 300)


def test_fill_locked_through_an_arbs_basket(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.30: 2000.0}, "no": {0.50: 2000.0}},
             "Z-1-B": {"yes": {0.10: 2000.0}, "no": {0.80: 2000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.books = {t: {"yes": dict(b["yes"]), "no": dict(b["no"])} for t, b in books.items()}
    _held(r, "Z-1-A", "yes", 0.30, 100.0)
    # Z-1-A yes + Z-1-B yes pays $1; Z-1-B yes asks 1 - 0.80 = 0.20: 0.30 + 0.20 + fees < $1.
    r.baskets = {("Z-1-A", "yes"): [(1.0, (("Z-1-A", "yes"), ("Z-1-B", "yes")))]}

    async def go():
        await boot(r)
        await r.basket_lock("Z-1-A", "yes")
    asyncio.run(go())
    assert [x[1:3] for x in c.calls if x[1] == "ioc"] == [("ioc", "Z-1-B")]
    assert r.state["locked"] == {"Z-1-A": 100.0, "Z-1-B": 100.0} and not r.free_positions().get("Z-1-A")


def test_a_fill_pulls_bids_that_could_take_us_past_max_capital(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.96: 300.0}, "no": {0.02: 5000.0}},
             "Z-1-B": {"yes": {0.40: 900.0}, "no": {0.50: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.cfg = dataclasses.replace(CFG, max_capital=500.0)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.97, oid="o1", group=None),
                         "Z-1-B|yes": _order("yes", 0.40, oid="o2", group=None)}
    r.state.update(lip_oid_size={"o1": 1000, "o2": 1000}, hedged_oids=["o1", "o2"], all_oids=["o1", "o2"])

    async def go():
        await boot(r)
        r._on_fill(_fill("Z-1-A", "yes", 0.97, 200.0, "o1", "t1"))         # $194 held: a 0.40 x1000 bid passes $500
        await settle(r)
    asyncio.run(go())
    assert "Z-1-B|yes" not in r.state["orders"]


def test_sync_keeps_a_resting_order_the_list_missed_and_cancels_orphans(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {}, "no": {}}}
    r, c = make(tmp_path, monkeypatch, books)
    old = time.time() - 60
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.4, oid="still", placed=old),
                         "Z-1-A|no": _order("no", 0.3, oid="gone", placed=old)}
    c._resting = lambda prefix: [("orphan", "Z-1-A", "lipf-x"), ("arbs", "Z-1-A", "someone-else")]
    c.order = lambda oid: {"status": "resting" if oid == "still" else "canceled"}

    async def go():
        await boot(r)
        await r.sync_live_orders()
        await settle(r)
    asyncio.run(go())
    assert list(r.state["orders"]) == ["Z-1-A|yes"]
    assert [x[1:] for x in c.calls if x[1] == "cancel"] == [("cancel", "Z-1-A", "orphan")]


def test_moves_go_to_the_back_only_with_size_behind_and_are_capped(tmp_path, monkeypatch):
    books = {f"Z-1-{i}": {"yes": {0.40: 3000.0}, "no": {0.30: 900.0}} for i in range(14)}
    r, c = make(tmp_path, monkeypatch, books)
    old = time.time() - 60
    r.state["orders"] = {f"Z-1-{i}|yes": _order("yes", 0.40, oid=f"o{i}", placed=old) for i in range(14)}
    r.plans = {t: _plan(t, "wide", (("yes", 0.40),), capital=400.0) for t in books}
    r.books = {t: r.ws_others(t) for t in books}
    ahead = {f"o{i}": (3000.0 if i == 0 else 1000.0) for i in range(14)}      # Z-1-0: nothing behind it
    monkeypatch.setattr(farm, "queue_ahead", lambda c, ts: ahead)

    async def go():
        await boot(r)
        await r.move_back()
        await settle(r)
    asyncio.run(go())
    moved = {x[2] for x in c.calls if x[1] == "rest"}
    assert "Z-1-0" not in moved and len(moved) == farm.MAX_MOVES


def test_fill_of_adverse_lots_is_informed_even_on_a_big_bid(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.96: 300.0}, "no": {0.02: 5000.0}}}
    r, _ = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.97, size=5000, oid="o1", group=None)}
    r.state.update(lip_oid_size={"o1": 5000}, all_oids=["o1"])
    r.book_fills([_fill("Z-1-A", "yes", 0.97, 300.0, "o1", "t1")])
    assert r.dumping == {"Z-1-A"}                                           # 6% of the bid, still informed


def test_flow_gate(tmp_path, monkeypatch):
    books = {"S-1-A": {"yes": {}, "no": {}}, "Q-1-A": {"yes": {}, "no": {}}}
    r, _ = make(tmp_path, monkeypatch, books)
    r.categories = {"S": "Sports", "Q": "Politics"}
    assert r.flow_ok("S-1-A", CFG) is False and not r.flow_futs
    assert r.flow_ok("Q-1-A", CFG) is None
    r.flow_futs["Q-1-A"].result()
    assert r.flow_ok("Q-1-A", CFG) is True
    r.state["flow"]["Q-1-A"].update(trades_day=CFG.max_trades_day + 1)
    assert r.flow_ok("Q-1-A", CFG) is False


def test_expected_loss_grows_with_trades_up_to_the_cap():
    two = farm.Plan("T", "wide", (("yes", 0.4), ("no", 0.5)), 1e-3, 900.0, 0.002, 1000)
    one = farm.Plan("T", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002, 500)
    assert farm.expected_loss_day(4.0, two, CFG) == pytest.approx(12.0)        # 3 x 4, both sides, 1000 lots
    assert farm.expected_loss_day(40.0, two, CFG) == pytest.approx(30.0)       # capped
    assert farm.expected_loss_day(4.0, one, CFG) == pytest.approx(3.0)         # one side, 500 lots


def test_a_busier_market_is_quoted_only_when_its_lip_pays_for_the_risk(tmp_path, monkeypatch):
    books = {"R-1-A": {"yes": {0.97: 2000.0}, "no": {0.02: 5000.0}}, "P-1-A": {"yes": {0.97: 2000.0}, "no": {0.02: 5000.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    ob = {"yes_dollars": [["0.97", "2000"]], "no_dollars": [["0.02", "5000"]]}
    monkeypatch.setattr(farm, "fetch_book", lambda t: ob)
    monkeypatch.setattr(farm, "arbs_held_events", lambda: set())
    rich = {"R-1-A": 10.0, "P-1-A": 10.0}
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days, opened=None: {"trades_day": rich[t], "days": days,
                                                                          "at": time.time()})
    # One-day periods; same book, so P's program paying a twentieth of R's is the only difference.
    for t in books:
        r.programs[t] = dict(r.programs[t], start_date="2026-11-05T00:00:00Z", end_date="2099-11-06T00:00:00Z")
    day = {"start_date": "2026-11-05T00:00:00Z", "end_date": "2026-11-06T00:00:00Z"}
    r.programs["R-1-A"].update(day, period_reward=3600000)
    r.programs["P-1-A"].update(day, period_reward=180000)

    async def go():
        await boot(r)
        cfg = dataclasses.replace(CFG, scan_markets=10)
        await r.cycle(cfg)
        for f in list(r.flow_futs.values()):
            f.result()
        await r.cycle(cfg)
    asyncio.run(go())
    # 10 trades/day: expected loss min(30, 30) / 2 x 1 leg = $15/day; R's LIP clears 2x that, P's doesn't.
    assert "R-1-A" in r.plans and "P-1-A" not in r.plans
    assert r.flow_held["P-1-A"]["exp_loss_day"] == pytest.approx(15.0)
    pl = r.plans["R-1-A"]
    assert r.est["R-1-A"] == pytest.approx(pl.rate - pl.loss_rate)              # ranked by LIP net of the loss


def test_the_ten_seconds_of_depth_before_a_lip_fill_are_journaled(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 1300.0, 0.39: 200.0}, "no": {0.50: 900.0}}}
    r, _ = make(tmp_path, monkeypatch, books)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.40, oid="o1")}
    r.state.update(lip_oid_size={"o1": 1000}, all_oids=["o1"])
    view = SimpleNamespace(ladder=SimpleNamespace(ticker="Z-1-A", yes_bids=books["Z-1-A"]["yes"],
                                                  no_bids=books["Z-1-A"]["no"]))

    async def go():
        await boot(r)
        r._on_quote(view)
        books["Z-1-A"]["yes"][0.40] = 1050.0                                  # others pulled 250
        r._on_quote(view)
        r.take_fills([_fill("Z-1-A", "yes", 0.40, 1000.0, "o1", "t1")])
    asyncio.run(go())
    row = next(x for x in r.rows if x["kind"] == "fill_context")
    assert [snap["at_ours"]["yes@0.4"] for _, snap in row["depth_before"]] == [300.0, 50.0]
    assert row["depth_before"][0][1]["no_top"] == [0.5, 900.0]


def test_a_hold_pulls_our_lip_bids_and_keeps_the_market_off_until_it_ends(tmp_path, monkeypatch):
    books = {"Z-1-A": {"yes": {0.40: 1500.0}, "no": {0.30: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    monkeypatch.setattr(runner, "HOLDS_S", 0.01)
    r.state["orders"] = {"Z-1-A|yes": _order("yes", 0.40, oid="a"), "Z-1-A|no|h": _order("no", 0.55, oid="h")}
    until = time.time() + 600
    (tmp_path / "holds.json").write_text(json.dumps({"Z-1-A": {"until": until, "why": "test"}}))

    async def go():
        await boot(r)
        task = asyncio.get_running_loop().create_task(r.holds_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        await settle(r)
    asyncio.run(go())
    assert list(r.state["orders"]) == ["Z-1-A|no|h"]                     # the exit stays
    assert r.state["cooldown"]["Z-1-A"] == until
    assert [x[1:] for x in c.calls if x[1] == "cancel"] == [("cancel", "Z-1-A", "a")]



def test_a_side_scoring_nothing_is_re_placed_at_once(tmp_path, monkeypatch):
    # FOC-40 11-05: others stacked 1824 (past Target) at the sentinel's 63c; our bid at 62c scored 0.
    books = {"Z-1-A": {"yes": {0.63: 1825.1, 0.62: 2700.0, 0.58: 20034.0}, "no": {0.34: 2591.0, 0.32: 36.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.state["groups"] = {"Z-1": {"id": "g1", "tripped": False, "fills": [], "used": time.time()}}
    r.state["orders"] = {"Z-1-A|yes|s": _order("yes", 0.63, size=1.1, oid="s"),
                         "Z-1-A|yes": _order("yes", 0.62, size=1000, oid="m")}

    async def go():
        await boot(r)
        r.guard_market("Z-1-A", r.ws_others("Z-1-A"), CFG)
        await settle(r)
    asyncio.run(go())
    assert {x[3] for x in c.calls if x[1] == "cancel"} == {"s", "m"}
    assert [x[1:5] for x in c.calls if x[1] == "rest"] == [("rest", "Z-1-A", "yes", 0.63)]


def test_an_adverse_fill_pulls_its_whole_event_for_event_pull_s(tmp_path, monkeypatch):
    books = {"F-1-40": {"yes": {0.60: 2000.0}, "no": {0.34: 900.0}},
             "F-1-45": {"yes": {0.56: 2000.0}, "no": {0.43: 900.0}},
             "G-1-A": {"yes": {0.50: 2000.0}, "no": {0.40: 900.0}}}
    r, c = make(tmp_path, monkeypatch, books)
    r.cfg = dataclasses.replace(CFG, event_pull_s=1800.0)
    r.state["orders"] = {"F-1-40|no": _order("no", 0.34, oid="a", group=None),
                         "F-1-45|no": _order("no", 0.43, oid="b", group=None),
                         "G-1-A|no": _order("no", 0.40, oid="g", group=None)}
    r.state.update(lip_oid_size={"a": 1000, "b": 1000, "g": 1000}, hedged_oids=["a"], all_oids=["a", "b", "g"])

    async def go():
        await boot(r)
        r._on_fill(_fill("F-1-40", "no", 0.34, 730.0, "a", "t1"))
        await settle(r)
    asyncio.run(go())
    cool = r.state["cooldown"]
    assert cool["F-1-45"] > time.time() + 1700 and cool["F-1-40"] > time.time() + 1700
    assert "F-1-45|no" not in r.state["orders"] and "G-1-A|no" in r.state["orders"]    # other events untouched
