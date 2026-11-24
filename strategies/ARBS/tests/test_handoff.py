"""Hot handoff and warm start in the runner (no network): a standby writes
nothing, the old process cancels nothing, state and fills cross over exactly."""
import asyncio
import collections
import dataclasses
import json
import os
import time

import pandas as pd
import pytest

from backtest.book import L2Book
from infra.kalshi.orders import RestingAck
from infra.runtime import handoff as hand
from strategies.ARBS import SERIES
from strategies.ARBS import runner as R
from strategies.ARBS.ledger import Ledger
from strategies.ARBS.spec import Basket, Leg


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    return tmp_path


def _runner(standby=None):
    """The test_runner fixture's basket and books, built through set_baskets."""
    r = R.ArbsRunner(SERIES["ARBS"], live_trading=False, standby=standby)
    r.part_idx = None
    b = Basket(basket_id="t:b", relationship_id="t", type="complete_set", cluster="t",
               legs=(Leg("X-A", "yes"), Leg("X-B", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.set_baskets([b])
    r.feed.add(r.tickers)
    r.feed.kalshi._new = []
    r.feed._books = {t: L2Book(t) for t in r.tickers}
    r.feed.up = True
    for t in r.tickers:
        r.meta[t] = R.Meta(ticker=t, event="X", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_edge=0.01, quote_edge_add=0.02, quote_size=5,
                                        max_bundles_per_basket=50, max_dollars=1000.0, take_cooldown_s=0.0)
    r.deep_cap = lambda b, j, live: 0.0
    r.hot = [b.basket_id]
    return r, b


def book(r, t, yes_bids, no_bids):
    now = pd.Timestamp.now(tz="UTC")
    r.feed._books[t].apply_snapshot("yes", yes_bids, time=now)
    r.feed._books[t].apply_snapshot("no", no_bids, time=now)


def _books(r):
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])     # A ask 0.65
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])     # B ask 0.55: no take, both legs quoted


async def drain(r):
    for _ in range(50):
        if not r.tasks:
            return
        await asyncio.sleep(0)
        await asyncio.gather(*list(r.tasks), return_exceptions=True)


class Client:
    """A Kalshi shard client that logs every order write."""
    shard = 0

    def __init__(self):
        self.writes, self.ids = [], iter(range(1000))
        self.resting: list[tuple] = []
        self.rest_fills: list[dict] = []

    def allow_now(self, *a, **k):
        return True

    def can_afford(self, *a, **k):
        return True

    def tokens_now(self):
        return 100.0

    def place_resting(self, order, order_group_id=None):
        oid = f"o{next(self.ids)}"
        self.writes.append(("rest", order.ticker, oid))
        self.resting.append((oid, order.ticker, order.client_order_id))
        return RestingAck(exchange_order_id=oid, fill=None)

    def place_ioc(self, order):
        self.writes.append(("ioc", order.ticker))

    def amend(self, *a, **k):
        self.writes.append(("amend",) + a[:1])

    def cancel(self, oid, ticker=None):
        self.writes.append(("cancel", oid))
        return True

    def create_order_group(self, *a):
        self.writes.append(("group",))

    def _resting(self, event_ticker, max_pages=10, info=None):
        if info is not None:
            info["complete"] = True
        return list(self.resting)

    def fills(self, event_ticker, min_ts=None):
        return list(self.rest_fills)


def _attach(r, client):
    r.gw.clients = {0: client}
    r.gw.shard_of.update({t: 0 for t in r.tickers})

    async def call(fn, *a, **k):
        return fn(*a, **k)
    r.gw.call = call


def _fill(oid, tid, size, price=0.31):
    return {"order_id": oid, "trade_id": tid, "size": size, "price": price, "fee": 0.0, "liquidity": "maker",
            "ticker": "X-A", "side": "yes", "time": pd.Timestamp.now(tz="UTC").isoformat()}


def _files(root) -> set[str]:
    return {os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
            for d, _, fs in os.walk(root) for f in fs}


def test_standby_never_writes_orders_or_files_but_its_ready_marker(root, monkeypatch):
    before = _files(root)
    sb, b = _runner(standby="h1")
    client = Client()
    _attach(sb, client)
    _books(sb)
    monkeypatch.setattr(R, "HOT_READY_S", 0.0)
    monkeypatch.setattr(R, "kalshi_get", lambda *a, **k: {"trading_active": True})
    sb.load_meta = lambda *a: None
    sb.gw.setup = lambda *a: None
    sb.cfg.watch = dataclasses.replace(sb.cfg.watch, rank_interval_s=0.05)

    async def idle():
        await asyncio.sleep(3600)
    sb.feed.run = idle

    async def go():
        t = asyncio.get_running_loop().create_task(sb.run())
        await asyncio.sleep(0.3)
        sb._on_fill(_fill("o9", "t1", 2))
        sb._on_gone("pm-1", "EXECUTION_TYPE_CANCELED", None, 0.0, None)
        sb.alert("warn", None, "a journaled alert")
        sb.real_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 1.0, 0.3, time.time())
        sb.settle()
        sb.mark(b.basket_id)
        await asyncio.sleep(1.5)
        assert hand.has(R.LIVE_ROOT, "ARBS", "h1", "ready")
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert client.writes == []
    assert not sb.quotes and b.basket_id in sb.rank_rows               # ranked, nothing quoted
    assert [k for k, _ in sb._buffer] == ["fill", "gone"]
    assert _files(root) - before == {"ARBS/handoff/h1.ready"}


def _quotes(r, b):
    a = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.31, size=5.0, cid="c1", edge=0.02,
                max_price=0.33, filled=1.0, exchange_id="o1", placed_at=100.0, moved_at=150.0, pending=False,
                group="g1", booked_to=b.basket_id, short_at=140.0, pm_from=(0.30, 150.0))
    d = R.Quote(basket_id=b.basket_id, leg=1, ticker="X-B", side="yes", price=0.40, size=20.0, cid="c2", edge=0.05,
                max_price=0.42, exchange_id="o2", placed_at=120.0, pending=False, deep=True, near=True,
                unpriced_at=130.0)
    x = R.Quote(basket_id=b.basket_id, leg=1, ticker="X-B", side="no", price=0.5, size=3.0, cid="c3", edge=0.0,
                max_price=0.5, exchange_id="o3", placed_at=125.0, pending=False, kind="unwind")
    r.quotes[a.key], r.quotes[d.key] = a, d
    return a, d, x


def test_state_round_trips_the_quote_table_and_exit_state_exactly(root):
    old, b = _runner()
    a, d, _ = _quotes(old, b)
    old.live, old.arm_want, old.halt, old.audit_strikes = True, True, None, 1
    old.cash_base, old.cash_at = {0: 1234.5}, 99.0
    old.exit_hist[b.basket_id] = collections.deque([(1.0, -0.5), (2.0, -0.4)])
    old.exit_planned[b.basket_id], old.exit_base[b.basket_id] = -0.6, [0.1, 3.0]
    old.exit_misses[b.basket_id], old.completion_note[b.basket_id] = 2, {"path": "complete", "excess": {"X-A": 1.0}}
    old.groups = {"X": {"id": "g1", "ticker": "X-A", "tripped_at": None, "resetting": False,
                        "fills": collections.deque([(1.0, 1.0)]), "used": 5.0}}
    old._group_ev = {"g1": "X"}
    old.flagged = {"X-A": 10.0}
    old.model_state = {(b.basket_id, 0, True): {"edge": 0.05, "size": 20.0, "narrow": None, "grow": 3.0}}
    now = time.time()
    old.top_seen = {"X-A": now - 7200.0, "X-B": now - 100.0, "GONE": now - 9000.0}
    old.top_recent = {"X-A": collections.deque([now - 4000.0, now - 60.0])}
    old.upkeep = {b.basket_id: collections.deque([(now - 5000.0, 10.0), (now - 30.0, 12.0)])}
    st = json.loads(json.dumps(old.handoff_state("h1")))

    sb, _ = _runner(standby="h1")
    sb.baskets.pop(b.basket_id)                    # a basket only the state still knows
    sb.top_seen = {"X-A": now - 50.0}              # the standby's own first sight, later than the old one's

    async def go():
        sb.restore(st)
    asyncio.run(go())
    assert {k: dataclasses.asdict(q) for k, q in sb.quotes.items()} == \
        {k: dataclasses.asdict(q) for k, q in old.quotes.items()}
    assert sb.quotes[a.key].pm_from == (0.30, 150.0) and b.basket_id in sb.baskets
    for name in ("exit_planned", "exit_base", "exit_misses", "completion_note", "flagged", "model_state",
                 "cash_base", "_group_ev"):
        assert getattr(sb, name) == getattr(old, name), name
    assert {k: list(v) for k, v in sb.exit_hist.items()} == {k: list(v) for k, v in old.exit_hist.items()}
    assert sb.groups["X"]["fills"] == old.groups["X"]["fills"] and sb.groups["X"]["id"] == "g1"
    assert (sb.live, sb.arm_want, sb.audit_strikes, sb.cash_at) == (True, True, 1, 99.0)
    assert sb.hot == old.hot
    # is_cheap's hour: markets no basket trades and writes past the window aren't handed over
    assert sb.top_seen == {"X-A": now - 7200.0, "X-B": now - 100.0}
    assert list(sb.top_recent["X-A"]) == [now - 60.0]
    assert list(sb.upkeep[b.basket_id]) == [(now - 30.0, 12.0)]


def test_restored_quotes_stay_on_their_legs_when_the_new_scan_orders_legs_differently(root):
    old, b = _runner()
    _quotes(old, b)
    st = json.loads(json.dumps(old.handoff_state("h1")))
    sb, _ = _runner(standby="h1")
    sb.baskets.clear()                              # the standby's own scan: same basket, legs the other way
    sb.set_baskets([dataclasses.replace(b, legs=tuple(reversed(b.legs)))])

    async def go():
        sb.restore(st)
    asyncio.run(go())
    assert sb.baskets[b.basket_id].legs == b.legs
    assert all(sb.baskets[q.basket_id].legs[q.leg].ticker == q.ticker for q in sb.quotes.values())


def test_restored_quotes_keep_their_order_client(root):
    old, b = _runner()
    _quotes(old, b)
    old.gw.shard_of["Y-NEW"] = 3                    # a market the standby hasn't read meta for
    st = json.loads(json.dumps(old.handoff_state("h1")))
    sb, _ = _runner(standby="h1")
    sb.gw.shard_of.pop("Y-NEW", None)

    async def go():
        sb.restore(st)
    asyncio.run(go())
    assert sb.gw.shard_of["Y-NEW"] == 3


def test_handoff_books_every_fill_once_and_the_old_process_cancels_nothing(root, monkeypatch):
    old, b = _runner()
    oc = Client()
    _attach(old, oc)
    old.live = True
    _books(old)
    pulls = []

    async def no_pull(*a, **k):
        pulls.append(a)
    old.pull_all = old.disarm = no_pull

    sb, _ = _runner(standby="h1")
    sc = Client()
    _attach(sb, sc)
    _books(sb)

    async def no_audit():
        return True
    sb.audit = no_audit
    old._exit_fast = sb._exit_fast = lambda: None    # booking only: the exit is left to the eval loop

    async def go():
        old.evaluate(b)
        await drain(old)
        q = old.quotes[(b.basket_id, 0)]
        f1, f2 = _fill(q.exchange_id, "t1", 2), _fill(q.exchange_id, "t2", 1)
        old._on_fill(f1)                           # the old process books the first fill...
        sb._on_fill(f1)                            # ...the standby holds both
        sb._on_fill(f2)
        n_writes = len(oc.writes)
        assert await old.hand_off("h1")
        assert oc.writes[n_writes:] == [] and not pulls     # nothing cancelled on the way out
        assert old.retired and old.gw.frozen and old.real_ledger.readonly
        sc.resting, sc.rest_fills = list(oc.resting), [f1, f2]   # the REST poll returns both again
        await sb.take_over("h1")
        await drain(sb)
        return q
    q = asyncio.run(go())
    assert sb.writer and sb.live and not sb.gw.frozen
    held = sb.real_ledger.baskets[b.basket_id].legs["X-A"].qty
    assert held == 3 and Ledger(os.path.join(root, "ARBS", "ledger.json")).baskets[b.basket_id].legs["X-A"].qty == 3
    assert sb.quotes[q.key].filled == 3 and sb.quotes[q.key].exchange_id == q.exchange_id
    assert not [w for w in sc.writes if w[0] == "cancel"]   # every resting order was known
    assert b.basket_id in sb.urgent                         # broken by the fills: exits first
    assert hand.has(R.LIVE_ROOT, "ARBS", "h1", "taken")
    with open(os.path.join(root, "ARBS", "journal", f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}.jsonl")) as fh:
        rows = [json.loads(line) for line in fh]
    assert sorted(r["trade_id"] for r in rows if r["kind"] == "fill") == ["t1", "t2"]


def test_takeover_cancels_only_untracked_orders_of_ours(root):
    sb, b = _runner(standby="h1")
    sc = Client()
    _attach(sb, sc)
    sb.real_ledger.quote_ids["o7"] = {"basket_id": b.basket_id, "ticker": "X-A", "side": "yes", "retired_at": 1.0}
    q = R.Quote(basket_id=b.basket_id, leg=1, ticker="X-B", side="yes", price=0.4, size=5.0, cid="c", edge=0.02,
                max_price=0.42, exchange_id="o1", placed_at=1.0, pending=False)
    gone = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.3, size=5.0, cid="d", edge=0.02,
                   max_price=0.32, exchange_id="o2", placed_at=1.0, pending=False)
    sb.quotes[q.key], sb.quotes[gone.key] = q, gone
    sb._handoff_cids = {"lost"}
    sc.resting = [("o1", "X-B", "c"), ("o7", "X-A", None), ("o8", "X-A", "lost"), ("o9", "X-A", "sibling"),
                  ("o6", "X-A", None)]
    sb.real_ledger.quote_ids["o6"] = {"basket_id": b.basket_id, "ticker": "X-A", "side": "yes", "retired_at": None}
    sb.recent_cancels["o6"] = time.monotonic()      # being cancelled already: not a stray
    sb.standby, sb.gw.frozen = None, False

    async def go():
        await sb._after_takeover("h1")
    asyncio.run(go())
    # o7 is ours by id, o8 a create in flight at the hand-over; o9 another process's.
    assert sorted(w[1] for w in sc.writes if w[0] == "cancel") == ["o7", "o8"]
    assert q.key in sb.quotes and gone.key not in sb.quotes


def test_cold_start_adopts_handed_over_quotes_still_resting(root):
    r, b = _runner()
    c = Client()
    _attach(r, c)
    keep = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.3, size=5.0, cid="c",
                   edge=0.02, max_price=0.32, exchange_id="o1", placed_at=1.0, pending=False)
    gone = dataclasses.replace(keep, leg=1, ticker="X-B", exchange_id="o2", cid="d")
    r.adopted = [keep, gone]
    c.resting = [("o1", "X-A", "c"), ("o5", "X-B", None)]      # o5: ours on our market, untracked

    async def bal():
        return {0: 1000.0}, 0.0

    async def ok(*a, **k):
        return True
    r.shard_balances, r.audit, r.poll_fills = bal, ok, ok

    async def go():
        return await r.arm()
    assert asyncio.run(go())
    assert [w for w in c.writes if w[0] == "cancel"] == [("cancel", "o5")]
    assert list(r.quotes.values()) == [keep] and r.live and r.adopted is None


def test_warm_cache_seeds_the_hot_list_until_its_books_are_in(root):
    old, b = _runner()
    R._write_summary(os.path.join(old.root, R.WARM_CACHE), old.warm_state())
    r, _ = _runner()
    r.hot = []
    r.seed_warm()
    assert r.hot == [b.basket_id] and r.seeded == [b.basket_id]
    r.check_hot_ready()
    assert not r.hot_ready                          # no books yet
    _books(r)
    r.check_hot_ready()
    assert r.hot_ready


def test_stale_warm_cache_is_ignored(root):
    old, b = _runner()
    st = {**old.warm_state(), "at": time.time() - R.WARM_CACHE_MAX_AGE_S - 1}
    R._write_summary(os.path.join(old.root, R.WARM_CACHE), st)
    r, _ = _runner()
    r.hot = []
    r.seed_warm()
    _books(r)
    r.check_hot_ready()
    assert r.hot == [] and not r.seeded and not r.hot_ready


def test_meta_cache_round_trip_and_staleness(root):
    old, _ = _runner()
    old.meta["X-A"].ticks = R.pricing.Ticks(((0.0, 0.1, 0.001), (0.1, 0.9, 0.01), (0.9, 1.0, 0.001)))
    old.meta["X-B"].result, old.meta["X-B"].series = "yes", "X"
    old.save_meta_cache()
    r, _ = _runner()
    r.meta.clear()
    assert r.load_meta_cache() and r.meta == old.meta
    path = os.path.join(old.root, R.META_CACHE)
    with open(path) as fh:
        d = json.load(fh)
    d["at"] -= R.META_CACHE_MAX_AGE_S + 1
    with open(path, "w") as fh:
        json.dump(d, fh)
    r.meta.clear()
    assert not r.load_meta_cache() and not r.meta


def _taken_over(root):
    old, b = _runner()
    _quotes(old, b)
    st = json.loads(json.dumps(old.handoff_state("h1")))
    sb, _ = _runner(standby="h1")
    sb.hot = []

    async def go():
        sb.restore(st)
    asyncio.run(go())
    sb.standby, sb.hot_ready = None, True
    return sb, b


def test_takeover_holds_inherited_baskets_until_they_are_ranked(root):
    sb, b = _taken_over(root)
    assert sb.inherited == {b.basket_id} and sb.holding()
    sb.rank_rows[b.basket_id] = {"basket_id": b.basket_id}
    assert not sb.holding() and not sb.inherited
    sb, b = _taken_over(root)
    sb.hold_until = time.monotonic() - 1.0
    assert not sb.holding()


def test_takeover_keeps_quotes_on_markets_still_loading(root):
    sb, b = _taken_over(root)
    cancels = []

    async def cancel_quotes(qs, why):
        cancels.append(why)
    sb.cancel_quotes = cancel_quotes
    sb.feed._books.pop("X-B")                      # its book isn't in yet

    async def go():
        sb.evaluate(b)
        await drain(sb)
    asyncio.run(go())
    assert cancels == []
    sb.hold_until = time.monotonic() - 1.0         # past the hold: pulled as before
    asyncio.run(go())
    assert cancels == ["untradeable"]


def test_quotes_hold_while_their_markets_meta_is_read_after_the_takeover_hold(root):
    sb, b = _taken_over(root)
    cancels = []

    async def cancel_quotes(qs, why):
        cancels.append(why)
    sb.cancel_quotes = cancel_quotes
    sb.hold_until = time.monotonic() - 1.0         # the takeover hold is over
    sb.meta.pop("X-B")
    sb._meta_pending = {"X-B"}                     # its meta is still being read

    async def go():
        sb.evaluate(b)
        await drain(sb)
    asyncio.run(go())
    assert cancels == []
    sb._meta_pending = set()                       # the read ended without it: pulled
    asyncio.run(go())
    assert cancels == ["untradeable"]


def test_start_reads_markets_missing_from_the_meta_cache_in_the_background(root, monkeypatch):
    old, _ = _runner()
    old.save_meta_cache()
    r, _ = _runner()
    r.meta.clear()
    r.tickers.append("X-NEW")
    calls, release = [], None

    def slow(tickers=None):
        calls.append(None if tickers is None else list(tickers))
        if tickers is not None:
            release.wait(5)
    monkeypatch.setattr(r, "load_meta", slow)

    async def go():
        nonlocal release
        import threading
        release = threading.Event()
        await asyncio.wait_for(r._meta_at_start(), 1.0)   # returns without waiting on the read
        assert r._meta_pending == {"X-NEW"} and "X-A" in r.meta
        release.set()
        await r._meta_task
        assert not r._meta_pending and calls == [["X-NEW"], None]
    asyncio.run(go())


def test_exposed_tickers_are_hot_quoted_and_held_legs(root):
    r, b = _runner()
    r.hot = []
    assert r.exposed_tickers() == []
    r.hot = [b.basket_id]
    assert r.exposed_tickers() == sorted(b.tickers)
    r.hot = []
    r.real_ledger.record("gone:basket", ["Y-1", "Y-2"], "Y-1", "yes", 1, 0.4, time.time())
    assert r.exposed_tickers() == ["Y-1"]


def test_takeover_is_marked_before_new_markets_load(root):
    old, b = _runner()
    _attach(old, Client())
    old.live = True
    _books(old)
    sb, _ = _runner(standby="h1")
    sc = Client()
    _attach(sb, sc)
    _books(sb)
    sb.set_baskets([])                             # the held basket's markets are new to the standby
    loads = []

    def slow_meta(tickers=None):
        loads.append(tickers)
        time.sleep(1.0)                            # the standby's full load holding _meta_lock
    sb.load_meta = slow_meta

    async def no_audit():
        return True
    sb.audit = no_audit

    async def go():
        old.evaluate(b)
        await drain(old)
        assert await old.hand_off("h1")
        sc.resting = []
        t0 = time.monotonic()
        await sb.take_over("h1")
        took = time.monotonic() - t0
        assert hand.has(R.LIVE_ROOT, "ARBS", "h1", "taken") and not loads[1:]
        await drain(sb)
        return took
    assert asyncio.run(go()) < 0.5
    assert loads and sorted(loads[0]) == ["X-A", "X-B"]


def test_a_failed_hand_over_trades_on_unfrozen(root):
    old, b = _runner()
    _attach(old, Client())
    old.live = True

    def broken(hid):
        raise RuntimeError("dictionary changed size during iteration")
    old.handoff_state = broken

    async def go():
        return await old.hand_off("h1")
    assert asyncio.run(go()) is False
    assert not (old.handing or old.gw.frozen or old._buffering or old.retired)
    assert old._io_lock.acquire(blocking=False)               # released
    assert not hand.has(R.LIVE_ROOT, "ARBS", "h1", "handed")
    assert hand.has(R.LIVE_ROOT, "ARBS", "h1", "resumed")    # the parent's cue that it trades on
    assert any("hand-over failed" in a["msg"] for a in old.alerts)


def test_an_aborted_hand_over_says_it_trades_on(root):
    old, b = _runner()
    _attach(old, Client())
    old.live = True
    hand.mark(R.LIVE_ROOT, "ARBS", "h1", "abort")

    async def go():
        return await old.hand_off("h1")
    assert asyncio.run(go()) is False
    assert hand.has(R.LIVE_ROOT, "ARBS", "h1", "resumed") and not hand.has(R.LIVE_ROOT, "ARBS", "h1", "handed")
