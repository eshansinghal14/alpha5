"""Runner decisions in shadow mode on hand-built books (no network)."""
import collections
import os
import json
import asyncio
import dataclasses
import time

import pandas as pd
import pytest

from backtest.core import Trade
from strategies.ARBS import runner as R
from strategies.ARBS.spec import Basket, Leg


@pytest.fixture
def arbs(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    # The fixture's basket is pre-game: tests of the move rules get the live
    # ones; the pre-game damping has tests of its own.
    monkeypatch.setattr(R, "PRE_REQUOTE_MIN_TICKS", 1)
    monkeypatch.setattr(R, "PRE_REQUOTE_COOLDOWN_S", 0.0)
    monkeypatch.setattr(R, "PRE_NO_EDGE_GRACE_S", 0.0)
    monkeypatch.setattr(R, "PRE_LIMIT_SLACK_TICKS", 0)
    from strategies.ARBS import SERIES
    r = R.ArbsRunner(SERIES["ARBS"], live_trading=False)
    r.part_idx = None                                     # a whole process, not one half of shard 0
    b = Basket(basket_id="t:b", relationship_id="t", type="complete_set", cluster="t",
               legs=(Leg("X-A", "yes"), Leg("X-B", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.all_baskets, r.baskets, r.tickers = [b], {b.basket_id: b}, ["X-A", "X-B"]
    r.listed = {b.relationship_id}
    r.by_ticker = {"X-A": [b.basket_id], "X-B": [b.basket_id]}
    r.feed.add(r.tickers)                                 # a feed with no markets reads as down
    r.feed.kalshi._new = []                               # as subscribed already
    from backtest.book import L2Book
    r.feed._books = {t: L2Book(t) for t in r.tickers}
    r.feed.up = True
    for t in r.tickers:
        r.meta[t] = R.Meta(ticker=t, event="X", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_edge=0.01, quote_edge_add=0.02, quote_size=5,
                                        max_bundles_per_basket=50, max_dollars=1000.0, take_cooldown_s=0.0)
    r.hot = [b.basket_id]
    return r, b


def book(r, t, yes_bids, no_bids):
    now = pd.Timestamp.now(tz="UTC")
    r.feed._books[t].apply_snapshot("yes", yes_bids, time=now)
    r.feed._books[t].apply_snapshot("no", no_bids, time=now)


async def drain(r):
    for _ in range(50):
        if not r.tasks:
            return
        await asyncio.sleep(0)
        await asyncio.gather(*list(r.tasks), return_exceptions=True)


def test_take_when_basket_cheap(arbs):
    r, b = arbs
    # Buy YES A at 0.40 (NO bid 0.60), YES B at 0.50 (NO bid 0.50).
    book(r, "X-A", [(0.35, 10)], [(0.60, 3)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 3 and not pos.unhedged(b.tickers)


def _fill_quote_on_a(r, b, move_b_to=None):
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])     # A ask 0.65
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])     # B ask 0.55 -> gap < 0, no take

    async def go():
        r.evaluate(b)
        await drain(r)
        assert {q.leg for q in r.basket_quotes(b.basket_id)} == {0, 1}   # every leg quoted at once
        q = r.quotes[(b.basket_id, 0)]
        if move_b_to is not None:
            book(r, "X-B", [(0.40, 10)], [(move_b_to, 10)])
        # A print through our quote fills it in shadow.
        r._on_trade(q.ticker, Trade(ticker=q.ticker, time=pd.Timestamp.now(tz="UTC"), price=q.price, size=5,
                                    taker_side="no" if q.side == "yes" else "yes"))
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())


def test_quote_fill_completes_at_plan_when_the_book_held(arbs):
    r, b = arbs
    _fill_quote_on_a(r, b)
    _eval(r, b)
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 5 and not pos.unhedged(b.tickers)


def test_quote_fill_completes_at_once_when_the_other_leg_ran(arbs):
    r, b = arbs
    _fill_quote_on_a(r, b, move_b_to=0.42)          # B's ask 0.55 -> 0.58 as A fills
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 5 and not pos.unhedged(b.tickers)
    assert not r.basket_quotes(b.basket_id)


def test_each_leg_priced_against_the_others_asks(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])     # A ask 0.65
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])     # B ask 0.55

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    qa, qb = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 1)]
    req = R.edge_dollars(b.payout, r.cfg.trading.min_edge + r.cfg.trading.quote_edge_add)
    assert qa.max_price + 0.55 + 0.0174 <= 1 - req + 1e-9
    assert qb.max_price + 0.65 + 0.0160 <= 1 - req + 1e-9


def _broken(r, b):
    # Hold 5 YES A at 0.40. B's YES ask is 0.80 (complete loses ~$1.0); A's NO
    # ask is 0.70 (unwind loses ~$0.5 + fee), its NO bid 0.35.
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.20, 10)])
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.40, time.time())


def _eval(r, b):
    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())


def _no_deep(r):
    """No room for deep quotes, for tests about the other quotes."""
    r.deep_cap = lambda b, j, live: 0.0


def test_broken_basket_rests_on_the_cheaper_exit(arbs):
    r, b = arbs
    _broken(r, b)
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert (q.kind, q.side, q.size, q.price) == ("unwind", "no", 5, 0.36)
    assert (b.basket_id, 1) not in r.quotes
    note = r.summary()["holdings"][0]["completion"]
    assert note["action"] == "rest" and note["path"] == "unwind"


def test_broken_basket_takes_when_the_exit_moves_away(arbs):
    r, b = arbs
    _broken(r, b)
    _eval(r, b)
    book(r, "X-A", [(0.25, 10)], [(0.35, 10)])      # A's NO ask 0.75: unwind 0.25 worse
    _eval(r, b)
    assert b.basket_id not in r.shadow_ledger.baskets
    row = r.shadow_ledger.settled[-1]
    fee = R.kfees.fee(0.75, 5, 0.07)
    assert row["pnl"] == pytest.approx(5 - 5 * 0.75 - fee - 2.0, abs=1e-4)
    assert not r.quotes


def test_broken_basket_takes_at_once_when_chase_is_zero_or_live(arbs):
    r, b = arbs
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_ticks=0)
    _broken(r, b)
    _eval(r, b)
    assert b.basket_id not in r.shadow_ledger.baskets


def test_live_event_exits_at_once(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.baskets[b.basket_id] = live
    _broken(r, live)
    _eval(r, live)
    assert live.basket_id not in r.shadow_ledger.baskets


def test_exit_quote_fill_nets_the_leg_out(arbs):
    r, b = arbs
    _broken(r, b)
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    r._quote_fill(q, 5, 5 * 0.36, None, "shadow")
    assert b.basket_id not in r.shadow_ledger.baskets
    assert r.shadow_ledger.settled[-1]["pnl"] == pytest.approx(5 - 1.8 - 2.0)


def test_quote_sized_to_the_other_legs_touch(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 3)])        # A's YES ask 0.65, 3 deep
    book(r, "X-B", [(0.20, 10)], [(0.45, 10)])
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_edge=0.0, quote_edge_add=0.0)
    _eval(r, b)
    assert r.quotes[(b.basket_id, 1)].size == 3


def test_settlement_books_pnl(arbs):
    r, b = arbs
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 2, 0.8, time.time())
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-B", "yes", 2, 1.0, time.time())
    r.meta["X-A"].status, r.meta["X-A"].result = "finalized", "yes"
    r.meta["X-B"].status, r.meta["X-B"].result = "finalized", "no"
    r.settle()
    assert b.basket_id not in r.shadow_ledger.baskets
    assert r.shadow_ledger.settled[-1]["pnl"] == pytest.approx(0.2)


def test_voided_event_settles_at_scalar_values(arbs):
    r, b = arbs
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 2, 0.8, time.time())
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-B", "yes", 2, 1.0, time.time())
    r.meta["X-A"].status, r.meta["X-A"].result = "finalized", "scalar"
    r.meta["X-B"].status, r.meta["X-B"].result = "finalized", "scalar"
    r.meta["X-A"].value = 0.3
    r.settle(); r.settle()
    assert b.basket_id in r.shadow_ledger.baskets and len(r.alerts) == 1    # X-B has no value yet
    r.meta["X-B"].value = 0.7
    r.settle()
    assert b.basket_id not in r.shadow_ledger.baskets
    assert r.shadow_ledger.settled[-1]["pnl"] == pytest.approx(2 * 0.3 + 2 * 0.7 - 1.8)


def test_resting_quotes_are_not_capital_used(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    worst = b.payout + 0.01 * r.cfg.completion.chase_ticks
    # Room for one basket's 5 bundles: each quote is sized to fit it alone.
    r.cfg.trading = dataclasses.replace(r.cfg.trading, max_dollars=5 * worst + 0.01)
    _eval(r, b)
    qs = r.basket_quotes(b.basket_id)
    assert {q.leg for q in qs} == {0, 1} and all(q.size == 5 for q in qs)
    assert r.committed() == 0.0


def test_edge_is_a_return_on_bundle_cost():
    # 1.5% on a payout-2 bundle needs about twice the dollar edge of payout-1.
    assert R.edge_dollars(2.0, 0.015) == pytest.approx(2 * R.edge_dollars(1.0, 0.015))
    e = R.edge_dollars(1.0, 0.015)
    assert R.edge_return(1.0, e) == pytest.approx(0.015)


def test_take_needs_return_not_cents(arbs):
    r, b2 = arbs
    b = dataclasses.replace(b2, payout=2.0, basket_id="t:b2")
    r.all_baskets.append(b)
    r.baskets[b.basket_id] = b
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_edge=0.015, enable_quoting=False)
    # Asks 0.99 + 0.99 (+fees ~0.0014): edge ~1.86c, which clears 1.5c, but on a
    # ~$1.98 bundle that is 0.94% < 1.5%.
    book(r, "X-A", [], [(0.01, 10)])
    book(r, "X-B", [], [(0.01, 10)])

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    assert b.basket_id not in r.shadow_ledger.baskets


def test_dollar_cap_blocks_quote(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, max_dollars=0.5)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    assert b.basket_id not in r.quotes


def _with_flip(r, b):
    f = dataclasses.replace(b, basket_id="t:flip", legs=(Leg("X-A", "no"), Leg("X-B", "no")))
    r.all_baskets.append(f)
    r.baskets[f.basket_id] = f
    for t in r.tickers:
        r.by_ticker[t].append(f.basket_id)
    r.flip = R.flips(r.all_baskets)
    return f


def test_flip_exits_held_bundles_at_min_exit_edge(arbs):
    r, b = arbs
    f = _with_flip(r, b)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_exit_edge=0.01, min_edge=0.05, max_dollars=0.0,
                                        enable_quoting=False, exit_ref_days=7.0)     # the scaled exit (Polymarket's)
    for t in b.tickers:
        r.shadow_ledger.record(b.basket_id, b.tickers, t, "yes", 3, 1.35, time.time())
    # NO asks 0.47 + 0.47 + fees 0.035: a 2.5c edge, above min_exit_edge, below min_edge.
    book(r, "X-A", [(0.53, 10)], [(0.40, 10)])
    book(r, "X-B", [(0.53, 10)], [(0.40, 10)])

    async def go():
        r.evaluate(f)
        await drain(r)
    asyncio.run(go())
    pos = r.shadow_ledger.baskets[f.basket_id]
    # Capped at the 3 held bundles though the touch holds 10 and max_dollars is 0.
    assert pos.bundles(f.tickers) == 3
    assert r.exit_room(f) == 0
    # The netted pairs are cash back: 3 bundles x 2 legs x $1 off the held cost.
    assert r.committed() == pytest.approx(r.shadow_ledger.baskets[b.basket_id].cost() + pos.cost() - 6.0)


def test_flip_without_held_bundles_needs_min_edge(arbs):
    r, b = arbs
    f = _with_flip(r, b)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_exit_edge=0.01, min_edge=0.05, enable_quoting=False)
    book(r, "X-A", [(0.53, 10)], [(0.40, 10)])
    book(r, "X-B", [(0.53, 10)], [(0.40, 10)])

    async def go():
        r.evaluate(f)
        await drain(r)
    asyncio.run(go())
    assert f.basket_id not in r.shadow_ledger.baskets


def test_flip_quoted_for_exit_when_not_hot(arbs):
    r, b = arbs
    f = _with_flip(r, b)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_exit_edge=0.01, min_edge=0.05, max_dollars=0.0)
    for t in b.tickers:
        r.shadow_ledger.record(b.basket_id, b.tickers, t, "yes", 3, 1.35, time.time())
    book(r, "X-A", [(0.50, 10)], [(0.30, 10)])     # NO asks 0.50 + 0.52: no take
    book(r, "X-B", [(0.48, 10)], [(0.30, 10)])

    async def go():
        r.evaluate(f)
        await drain(r)
    asyncio.run(go())
    qs = r.basket_quotes(f.basket_id)
    assert {q.leg for q in qs} == {0, 1} and all(q.size == 3 for q in qs)


TAPERED = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
           {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
           {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]


def test_sub_cent_ask_is_taken_at_its_price(arbs):
    r, b = arbs
    for t in r.tickers:
        r.meta[t].ticks = R.pricing.Ticks.from_market(TAPERED)
        r.feed._books[t].price_dp = 4
    # YES A ask 0.914 (NO bid 0.086), YES B ask 0.05: rounding to cents read A
    # as 0.91 and its IOC took nothing (TXSENR-P6, 10-28 14:01).
    book(r, "X-A", [(0.90, 10)], [(0.086, 2)])
    book(r, "X-B", [(0.04, 10)], [(0.95, 2)])
    vs = r.views(b)
    assert vs[0].best_ask == 0.914

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 2
    assert pos.legs["X-A"].cost == pytest.approx(2 * 0.914 + R.kfees.fee(0.914, 2, 0.07))


def test_baskets_sharing_a_leg_dont_take_the_same_touch(arbs):
    r, b = arbs
    c = dataclasses.replace(b, basket_id="t:c", legs=(Leg("X-A", "yes"), Leg("X-C", "yes")))
    r.all_baskets.append(c)
    r.baskets[c.basket_id] = c
    r.tickers.append("X-C")
    r.by_ticker["X-A"].append(c.basket_id)
    r.by_ticker["X-C"] = [c.basket_id]
    from backtest.book import L2Book
    r.feed._books["X-C"] = L2Book("X-C")
    r.meta["X-C"] = R.Meta(ticker="X-C", event="X", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, enable_quoting=False)
    # One contract of YES A at 0.40 serves both baskets' touch.
    book(r, "X-A", [(0.35, 10)], [(0.60, 1)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])
    book(r, "X-C", [(0.45, 10)], [(0.50, 10)])

    async def go():
        r.evaluate(b)
        r.evaluate(c)            # same pass, before b's take has run
        assert r.views(c)[0].asks == []
        await drain(r)
    asyncio.run(go())
    assert r.shadow_ledger.baskets[b.basket_id].bundles(b.tickers) == 1
    assert c.basket_id not in r.shadow_ledger.baskets
    assert r.inflight == {}


def test_reload_keeps_held_basket_and_adds_new_tickers(arbs):
    r, b = arbs
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 1.0, 0.4, time.time())
    n = Basket(basket_id="n:b", relationship_id="n", type="mutually_exclusive_no", cluster="n",
               legs=(Leg("Y-A", "no"), Leg("Y-B", "no")), payout=1.0, quote_until=None, resume_quoting=None)
    assert r.set_baskets([n]) == ["Y-A", "Y-B"]
    assert set(r.baskets) == {"t:b", "n:b"} and r.by_ticker["Y-A"] == ["n:b"] and "n:b" in r.dirty
    r.shadow_ledger.baskets.clear()
    r.set_baskets([n])
    assert set(r.baskets) == {"n:b"} and "t:b" not in r.hot
    r.feed.add(["Y-A", "Y-B"])
    assert r.feed._new == ["Y-A", "Y-B"] and "Y-A" in r.feed.tickers


def test_days_till_expiry_gates_new_trades(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, days_till_expiry=30)
    soon = dataclasses.replace(b, settles=pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=20))
    late = dataclasses.replace(b, settles=pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=40))
    assert not r.excluded(soon) and r.excluded(late) and not r.excluded(b)


def test_audit_books_settlement_before_striking(arbs):
    r, b = arbs
    for t in b.tickers:
        r.real_ledger.record(b.basket_id, b.tickers, t, "yes", 5.0, 2.5, time.time())
        r.meta[t].status = "active"                 # our cache hasn't seen the settlement yet

    class Client:
        def positions_all(self):
            return {}                       # Kalshi already dropped the settled position

    async def call(fn, *a):
        return fn(*a)

    def load_meta(*a):
        for t, res in zip(b.tickers, ("yes", "no")):
            r.meta[t].status, r.meta[t].result = "finalized", res
    r.gw.any_client, r.gw.call, r.load_meta = Client, call, load_meta
    assert asyncio.run(r.audit()) and r.audit_strikes == 0 and b.basket_id not in r.real_ledger.baskets
    assert r.real_ledger.settled[-1]["pnl"] == 0.0


def test_unfinished_basket_marked_at_sale_value(arbs):
    r, b = arbs
    r.real_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 10.0, 4.0, time.time())
    book(r, "X-A", [(0.25, 50)], [(0.70, 50)])       # YES A sells at 0.25
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])
    s = r.summary()
    h = s["holdings"][0]
    fee = R.kfees.fee(0.25, 10.0, 0.07)
    assert s["ledger"] == "real" and abs(h["broken_pnl"] - (2.5 - fee - 4.0)) < 1e-4
    assert s["pnl"]["broken"] == h["broken_pnl"] and h["locked_pnl"] == 0.0


def test_slow_drift_keeps_resting(arbs):
    r, b = arbs
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_window_s=0.0)
    _broken(r, b)
    _eval(r, b)
    book(r, "X-A", [(0.25, 10)], [(0.35, 10)])       # same 5-tick move, but nothing recent to measure from
    _eval(r, b)
    assert r.shadow_ledger.baskets[b.basket_id].unhedged(b.tickers) == {"X-A": 5}
    assert r.quotes[(b.basket_id, 0)].kind == "unwind"


def test_one_quote_per_market_side(arbs):
    r, b = arbs
    b2 = Basket(basket_id="t:b2", relationship_id="t", type="mutually_exclusive_no", cluster="t",
                legs=(Leg("X-A", "yes"), Leg("X-B", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.set_baskets([b, b2])
    r.hot = [b.basket_id, b2.basket_id]
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    _eval(r, b2)
    assert {q.basket_id for q in r.quotes.values()} == {b.basket_id}


def test_rescan_reordering_legs_keeps_quotes_on_their_markets(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    before = {q.ticker: q.price for q in r.quotes.values() if not q.deep}
    r.set_baskets([dataclasses.replace(b, legs=tuple(reversed(b.legs)))])
    assert r.baskets[b.basket_id].legs == b.legs
    _eval(r, r.baskets[b.basket_id])
    assert {q.ticker: q.price for q in r.quotes.values() if not q.deep} == before


def test_live_events_quoted_only_when_enabled_and_smaller(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, live)
    assert not r.quotes
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=2)
    _eval(r, live)
    assert {q.size for q in r.quotes.values() if not q.deep} == {2}


def test_held_basket_survives_dropping_out_of_the_spec(arbs, tmp_path):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.50, 10)])
    r._book(b.basket_id, "X-A", "yes", 5, 2.0, "take")
    r._book(b.basket_id, "X-B", "yes", 5, 2.5, "take")
    from strategies.ARBS.ledger import Ledger
    again = Ledger(r.shadow_ledger.path)                 # as after a restart
    r.shadow_ledger, r.baskets, r.all_baskets = again, {}, []
    r.set_baskets([])
    assert r.baskets[b.basket_id] == dataclasses.replace(b, cluster=b.cluster)
    assert r.summary()["holdings"][0]["locked_pnl"] == pytest.approx(5 * 1.0 - 4.5)


def test_held_basket_wins_over_a_renamed_duplicate(arbs):
    r, b = arbs
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 1, 0.4, time.time())
    renamed = dataclasses.replace(b, basket_id="t:renamed", legs=tuple(reversed(b.legs)))
    r.set_baskets([renamed])
    assert set(r.baskets) == {b.basket_id}
    r.shadow_ledger.baskets.clear()
    r.set_baskets([renamed])
    assert set(r.baskets) == {"t:renamed"}


def test_missed_exit_retries_only_after_the_book_changes(arbs):
    r, b = arbs
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_ticks=0)
    _broken(r, b)
    sent = []

    async def miss(ticker, side, limit, size, reason, view):
        sent.append(ticker)
        return None, None
    r._ioc = miss
    _eval(r, b)
    _eval(r, b)                                      # same book: no second IOC at the level that missed
    assert len(sent) == 1
    r._on_quote(type("V", (), {"ticker": "X-A", "ask": 0.7, "ask_depth": 1, "bid": 0.3, "bid_depth": 1})())
    _eval(r, b)
    assert len(sent) == 2


def test_market_missing_at_first_read_is_picked_up_later(arbs, monkeypatch):
    r, b = arbs
    del r.meta["X-B"]
    listed = {"markets": [{"ticker": "X-A", "event_ticker": "X", "status": "active"}]}
    calls = []

    def get(path, params=None, auth=False):
        calls.append(path)
        if path.endswith("/markets"):
            return listed
        return {"event": {"series_ticker": "XS", "exchange_index": 0}}
    monkeypatch.setattr(R, "kalshi_get", get)
    monkeypatch.setattr(R.kfees, "for_event", lambda s, e: type("F", (), {"taker_rate": 0.07, "maker_rate": 0.0})())
    r.load_meta()
    assert r.meta["X-B"].status == "missing"
    listed["markets"].append({"ticker": "X-B", "event_ticker": "X", "status": "active"})
    r.load_meta()
    assert (r.meta["X-B"].event, r.meta["X-B"].status, r.meta["X-B"].shard) == ("X", "active", 0)
    assert "/trade-api/v2/events/" not in calls


def test_a_restart_takes_event_series_shard_and_fees_from_the_cache(arbs, monkeypatch):
    from strategies.ARBS import SERIES
    r, b = arbs
    listed = {"markets": [{"ticker": t, "event_ticker": "X", "status": "active"} for t in ("X-A", "X-B")]}
    calls = []

    def get(path, params=None, auth=False):
        calls.append(path)
        return listed if path.endswith("/markets") else {"event": {"series_ticker": "XS", "exchange_index": 3}}
    monkeypatch.setattr(R, "kalshi_get", get)
    monkeypatch.setattr(R.kfees, "for_event", lambda s, e: type("F", (), {"taker_rate": 0.07, "maker_rate": 0.01})())
    r.meta.clear()
    r.load_meta()
    assert calls.count("/trade-api/v2/events/X") == 1
    r2 = R.ArbsRunner(SERIES["ARBS"], live_trading=False)       # the next process, same files
    r2.tickers = r.tickers
    r2.load_meta()
    assert calls.count("/trade-api/v2/events/X") == 1               # no second read
    assert (r2.meta["X-A"].series, r2.meta["X-A"].shard, r2.meta["X-A"].taker_rate) == ("XS", 3, 0.07)
    stale = json.load(open(os.path.join(r.root, R.EVENT_META)))
    stale["X"]["at"] -= R.EVENT_META_TTL_S
    json.dump(stale, open(os.path.join(r.root, R.EVENT_META), "w"))
    r3 = R.ArbsRunner(SERIES["ARBS"], live_trading=False)
    r3.tickers = r.tickers
    r3.load_meta()
    assert calls.count("/trade-api/v2/events/X") == 2               # a day old: read again


def test_a_restart_starts_from_the_last_plan_scale(arbs):
    from strategies.ARBS import SERIES
    r, b = arbs
    d = os.path.join(r.root, f"{R._utc():%Y-%m-%d}")
    os.makedirs(d, exist_ok=True)
    json.dump({"write_plan_scale": {"0": 2.5, "3": 99.0}}, open(os.path.join(d, "summary.json"), "w"))
    assert R.ArbsRunner(SERIES["ARBS"], live_trading=False).plan_scale == {0: 2.5, 3: R.PLAN_SCALE_MAX}


def test_with_no_horizon_or_exit_scaling_a_far_basket_trades_at_the_full_edge(arbs):
    r, b = arbs
    far = dataclasses.replace(b, settles=R._utc() + pd.Timedelta(days=700))
    t = r.cfg.trading
    r.cfg.trading = dataclasses.replace(t, days_till_expiry=120.0, exit_ref_days=7.0, min_exit_edge=0.01)
    r._horizon = None
    assert r.excluded(far)
    assert r.exit_need(far, 0.05, R._utc()) == pytest.approx(0.01)       # 0.05 x 7 / 700, floored
    r.cfg.trading = dataclasses.replace(t, days_till_expiry=None, exit_ref_days=None, min_exit_edge=0.01)
    r._horizon = None
    assert not r.excluded(far)
    assert r.exit_need(far, 0.05, R._utc()) == 0.05


def test_scan_markets_hold_the_event_loop_near_its_cpu_target(arbs):
    r, b = arbs
    r.markets, W = 4000, R.LOOP_CPU_WINDOW_S
    assert r.scan_markets(0.0, 0.0) == 4000                      # before the hot list is up: held
    r.hot_ready = True
    assert r.scan_markets(0.0, 0.0) == 4000                      # starts a window
    assert r.scan_markets(W / 2, W / 2 * 0.9) == 4000            # half a window: held (CPU is cumulative)
    assert r.scan_markets(W, W * 0.9) == 3111 and r.loop_cpu == pytest.approx(0.9)   # 4000 * 0.7 / 0.9
    assert r.scan_markets(2 * W, 1.63 * W) == 3111               # 73%, inside the band: held
    r.listed_markets = 3500
    assert r.scan_markets(3 * W, 1.83 * W) == 3500               # 20%: +25% at most, then all listed
    assert r.scan_markets(4 * W, 2.03 * W) == 3500
    r.markets = 1100
    assert r.scan_markets(5 * W, 3.03 * W) == R.MARKETS_MIN      # a pinned loop: -25%, floored


def test_scan_markets_start_where_the_last_process_left_them(arbs):
    from strategies.ARBS import SERIES
    r, b = arbs
    assert r.scan_markets() == R.MARKETS_MIN                     # nothing saved, a small spec: the floor
    d = os.path.join(r.root, f"{R._utc():%Y-%m-%d}")
    os.makedirs(d, exist_ok=True)
    json.dump({"scan_markets": 4321}, open(os.path.join(d, "summary.json"), "w"))
    assert R.ArbsRunner(SERIES["ARBS"], live_trading=False).scan_markets() == 4321


def _cheap_book(r):
    book(r, "X-A", [(0.35, 10)], [(0.60, 10)])      # YES A ask 0.40
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])      # YES B ask 0.50


def _live_takes(r, b):
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, take_live_events=True, live_take_size=100)
    return live


def test_live_take_sends_the_stalest_leg_first_and_stops_if_it_misses(arbs):
    r, b = arbs
    b = _live_takes(r, b)
    _cheap_book(r)
    r.book_time = {"X-A": 2.0, "X-B": 1.0}          # B hasn't updated for longer
    sent = []

    async def ioc(ticker, side, limit, size, reason, view):
        sent.append((ticker, size))
        return None, None                             # B's level was already gone
    r._ioc = ioc
    _eval(r, b)
    assert sent == [("X-B", 10.0)] and b.basket_id not in r.shadow_ledger.baskets


def test_live_take_rest_of_the_legs_sized_to_the_first_fill(arbs):
    r, b = arbs
    b = _live_takes(r, b)
    _cheap_book(r)
    r.book_time = {"X-A": 2.0, "X-B": 1.0}
    sent = []
    real = r._ioc

    async def ioc(ticker, side, limit, size, reason, view):
        sent.append((ticker, size))
        if ticker == "X-B":
            return R.RealFill(price=0.50, size=2.0, fee=0.0, exchange_order_id=None), None
        return await real(ticker, side, limit, size, reason, view)
    r._ioc = ioc
    _eval(r, b)
    assert sent == [("X-B", 10.0), ("X-A", 2.0)]
    assert r.shadow_ledger.baskets[b.basket_id].bundles(b.tickers) == 2


def test_take_off_live_sends_every_leg_at_once(arbs):
    r, b = arbs
    _cheap_book(r)
    r.book_time = {"X-A": 2.0, "X-B": 1.0}
    sent = []

    async def ioc(ticker, side, limit, size, reason, view):
        sent.append((ticker, size))
        return None, None
    r._ioc = ioc
    _eval(r, b)
    assert sorted(sent) == [("X-A", 10.0), ("X-B", 10.0)]


def test_no_takes_on_live_events_unless_enabled(arbs):
    r, b = arbs
    b = _live_takes(r, b)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, take_live_events=False)
    _cheap_book(r)
    _eval(r, b)
    assert b.basket_id not in r.shadow_ledger.baskets


def test_take_capped_on_live_events(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.cfg.trading = dataclasses.replace(r.cfg.trading, live_take_size=1, live_quote_size=100, take_live_events=True)
    _cheap_book(r)
    _eval(r, live)
    assert r.shadow_ledger.baskets[live.basket_id].bundles(live.tickers) == 1


def test_quote_past_its_limit_is_cancelled_when_the_move_is_refused(arbs):
    r, b = arbs
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=5, cid="c", edge=0.0,
                max_price=0.40, pending=False, exchange_id="oid")
    r.quotes[q.key] = q
    r.live = True

    async def amend(*a, **k):
        return None, None, False                   # write budget refused it

    async def cancel(ticker, oid, reason):
        return None, None, True
    r.gw.amend, r.gw.cancel = amend, cancel
    r.book_wall.update({"X-A": 100.0, "X-B": 101.5})
    asyncio.run(r.move_quote(q, R.pricing.QuotePlan(leg=0, price=0.35, max_price=0.36, edge=0.0, others_cost=0.0)))
    assert q.key not in r.quotes and r.n["moves_refused"] == 1
    # Both writes logged against the newest book update on the basket's markets.
    assert [(o["kind"], o["book"]) for o in r.order_times] == [("amend", 101.5), ("cancel", 101.5)]
    r.live = False


def test_chase_ignores_what_older_bundles_cost(arbs):
    r, b = arbs
    # 5 whole bundles bought dear, then a quote on A fills 1 more at 0.40,
    # priced against B's 0.50 ask, now 0.62: a slow drift, so it rests.
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.70, time.time())
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-B", "yes", 5, 5 * 0.29, time.time())
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.38, 10)])
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=1, cid="c",
                edge=1 - 0.40 - R.pricing.taker_cost(0.50, 0.07), max_price=0.40, pending=False)
    r.quotes[q.key] = q
    r._quote_fill(q, 1, 0.40, None, "shadow")
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_ticks=50)
    _eval(r, b)
    note = r.summary()["holdings"][0]["completion"]
    assert note["action"] == "rest" and note["why"] is None
    # Costed at its own 0.40, not the leg's 0.65 average.
    assert note["complete_loss"] == pytest.approx(0.40 + R.pricing.taker_cost(0.62, 0.07) - 1, abs=1e-4)


def _top(r, t, bid, bid_depth, ask=None, ask_depth=0.0):
    r._on_quote(type("V", (), {"ticker": t, "bid": bid, "bid_depth": bid_depth, "ask": ask,
                               "ask_depth": ask_depth})())


def test_live_quotes_pulled_when_a_leg_they_complete_with_moves_away(arbs):
    r, b = arbs
    b = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([b])
    r.hot = [b.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=5, live_edge_add=0.0,
                                        pull_drop=0.5, pull_window_s=30.0, pull_hold_s=2.0, pull_only=None)
    rows = []
    r.journal = rows.append
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    assert (b.basket_id, 0) in r.quotes and (b.basket_id, 1) in r.quotes
    _top(r, "X-A", 0.31, 20, 0.35, 10)
    _top(r, "X-A", 0.31, 2, 0.35, 10)                 # A's own bids thin: not a completion leg's ask
    assert not r.pulled
    _top(r, "X-B", 0.40, 10, 0.55, 10)
    _top(r, "X-B", 0.40, 10, 0.57, 10)                 # B's ask rises: A's quote completes through B
    assert ("X-A", "yes") in r.pulled and ("X-B", "yes") not in r.pulled
    _eval(r, b)
    assert (b.basket_id, 0) not in r.quotes and (b.basket_id, 1) in r.quotes
    pull = [x for x in rows if x["kind"] == "pull"][0]
    assert (pull["ticker"], pull["by"], pull["quoted"]) == ("X-A", "X-B", [b.basket_id])
    r.pulled[("X-A", "yes")] = time.monotonic() - 0.01   # the hold ran out
    _eval(r, b)
    assert (b.basket_id, 0) in r.quotes


def test_cross_pull_runs_only_where_pull_only_covers_a_market(arbs):
    r, b = arbs
    b = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([b])
    r.hot = [b.basket_id]
    r.series_group = {"X": "S:Hockey"}
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=5, live_edge_add=0.0,
                                        pull_drop=0.5, pull_window_s=30.0, pull_hold_s=2.0, pull_only=["S:Soccer"])
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    _top(r, "X-B", 0.40, 10, 0.55, 10)
    _top(r, "X-B", 0.40, 10, 0.57, 10)
    assert not r.pulled                              # hockey isn't listed
    r.cfg.trading = dataclasses.replace(r.cfg.trading, pull_only=["KXEBASKETBALLGAME", "S:Hockey"])
    _top(r, "X-B", 0.40, 10, 0.55, 10)
    _top(r, "X-B", 0.40, 10, 0.57, 10)
    assert ("X-A", "yes") in r.pulled


def test_cross_pull_fires_on_a_vanishing_ask_and_never_for_pregame_baskets(arbs):
    r, b = arbs
    r.hot = [b.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, pull_drop=0.5, pull_window_s=30.0, pull_hold_s=2.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    _top(r, "X-B", 0.40, 10, 0.55, 10)
    _top(r, "X-B", 0.40, 10, 0.70, 10)
    assert not r.pulled                              # pre-game
    w = R.AskWatch()
    k = ("T", "no")
    assert not w.fires(k, 0.0, 0.45, 10.0, 0.25, 0.5)
    assert not w.fires(k, 0.05, 0.45, 6.0, 0.25, 0.5)        # 10 -> 6 at the same price: not halved
    assert w.fires(k, 0.10, 0.45, 4.0, 0.25, 0.5)            # 10 -> 4: halved
    k = ("T", "yes")
    assert not w.fires(k, 0.0, 0.45, 10.0, 0.25, 0.5)
    assert w.fires(k, 0.1, 0.44, 10.0, 0.25, 0.5)            # the bid fell: the ask rose
    assert not w.fires(k, 1.0, 0.43, 10.0, 0.25, 0.5)        # its last high is out of the window
    assert w.fires(k, 1.1, None, 0.0, 0.25, 0.5)             # the bids are gone


def _second_basket(r, b, legs):
    from backtest.book import L2Book
    c = Basket(basket_id="t:c", relationship_id="t2", type="complete_set", cluster="t2", legs=legs, payout=1.0,
               quote_until=None, resume_quoting=None)
    r.set_baskets([b, c])
    for t in c.tickers:
        r.feed._books.setdefault(t, L2Book(t))
        r.meta[t] = R.Meta(ticker=t, event="X", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    return c


def _cheaper_through_c(r, b):
    # b = A + B, c = A + C; C's ask (0.55) is 10c under B's (0.65), so A's
    # quote can rest higher if a fill completes through c.
    c = _second_basket(r, b, (Leg("X-A", "yes"), Leg("X-C", "yes")))
    book(r, "X-A", [(0.20, 10)], [(0.55, 10)])     # A: bid 0.20, ask 0.45
    book(r, "X-B", [(0.30, 10)], [(0.35, 10)])     # B ask 0.65
    book(r, "X-C", [(0.40, 10)], [(0.45, 10)])     # C ask 0.55
    return c


def test_quote_priced_through_the_cheapest_basket_with_its_leg(arbs):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    t = r.cfg.trading
    own = R.pricing.quote_leg(r.views(b), 0, 1.0, R.edge_dollars(1.0, t.min_edge + t.quote_edge_add), 5)
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert r.via[(b.basket_id, 0)] == c.basket_id
    assert q.max_price == pytest.approx(own.max_price + 0.10, abs=0.005)
    assert b.basket_id in r.via_by_ticker["X-C"]     # C's book now moves this quote


def test_fill_booked_into_the_basket_that_completes_it_cheapest(arbs):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    rows = []
    r.journal = rows.append
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    r._quote_fill(q, 5, 5 * q.price, None, "shadow")
    L = r.shadow_ledger
    assert b.basket_id not in L.baskets and L.baskets[c.basket_id].held(c.tickers) == [5, 0]
    fill = [x for x in rows if x["kind"] == "fill"][0]
    assert fill["basket_id"] == c.basket_id and fill["quoted_in"] == b.basket_id
    _eval(r, c)
    assert L.baskets[c.basket_id].bundles(c.tickers) == 5      # completed by buying C, not B


def test_a_kept_alternative_is_priced_on_fresh_books_and_dropped_when_unbookable(arbs):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    t = r.cfg.trading
    need = R.edge_dollars(1.0, t.min_edge + t.quote_edge_add)
    plan = lambda: R.pricing.quote_leg(r.views(b), 0, 1.0, need, 5)
    p1, _ = r.best_plan(b, 0, plan(), 5, 5.0)
    assert r.via[(b.basket_id, 0)] == c.basket_id
    book(r, "X-C", [(0.40, 10)], [(0.50, 10)])     # C's ask falls 0.55 -> 0.50 within ALT_SEARCH_S
    p2, _ = r.best_plan(b, 0, plan(), 5, 5.0)
    assert r.via[(b.basket_id, 0)] == c.basket_id and p2.max_price >= p1.max_price + 0.04 - 1e-9   # re-priced
    r.cfg.watch = dataclasses.replace(r.cfg.watch, exclude=[c.basket_id])
    r.best_plan(b, 0, plan(), 5, 5.0)
    assert (b.basket_id, 0) not in r.via                                    # searched again, none left


def test_excluded_basket_is_no_alternative(arbs):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    r.cfg.watch = dataclasses.replace(r.cfg.watch, exclude=[c.basket_id])
    _eval(r, b)
    assert (b.basket_id, 0) not in r.via


def test_a_model_quote_priced_through_another_basket_keeps_the_models_edge(arbs, monkeypatch):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    b, c = (dataclasses.replace(x, live_from=pd.Timestamp("2026-01-01", tz="UTC")) for x in (b, c))
    r.set_baskets([b, c])
    r.hot = [b.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=5, live_edge_add=0.0,
                                        requote_cooldown_s=0.0, depth_age_s=0.0, model_series=["X"])
    r.exit_model = object()                                 # model_pick is stubbed: only its presence counts
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, *a, **k: (0.20, 5.0))
    own = R.pricing.quote_leg(r.views(b), 0, 1.0, 0.20, 5)
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert r.via[(b.basket_id, 0)] == c.basket_id                           # still through c, 10c cheaper to complete
    assert q.max_price == pytest.approx(own.max_price + 0.10, abs=0.005)    # at the model's 20c, not config's 3c


def test_unwind_nets_out_the_newest_contracts(arbs):
    r, b = arbs
    L = r.shadow_ledger
    L.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.70, time.time())
    L.record(b.basket_id, b.tickers, "X-B", "yes", 5, 5 * 0.29, time.time())
    L.record(b.basket_id, b.tickers, "X-A", "yes", 1, 0.40, time.time())
    assert L.unwind(b.basket_id, b.tickers, "X-A", 1, time.time()) == pytest.approx(0.40)
    assert L.baskets[b.basket_id].legs["X-A"].cost == pytest.approx(3.50)   # the bundles keep what they cost


def _filled_a(r, b):
    # A quote on A fills 10 at 0.40, priced against B's 0.50 ask.
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=10, cid="c",
                edge=1 - 0.40 - R.pricing.taker_cost(0.50, 0.07), max_price=0.40, pending=False)
    r.quotes[q.key] = q
    r._quote_fill(q, 10, 4.0, None, "shadow")


def test_partial_completion_retry_is_scored_against_the_plan(arbs):
    r, b = arbs
    rows = []
    r.journal = rows.append
    book(r, "X-A", [(0.30, 10)], [(0.55, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])
    _filled_a(r, b)
    real, sent = r._ioc, []

    async def ioc(ticker, side, limit, size, reason, view):
        fill, exc = await real(ticker, side, limit, size, reason, view)
        sent.append(size)
        if len(sent) == 1:                           # the first IOC gets 4 of its 10
            fill = dataclasses.replace(fill, size=4.0, fee=fill.fee * 0.4)
        return fill, exc
    r._ioc = ioc
    _eval(r, b)
    _eval(r, b)
    exits = [x for x in rows if x["kind"] == "complete"]
    assert [x["orders"]["X-B"][1] for x in exits] == [10.0, 6.0]
    # Same price as planned both times; net of today's broken cost alone, the
    # retry missed the 4 completed bundles' cost and payout.
    assert all(abs(x["vs_plan"]) < 0.01 for x in exits)
    assert not r.shadow_ledger.baskets[b.basket_id].unhedged(b.tickers)


def test_exit_rests_after_max_exit_misses(arbs):
    r, b = arbs
    b = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([b])
    r.cfg.completion = dataclasses.replace(r.cfg.completion, max_exit_misses=3)
    book(r, "X-A", [(0.30, 10)], [(0.55, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])
    _filled_a(r, b)
    sent = []

    async def ioc(ticker, side, limit, size, reason, view):
        sent.append(ticker)
        return None, None                            # the level was gone
    r._ioc = ioc
    for i in range(5):
        r.book_seq["X-B"] = i + 1                    # a new book update each time, retried at once
        _eval(r, b)
    assert len(sent) == 3 and r.exit_misses[b.basket_id] == 3
    q = r.quotes[(b.basket_id, 1)]
    assert (q.kind, q.side) == ("complete", "yes")   # resting the exit instead
    assert any("missed 3 times" in a["msg"] for a in r.alerts)


def test_audit_ignores_a_determined_market_kalshi_hasnt_paid_out(arbs):
    r, b = arbs
    for t in b.tickers:
        r.meta[t].status = "determined"             # we booked the payout; Kalshi still shows the contracts

    class Client:
        def positions_all(self):
            return {"X-A": 1.0, "X-B": 1.0}

    async def call(fn, *a):
        return fn(*a)
    r.gw.any_client, r.gw.call, r.load_meta = Client, call, lambda: None
    assert asyncio.run(r.audit()) and r.audit_strikes == 0


def test_broken_basket_with_no_plan_completes_once_finishing_doesnt_lose(arbs):
    r, b = arbs
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_ticks=50)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])       # B's YES ask 0.50: 0.40 + 0.50 + fees < 1
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.40, time.time())   # broke before a restart
    _eval(r, b)
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 5 and not pos.unhedged(b.tickers)


def test_paused_market_is_held_with_one_alert(arbs):
    r, b = arbs
    _cheap_book(r)
    import requests
    exc = requests.HTTPError('409 for x: {"error":{"code":"trading_is_paused"}}')
    for _ in range(3):
        r._order_err(b.basket_id, "quote X-A", exc, "X-A")
    assert r.order_errors == 0 and sum("paused" in a["msg"] for a in r.alerts) == 1
    _eval(r, b)                                    # would take, but X-A is held
    assert b.basket_id not in r.shadow_ledger.baskets and not r.quotes
    r.paused_until["X-A"] = 0.0
    _eval(r, b)
    assert b.basket_id in r.shadow_ledger.baskets


def test_cancel_on_an_inactive_market_retires_the_quote_without_an_error(arbs):
    r, b = arbs
    import requests
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=5, cid="c", edge=0.0,
                max_price=0.40, pending=False, exchange_id="oid")
    r.quotes[q.key] = q
    r.live = True

    async def cancel(ticker, oid, reason):
        return None, requests.HTTPError('409 for x: {"error":{"code":"market_inactive"}}'), True
    r.gw.cancel = cancel
    asyncio.run(r.cancel_quote(q, "no_edge"))
    assert q.key not in r.quotes and r.order_errors == 0 and r.blocked() is None
    r.live = False


def test_exchange_pause_blocks_and_silences_market_pauses(arbs):
    r, b = arbs
    import requests
    r.exchange_paused = True
    assert r.blocked() == "Kalshi trading paused"
    r._order_err(b.basket_id, "cancel X-A", requests.HTTPError('409 for x: {"error":{"code":"trading_is_paused"}}'),
                 "X-A")
    assert not r.alerts and r.order_errors == 0


def test_flip_on_a_live_event_needs_min_edge(arbs):
    r, b = arbs
    f = _with_flip(r, b)
    f = dataclasses.replace(f, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.baskets[f.basket_id] = f
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_exit_edge=0.01, min_edge=0.05, enable_quoting=False)
    for t in b.tickers:
        r.shadow_ledger.record(b.basket_id, b.tickers, t, "yes", 3, 1.35, time.time())
    book(r, "X-A", [(0.53, 10)], [(0.40, 10)])     # a 2.5c edge: min_exit_edge clears, min_edge doesn't
    book(r, "X-B", [(0.53, 10)], [(0.40, 10)])
    _eval(r, f)
    assert f.basket_id not in r.shadow_ledger.baskets


def test_write_cap_rises_while_priority_series_is_out_of_session(arbs, tmp_path):
    r, b = arbs
    r.cfg.runtime = dataclasses.replace(r.cfg.runtime, max_write_tokens_per_s=150.0,
                                        max_write_tokens_per_s_solo=250.0, write_priority_series=["KXINXU"])
    now = pd.Timestamp.now(tz="UTC")
    d = tmp_path / "KXINXU" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True)

    def summary(start, close):
        (d / "summary.json").write_text(json.dumps({
            "live_trading": True, "last_update": now.isoformat(),
            "session_started": (now + start).isoformat(), "close_time": (now + close).isoformat()}))
    summary(pd.Timedelta(hours=-1), pd.Timedelta(hours=1))
    assert r.series_trading("KXINXU")
    summary(pd.Timedelta(hours=-8), pd.Timedelta(hours=-1))     # session over
    assert not r.series_trading("KXINXU") and not r.series_trading("KXINXDUD")
    assert r.write_cap() == 250.0
    r.priority_trading = ["KXINXU"]
    assert r.write_cap() == 150.0


def test_queue_wait_is_depth_ahead_over_the_fill_rate(arbs):
    r, b = arbs
    r.cfg.watch = dataclasses.replace(r.cfg.watch, queue_window_s=3600.0)
    book(r, "X-A", [(0.30, 100), (0.29, 50)], [(0.60, 10)])
    r.flow.add("X-A", "yes", 360.0, time.time())          # 0.1 contracts/s sold into YES bids
    assert r.queue_wait("X-A", "yes", 0.31, 5) == pytest.approx(50.0)      # alone above the best bid
    assert r.queue_wait("X-A", "yes", 0.30, 5) == pytest.approx(1050.0)    # behind the 100 at 0.30
    assert r.queue_wait("X-A", "no", 0.30, 5) == float("inf")              # nothing sold into NO bids


def test_quote_score_counts_only_legs_that_trade(arbs):
    r, b = arbs
    r.cfg.watch = dataclasses.replace(r.cfg.watch, queue_window_s=3600.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    assert r.rank_rows[b.basket_id]["quote_usd_h"] == 0         # nothing sold into either side lately
    r.flow.add("X-A", "yes", 3600.0, time.time())
    _eval(r, b)
    assert r.rank_rows[b.basket_id]["quote_usd_h"] == 0         # priced once per rank interval
    r.rank_at.clear()
    _eval(r, b)
    row = r.rank_rows[b.basket_id]
    assert row["quote_usd_h"] > 0 and row["wait_s"] is not None


def test_print_through_an_unquoted_basket_is_logged_as_missed(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    r.hot, r.hot_ready = [], True
    px = r.quote_px[b.basket_id][0][0]
    from backtest.core import Trade
    r._on_trade("X-A", Trade(ticker="X-A", time=pd.Timestamp.now(tz="UTC"), price=px, size=3.0, taker_side="no"))
    m = r.summary()["missed"]
    assert m and m[0]["basket_id"] == b.basket_id and m[0]["contracts"] == 3.0


def test_exit_edge_scales_with_days_to_settlement(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_exit_edge=0.01, exit_ref_days=30.0)
    now = pd.Timestamp("2026-10-29", tz="UTC")
    at = lambda days: dataclasses.replace(b, settles=now + pd.Timedelta(days=days))
    assert r.exit_need(at(10), 0.03, now) == pytest.approx(0.03)        # within ref_days: the full edge
    assert r.exit_need(at(90), 0.03, now) == pytest.approx(0.01)        # a third of it
    assert r.exit_need(at(60), 0.03, now) == pytest.approx(0.015)
    assert r.exit_need(at(900), 0.03, now) == pytest.approx(0.01)       # floored
    assert r.exit_need(at(90), 0.005, now) == pytest.approx(0.005)      # never above the arb edge
    live = dataclasses.replace(at(900), live_from=now - pd.Timedelta(hours=1))
    assert r.exit_need(live, 0.03, now) == pytest.approx(0.03)


def test_hot_list_fills_the_write_budget_keeping_incumbents(arbs):
    r, b = arbs
    cost = {"a": 40.0, "x": 40.0, "y": 40.0, "b": 40.0, "q": 0.0}
    r.hot = []
    assert r.pick_hot(["a", "x", "y", "b", "q"], 100.0, cost) == ["a", "x", "q"]     # q is free
    r.hot = ["a", "b"]                   # incumbents keep their places; x and y no longer fit
    assert r.pick_hot(["a", "x", "y", "b", "q"], 100.0, cost) == ["a", "b", "q"]


def test_a_shards_write_rate_is_shared_with_the_other_processes_planning_on_it(arbs):
    import types
    r, b = arbs
    now = time.time()

    def spend(p, t, tps, plans):
        os.makedirs(os.path.join(R.LIVE_ROOT, p), exist_ok=True)
        json.dump({"t": t, "tps": tps, "plans": plans}, open(os.path.join(R.LIVE_ROOT, p, R.WRITE_SPEND), "w"))
    spend("ARBS0B", now, {"0": 150.0}, [0])
    spend("ARBS0C", now, {"0": 50.0}, [0])
    spend("ARBSPM", now, {"0": 0.0, "100": 40.0}, [100])       # rests no Kalshi quotes: not a sharer
    spend("ARBS3", now - 60, {"3": 200.0}, [3])                 # stale: not counted
    r.write_budget = lambda: 300.0
    c = types.SimpleNamespace(budget=types.SimpleNamespace(tokens_per_s=300.0))
    T = R.WRITE_TARGET * 300
    assert r.spend_target(0, 25.0, c) == pytest.approx(25 + (T - 225) / 3)     # its spend + a third of what's left
    assert r.spend_target(3, 25.0, c) == pytest.approx(T)                       # alone on shard 3: the whole target
    spend("ARBS0B", now, {"0": 250.0}, [0])
    r._sib_at = 0.0
    assert r.spend_target(0, 25.0, c) == pytest.approx(R.PLAN_SPEND_FLOOR * T)  # oversubscribed: down to the floor
    r.spend_tps, r.hot_cost_by_shard = {0: 25.0}, {"0": 100.0, "None": 3.0}
    r.gw.clients = {}
    r.update_plan_scale(now)
    assert json.load(open(os.path.join(r.root, R.WRITE_SPEND))) == {"t": now, "tps": {"0": 25.0}, "plans": [0]}


def test_plan_budget_follows_real_write_spend(arbs):
    r, b = arbs
    import types
    c = types.SimpleNamespace(spent_total=0.0)
    r.gw.clients = {0: c, 100: object()}                 # a client without spent_total is left alone
    r.hot_cost_by_shard = {"0": 300.0}
    t0 = 1000.0
    r.update_plan_scale(t0)
    assert r.plan_scale == {}                            # held at 1 until the hot list has filled
    r.hot_ready = True
    for i in range(0, 1801, 5):                          # planned 300 tokens/s, really spent 100
        c.spent_total = 100.0 * i
        r.update_plan_scale(t0 + i)
    assert abs(r.spend_tps[0] - 100.0) < 1e-6
    assert abs(r.plan_scale[0] - R.WRITE_TARGET * 300 / 100) < 0.01 and 100 not in r.plan_scale
    for i in range(1805, 3601, 5):                       # now spending more than planned: scale floors at 0.5
        c.spent_total += 5 * 1000.0
        r.update_plan_scale(t0 + i)
    assert abs(r.plan_scale[0] - 0.5) < 0.01


def test_write_cost_counts_top_of_book_changes(arbs, monkeypatch):
    r, b = arbs
    monkeypatch.setattr(R, "COST_FROM_SPEND", False)        # the book-move estimate itself
    now = time.time()
    quiet = r.write_cost(b, now)
    for i in range(60):
        r.top_moves.setdefault("X-A", R._Rate()).add(1.0, now)
    assert r.write_cost(b, now) > quiet
    r.charge(b.basket_id, 10_000.0)
    assert r.write_cost(b, now) == pytest.approx(10_000.0 / R.WRITE_COST_TAU_S, rel=0.01)


def test_quote_holds_a_small_move_up_but_follows_down(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, requote_hold_up=0.01)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.price == pytest.approx(0.31)
    book(r, "X-A", [(0.31, 10)], [(0.35, 10)])          # joined: wants 0.32, holds
    _eval(r, b)
    assert q.price == pytest.approx(0.31)
    book(r, "X-A", [(0.32, 10)], [(0.35, 10)])          # outbid: wants 0.33, 2c up
    _eval(r, b)
    assert q.price == pytest.approx(0.33)
    book(r, "X-A", [(0.31, 10)], [(0.35, 10)])          # bid falls 1c: follows down
    _eval(r, b)
    assert q.price == pytest.approx(0.32)


def test_a_pregame_quote_moves_by_two_ticks_after_its_cooldown(arbs, monkeypatch):
    r, b = arbs
    _no_deep(r)
    monkeypatch.setattr(R, "PRE_REQUOTE_MIN_TICKS", 2)
    monkeypatch.setattr(R, "PRE_REQUOTE_COOLDOWN_S", 10.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, requote_hold_up=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.price == pytest.approx(0.31)
    q.placed_at = q.moved_at = time.time() - 60.0
    book(r, "X-A", [(0.31, 10)], [(0.35, 10)])          # wants 0.32: one tick, stays
    _eval(r, b)
    assert q.price == pytest.approx(0.31)
    book(r, "X-A", [(0.32, 10)], [(0.35, 10)])          # wants 0.33: two ticks, cooled: moves
    _eval(r, b)
    assert q.price == pytest.approx(0.33)
    book(r, "X-A", [(0.34, 10)], [(0.35, 10)])          # wants 0.35 two ticks up, but it just moved
    _eval(r, b)
    assert q.price == pytest.approx(0.33)


def test_a_pregame_quote_without_a_plan_rests_out_its_grace_while_still_profitable(arbs, monkeypatch):
    r, b = arbs
    _no_deep(r)
    monkeypatch.setattr(R, "PRE_NO_EDGE_GRACE_S", 10.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, min_exit_edge=0.01, depth_age_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])          # B's YES ask: 10 at 0.55
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.price == pytest.approx(0.31) and q.remaining == 5
    real_best = r.best_plan
    r.best_plan = lambda *a, **k: (None, 0.0)            # its plan gone; a fill at 0.31 still clears 1c
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.unpriced_at is not None and not q.pending
    q.unpriced_at -= 10.0                                 # grace over: goes
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is not q or q.pending
    r.quotes.clear()
    r.best_plan = real_best
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    r.best_plan = lambda *a, **k: (None, 0.0)
    book(r, "X-B", [(0.30, 10)], [(0.30, 10)])          # B's ask 0.70: a fill at 0.31 would lose: goes at once
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is not q or q.pending


def test_a_small_pregame_quote_one_tick_past_its_limit_stays(arbs, monkeypatch):
    r, b = arbs
    _no_deep(r)
    monkeypatch.setattr(R, "PRE_LIMIT_SLACK_TICKS", 1)
    monkeypatch.setattr(R, "PRE_REQUOTE_MIN_TICKS", 2)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, depth_age_s=0.0)
    book(r, "X-A", [(0.50, 10)], [(0.30, 10)])          # A's best bid 0.50: the quote rests at its limit
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])          # B's YES ask 0.55
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    start = q.price
    assert start < 0.50
    book(r, "X-B", [(0.40, 10)], [(0.44, 10)])          # B's ask 0.56: the limit one tick under the quote
    _eval(r, b)
    assert q.price == pytest.approx(start)
    book(r, "X-B", [(0.40, 10)], [(0.43, 10)])          # 0.57: two ticks past: moves
    _eval(r, b)
    assert q.price < start - 0.015
    monkeypatch.setattr(R, "MODEL_PRE_NARROW_SIZE", 1.0)  # a quote bigger than the slack's size moves at one tick
    book(r, "X-B", [(0.40, 10)], [(0.42, 10)])
    p1 = q.price
    _eval(r, b)
    assert q.price < p1 - 0.005


def test_move_held_by_cooldown_is_rechecked_when_it_ends(arbs):
    # A bid that leaves inside the cooldown, then a quiet book: the basket is
    # evaluated again once the cooldown ends, not at the next book update.
    r, b = arbs
    _no_deep(r)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, requote_hold_up=0.01)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    book(r, "X-A", [(0.32, 10)], [(0.35, 10)])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.price == pytest.approx(0.33)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.2)
    q.moved_at = time.time()
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])          # the bid we followed leaves

    async def go():
        r.dirty.clear()
        r.evaluate(b)
        r.evaluate(b)                                   # one wake-up, not one per evaluation
        await drain(r)
        assert q.price == pytest.approx(0.33) and b.basket_id not in r.dirty
        await asyncio.sleep(0.3)
        assert b.basket_id in r.dirty
        r.dirty.clear()
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    assert q.price == pytest.approx(0.31)


def test_write_cost_skips_top_changes_quotes_hold(arbs):
    r, b = arbs
    hold = r._moves_quotes
    assert not hold((0.30, 0.40), (0.31, 0.40))         # bid up 1c
    assert not hold((0.30, 0.40), (0.30, 0.39))         # ask down 1c
    assert hold((0.30, 0.40), (0.29, 0.40))             # bid down
    assert hold((0.30, 0.40), (0.30, 0.41))             # ask up
    assert hold((0.30, 0.40), (0.32, 0.40))             # bid up 2c
    assert hold(None, (0.30, 0.40)) and hold((None, 0.40), (0.30, 0.40))


def test_bad_exit_flags_markets_live_until_a_clean_fill(arbs):
    r, b = arbs
    now = pd.Timestamp.now(tz="UTC")
    assert not r.live_now(b, now)
    r.flag_live(b, "a fast move unwind")
    assert r.live_now(b, now) and set(r.flagged) == {"X-A", "X-B"}
    other = Basket(basket_id="t:o", relationship_id="t", type="complete_set", cluster="t",
                   legs=(Leg("X-A", "no"), Leg("X-C", "no")), payout=1.0, quote_until=None, resume_quoting=None)
    assert r.live_now(other, now)                       # shares X-A
    r.unflag_live(b)
    assert not r.live_now(b, now) and not r.flagged


def test_an_unwind_that_does_not_lose_leaves_the_markets_pre_game(arbs):
    r, b = arbs
    # 5 YES A bought at 0.20 with others bidding 0.45: netting out at A's 0.55 NO ask makes money,
    # and beats completing at B's 0.80 YES ask.
    book(r, "X-A", [(0.45, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.20, 10)])
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.20, time.time())
    _eval(r, b)
    assert b.basket_id not in r.shadow_ledger.baskets and r.shadow_ledger.settled[-1]["pnl"] > 0
    assert not r.flagged


def test_an_unwind_on_a_fast_move_flags_the_markets_live(arbs):
    r, b = arbs
    _broken(r, b)
    _eval(r, b)
    book(r, "X-A", [(0.25, 10)], [(0.35, 10)])      # the unwind runs 0.25 away
    _eval(r, b)
    assert b.basket_id not in r.shadow_ledger.baskets and set(r.flagged) == {"X-A", "X-B"}


def test_live_edge_add_raises_the_quote_edge(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    calm = r.quotes[(b.basket_id, 0)].max_price
    r.quotes.clear()
    r.cfg.trading = dataclasses.replace(r.cfg.trading, live_edge_add=0.05, quote_live_events=True)
    r.flag_live(b, "test")
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].max_price <= calm - 0.05 + 1e-9     # the limit drops by the add


def test_no_missed_fills_counted_on_live_baskets(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    r.hot, r.hot_ready = [], True
    r.flag_live(b, "test")
    px = r.quote_px[b.basket_id][0][0]
    from backtest.core import Trade
    r._on_trade("X-A", Trade(ticker="X-A", time=pd.Timestamp.now(tz="UTC"), price=px, size=3.0, taker_side="no"))
    assert not r.summary()["missed"]


def test_resting_quote_grows_when_the_room_does(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 1)])       # B's YES ask 0.55: 1 offered
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].size == 1
    book(r, "X-B", [(0.40, 10)], [(0.45, 1), (0.44, 40)])     # 40 more behind it at 0.56
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].size == 5     # quote_size in the fixture


def test_quote_grows_only_into_depth_that_has_rested(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, depth_age_s=5.0)
    now = time.time()
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 40)])      # B's YES ask 0.55, 40 deep...
    r.top_hist.on_book("X-B", now - 8, 0.40, 10, 0.55, 1)
    r.top_hist.on_book("X-B", now - 0.5, 0.40, 10, 0.55, 40)   # ...of which 39 arrived just now
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].size == 1
    r.top_hist.h["X-B"].clear()
    r.top_hist.on_book("X-B", now - 8, 0.40, 10, 0.55, 40)
    r.book_seq["X-B"] = r.book_seq.get("X-B", 0) + 1   # as the book update that writes the history does
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].size == 5     # rested 8 s: grows to quote_size
    r.top_hist.on_book("X-B", time.time(), 0.40, 10, 0.55, 2)
    r.book_seq["X-B"] = r.book_seq.get("X-B", 0) + 1
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].remaining == 5     # aged depth fell, but the book still completes 5


def _live_gap(r, b, a_yes_bids):
    """b as a live basket; A's arb quote sits at its 0.40 limit under bids at 0.45."""
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=2, live_edge_add=0.0,
                                        deep_min_gap_ticks=5, requote_cooldown_s=0.0)
    book(r, "X-A", a_yes_bids, [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])      # B's YES ask 0.55
    _eval(r, live)
    for q in list(r.quotes.values()):               # deep quotes wait for arb quotes that have rested
        q.placed_at -= R.DEEP_REQUOTE_S
    _eval(r, live)
    return live


def test_deep_quote_rests_one_tick_over_the_next_bid_behind(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    main, deep = r.quotes[(live.basket_id, 0)], r.quotes[(live.basket_id, 0, "deep")]
    assert main.price == pytest.approx(0.40) and not main.deep
    # Held to the arb quote's live_quote_size of 2.
    assert deep.price == pytest.approx(0.21) and deep.size == 2 and deep.edge > main.edge


def test_no_deep_quote_on_a_small_gap(arbs, monkeypatch):
    r, b = arbs
    monkeypatch.setattr(R, "REACH_MODE", "shadow")         # the gap rule alone: a reach quote may rest here
    live = _live_gap(r, b, [(0.45, 10), (0.37, 5)])        # 0.38 is 2 ticks behind 0.40
    assert (live.basket_id, 0) in r.quotes and (live.basket_id, 0, "deep") not in r.quotes


def _slow_gap(r, b):
    r.cfg.trading = dataclasses.replace(r.cfg.trading, deep_min_gap_ticks=5, requote_cooldown_s=0.0)
    book(r, "X-A", [(0.45, 10), (0.20, 5)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    for q in list(r.quotes.values()):
        q.placed_at -= R.DEEP_REQUOTE_S
    _eval(r, b)


def test_non_live_basket_gets_a_deep_quote_at_its_quote_size(arbs):
    r, b = arbs
    _slow_gap(r, b)
    deep = r.quotes[(b.basket_id, 0, "deep")]
    assert deep.price == pytest.approx(0.21) and deep.size == r.cfg.trading.quote_size


def test_deep_quote_sized_to_what_the_other_legs_complete(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=50)
    _slow_gap(r, b)
    # B's NO bids hold 10 at 0.45 (its YES ask 0.55): past that nothing completes.
    assert r.quotes[(b.basket_id, 0, "deep")].size == 10


def test_deep_fill_is_journaled_as_deep_and_completed(arbs, tmp_path):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    deep = r.quotes[(live.basket_id, 0, "deep")]
    r.quotes.pop((live.basket_id, 0))                      # only the deep quote is hit
    r._quote_fill(deep, 5, 5 * deep.price, None, "shadow")
    _eval(r, live)
    # Closed by the cheaper exit (here netting out into A's 0.45 bid), at a profit.
    pos = r.shadow_ledger.baskets.get(live.basket_id)
    assert pos is None or not pos.unhedged(live.tickers)
    assert sum(x["pnl"] for x in r.shadow_ledger.settled if x["basket_id"] == live.basket_id) > 0
    rows = [json.loads(l) for f in (tmp_path / "ARBS" / "journal").glob("*.jsonl") for l in open(f)]
    assert any(x["kind"] == "fill" and x.get("fill_kind") == "deep" for x in rows)


def test_basket_stays_busy_until_its_last_task_ends(arbs):
    r, b = arbs

    async def go():
        short, long_ = asyncio.Event(), asyncio.Event()
        r.spawn(short.wait(), b.basket_id)
        r.spawn(long_.wait(), b.basket_id)
        short.set()
        await asyncio.sleep(0.01)
        assert b.basket_id in r.busy           # the second task is still running
        long_.set()
        await drain(r)
        assert b.basket_id not in r.busy
    asyncio.run(go())


def test_main_and_deep_fill_together_exit_once(arbs, tmp_path):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    main, deep = r.quotes[(live.basket_id, 0)], r.quotes[(live.basket_id, 0, "deep")]
    r._quote_fill(main, main.size, main.size * main.price, None, "shadow")
    r._quote_fill(deep, 5, 5 * deep.price, None, "shadow")
    for _ in range(5):
        _eval(r, live)
    pos = r.shadow_ledger.baskets.get(live.basket_id)
    assert pos is None or not pos.unhedged(live.tickers)
    rows = [json.loads(l) for f in (tmp_path / "ARBS" / "journal").glob("*.jsonl") for l in open(f)]
    assert sum(x["kind"] in ("complete", "unwind") for x in rows) == 1


def test_basket_of_a_retired_quote_id_is_kept_for_late_fills(arbs):
    r, b = arbs
    r.real_ledger.quote_ids["oid-1"] = {"basket_id": b.basket_id, "ticker": "X-A", "side": "yes",
                                        "retired_at": time.time()}
    r.set_baskets([])                                    # the scan dropped it
    assert b.basket_id in r.baskets


def test_deep_quote_outlives_its_arb_quote_while_it_clears_the_edge(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    deep = r.quotes[(live.basket_id, 0, "deep")]
    # A bid joins right behind the arb quote: no gap to place a deep quote in any
    # more, but the resting one at 0.21 still clears the edge, so it stays put.
    book(r, "X-A", [(0.45, 10), (0.38, 5), (0.20, 5)], [(0.35, 10)])
    _eval(r, live)
    assert r.quotes.get((live.basket_id, 0, "deep")) is deep and deep.price == pytest.approx(0.21)


def test_deep_quote_pulled_when_another_basket_quotes_the_side(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    other = R.Quote(basket_id="t:other", leg=0, ticker="X-A", side="yes", price=0.40, size=5, cid="c",
                    edge=0.05, max_price=0.40, pending=False)
    del r.quotes[(live.basket_id, 0)]
    r.quotes[("t:other", 0)] = other
    r.baskets["t:other"] = dataclasses.replace(b, basket_id="t:other")
    _eval(r, live)
    assert (live.basket_id, 0, "deep") not in r.quotes


def test_deep_quote_reprices_at_most_every_deep_requote_s(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    # Someone joins the gap: the deep quote moves up to stay first behind the arb quote, but not at once.
    book(r, "X-A", [(0.45, 10), (0.30, 5), (0.20, 5)], [(0.35, 10)])
    _eval(r, live)
    assert r.quotes[(live.basket_id, 0, "deep")].price == pytest.approx(0.21)      # placed just now: waits
    r.quotes[(live.basket_id, 0, "deep")].placed_at -= R.DEEP_REQUOTE_S
    _eval(r, live)
    assert r.quotes[(live.basket_id, 0, "deep")].price == pytest.approx(0.31)


def test_deep_quote_leaves_the_arb_price_when_the_arb_quote_comes_down(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.20, 5)])
    assert r.quotes[(live.basket_id, 0, "deep")].price == pytest.approx(0.21)
    # B's ask rises to 0.72: A's arb limit drops to 0.23, two ticks over the deep
    # quote at 0.21, which still clears its edge but is no longer behind a gap.
    book(r, "X-B", [(0.40, 10)], [(0.28, 10)])
    _eval(r, live)
    assert r.quotes[(live.basket_id, 0)].price == pytest.approx(0.23)
    assert (live.basket_id, 0, "deep") not in r.quotes


def test_no_deep_quote_behind_a_fresh_arb_quote(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=2, live_edge_add=0.0,
                                        deep_min_gap_ticks=5)
    book(r, "X-A", [(0.45, 10), (0.20, 5)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, live)
    assert (live.basket_id, 0) in r.quotes and not any(q.deep for q in r.quotes.values())


def _near(r, b, second_level, deep=False, **cfg):
    """b pre-game. A's arb quote rests at its 0.40 limit, sized to the 10 at
    B's 0.55 ask; B's next ask, 0.56, completes a quote at 0.39. No deep
    quotes unless `deep`."""
    r.cfg.trading = dataclasses.replace(r.cfg.trading, **{
        "quote_size": 5000, "near_min_size": 100,
        "max_bundles_per_basket": 10000, "max_dollars": 1e6, "requote_cooldown_s": 0.0, "depth_age_s": 0.0, **cfg})
    if not deep:
        _no_deep(r)
    book(r, "X-A", [(0.45, 100)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10), (0.44, second_level)])
    _eval(r, b)
    for q in list(r.quotes.values()):               # like a deep quote, it waits for an arb quote that has rested
        q.placed_at -= R.DEEP_REQUOTE_S
    _eval(r, b)


def _b_depth(r, b, second_level):
    book(r, "X-B", [(0.40, 10)], [(0.45, 10), (0.44, second_level)])
    _eval(r, b)


def test_near_quote_rests_a_tick_under_a_pre_game_arb_quote_with_the_size_that_tick_adds(arbs):
    r, b = arbs
    rows = []
    r.journal = rows.append
    _near(r, b, 500)
    main, near = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 0, "deep")]
    assert main.price == pytest.approx(0.40) and main.size == 10        # one level of B clears the edge at 0.40
    assert near.near and near.price == pytest.approx(0.39) and near.size == 500 and near.edge > main.edge
    assert (b.basket_id, 1, "deep") not in r.quotes                     # A has no second level for B's quote
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=310)
    _b_depth(r, b, 500)
    assert near.remaining == 300                                        # the two together stay within quote_size
    assert [x for x in rows if x["kind"] == "quote" and x.get("near")][0]["deep"] is True


def test_no_near_quote_when_off_too_small_or_live(arbs):
    r, b = arbs
    _near(r, b, 500, near_min_size=0)
    assert (b.basket_id, 0, "deep") not in r.quotes
    _near(r, b, 500, quote_size=300, near_min_size=300)     # 290 of quote_size left after the arb quote's 10
    assert (b.basket_id, 0, "deep") not in r.quotes
    _near(r, b, 500, near_min_size=1000)
    assert (b.basket_id, 0, "deep") not in r.quotes
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    _near(r, live, 500, quote_live_events=True, live_quote_size=5000, live_edge_add=0.0)
    assert (live.basket_id, 0) in r.quotes and (live.basket_id, 0, "deep") not in r.quotes


def test_near_quote_is_cut_at_once_and_grown_only_after_a_while(arbs):
    r, b = arbs
    _near(r, b, 500)
    near = r.quotes[(b.basket_id, 0, "deep")]
    _b_depth(r, b, 420)
    assert near.remaining == 500                    # within NEAR_SHRINK of it: left alone
    _b_depth(r, b, 300)
    assert near.remaining == 300                    # the exits take 300: cut now
    _b_depth(r, b, 2000)
    assert near.remaining == 300                    # more room, but it has just moved
    near.moved_at -= R.DEEP_REQUOTE_S
    near.placed_at -= R.DEEP_REQUOTE_S
    _b_depth(r, b, 2000)
    assert near.remaining == 2000


def test_resting_near_quote_stays_down_to_half_its_minimum_then_goes(arbs):
    r, b = arbs
    _near(r, b, 500, near_min_size=400)
    _b_depth(r, b, 250)
    assert r.quotes[(b.basket_id, 0, "deep")].remaining == 250
    _b_depth(r, b, 150)
    assert (b.basket_id, 0, "deep") not in r.quotes
    r._near_at.clear()
    _b_depth(r, b, 250)                             # not enough to place a new one
    assert (b.basket_id, 0, "deep") not in r.quotes


def test_a_leg_without_a_near_quote_is_checked_for_one_only_every_near_eval_s(arbs):
    r, b = arbs
    _near(r, b, 50)                                 # a tick lower adds too little
    assert (b.basket_id, 0, "deep") not in r.quotes
    _b_depth(r, b, 500)                             # now it adds enough, but the leg was just checked
    assert (b.basket_id, 0, "deep") not in r.quotes
    r._near_at[(b.basket_id, 0)] -= R.NEAR_EVAL_S
    _b_depth(r, b, 500)
    assert r.quotes[(b.basket_id, 0, "deep")].near


def test_near_quote_waits_for_spare_write_tokens_and_is_not_charged_to_its_basket(arbs):
    from infra.kalshi.orders import RestingAck
    r, b = arbs
    r.live = True
    ids = iter(range(1000))

    async def rest(ticker, side, price, size, cid, group=None):
        return RestingAck(exchange_order_id=f"o{next(ids)}", fill=None), None, True
    r.gw.rest = rest
    spare = [r.cfg.runtime.write_reserve_tokens]    # what the bucket would have left after the write
    r.gw.can_afford = lambda ticker, kind, reserve: reserve <= spare[0]
    try:
        _near(r, b, 500)
        assert (b.basket_id, 0) in r.quotes and (b.basket_id, 0, "deep") not in r.quotes    # arb quotes go, it waits
        charged = r.spend[b.basket_id].v
        spare[0] = R.NEAR_RESERVE * r.write_budget() * r.gw.burst_s()
        r._near_at.clear()
        _eval(r, b)
        assert r.quotes[(b.basket_id, 0, "deep")].near and r.spend[b.basket_id].v == charged
    finally:
        r.live = False


def test_near_quote_is_sized_through_the_basket_the_arb_quote_is_priced_through(arbs):
    r, b = arbs
    c = _second_basket(r, b, (Leg("X-A", "yes"), Leg("X-C", "yes")))
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=5000, near_min_size=100,
                                        max_bundles_per_basket=10000, max_dollars=1e6,
                                        requote_cooldown_s=0.0, depth_age_s=0.0)
    _no_deep(r)
    book(r, "X-A", [(0.43, 100)], [(0.55, 10)])                 # A ask 0.45
    book(r, "X-B", [(0.30, 10)], [(0.35, 10)])                  # B ask 0.65: b alone clears at 0.30
    book(r, "X-C", [(0.40, 10)], [(0.45, 10), (0.44, 500)])     # C asks 0.55 x 10, 0.56 x 500
    _eval(r, b)
    for q in list(r.quotes.values()):
        q.placed_at -= R.DEEP_REQUOTE_S
    _eval(r, b)
    main, near = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 0, "deep")]
    assert r.via[(b.basket_id, 0)] == c.basket_id and main.price == pytest.approx(0.40) and main.size == 10
    assert near.near and near.price == pytest.approx(0.39) and near.size == 500


def test_resting_near_quote_stays_where_it_is_when_the_arb_quote_steps_up(arbs):
    r, b = arbs
    rows = []
    r.journal = rows.append
    _near(r, b, 500)
    main, near = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 0, "deep")]
    book(r, "X-B", [(0.40, 10)], [(0.47, 10), (0.45, 10), (0.44, 500)])     # B's ask 0.53 x 10: A's limit 0.42
    _eval(r, b)
    _eval(r, b)
    assert main.price == pytest.approx(0.42)                    # and 0.41 adds nothing to its 10
    assert r.quotes[(b.basket_id, 0, "deep")] is near and near.price == pytest.approx(0.39) and near.remaining == 500
    assert not [x for x in rows if x["kind"] == "cancel"]
    book(r, "X-B", [(0.40, 10)], [(0.47, 10), (0.45, 10), (0.44, 300)])
    _eval(r, b)
    assert near.price == pytest.approx(0.39) and near.remaining == 310      # still cut at once, where it rests
    book(r, "X-B", [(0.40, 10)], [(0.49, 10), (0.45, 10), (0.44, 500)])     # A's limit 0.44: 5c over it
    _eval(r, b)
    _eval(r, b)
    assert main.price == pytest.approx(0.44) and (b.basket_id, 0, "deep") not in r.quotes


def test_near_quote_waits_for_an_arb_quote_that_has_stopped_moving(arbs):
    r, b = arbs
    _near(r, b, 50)                                 # a tick lower adds too little
    main = r.quotes[(b.basket_id, 0)]
    main.moved_at = time.time()
    r._near_at.clear()
    _b_depth(r, b, 500)                             # now it adds enough, but the arb quote has just moved
    assert (b.basket_id, 0, "deep") not in r.quotes
    main.moved_at -= R.DEEP_REQUOTE_S
    _b_depth(r, b, 500)
    near = r.quotes[(b.basket_id, 0, "deep")]
    assert near.near and near.price == pytest.approx(0.39)
    near.placed_at -= R.DEEP_REQUOTE_S
    book(r, "X-B", [(0.40, 10)], [(0.47, 10), (0.46, 500)])     # A's limit 0.42, and 0.41 adds 500
    _eval(r, b)
    _eval(r, b)
    assert main.price == pytest.approx(0.42) and near.price == pytest.approx(0.39)
    main.moved_at -= R.DEEP_REQUOTE_S
    _eval(r, b)
    assert near.price == pytest.approx(0.41)


def test_near_quote_with_no_near_plan_goes_at_once_even_where_a_deep_quote_could_rest(arbs):
    r, b = arbs
    _near(r, b, 500, deep=True)
    assert r.quotes[(b.basket_id, 0, "deep")].near
    _b_depth(r, b, 10)                              # the exits no longer take it
    assert not any(q.near for q in r.quotes.values())
    r._near_at.clear()
    _near(r, b, 500, deep=True, quote_live_events=True, live_quote_size=5000, live_edge_add=0.0)
    assert r.quotes[(b.basket_id, 0, "deep")].near
    r.flag_live(b, "test")
    _eval(r, b)
    assert not any(q.near for q in r.quotes.values())


def test_deep_gap_is_in_cents_not_ticks(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, deep_min_gap_ticks=5)
    assert not r.deep_gap(0.002, 0.010)        # 8 ticks of a 0.1c tail, under a cent
    assert not r.deep_gap(0.31, 0.35)
    assert r.deep_gap(0.30, 0.35)


def test_eval_pass_runs_a_basket_with_a_fill_first_and_yields_after_its_slice(arbs, monkeypatch):
    r, b = arbs
    others = [dataclasses.replace(b, basket_id=f"t:o{i}") for i in range(30)]
    r.set_baskets([b] + others)
    order = []
    monkeypatch.setattr(r, "evaluate", lambda x: order.append(x.basket_id))
    r.exit_hist[b.basket_id] = collections.deque([(time.time(), 0.0)])
    monkeypatch.setattr(R, "EVAL_SLICE_S", -1.0)          # every pass yields after one basket

    async def go():
        r.dirty |= {o.basket_id for o in others}
        r.mark(b.basket_id)                                  # has a fill to exit: urgent
        task = asyncio.create_task(r.eval_loop())
        for _ in range(4):
            await asyncio.sleep(0)
        seen = list(order)
        await asyncio.sleep(0.05)
        task.cancel()
        return seen
    first = asyncio.run(go())
    assert first[0] == b.basket_id
    assert len(first) < 31                                   # passes yield between baskets
    assert sorted(order) == sorted([b.basket_id] + [o.basket_id for o in others])   # and still drain


def test_leg_view_cache_in_a_pass_follows_our_own_quotes(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    r._in_pass = True
    v1 = r.leg_view("X-A", "yes")
    assert r.leg_view("X-A", "yes") is v1
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.30, size=4, cid="c",
                edge=0.0, max_price=0.30, pending=False)
    r.quotes[q.key] = q                                      # our own bid comes out of the book
    v2 = r.leg_view("X-A", "yes")
    assert v2 is not v1 and v2.bid == pytest.approx(0.30)
    r.hold([("X-A", "yes", 3)])                              # an IOC in flight eats the ask
    assert r.leg_view("X-A", "yes").asks[0][1] == pytest.approx(7)
    r._in_pass = False


def test_leg_view_cache_follows_book_updates_across_passes(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    r._in_pass = True
    v1 = r.leg_view("X-A", "yes")
    book(r, "X-A", [(0.30, 10)], [(0.40, 10)])               # a delta: the feed calls _on_quote
    r.book_seq["X-A"] = r.book_seq.get("X-A", 0) + 1
    v2 = r.leg_view("X-A", "yes")
    assert v2 is not v1 and v2.asks[0][0] == pytest.approx(0.60)
    r._in_pass = False


def _live_groups(r, b, log):
    """b as a live basket, real trading, order groups on; a gateway that logs calls."""
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.live = True
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=2,
                                        live_edge_add=0.0, live_order_groups=True)
    _no_deep(r)
    ids = iter(range(1000))

    async def rest(ticker, side, price, size, cid, group=None):
        log.append(("rest", ticker, group))
        from infra.kalshi.orders import RestingAck
        return RestingAck(exchange_order_id=f"o{next(ids)}", fill=None), None, True

    async def group_call(ticker, method, *args):
        log.append((method,) + args)
        return ("g1" if method == "create_order_group" else None), None

    async def cancel(ticker, oid, reason):
        log.append(("cancel_start", oid))
        await asyncio.sleep(0.02)
        log.append(("cancel_end", oid))
        return True, None, True

    async def ioc(ticker, side, limit, size, *, force, reason):
        log.append(("ioc", ticker))
        return None, None, True
    r.gw.rest, r.gw.group_call, r.gw.cancel, r.gw.ioc = rest, group_call, cancel, ioc
    r.gw.can_afford = lambda *a, **k: True
    return live


def test_live_quotes_join_their_events_order_group(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, live)                                           # first quotes open the group, go ungrouped
    assert ("create_order_group", R.ORDER_GROUP_LIMIT) in log and r.groups["X"]["id"] == "g1"
    r.quotes.clear()
    _eval(r, live)
    assert "g1" in {e[2] for e in log if e[0] == "rest"}
    assert all(q.group == "g1" for q in r.quotes.values())
    r.live = False


def test_two_grouped_contracts_trip_the_group_which_is_pulled_and_reset(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    r.groups["X"] = {"id": "g1", "ticker": "X-A", "tripped_at": None, "resetting": False,
                     "fills": collections.deque(), "used": time.time()}
    r._group_ev["g1"] = "X"
    qa = R.Quote(basket_id=live.basket_id, leg=0, ticker="X-A", side="yes", price=0.3, size=5, cid="a",
                 edge=0.05, max_price=0.3, pending=False, exchange_id="oa", group="g1")
    qb = R.Quote(basket_id=live.basket_id, leg=1, ticker="X-B", side="yes", price=0.4, size=5, cid="b",
                 edge=0.05, max_price=0.4, pending=False, exchange_id="ob", group="g1")
    r.quotes[qa.key], r.quotes[qb.key] = qa, qb

    async def go():
        r._quote_fill(qa, 1, 0.3, None, "ws")
        assert r.groups["X"]["tripped_at"] is None          # 1 contract: not past the limit
        r._quote_fill(qa, 1, 0.3, None, "ws")
        assert r.groups["X"]["tripped_at"] is not None
        await drain(r)
    asyncio.run(go())
    assert ("reset_order_group", "g1") in log and r.groups["X"]["tripped_at"] is None
    assert ("cancel_start", "ob") in log and qb.key not in r.quotes
    r.live = False


def test_live_exit_does_not_wait_for_grouped_quotes_cancels(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    _broken(r, live)
    r.real_ledger.record(live.basket_id, live.tickers, "X-A", "yes", 5, 2.0, time.time())
    q = R.Quote(basket_id=live.basket_id, leg=1, ticker="X-B", side="yes", price=0.1, size=5, cid="b",
                edge=0.05, max_price=0.1, pending=False, exchange_id="ob", group="g1")
    r.quotes[q.key] = q
    _eval(r, live)
    kinds = [e[0] for e in log]
    assert "ioc" in kinds and kinds.index("ioc") < kinds.index("cancel_end")
    r.live = False


def test_live_exit_cancels_an_ungrouped_quote_alongside_its_iocs(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    _broken(r, live)
    r.real_ledger.record(live.basket_id, live.tickers, "X-A", "yes", 5, 2.0, time.time())
    q = R.Quote(basket_id=live.basket_id, leg=1, ticker="X-B", side="yes", price=0.1, size=5, cid="b",
                edge=0.05, max_price=0.1, pending=False, exchange_id="ob")
    r.quotes[q.key] = q
    _eval(r, live)
    kinds = [e[0] for e in log]
    # Sent together: awaiting the cancel first put a round trip before the exit.
    assert "ioc" in kinds and "cancel_end" in kinds and kinds.index("ioc") < kinds.index("cancel_end")
    r.live = False


def test_live_exit_does_not_wait_out_a_grouped_create_in_flight(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    _broken(r, live)
    r.real_ledger.record(live.basket_id, live.tickers, "X-A", "yes", 5, 2.0, time.time())
    q = R.Quote(basket_id=live.basket_id, leg=1, ticker="X-B", side="yes", price=0.1, size=5, cid="b",
                edge=0.05, max_price=0.1, pending=True, group="g1")
    r.quotes[q.key] = q
    _eval(r, live)
    assert any(e[0] == "ioc" for e in log)
    q.group = None                                           # ungrouped: waits as before
    log.clear()
    _eval(r, live)
    assert not any(e[0] == "ioc" for e in log)
    r.live = False


def test_book_update_on_a_broken_basket_marks_it_urgent(arbs):
    r, b = arbs
    import types
    bk = types.SimpleNamespace(ticker="X-A", bid=0.3, ask=0.35, bid_depth=10, ask_depth=10)
    r._on_quote(bk)
    assert b.basket_id in r.dirty and b.basket_id not in r.urgent
    r.exit_hist[b.basket_id] = collections.deque([(time.time(), 0.0)])
    r.dirty.discard(b.basket_id)
    r._on_quote(bk)
    assert b.basket_id in r.urgent


def test_upside_only_where_an_outcome_pays_more(arbs):
    r, b = arbs
    import dataclasses
    book(r, "X-A", [(0.40, 10)], [(0.56, 10)])        # A mid 0.42
    book(r, "X-B", [(0.50, 10)], [(0.46, 10)])        # B mid 0.52
    no = dataclasses.replace(b, type="mutually_exclusive_no", legs=(Leg("X-A", "no"), Leg("X-B", "no")))
    assert r.upside(no, 10) == pytest.approx(10 * (0.58 + 0.48 - 1.0))   # both NO pay when neither wins
    assert r.upside(b, 10) == 0.0                                        # a complete set pays 1 every time
    tie = dataclasses.replace(b, payout=0.5, exact=False)                # payout lowered for a tie
    assert r.upside(tie, 10) == pytest.approx(10 * (0.94 - 0.5))
    assert r.upside(no, 0) == 0.0
    started = dataclasses.replace(no, live_from=pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=5))
    assert r.upside(started, 10) == 0.0


def test_upside_prices_a_one_sided_book_against_its_bound(arbs):
    r, b = arbs
    import dataclasses
    book(r, "X-A", [(0.98, 10)], [])                  # YES bid 0.98, no asks: mid 0.99
    book(r, "X-B", [(0.12, 10)], [(0.84, 10)])        # B mid 0.14, NO worth 0.86
    ladder = dataclasses.replace(b, type="implication", legs=(Leg("X-A", "yes"), Leg("X-B", "no")))
    assert r.upside(ladder, 10) == pytest.approx(10 * (0.99 + 0.86 - 1.0))
    book(r, "X-A", [], [])
    assert r.upside(ladder, 10) == 0.0


def test_print_recorded_only_where_a_quote_could_make_edge(arbs):
    r, b = arbs
    from backtest.core import Trade
    book(r, "X-A", [(0.40, 10)], [(0.55, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])           # YES B ask 0.50
    now = pd.Timestamp.now(tz="UTC")

    async def go():
        r._on_trade("X-A", Trade("X-A", now, 0.45, 3, "no"))      # 0.45 + 0.50 < 1: a quote there clears
        r._on_trade("X-A", Trade("X-A", now, 0.44, 2, "no"))      # same sweep: merged into that row
        r._on_trade("X-B", Trade("X-B", now, 0.62, 5, "no"))      # 0.62 + YES A ask 0.45 > 1.01
        await asyncio.sleep(1.1)
    asyncio.run(go())
    rows = r.prints.take(time.time() + 1.0)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "X-A" and row["side"] == "yes" and [p[1:] for p in row["prints"]] == [[0.45, 3], [0.44, 2]]
    assert row["baskets"] == [[b.basket_id, 1.0, False]]
    assert row["books"]["X-B"]["no"] == [[0.5, 10]] and set(row["after"]) == {"0.03", "0.1", "1.0"}
    assert row["quote"] is None and row["pull"] == {"on": False, "ratio": None}


def test_print_that_fills_our_quote_is_recorded_after_the_other_legs_moved(arbs):
    r, b = arbs
    from backtest.core import Trade
    book(r, "X-A", [(0.40, 10)], [(0.55, 10)])
    book(r, "X-B", [(0.20, 10)], [(0.30, 10)])           # YES B ask 0.70: 0.45 + 0.70 is no arb now
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.45, size=5, cid="c", edge=0.04,
                max_price=0.45, pending=False, placed_at=time.time() - 2.0)
    r.quotes[q.key] = q

    async def go():
        r._on_trade("X-A", Trade("X-A", pd.Timestamp.now(tz="UTC"), 0.45, 5, "no"))
        await asyncio.sleep(1.1)
    asyncio.run(go())
    rows = r.prints.take(time.time() + 1.0)
    assert len(rows) == 1 and rows[0]["baskets"] == [[b.basket_id, 1.0, False]]
    assert rows[0]["quote"]["price"] == 0.45


def test_print_recorded_when_an_arb_stood_just_before_it(arbs):
    r, b = arbs
    from backtest.core import Trade
    from strategies.ARBS.depth import TopHistory
    r.top_hist = TopHistory(str(r.root))
    book(r, "X-A", [(0.40, 10)], [(0.55, 10)])
    book(r, "X-B", [(0.20, 10)], [(0.30, 10)])           # YES B ask 0.70 now
    now = time.time()
    r.top_hist.on_book("X-B", now - 1.0, 0.45, 50, 0.50, 40)        # 0.50 until 10 ms ago
    r.top_hist.on_book("X-B", now - 0.01, 0.20, 50, 0.70, 40)

    async def go():
        r._on_trade("X-A", Trade("X-A", pd.Timestamp.now(tz="UTC"), 0.45, 5, "no"))
        await asyncio.sleep(1.1)
    asyncio.run(go())
    rows = r.prints.take(time.time() + 1.0)
    assert len(rows) == 1 and rows[0]["quote"] is None


def test_print_row_keeps_the_book_a_resting_quote_saw(arbs):
    r, b = arbs
    from strategies.ARBS.depth import TopHistory
    r.top_hist = TopHistory(str(r.root))
    now = time.time()
    r.top_hist.on_book("X-B", now - 1.0, 0.45, 50, 0.50, 40)
    r.top_hist.on_book("X-B", now - 0.03, 0.45, 50, 0.53, 40)        # B's ask jumps 30 ms before the print
    r.top_hist.on_book("X-A", now - 1.5, 0.40, 100, 0.55, 10)
    r.top_hist.on_book("X-A", now - 0.01, 0.40, 30, 0.55, 10)        # others pull at A's bid
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.42, size=5, cid="c", edge=0.04,
                max_price=0.42, pending=False, placed_at=now - 2.0)
    r.quotes[q.key] = q
    r.pulled[("X-A", "yes")] = time.monotonic() + 1.0
    s = r._print_state("X-A", "yes", ["X-A", "X-B"], now)
    assert [[r[0], r[1], r[3]] for r in s["pre"]["X-B"]] == [[0.02, 0.45, 0.53], [0.05, 0.45, 0.50], [0.1, 0.45, 0.50],
                                                          [0.25, 0.45, 0.50]]
    assert s["pre"]["X-B"][0] == [0.02, 0.45, 50, 0.53, 40]
    assert s["pull"] == {"on": True, "ratio": 0.3}
    assert s["quote"]["price"] == 0.42 and s["quote"]["left"] == 5 and s["quote"]["age_s"] == 2.0
    assert not s["quote"]["tripped"] and s["quote"]["gone_s"] is None


def test_print_row_keeps_a_quote_its_own_fill_just_removed(arbs):
    r, b = arbs
    now = time.time()
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.42, size=5, cid="c", edge=0.04,
                max_price=0.42, pending=False, placed_at=now - 2.0)
    r.quotes[q.key] = q
    q.filled = 5.0
    del r.quotes[q.key]                                              # the fill landed before the print
    s = r._print_state("X-A", "yes", ["X-A", "X-B"], time.time())
    assert s["quote"]["price"] == 0.42 and s["quote"]["left"] == 0 and s["quote"]["gone_s"] is not None
    assert r._print_state("X-A", "yes", ["X-A", "X-B"], time.time() + 2 * R.QUOTE_GONE_S)["quote"] is None
    c = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.42, size=5, cid="d", edge=0.04,
                max_price=0.42, pending=False, placed_at=now - 2.0)
    r.quotes[c.key] = c
    del r.quotes[c.key]                                              # cancelled unfilled: not quoting
    assert r._print_state("X-A", "yes", ["X-A", "X-B"], time.time())["quote"] is None


def test_top_history_lookup_and_pull_ratio(tmp_path):
    from strategies.ARBS.depth import TopHistory
    h = TopHistory(str(tmp_path))
    h.on_book("X", 100.0, 0.45, 80, 0.50, 20)
    h.on_book("X", 101.0, 0.46, 10, 0.50, 20)                      # a new price: not a pull
    h.on_book("X", 102.0, 0.46, 4, 0.50, 5)
    assert h.at("X", 99.0) is None and h.at("X", 100.5) == (0.45, 80, 0.50, 20)
    assert h.pull_ratio("X", "yes", 102.0, 2.0) == 0.4             # 4 of the 10 seen at 0.46
    assert h.pull_ratio("X", "no", 102.0, 2.0) == 0.25             # NO bid 0.50: 5 of 20
    assert h.pull_ratio("X", "no", 102.0, 0.5) == 1.0              # the 20 is out of the window


def test_fill_logs_top_of_book_history_without_our_own_size(arbs):
    r, b = arbs
    import types
    from strategies.ARBS.depth import AFTER_S, TopHistory
    r.top_hist = TopHistory(str(r.root))
    t0 = time.time()
    r.top_hist.on_book("X-B", t0 - 30.0, 0.40, 99, 0.6, 99)       # ages out
    r.top_hist.on_book("X-B", t0 - 3.0, 0.45, 40, 0.5, 30)
    r.top_hist.on_book("X-B", t0 - 2.995, 0.45, 35, 0.5, 30)      # depth only, within 10 ms: replaces it
    r.top_hist.on_book("X-B", t0 - 1.0, 0.45, 5, 0.5, 30)         # the others pulled
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=5, cid="c", edge=0.05,
                max_price=0.40, pending=False, exchange_id="oid")
    r.quotes[q.key] = q
    r._quote_fill(q, 2, 0.8, None, "ws", exch_t=t0 - 0.004)
    t1 = r.top_hist.pending[0][1]["t"]
    r.top_hist.on_book("X-B", t1 + 2.0, 0.45, 5, 0.6, 30)          # the ask moves after the fill
    r.top_hist.on_book("X-B", t1 + 2.005, 0.45, 9, 0.6, 30)        # depth only, within 10 ms: replaces it
    r.top_hist.on_book("X-B", t1 + AFTER_S + 1, 0.45, 9, 0.5, 30)  # past the window: not collected
    assert r.top_hist.take(t1 + 5.0) == []                         # held until the window has passed
    row = r.top_hist.take(t1 + AFTER_S)[0]
    assert row["kind"] == "fill" and row["size"] == 2 and row["ticker"] == "X-A" and row["exch_t"] == t0 - 0.004
    assert [s[2] for s in row["context"]["X-B"]] == [99, 35, 5]     # the book before the window, then changes
    assert row["context"]["X-B"][0][0] >= 30.0
    assert row["after"] == {"X-B": [[2.005, 0.45, 9, 0.6, 30]]} and not r.top_hist.watch
    bk = types.SimpleNamespace(ticker="X-A", bid=0.4, bid_depth=12, ask=0.45, ask_depth=8)
    r._note_top(bk)
    assert r.top_hist.h["X-A"][-1][2] == 12 - 3                   # our 3 left resting at 0.40 taken out


def test_a_fill_outside_any_fill_window_opens_its_own(arbs):
    r, b = arbs
    from strategies.ARBS.depth import AFTER_S, TopHistory
    r.top_hist = TopHistory(str(r.root))
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=5, cid="c", edge=0.05,
                max_price=0.40, pending=False, exchange_id="oid")
    r._log_quote_context("control", q)                              # a control row doesn't count as one
    r._book(b.basket_id, "X-A", "yes", 5, 2.0, "take")
    r._book(b.basket_id, "X-B", "yes", 5, 2.5, "take")              # inside the first take's window
    rows = r.top_hist.take(time.time() + AFTER_S)
    fills = [x for x in rows if x["kind"] == "fill"]
    assert len(rows) == 2 and len(fills) == 1
    assert fills[0]["fill_kind"] == "take" and fills[0]["ticker"] == "X-A" and fills[0]["price"] == 0.4
    r._book(b.basket_id, "X-B", "yes", 5, 2.5, "complete")          # the window has closed: a new one
    assert [p[1]["fill_kind"] for p in r.top_hist.pending] == ["complete"]


def test_top_history_keeps_price_moves_in_a_busy_book(tmp_path):
    from strategies.ARBS.depth import TopHistory
    h = TopHistory(str(tmp_path))
    t0 = 1000.0
    for i in range(10):                                           # an update every 4 ms, depth only
        h.on_book("X", t0 + 0.004 * i, 0.45, 100 - i, 0.5, 30)
    h.on_book("X", t0 + 0.041, 0.45, 50, 0.52, 30)                # the ask moves: its own sample
    rows = h.context(["X"], t0 + 0.071)["X"]
    # Depth coalesced from each sample's opening (0, 12, 24, 36 ms), not chained into one.
    assert [r[2] for r in rows] == [98, 95, 92, 91, 50]
    assert rows[-1][3] == 0.52 and abs(rows[-1][0] - 0.030) < 1e-9


def test_dollar_room_is_the_baskets_own_shard_cash(arbs):
    r, b = arbs
    t = r.cfg.trading
    t.max_dollars_auto, t.cash_reserve = True, 100.0
    r.live = True
    r.set_cash({0: 900.0, 3: 150.0}, 0.0)
    for tk in ("X-A", "X-B"):
        r.meta[tk] = dataclasses.replace(r.meta[tk], shard=3)
    assert abs(r.room_dollars(b) - (150.0 - 100.0)) < 1e-9          # shard 0's cash can't back it
    assert abs(r.room_dollars() - (1050.0 - 200.0)) < 1e-9          # each shard keeps its reserve
    r.live = False


def test_dollar_room_follows_shard_cash_when_live(arbs):
    r, b = arbs
    t = r.cfg.trading
    t.max_dollars_auto, t.cash_reserve, t.max_dollars = True, 100.0, 50.0
    assert r.room_dollars(b) == 50.0 - r.committed()               # shadow: max_dollars
    r.live = True
    assert r.room_dollars(b) == 50.0 - r.committed()               # balance not read yet
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=50, cid="c", edge=0.05,
                max_price=0.40, pending=False, exchange_id="oid")
    r.quotes[q.key] = q
    r.set_cash({0: 900.0}, 0.0)
    # Kalshi's balance is net of this quote's 50 x 0.40; the basket gets it back.
    assert abs(r.room_dollars(b) - (900.0 + 20.0 - 100.0)) < 1e-9
    assert abs(r.room_dollars() - 800.0) < 1e-9
    r.cash_at = time.time() - 10 * r.cfg.runtime.balance_poll_s * R.CASH_STALE_POLLS
    assert r.room_dollars(b) == 50.0 - r.committed()               # stale: back to max_dollars
    r.live = False


def test_cash_is_tracked_between_balance_reads(arbs, monkeypatch):
    r, b = arbs
    r.live = True
    r.set_cash({0: 900.0}, 10.0)
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=50, cid="c", edge=0.05,
                max_price=0.40, pending=False, exchange_id="oid")
    r.quotes[q.key] = q
    assert r.cash_now()[0] == 900.0                                 # a resting quote holds nothing back
    del r.quotes[q.key]
    r._cash_move("X-A", -20.1)                                      # it fills: cost with a 10c fee
    assert abs(r.cash_now()[0] - 879.9) < 1e-9
    r._cash_move("X-A", 50.0)                                       # settles or unwinds for $50
    assert abs(r.cash_now()[0] - 929.9) < 1e-9
    r.live = False
    calls = []

    async def call(fn, *a):
        calls.append(fn)
        raise RuntimeError("429")

    monkeypatch.setattr(r.gw, "call", call)
    monkeypatch.setattr(r.gw, "any_client", lambda: type("C", (), {"shard_balances": None})())
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(r.shard_balances())
    with pytest.raises(RuntimeError, match="backing off"):
        asyncio.run(r.shard_balances())                                 # no second call
    assert len(calls) == 1


_OUR_QUOTE = {"basket_id": "complete_set:X:buy_all_yes", "price": 0.41, "left": 100.0, "edge": 0.05,
              "age_s": 5.0, "pending": False, "tripped": False}


def _live_print_row(b_ask_after_no_bid, ratio=None, quote=_OUR_QUOTE, pulled=False):
    after = {"X-A": {"yes": [[0.40, 100]], "no": [[0.55, 100]]},
             "X-B": {"yes": [[0.45, 100]], "no": [[b_ask_after_no_bid, 100]]}}
    return {"t": time.time(), "ticker": "X-A", "side": "yes", "prints": [[0.0, 0.40, 50]],
            "baskets": [["complete_set:X:buy_all_yes", 1.0, True]],
            "books": {"X-A": {"yes": [[0.40, 100]], "no": [[0.55, 100]]},
                      "X-B": {"yes": [[0.45, 100]], "no": [[0.50, 100]]}},
            "pre": {"X-B": [[0.02, 0.45, 100, 0.50, 100]]},
            "after": {"0.03": after, "0.1": after, "1.0": after},
            "pull": {"on": pulled, "ratio": ratio}, "quote": quote}


def test_shadow_live_learns_an_event_and_reports_it(arbs):
    r, b = arbs
    r.series_group = {"X": "S:Soccer"}
    r.meta["X-A"].series = "X"
    view, errs = r._shadow_live([_live_print_row(0.50) for _ in range(50)])
    assert errs == 0 and set(view["events"]) == {"X"} and set(view["groups"]) == {"S:Soccer"}
    assert view["groups"]["S:Soccer"]["value"] > 0                 # quotes there would have made money
    node = r.lp.nodes[("S:Soccer", "X", "X")]
    assert node.w == pytest.approx(50, rel=0.01)


def test_shadow_live_counts_a_pulled_side_as_no_fill(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, pull_drop=0.5)
    r._shadow_live([_live_print_row(0.50, pulled=True)])
    assert sum(r.lp.nodes[()].f) == 0.0


def test_shadow_live_learns_only_from_prints_we_were_quoting(arbs):
    r, b = arbs
    tripped = {**_OUR_QUOTE, "tripped": True, "tripped_s": 5.0}
    view, errs = r._shadow_live([_live_print_row(0.50, quote=None), _live_print_row(0.50, quote=tripped)])
    assert errs == 0 and not r.lp.nodes and not view["events"]
    r._shadow_live([_live_print_row(0.50)])
    assert r.lp.nodes[()].w == pytest.approx(1.0)
    r._shadow_live([_live_print_row(0.50, quote={**tripped, "tripped_s": 0.01})])   # tripped by this print's fill
    assert r.lp.nodes[()].w == pytest.approx(2.0, rel=0.01)


def test_series_groups_read_sports_tags(monkeypatch):
    pages = [{"series": [{"ticker": "KXEPL", "category": "Sports", "tags": ["Soccer"]},
                         {"ticker": "KXHOUSE", "category": "Elections", "tags": []}], "cursor": "c"},
             {"series": [], "cursor": None}]
    monkeypatch.setattr(R, "kalshi_get", lambda path, params=None: pages.pop(0))
    assert R.series_groups() == {"KXEPL": "S:Soccer", "KXHOUSE": "Elections"}


def test_audit_takes_foreign_positions_off_kalshis(arbs):
    r, b = arbs
    path = R.os.path.join(R.LIVE_ROOT, "ARBS")
    R.os.makedirs(path, exist_ok=True)
    with open(R.os.path.join(path, R.FOREIGN_POSITIONS), "w") as fh:
        json.dump({"X-A": -100.0}, fh)
    r.foreign = R._load_foreign()
    r.real_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 30, 0.4, time.time())

    class Client:
        def positions_all(self):
            return {"X-A": -70.0}           # LIPFARM's 100 NO netted with our 30 YES

    async def call(fn, *a):
        return fn(*a)
    r.gw.venue_clients, r.gw.call = lambda: [Client()], call
    assert asyncio.run(r.audit()) and r.audit_diff is None
    assert not r.excluded(b)                                     # still traded


def test_settlement_rereads_the_balance_instead_of_adding_its_payout(arbs):
    r, b = arbs
    r.live = True
    r.set_cash({0: 900.0}, 0.0)
    r.real_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 2, 0.4, time.time())
    r.real_ledger.record(b.basket_id, b.tickers, "X-B", "yes", 2, 0.5, time.time())
    r.meta["X-A"].status, r.meta["X-A"].result = "finalized", "yes"
    r.meta["X-B"].status, r.meta["X-B"].result = "finalized", "no"
    r.settle()
    assert r.cash_now()[0] == 900.0
    assert r._cash_reread_at is not None and r._cash_reread_at > time.time()
    r.live = False


def test_move_watch_legs_together_and_jump():
    from strategies.ARBS.depth import MoveWatch
    w = MoveWatch()
    assert w.on_mid("E", "E-A", 0.0, 0.40) == [] and w.on_mid("E", "E-B", 0.0, 0.30) == []
    assert w.on_mid("E", "E-A", 12.0, 0.43) == []                  # one leg moving is a trade, not news
    assert w.on_mid("E", "E-B", 15.0, 0.27) == [("legs", 2.0)]     # a second within 10 s
    assert w.on_mid("E", "E-B", 30.0, 0.27) == []                  # both moves aged out
    w.on_mid("F", "F-A", 0.0, 0.50)
    w.on_mid("F", "F-A", 299.0, 0.56)
    assert w.on_mid("F", "F-A", 301.0, 0.56) == [("jump", 0.06)]   # 6c off where it stood 5 min ago
    assert w.on_mid("G", "G-A", 0.0, 0.5) == [] and w.on_mid("G", "G-A", 5.0, 0.6) == []   # no history 10 s back


def test_regime_shadow_journals_once_per_hold_and_ignores_our_own_top(arbs):
    r, b = arbs
    import types
    rows = []
    r.journal = rows.append
    r.cfg.runtime = dataclasses.replace(r.cfg.runtime, regime_shadow=True)
    t0 = time.time()
    for t in ("X-A", "X-B"):
        r.move_watch.on_mid("X", t, t0 - 20.0, 0.40)
    r._note_top(types.SimpleNamespace(ticker="X-A", bid=0.42, bid_depth=10, ask=0.44, ask_depth=10))
    r._note_top(types.SimpleNamespace(ticker="X-B", bid=0.42, bid_depth=10, ask=0.44, ask_depth=10))
    r._note_top(types.SimpleNamespace(ticker="X-B", bid=0.43, bid_depth=10, ask=0.45, ask_depth=10))
    fired = [x for x in rows if x["kind"] == "regime_shadow"]
    assert [(x["event"], x["signal"]) for x in fired] == [("X", "legs")] and fired[0]["live"] is False
    assert r.summary()["regime_shadow"] == ["X"]
    # A top that is only our own size doesn't count as the market moving.
    r.move_watch = R.MoveWatch()
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.30, size=5, cid="c2",
                edge=0.05, max_price=0.30, pending=False, exchange_id="o2")
    r.quotes[q.key] = q
    r._note_top(types.SimpleNamespace(ticker="X-A", bid=0.30, bid_depth=5, ask=0.44, ask_depth=10))
    assert "X-A" not in r.move_watch.h


def test_late_cutoff_stops_quoting_a_soccer_game_past_its_minute(arbs):
    r, b = arbs
    now = R._utc()
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, late_quote_cutoff_min={"S:Soccer": 75.0})
    for t in b.tickers:
        r.meta[t] = dataclasses.replace(r.meta[t], series="KXEPLGAME")
    r.series_group = {"KXEPLGAME": "S:Soccer"}
    early = dataclasses.replace(b, live_from=now - pd.Timedelta(minutes=60))
    late = dataclasses.replace(b, live_from=now - pd.Timedelta(minutes=80))
    assert r.may_quote(early, now) and not r.may_quote(late, now)
    r.series_group = {"KXEPLGAME": "S:Football"}
    assert r.may_quote(late, now)                                   # only the listed groups
    r.series_group = {}
    assert r.may_quote(late, now)                                   # groups not loaded yet: no cutoff


def test_fill_booked_into_the_best_subset_at_todays_books_even_if_unlisted(arbs):
    from backtest.book import L2Book
    r, _ = arbs
    mk = lambda bid, rel, ts, payout: Basket(basket_id=bid, relationship_id=rel, type="mutually_exclusive_no",
                                             cluster="E", legs=tuple(Leg(f"E-{t}", "no") for t in ts), payout=payout,
                                             quote_until=None, resume_quoting=None)
    # Scanned when D ranked third: A+B+D was listed, not A+B+C. D's YES bid
    # has since gone and C's come in.
    ab = mk("mecnet:E:no:A+B", "mecnet:E", "AB", 1.0)
    abd = mk("mecnet:E:no:A+B+D", "mecnet:E", "ABD", 2.0)
    full = mk("complete_set:E:buy_all_no", "complete_set:E", "ABCD", 3.0)
    for t in ("E-A", "E-B", "E-C", "E-D"):
        r.feed._books[t] = L2Book(t)
        r.meta[t] = R.Meta(ticker=t, event="E", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r.set_baskets([ab, abd, full])
    r.hot = [ab.basket_id]
    book(r, "E-A", [(0.45, 10)], [(0.20, 10)])
    book(r, "E-B", [(0.30, 10)], [(0.60, 10)])     # B's NO ask 0.70
    book(r, "E-C", [(0.40, 10)], [(0.50, 10)])     # C's NO ask 0.60
    book(r, "E-D", [], [(0.90, 10)])               # no NO ask: left out
    rows = []
    r.journal = rows.append
    _eval(r, ab)
    q = r.quotes[(ab.basket_id, 0)]
    r._quote_fill(q, 5, 5 * q.price, None, "shadow")
    fill = [x for x in rows if x["kind"] == "fill"][0]
    assert fill["basket_id"] == "mecnet:E:no:A+B+C" and fill["quoted_in"] == ab.basket_id
    assert r.n["live_subset_fills"] == 1
    ac = r.baskets["mecnet:E:no:A+B+C"]
    assert ac.payout == 2.0 and ac.relationship_id == "mecnet:E"
    _eval(r, ac)
    assert r.shadow_ledger.baskets[ac.basket_id].bundles(ac.tickers) == 5     # completed by buying B and C
    assert ac.basket_id in {b.basket_id for b in r.all_baskets} and ac.basket_id in r.by_ticker["E-C"]
    r.set_baskets([ab, abd, full])                 # a rescan keeps it while it holds the position
    assert ac.basket_id in r.baskets


def test_a_finished_near_arb_teaches_the_shadow_sizer(arbs):
    r, b = arbs
    soon = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=3)
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"), settles=soon)
    r.set_baskets([live])
    rows = []
    r.journal = rows.append
    r.cfg.trading = dataclasses.replace(r.cfg.trading, live_quote_size=100)
    g, s, e = r._lp_path("X-A")
    path = (f"{g}|<6h", s, e)
    start = r.lsizer.mult(path)
    r._book(live.basket_id, "X-A", "yes", 10, 4.0, "quote")      # quote fill, then the other leg
    r._book(live.basket_id, "X-B", "yes", 10, 7.0, "complete")   # moved: 1.10 for a $1 bundle
    got = [x for x in rows if x["kind"] == "size_shadow"]
    assert len(got) == 1 and got[0]["pnl"] == pytest.approx(-1.0) and got[0]["lots"] == 10
    assert got[0]["bucket"] == "<6h" and got[0]["live"]
    assert got[0]["entry"] == ["quote", "X-A", "yes"] and got[0]["t0"] > 0
    assert r.lsizer.mult(path) < start                          # -10c a lot cut it
    assert got[0]["size"] == max(1.0, round(100 * 0.25 * r.lsizer.mult(path)))
    assert not r.cycles


def test_a_far_arb_sizes_at_full_and_teaches_nothing(arbs):
    r, b = arbs
    far = dataclasses.replace(b, settles=pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=3))
    r.set_baskets([far])
    rows = []
    r.journal = rows.append
    r._book(far.basket_id, "X-A", "yes", 10, 4.0, "quote")
    r._book(far.basket_id, "X-B", "yes", 10, 7.0, "complete")
    got = [x for x in rows if x["kind"] == "size_shadow"]
    assert len(got) == 1 and got[0]["bucket"] == ">24h" and not got[0]["live"]
    assert got[0]["mult"] == 1.0 and got[0]["size"] == r.cfg.trading.quote_size
    assert not r.lsizer.m                                       # one loss past 24h cuts nothing


def test_an_unwound_live_arb_counts_its_loss(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    rows = []
    r.journal = rows.append
    r._book(live.basket_id, "X-A", "yes", 100, 41.0, "quote")
    r._book(live.basket_id, "X-A", "no", 100, 68.54, "unwind")   # VORSLE 11-10: -$9.54
    got = [x for x in rows if x["kind"] == "size_shadow"]
    assert len(got) == 1 and got[0]["pnl"] == pytest.approx(-9.54)


def test_shard3_process_trades_only_its_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    from strategies.ARBS import SERIES
    r = R.ArbsRunner(SERIES["ARBS3"], live_trading=False)
    assert r.cfg.scan.shards == [3] and r.root.endswith("ARBS3") and r.part_idx == 0 and r.part_group == ("ARBS3", "ARBS3B")
    assert R.ArbsRunner(SERIES["ARBS0B"], live_trading=False).part_idx == 1


def test_a_scanned_basket_on_another_processes_shard_is_not_traded(arbs):
    r, b = arbs
    assert not r.excluded(b)
    for tk in ("X-A", "X-B"):
        r.meta[tk] = dataclasses.replace(r.meta[tk], shard=3)
    assert r.excluded(b)                                   # ARBS scans shard 0; ARBS3 trades this one


def test_feed_with_no_markets_yet_waits_for_its_first_scan():
    from strategies.ARBS.feed import MultiFeed

    async def go():
        f = MultiFeed([], lambda *a: None, lambda *a: None, label="t")
        task = asyncio.create_task(f.run())
        await asyncio.sleep(0.2)
        assert not task.done()                             # waiting, not raised
        task.cancel()
    asyncio.run(go())


def test_an_excluded_event_covers_its_baskets(arbs):
    r, b = arbs
    b = dataclasses.replace(b, basket_id="mecnet:EV1:no:A+B", relationship_id="mecnet:EV1", cluster="SER")
    r.listed = {b.relationship_id}
    assert not r.excluded(b)
    r.cfg.watch = dataclasses.replace(r.cfg.watch, exclude=["EV1"])
    assert r.excluded(b)


def test_record_only_series_is_watched_but_never_traded(arbs):
    r, b = arbs
    assert not r.excluded(b)
    series = r._lp_path(b.legs[0].ticker)[1]
    r.cfg.watch = dataclasses.replace(r.cfg.watch, record_only=[series])
    assert r.excluded(b)
    r.series_group = {series: "S:Hockey"}
    r.cfg.watch = dataclasses.replace(r.cfg.watch, record_only=["S:Hockey"])
    assert r.excluded(b)


def test_a_basket_of_an_event_the_other_shard0_process_owns_is_only_finished(arbs):
    r, b = arbs
    mine, theirs, new = (dataclasses.replace(b, basket_id=f"mecnet:{e}:no:A+B", relationship_id=f"mecnet:{e}")
                         for e in ("E1", "E2", "E3"))
    r.listed = {x.relationship_id for x in (mine, theirs, new)}
    r.part_idx, r.owners = 0, {"owners": {"E1": 0, "E2": 1}}
    assert not r.excluded(mine) and r.excluded(theirs)
    assert not r.excluded(new)                             # not in the map yet: its own scan listed it
    r.part_idx = None                                      # a process that shares nothing (ARBS3)
    assert not r.excluded(theirs)


def test_a_basket_its_scan_no_longer_lists_is_only_finished(arbs):
    r, b = arbs
    assert not r.excluded(b)
    r.listed = set()                                       # e.g. the retired hand spec's, or delisted
    assert r.excluded(b)


def test_write_spend_is_kept_per_event(arbs):
    r, b = arbs
    r.charge("mecnet:EV1:no:A+B", 10.0)
    r.charge("ladder:EV1:T2=>T1", 2.0)
    r.charge("t:b", 10.0)                                  # not a scanned id: no event
    snap = r.event_spend.snapshot()
    assert list(snap.values()) == [{"EV1": 12.0}]


def test_a_watch_only_entry_can_cover_just_pre_game_or_just_in_play(arbs):
    r, b = arbs
    series = r._lp_path(b.legs[0].ticker)[1]
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    pre = dataclasses.replace(b, live_from=pd.Timestamp("2099-01-01", tz="UTC"))
    r.cfg.watch = dataclasses.replace(r.cfg.watch, record_only=[f"{series}:pre"])
    assert r.excluded(pre) and not r.excluded(live)
    r.cfg.watch = dataclasses.replace(r.cfg.watch, record_only=[f"{series}:live"])
    assert r.excluded(live) and not r.excluded(pre)
    r.flagged = {t: time.time() for t in pre.tickers}           # a fast move doesn't make pre-game in-play
    r.cfg.watch = dataclasses.replace(r.cfg.watch, record_only=[f"{series}:pre"])
    assert r.excluded(pre)


def test_shard3_takes_sizes_and_watch_list_from_config_json_and_follows_its_edits(tmp_path, monkeypatch):
    import json, os
    from strategies.ARBS import SERIES
    from strategies.ARBS.config import CONFIG_PATH, CONFIG3_PATH
    main, own = tmp_path / "config.json", tmp_path / "config_shard3.json"
    doc = json.load(open(CONFIG_PATH))
    doc["watch"]["record_only"] = ["X"]
    doc["trading"]["quote_size"] = 3000
    main.write_text(json.dumps(doc))
    own.write_text(open(CONFIG3_PATH).read())
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    r = R.ArbsRunner(dataclasses.replace(SERIES["ARBS3"], config_path=str(own)), live_trading=False)
    assert r.record_only() == ["X"] and r.cfg.trading.quote_size == 3000 and r.cfg.scan.shards == [3]
    doc["watch"]["record_only"] = ["X", "Y"]
    doc["trading"]["quote_size"] = 4000
    main.write_text(json.dumps(doc))
    os.utime(main, (r.store._mtime + 1, r.store._mtime + 1))
    r.store.poll()
    assert r.record_only() == ["X", "Y"] and r.cfg.trading.quote_size == 4000


def test_model_pick_is_quick_to_cut_risk_and_slow_to_add_it(arbs, monkeypatch):
    import types
    r, b = arbs
    table = {}

    class Stub:
        def value(self, f, x, live, n, maker_rate=0.0):          # x is the edge (quote_leg stubbed below)
            return table.get((x, n), (-1.0, -1.0))

    r.exit_model = Stub()
    monkeypatch.setattr(R.exit_cost, "live_features", lambda *a, **k: {})
    monkeypatch.setattr(R.pricing, "others_cost", lambda vs, j, n: n)          # stands in for the size below
    monkeypatch.setattr(R.pricing, "prices_at", lambda vs, j, payout, edges, o: list(edges))   # price = edge
    now_ts = pd.Timestamp.now(tz="UTC")
    vs = [types.SimpleNamespace(maker_rate=0.0)] * 2

    def pick(best):
        table.clear()
        for e in R.MODEL_EDGES:
            for n in R.MODEL_SIZES:
                table[(e, n)] = (1.0, 2.0)                        # every option worth a little
        table.update(best)
        r._model_cache.clear()
        return r.model_pick(b, 0, vs, now_ts)

    assert pick({(0.05, 50.0): (5.0, 2.0)}) == (0.05, 50.0)                 # start at the pick
    assert pick({(0.10, 50.0): (9.0, 3.0)}) == (0.05, 50.0)                 # wider worth more, no risk: stays
    assert pick({(0.10, 50.0): (9.0, 3.0), (0.05, 50.0): (1.0, 0.2)}) == (0.10, 50.0)   # 0.2c a lot: widens
    assert pick({(0.05, 50.0): (1.1, 2.0)}) == (0.10, 50.0)                 # narrower, under 1.25x: no clock
    assert r.model_state[(b.basket_id, 0, True)]["narrow"] is None
    assert pick({(0.05, 50.0): (12.0, 2.0)}) == (0.10, 50.0)                # narrower and worth it: waits
    r.model_state[(b.basket_id, 0, True)]["narrow"] -= R.MODEL_NARROW_HOLD_S
    assert pick({(0.05, 50.0): (12.0, 2.0)}) == (0.075, 50.0)               # then one step
    assert pick({(0.075, 20.0): (12.0, 2.0)}) == (0.075, 20.0)              # smaller: at once
    assert pick({(0.075, 100.0): (12.0, 2.0)}) == (0.075, 20.0)             # bigger: waits
    r.model_state[(b.basket_id, 0, True)]["grow"] -= R.MODEL_GROW_HOLD_S
    assert pick({(0.075, 100.0): (12.0, 2.0)}) == (0.075, 50.0)             # then one step
    table_none = {(e, n): (-1.0, -1.0) for e in R.MODEL_EDGES for n in R.MODEL_SIZES}
    table.clear(); table.update(table_none); r._model_cache.clear()
    assert r.model_pick(b, 0, vs, now_ts) == (None, 0.0)          # nothing worth quoting


def test_model_pick_keeps_its_holds_when_the_held_pick_cannot_be_priced(arbs, monkeypatch):
    import types
    r, b = arbs
    table, gone, rows = {}, set(), []
    r.journal = rows.append

    class Stub:
        def value(self, f, x, live, n, maker_rate=0.0):
            return table.get((x, n), (1.0, 2.0))

    r.exit_model = Stub()
    monkeypatch.setattr(R.exit_cost, "live_features", lambda *a, **k: {})
    monkeypatch.setattr(R.pricing, "others_cost", lambda vs, j, n: n)          # stands in for the size below
    monkeypatch.setattr(R.pricing, "prices_at",
                        lambda vs, j, payout, edges, n: [None if (e, n) in gone else e for e in edges])
    now_ts = pd.Timestamp.now(tz="UTC")
    vs = [types.SimpleNamespace(maker_rate=0.0)] * 2
    st = lambda: r.model_state[(b.basket_id, 0, True)]

    def pick(best, unpriceable=()):
        table.clear(); table.update(best)
        gone.clear(); gone.update(unpriceable)
        r._model_cache.clear()
        return r.model_pick(b, 0, vs, now_ts)

    wide = {(e, n) for e in (0.15, 0.20) for n in R.MODEL_SIZES}
    narrower_bigger = {(0.05, 100.0): (5.0, 2.0)}
    assert pick({(0.20, 50.0): (5.0, 2.0)}) == (0.20, 50.0)
    assert pick(narrower_bigger, wide) == (0.20, 50.0)                      # 20c can't be priced: no jump to the pick
    st()["narrow"] -= R.MODEL_NARROW_HOLD_S
    assert pick(narrower_bigger, wide) == (0.10, 50.0)                      # after the hold: the widest edge that prices
    st()["grow"] -= R.MODEL_GROW_HOLD_S
    assert pick(narrower_bigger, wide | {(0.10, 100.0)}) == (0.10, 50.0)    # 10c can't be priced bigger: stays
    assert pick(narrower_bigger, wide) == (0.10, 100.0)                     # it can: one step
    no_100 = {(e, 100.0) for e in R.MODEL_EDGES}
    assert pick({(0.05, 50.0): (5.0, 2.0)}, no_100) == (0.10, 50.0)         # size gone: shrinks, edge keeps its hold
    assert [x["why"] for x in rows] == ["start", "narrow", "grow", "shrink"]
    st()["grow"] = st()["narrow"] = time.time() - 3600                      # clocks running into a restart
    assert pick({k: (-1.0, -1.0) for k in [(e, n) for e in R.MODEL_EDGES for n in R.MODEL_SIZES]}) == (None, 0.0)
    assert pick({(0.05, 20.0): (5.0, 2.0)}) == (0.05, 20.0)
    assert st()["grow"] is None and st()["narrow"] is None


def test_live_features_match_the_training_inputs():
    from strategies.ARBS.pricing import LegView
    vs = [LegView(asks=[(0.55, 100)], bid=0.40, taker_rate=0.07, maker_rate=0.0),
          LegView(asks=[(0.50, 40), (0.51, 60)], bid=0.45, taker_rate=0.07, maker_rate=0.0)]
    f = R.exit_cost.live_features(vs, 0, 1.0, 20.0, [30.0], 80.0, True, 2.5)
    assert f["top_r_min"] == pytest.approx(2.0) and f["depth2_r"] == pytest.approx(5.0)
    assert f["aged_frac"] == pytest.approx(0.75) and f["unwind_top_r"] == pytest.approx(4.0)
    assert f["touch"] == 0.40 and f["c0"] == pytest.approx(0.50) and f["live"] == 1.0 and f["tte_h"] == 2.5
    assert R.exit_cost.row({**f}).shape == (R.exit_cost.WIDTH,)


def test_a_model_series_live_quote_takes_the_models_edge_and_size(arbs, monkeypatch):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=50, live_edge_add=0.0,
                                        requote_cooldown_s=0.0, depth_age_s=0.0, model_series=["X"])
    book(r, "X-A", [(0.20, 100)], [(0.55, 100)])
    book(r, "X-B", [(0.20, 100)], [(0.45, 100)])          # B's YES ask 0.55
    r.exit_model = object()                                 # model_pick is stubbed: only its presence counts
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, *a, **k: (0.10, 3.0))
    _eval(r, live)
    q = r.quotes[(live.basket_id, 0)]
    limit = 1.0 - R.pricing.taker_cost(0.55, 0.07) - 0.10                # the model's 10c, not config's
    assert q.size == 3 and limit - 0.01 - 1e-9 <= q.max_price <= limit + 1e-9
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, *a, **k: (None, 0.0))
    _eval(r, live)
    assert (live.basket_id, 0) not in r.quotes or r.quotes[(live.basket_id, 0)].pending   # pulled


def test_model_names_take_a_phase_and_a_bare_name_means_live(arbs):
    r, b = arbs
    r.exit_model = object()
    t = r.cfg.trading
    r.cfg.trading = dataclasses.replace(t, model_series=["X"], model_shadow=[])
    assert r.model_on(b, True) and not r.model_on(b, False)
    r.cfg.trading = dataclasses.replace(t, model_series=["*:pre"], model_shadow=[])
    assert r.model_on(b, False) and not r.model_on(b, True)
    r.cfg.trading = dataclasses.replace(t, model_series=["*"], model_shadow=["X:pre"])
    assert r.model_on(b, True) and not r.model_on(b, False) and r.model_shadow(b, False)


def test_a_pregame_model_quote_keeps_its_size_unless_narrower_than_the_config(arbs, monkeypatch):
    r, b = arbs
    r.hot = [b.basket_id]
    _no_deep(r)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=1000, max_bundles_per_basket=10000, max_dollars=1e6,
                                        requote_cooldown_s=0.0, depth_age_s=0.0, near_min_size=0,
                                        model_series=[], model_shadow=["*:pre"])
    book(r, "X-A", [(0.20, 5000)], [(0.55, 5000)])
    book(r, "X-B", [(0.20, 5000)], [(0.45, 5000)])          # B's YES ask 0.55
    r.exit_model = object()
    seen = []
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, live, cap, **k: seen.append((live, cap, k)) or
                        (0.10, cap))
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    fixed = R.edge_dollars(b.payout, r.cfg.trading.min_edge + r.cfg.trading.quote_edge_add)
    limit = 1.0 - R.pricing.taker_cost(0.55, 0.07) - fixed
    assert limit - 0.01 - 1e-9 <= q.max_price <= limit + 1e-9               # shadow: the config's edge
    assert seen[0][0] is False and seen[0][1] == 1000 and seen[0][2]["shadow"] is True
    r.quotes.clear()
    r.cfg.trading = dataclasses.replace(r.cfg.trading, model_series=["*:pre"], model_shadow=[])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    limit = 1.0 - R.pricing.taker_cost(0.55, 0.07) - 0.10
    assert limit - 0.01 - 1e-9 <= q.max_price <= limit + 1e-9 and q.size > R.MODEL_PRE_NARROW_SIZE   # wider: full size
    r.quotes.clear()
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, live, cap, **k: (fixed / 2, cap))
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].size <= R.MODEL_PRE_NARROW_SIZE      # narrower than the config: capped
    r.quotes.clear()
    monkeypatch.setattr(r, "model_pick", lambda b, j, vs, now_ts, live, cap, **k: (None, 0.0))
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    limit = 1.0 - R.pricing.taker_cost(0.55, 0.07) - fixed
    assert q.size <= R.MODEL_PRE_NARROW_SIZE and limit - 0.01 - 1e-9 <= q.max_price <= limit + 1e-9   # none: config edge, capped


def test_a_pregame_model_pick_values_each_edge_at_the_size_it_would_rest_at(arbs, monkeypatch):
    r, b = arbs
    book(r, "X-A", [(0.20, 5000)], [(0.55, 5000)])
    book(r, "X-B", [(0.20, 5000)], [(0.45, 40), (0.40, 5000)])     # B's YES asks: 40 at 0.55, then 0.60
    vs = r.views(b)
    seen = {}

    class M:
        def value(self, f, x, live, n, rate):
            seen[round(1.0 - x - 0.55, 2)] = (n, live)
            return (1.0, 5.0)
    r.exit_model = M()
    monkeypatch.setattr(R.exit_cost, "live_features", lambda *a, **k: {})
    r.model_pick(b, 0, vs, R._utc(), live=False, cap=1000.0)
    # Valued at the 40 B's top completes, not at quote_size (1,000) or the 500-lot cap.
    assert {n for n, _ in seen.values()} == {40.0} and all(live is False for _, live in seen.values())


def _second_level(r, b, behind, **cfg):
    """b pre-game. A's arb quote rests at its 0.40 limit. B's YES asks: 100 at
    0.55, `behind` at 0.56 (the last level that completes a fill at 0.40
    without a loss), 500 at 0.70. A's only ask is 10 at 0.65."""
    r.cfg.trading = dataclasses.replace(r.cfg.trading, **{
        "quote_size": 1000, "near_min_size": 0, "max_bundles_per_basket": 10000,
        "max_dollars": 1e6, "requote_cooldown_s": 0.0, "depth_age_s": 0.0, **cfg})
    _no_deep(r)
    book(r, "X-A", [(0.45, 100)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 100), (0.44, behind), (0.30, 500)])
    _eval(r, b)


def test_two_leg_quote_is_held_to_what_the_other_leg_completes_without_its_best_level(arbs):
    r, b = arbs
    _second_level(r, b, 30, second_level_size=True)
    main = r.quotes[(b.basket_id, 0)]
    assert main.price == pytest.approx(0.40) and main.size == 30
    assert (b.basket_id, 1) not in r.quotes                 # A has nothing behind its best ask


def test_second_level_size_off_sizes_quotes_to_the_best_level(arbs):
    r, b = arbs
    _second_level(r, b, 30)
    assert r.quotes[(b.basket_id, 0)].size == 100 and r.quotes[(b.basket_id, 1)].size == 10


def test_resting_two_leg_quote_rides_out_a_dip_behind_the_best_level_and_goes_if_it_lasts(arbs):
    r, b = arbs
    rows = []
    r.journal = rows.append
    _second_level(r, b, 60, second_level_size=True)
    q = r.quotes[(b.basket_id, 0)]
    assert q.size == 60
    book(r, "X-B", [(0.40, 10)], [(0.45, 100), (0.44, 25), (0.30, 500)])
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)] is q and q.short_at is not None       # over its cover: it waits
    book(r, "X-B", [(0.40, 10)], [(0.45, 100), (0.44, 60), (0.30, 500)])
    _eval(r, b)
    assert q.short_at is None and not [x for x in rows if x["kind"] == "cancel"]     # the level came back
    book(r, "X-B", [(0.40, 10)], [(0.45, 100), (0.44, 25), (0.30, 500)])
    _eval(r, b)
    q.short_at -= R.COVER_GRACE_S
    _eval(r, b)
    _eval(r, b)
    assert r.quotes[(b.basket_id, 0)].remaining == 25       # still short after the wait: re-placed at the cover
    book(r, "X-B", [(0.40, 10)], [(0.45, 20)])              # the best level no longer holds it: no wait
    _eval(r, b)
    assert (b.basket_id, 0) not in r.quotes


def test_basket_of_three_legs_is_not_held_to_the_second_level(arbs):
    from backtest.book import L2Book
    r, b = arbs
    c = Basket(basket_id="t:c", relationship_id="t2", type="complete_set", cluster="t2",
               legs=(Leg("X-A", "yes"), Leg("X-B", "yes"), Leg("X-C", "yes")), payout=1.0,
               quote_until=None, resume_quoting=None)
    r.set_baskets([c])
    r.hot = [c.basket_id]
    r.feed._books.setdefault("X-C", L2Book("X-C"))
    r.meta["X-C"] = R.Meta(ticker="X-C", event="X", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=1000, depth_age_s=0.0,
                                        max_bundles_per_basket=10000, max_dollars=1e6, second_level_size=True)
    _no_deep(r)
    book(r, "X-A", [(0.45, 100)], [(0.35, 10)])
    book(r, "X-B", [(0.20, 10)], [(0.70, 100)])             # B's YES ask 0.30 x 100, nothing behind it
    book(r, "X-C", [(0.15, 10)], [(0.75, 100)])             # C's 0.25 x 100
    _eval(r, c)
    assert r.quotes[(c.basket_id, 0)].size == 100


def test_near_quote_and_arb_quote_together_stay_within_the_second_level(arbs):
    r, b = arbs
    _near(r, b, 500, second_level_size=True)
    main, near = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 0, "deep")]
    # B's asks: 10 at 0.55, 500 at 0.56. Without the 10, 500 complete at 0.39: 10 of them are the arb quote's.
    assert main.size == 10 and near.near and near.price == pytest.approx(0.39) and near.size == 490


def test_deep_quote_is_held_to_the_second_level_and_goes_when_it_is_gone(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_size=50, deep_min_gap_ticks=5,
                                        requote_cooldown_s=0.0, depth_age_s=0.0, second_level_size=True)
    book(r, "X-A", [(0.45, 10), (0.20, 5)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10), (0.44, 5), (0.30, 40)])      # B's asks 0.55 x 10, 0.56 x 5, 0.70 x 40
    _eval(r, b)
    for q in list(r.quotes.values()):
        q.placed_at -= R.DEEP_REQUOTE_S
    _eval(r, b)
    main, deep = r.quotes[(b.basket_id, 0)], r.quotes[(b.basket_id, 0, "deep")]
    assert main.size == 5                                   # 0.40 completes through 0.56 without the best level
    assert deep.price == pytest.approx(0.21) and deep.size == 45        # 0.21 through 0.70 as well
    book(r, "X-B", [(0.40, 10)], [(0.45, 100)])             # 100 at the best ask, nothing behind it
    _eval(r, b)
    assert (b.basket_id, 0, "deep") not in r.quotes         # on its own the deep quote still cleared its edge
    main.short_at -= R.COVER_GRACE_S
    _eval(r, b)
    assert not r.basket_quotes(b.basket_id)


def test_quote_is_not_priced_through_a_two_leg_basket_with_nothing_behind_its_best_level(arbs):
    r, b = arbs
    c = _cheaper_through_c(r, b)
    book(r, "X-B", [(0.30, 10)], [(0.35, 10), (0.34, 20)])  # B's asks 0.65 x 10, 0.66 x 20; C's only 0.55 x 10
    _eval(r, b)
    assert r.via[(b.basket_id, 0)] == c.basket_id
    through_c = r.quotes[(b.basket_id, 0)].max_price
    r.cfg.trading = dataclasses.replace(r.cfg.trading, second_level_size=True, requote_cooldown_s=0.0)
    r._alt_pick.clear()
    _eval(r, b)
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert (b.basket_id, 0) not in r.via and q.max_price == pytest.approx(through_c - 0.10, abs=0.005)


def test_a_full_disk_drops_journal_rows_instead_of_raising_into_trading(arbs, monkeypatch):
    import os
    from infra.runtime import disk
    r, _ = arbs
    path = os.path.join(r.journal_dir, f"{R._utc():%Y-%m-%d}.jsonl")
    r.journal({"kind": "x"})
    assert sum(1 for _ in open(path)) == 1
    with monkeypatch.context() as m:
        m.setattr(disk, "recording_ok", lambda path=None: False)
        r.journal({"kind": "x"})                            # under the floor: skipped

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as m:
        m.setattr(disk, "recording_ok", lambda path=None: True)
        m.setattr("builtins.open", full)
        r.journal({"kind": "x"})                            # the disk refuses: dropped, not raised
    assert sum(1 for _ in open(path)) == 1
    assert r.n["journal_dropped"] == 2


def test_fill_stats_answers_are_cached_until_the_counts_change():
    from strategies.ARBS.exit_cost import Stats
    s = Stats()
    s.add_reach(True, 2)
    s.add_vol(True, 3.0)
    p, f = s.fill_prob(True, 1), s.expected_fill(True, 10.0)
    assert (s.fill_prob(True, 1), s.expected_fill(True, 10.0)) == (p, f)
    s.add_reach(True, 0)                          # a new count clears them
    assert s.fill_prob(True, 1) == pytest.approx(0.5)


def test_leg_view_cache_sees_our_quote_repriced_in_place(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.30, size=4.0, cid="c",
                edge=0.03, max_price=0.4, pending=False)
    r.quotes[q.key] = q
    r._in_pass = True
    try:
        v1 = r.leg_view("X-A", "no")                 # our 4 at 0.30 left out of the YES bids
        q.price = 0.29                               # repriced in place, no dict write
        v2 = r.leg_view("X-A", "no")
        assert v2 is not v1
        r.quotes.pop(q.key)
        assert r.leg_view("X-A", "no") is not v2
    finally:
        r._in_pass = False


def test_summary_written_with_orjson_reads_back_like_json(tmp_path):
    import numpy as np
    p = str(tmp_path / "summary.json")
    s = {"counts": {"a": 1}, "by_shard": {0: 2.5, 100: np.float64(1.25)}, "quotes": [{"price": 0.42}], "x": None}
    R._write_summary(p, s)
    import json as _j
    got = _j.load(open(p))
    assert got == {"counts": {"a": 1}, "by_shard": {"0": 2.5, "100": 1.25}, "quotes": [{"price": 0.42}], "x": None}


def test_exit_model_predict_equals_the_row_dot_product():
    import random
    import numpy as np
    from strategies.ARBS import exit_cost as E
    rng = random.Random(3)
    w = np.array([rng.uniform(-1, 1) for _ in range(E.WIDTH)])
    for _ in range(500):
        f = {name: rng.uniform(edges[0] - 1, edges[-1] + 1) if np.isfinite(edges[0]) and np.isfinite(edges[-1])
             else rng.uniform(-5, 50) for name, edges in E.BINS.items()}
        assert E.predict(w, f) == pytest.approx(float(E.row(f) @ w), abs=1e-12)


def test_rank_dump_writes_estimates_beside_real_spend(arbs, monkeypatch):
    import gzip
    r, b = arbs
    r.hot_ready = True
    rows = [{"basket_id": b.basket_id, "quote_usd_h": 1.5, "legs": 2}]
    r.quote_parts[b.basket_id] = [0, None, None]

    async def go():
        r.rank_dump(rows, [b.basket_id], {b.basket_id}, 1000.0)
        r.rank_dump(rows, [b.basket_id], {b.basket_id}, 1001.0)       # inside RANK_DUMP_S: skipped
        await asyncio.sleep(0.2)
    asyncio.run(go())
    files = list((__import__("pathlib").Path(r.root) / "rank_dump").glob("*.jsonl.gz"))
    lines = gzip.open(files[0], "rt").read().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])["rows"][0]
    assert row[0] == b.basket_id and row[1] == 1.5 and row[4] == 1 and row[5] == 2
    assert json.loads(lines[0])["legs"] == {b.basket_id: [[l.ticker, l.side] for l in b.legs]}


def test_recent_flow_is_kept_apart_from_the_long_window(arbs):
    r, b = arbs
    t = time.time()
    for dt in (3500.0, 100.0, 10.0):
        r.flow.add("X-A", "yes", 10.0, t - dt)
        r.flow_recent.add("X-A", "yes", 10.0, t - dt)
    _, rate, recent = r.queue_parts("X-A", "yes", 0.30) or (None, None, None)
    assert recent == pytest.approx(20.0 / 1800.0, rel=1e-3)
    assert r.flow.rate("X-A", "yes", t, 3600.0) == pytest.approx(30.0 / 3600.0, rel=1e-3)   # long window intact


def test_cost_from_spend_uses_real_spend_once_quoted_a_while(arbs, monkeypatch):
    r, b = arbs
    monkeypatch.setattr(R, "COST_FROM_SPEND", True)
    now = time.time()
    est, _ = r.write_cost_parts(b, now)
    assert r.write_cost(b, now) == pytest.approx(est * R.EST_SCALE)          # never quoted: scaled estimate
    r.spend_first[b.basket_id] = now - R.WRITE_COST_TAU_S - 1
    r.spend[b.basket_id] = R._Rate()
    r.spend[b.basket_id].add(R.WRITE_COST_TAU_S * 3.0, now)                   # 3 tokens/s really spent
    assert r.write_cost(b, now) == pytest.approx(3.0)
    monkeypatch.setattr(R, "COST_FROM_SPEND", False)
    assert r.write_cost(b, now) == pytest.approx(max(est, 3.0))              # off: as before


def test_a_split_process_counts_book_updates_per_event(arbs):
    import types
    r, b = arbs
    assert r.event_msgs is not None                         # ARBS is one of shard 0's processes
    r.meta["X-A"] = R.Meta(ticker="X-A", event="EV-1", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    bk = types.SimpleNamespace(ticker="X-A", bid=0.30, ask=0.35, bid_depth=10.0, ask_depth=10.0)
    r._on_quote(bk)
    r._on_quote(bk)
    assert r._msg_n == {"EV-1": 2}
    for ev, n in r._msg_n.items():
        r.event_msgs.add(ev, float(n))
    r.event_msgs.write(r.event_msgs.snapshot())
    saved = json.load(open(os.path.join(r.root, R.parts.MSGS)))
    assert list(saved.values())[0] == {"EV-1": 2.0}


def test_a_split_process_times_each_events_evaluations_and_book_updates(arbs):
    import types
    r, b = arbs
    assert r.event_cpu is not None
    r.meta["X-A"] = R.Meta(ticker="X-A", event="EV-1", status="active", shard=0, taker_rate=0.07, maker_rate=0.0)
    r._on_quote(types.SimpleNamespace(ticker="X-A", bid=0.30, ask=0.35, bid_depth=10.0, ask_depth=10.0))
    assert r._cpu_s.get("EV-1", 0.0) > 0.0
    bb = Basket(basket_id="mecnet:EV-2:no:a+b", relationship_id="mecnet:EV-2", type="mutually_exclusive_no",
                cluster="t", legs=(Leg("X-A", "no"), Leg("X-B", "no")), payout=1.0, quote_until=None,
                resume_quoting=None)
    r.baskets[bb.basket_id] = bb
    r.evaluate = lambda bk: time.sleep(0.002)

    async def go():
        r.dirty.add(bb.basket_id)
        t = asyncio.get_running_loop().create_task(r.eval_loop())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())
    assert r._cpu_s.get("EV-2", 0.0) >= 0.002


def _reach_setup(r, b, monkeypatch):
    monkeypatch.setattr(R, "REACH_MODE", "shadow")
    for t in ("X-A", "X-B"):
        r.meta[t] = R.Meta(ticker=t, event="e", status="active", shard=0, taker_rate=0.0, maker_rate=0.0)
    rows = []
    monkeypatch.setattr(r, "journal", lambda row: rows.append(row))
    return rows


def test_reach_quote_sits_one_tick_over_the_deepest_level_a_500_dollar_sweep_reaches(arbs, monkeypatch):
    r, b = arbs                                            # complete set X-A YES + X-B YES, payout 1
    rows = _reach_setup(r, b, monkeypatch)
    # X-A YES bids: 0.40 x 300 ($180 to sweep), 0.35 x 300 ($195), 0.30 x 1000 ($700), 0.25 x 100
    book(r, "X-A", [(0.40, 300), (0.35, 300), (0.30, 1000), (0.25, 100)], [(0.20, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.40, 5000)])          # X-B YES ask 0.60
    main = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.36, size=0.0, cid="m",
                   edge=0.04, max_price=0.37, pending=False, placed_at=0.0)
    vs = r.views(b)
    levels = r.reach_levels(b, 0, main)
    # 0.31 (one tick over 0.30) is reached after $375; 0.26 only after $1,075
    assert r.reach_plan(b, vs, 0, 0.36, 0.0, 1000.0, levels, 0.0)[0] == pytest.approx(0.31)
    plan = R.pricing.QuotePlan(leg=0, price=0.36, max_price=0.37, edge=0.04, others_cost=0.6)
    r.reach_shadow(b, vs, 0, plan, main, 0.0, 1000.0, False)
    acts = lambda: [(x["action"], x["price"]) for x in rows if x["kind"] == "reach_shadow"]   # noqa: E731
    assert acts() == [("place", 0.31)]
    # the book shifts a little, still inside the $1,500 keep band: it stays put
    book(r, "X-A", [(0.40, 400), (0.35, 300), (0.30, 1000), (0.25, 100)], [(0.20, 10)])
    r.reach_shadow(b, r.views(b), 0, plan, main, 0.0, 1000.0 + R.REACH_EVAL_S + 1, False)
    assert acts() == [("place", 0.31)]
    # $2,000 of bids now sit over it: out of reach; after the cooldown it moves up to a reachable price
    book(r, "X-A", [(0.40, 3000), (0.33, 10), (0.30, 1000), (0.25, 100)], [(0.20, 10)])
    r.reach_shadow(b, r.views(b), 0, plan, main, 0.0, 1000.0 + 2 * R.REACH_EVAL_S + R.DEEP_REQUOTE_S, False)
    assert [a for a, _ in acts()][-1] in ("move", "cancel") and len(acts()) == 2


def test_reach_quote_stays_unquoted_while_it_clears_its_edge(arbs, monkeypatch):
    r, b = arbs
    rows = _reach_setup(r, b, monkeypatch)
    book(r, "X-A", [(0.40, 300), (0.35, 300), (0.30, 1000), (0.25, 100)], [(0.20, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.40, 5000)])          # X-B YES ask 0.60: 0.31 + 0.60 clears
    r._reach[(b.basket_id, 0)] = {"price": 0.31, "size": 1.0, "spend": 375.0, "t": 0.0, "on_at": 0.0}
    r.reach_shadow(b, r.views(b), 0, None, None, 0.0, 1000.0, False)    # arb quote gone, hot or not: kept
    assert not [x for x in rows if x["kind"] == "reach_shadow"] and r._reach
    book(r, "X-B", [(0.10, 10)], [(0.05, 5000)])           # X-B YES ask 0.95: 0.31 + 0.95 > 1, edge gone
    r._reach_at.clear()
    r.reach_shadow(b, r.views(b), 0, None, None, 0.0, 1010.0, False)
    assert [(x["action"], x.get("why")) for x in rows if x["kind"] == "reach_shadow"] == [("cancel", "no_edge")]


def test_rank_loop_rechecks_reach_quotes_of_baskets_not_evaluated(arbs, monkeypatch):
    r, b = arbs
    rows = _reach_setup(r, b, monkeypatch)
    r.meta["X-A"].status = r.meta["X-B"].status = "active"
    book(r, "X-A", [(0.40, 300), (0.30, 1000)], [(0.20, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.05, 5000)])            # no edge at 0.31
    r._reach[(b.basket_id, 0)] = {"price": 0.31, "size": 10.0, "spend": 375.0, "t": 0.0, "on_at": 0.0}
    r.expire_reach(R.REACH_GRACE_S + 1)
    assert [x["action"] for x in rows if x["kind"] == "reach_shadow"] == ["cancel"] and not r._reach


def test_live_reach_quote_rests_in_the_deep_slot_and_goes_once_its_edge_does(arbs, monkeypatch):
    r, b = arbs
    _reach_setup(r, b, monkeypatch)
    monkeypatch.setattr(R, "REACH_MODE", "on")
    book(r, "X-A", [(0.40, 300), (0.35, 300), (0.30, 1000), (0.25, 100)], [(0.20, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.40, 5000)])
    main = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.36, size=10.0, cid="m",
                   edge=0.04, max_price=0.37, pending=False, placed_at=0.0)
    run = lambda acts: [asyncio.run(a) for a in acts]    # noqa: E731
    run(r.reach_live(b, r.views(b), 0, None, main, True, 0.0, 1000.0, False, 0.0))
    dq = r.quotes[(b.basket_id, 0, "deep")]
    assert dq.reach and dq.price == pytest.approx(0.31)
    # inside the throttle nothing but the edge is checked: unchanged book, no write
    assert r.reach_live(b, r.views(b), 0, dq, main, True, 0.0, 1001.0, False, 0.0) == []
    book(r, "X-B", [(0.10, 10)], [(0.05, 5000)])          # X-B YES ask 0.95: edge gone at once
    run(r.reach_live(b, r.views(b), 0, dq, None, False, 0.0, 1002.0, False, 0.0))
    assert (b.basket_id, 0, "deep") not in r.quotes


def test_sweep_cancels_our_order_on_a_market_no_longer_in_meta(arbs, monkeypatch):
    # The quote's market left the scan, so no shard maps it: the sweep asks
    # Kalshi for the shard instead of failing "no order client" every pass.
    import types
    r, b = arbs
    client = types.SimpleNamespace(shard=0, _resting=lambda _: [("oid-1", "SENATEIN-28-R", "cid-1")])
    r.gw.clients = {0: client}
    r.real_ledger.quote_ids["oid-1"] = {"ticker": "SENATEIN-28-R"}
    calls, sent = [], []
    monkeypatch.setattr(R, "kalshi_get", lambda path, **k: calls.append(path) or (
        {"market": {"event_ticker": "SENATEIN-28"}} if "/markets/" in path else {"event": {"exchange_index": 0}}))

    async def call(fn, *a):
        return fn(*a)

    async def cancel(ticker, oid, reason):
        sent.append((ticker, oid, r.gw.client(ticker) is client))
        return True, None, True

    r.gw.call, r.gw.cancel = call, cancel
    asyncio.run(r.sweep("test"))
    assert sent == [("SENATEIN-28-R", "oid-1", True)]
    assert calls == ["/trade-api/v2/markets/SENATEIN-28-R", "/trade-api/v2/events/SENATEIN-28"]


def test_gateway_keeps_a_dropped_markets_shard():
    from strategies.ARBS import execution as E
    g = E.Gateway(pool=None)
    g.clients = {0: object()}
    g.setup({"A-1": 0}, None)
    g.setup({"B-1": 0}, None)                               # A-1 left the scan
    assert g.client("A-1") is g.clients[0]


def test_a_quote_whose_completion_thins_is_cut_in_place(arbs):
    r, b = arbs
    _no_deep(r)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, depth_age_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])          # B's YES ask: 10 at 0.55
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.remaining == 5 and q.price == pytest.approx(0.31)
    book(r, "X-B", [(0.40, 10)], [(0.45, 3), (0.30, 10)])   # 3 at 0.55, then 0.70: 3 complete
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 3 and q.price == pytest.approx(0.31)
    book(r, "X-B", [(0.40, 10)], [(0.32, 2), (0.20, 10)])   # 2 at 0.68: 0.31 is past the limit
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 2 and q.price < 0.31 - 0.005


def test_a_cut_that_cannot_be_sent_cancels_the_quote(arbs):
    r, b = arbs
    _no_deep(r)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, depth_age_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    q.exchange_id = "ex-1"
    sent = []

    async def cancel(ticker, oid, reason):
        sent.append((oid, reason))
        return True, None, True

    r.live = True
    r.gw.can_afford = lambda *a: False
    r.gw.cancel = cancel
    book(r, "X-B", [(0.40, 10)], [(0.45, 3), (0.30, 10)])
    _eval(r, b)
    assert sent == [("ex-1", "no_edge")] and r.quotes.get((b.basket_id, 0)) is not q


def test_a_pregame_cut_rides_out_a_depth_flicker_and_keeps_its_price_within_the_tolerance(arbs, monkeypatch):
    r, b = arbs
    _no_deep(r)
    monkeypatch.setattr(R, "PRE_NO_EDGE_GRACE_S", 10.0)
    monkeypatch.setattr(R, "PRE_LIMIT_SLACK_TICKS", 1)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0, depth_age_s=0.0, min_exit_edge=0.01)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])          # B's YES ask: 10 at 0.55
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    assert q.remaining == 5 and q.price == pytest.approx(0.31)
    # B thins to 3 at 0.55 then 0.645: 5 lots at 0.31 still clear 1c, so the
    # quote rests at full size through the grace.
    book(r, "X-B", [(0.40, 10)], [(0.45, 3), (0.355, 10)])
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 5 and q.unpriced_at is not None
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])          # depth back: the clock stops
    _eval(r, b)
    assert q.remaining == 5 and q.unpriced_at is None
    book(r, "X-B", [(0.40, 10)], [(0.45, 3), (0.355, 10)])
    _eval(r, b)
    q.unpriced_at -= 10.0                                 # the thin book lasted: cut, price kept
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 3 and q.price == pytest.approx(0.31)
    # A cut that leaves 0.31 one tick past the new limit (B: 3 at 0.645, then
    # 0.80) keeps 0.31; two ticks past it reprices.
    r.quotes.clear()
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    q = r.quotes[(b.basket_id, 0)]
    book(r, "X-B", [(0.40, 10)], [(0.355, 3), (0.20, 10)])
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 3 and q.price == pytest.approx(0.31)
    book(r, "X-B", [(0.40, 10)], [(0.345, 2), (0.20, 10)])  # 2 at 0.655: limit 0.29
    _eval(r, b)
    assert r.quotes.get((b.basket_id, 0)) is q and q.remaining == 2 and q.price == pytest.approx(0.29)


def test_deep_only_book_updates_on_pre_game_baskets_are_coalesced(arbs, monkeypatch):
    import types
    r, b = arbs
    r.dirty.clear(); r.quoted.clear()
    upd = lambda bid, ask: types.SimpleNamespace(ticker="X-A", bid=bid, ask=ask, bid_depth=1, ask_depth=1)  # noqa: E731
    monkeypatch.setattr(r, "liq", types.SimpleNamespace(on_book=lambda *a: None))
    monkeypatch.setattr(r.cfg.runtime, "record_prints", False, raising=False)

    async def go():
        r._on_quote_inner(upd(0.40, 0.45))                  # first sight: a top change, marked now
        assert b.basket_id in r.dirty
        r.dirty.clear()
        for _ in range(5):
            r._on_quote_inner(upd(0.40, 0.45))              # deeper levels only: one deferred mark
        assert b.basket_id not in r.dirty and r.lazy == {b.basket_id}
        await asyncio.sleep(R.LAZY_EVAL_S + 0.05)
        assert b.basket_id in r.dirty and not r.lazy
        r.dirty.clear()
        r._on_quote_inner(upd(0.41, 0.45))                  # the top moved: now
        assert b.basket_id in r.dirty
    asyncio.run(go())


def test_live_baskets_are_marked_on_every_book_update(arbs, monkeypatch):
    import types
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    monkeypatch.setattr(r, "liq", types.SimpleNamespace(on_book=lambda *a: None))
    monkeypatch.setattr(r.cfg.runtime, "record_prints", False, raising=False)
    upd = types.SimpleNamespace(ticker="X-A", bid=0.40, ask=0.45, bid_depth=1, ask_depth=1)

    async def go():
        r._on_quote_inner(upd)
        r.dirty.clear()
        r._on_quote_inner(upd)
        assert live.basket_id in r.dirty and not r.lazy
    asyncio.run(go())


def test_scan_markets_do_not_grow_while_kalshi_rate_limits_reads(arbs, monkeypatch):
    r, b = arbs
    r.markets, W, r.hot_ready = 4000, R.LOOP_CPU_WINDOW_S, True
    monkeypatch.setattr(R.kalshi_api, "LAST_429_AT", [time.time()])
    assert r.scan_markets(0.0, 0.0) == 4000
    assert r.scan_markets(W, W * 0.2) == 4000                    # 20%: would grow, but reads are limited
    assert r.scan_markets(2 * W, W * 0.2 + W * 0.95) == 3000     # shrinking still works (-25% at most)
    R.kalshi_api.LAST_429_AT[0] = time.time() - 2 * W
    assert r.scan_markets(3 * W, W * 1.35) == 3750               # no recent 429: grows again (+25%)


def test_audit_rereads_only_the_markets_that_disagree(arbs, monkeypatch):
    r, b = arbs
    seen = []
    monkeypatch.setattr(R, "kalshi_get", lambda path, params=None, **k: seen.append(params["tickers"]) or {"markets": []})
    r.load_meta(["X-A"])
    assert seen == ["X-A"]
    seen.clear()
    r.load_meta()
    assert seen == ["X-A,X-B"]


def test_a_fill_for_an_unlisted_basket_is_held_aside_for_the_audit_until_booked(arbs):
    r, b = arbs
    r.real_ledger.quote_ids["oid-9"] = {"basket_id": "gone:basket", "ticker": "X-A", "side": "yes", "retired_at": None}
    f = {"order_id": "oid-9", "trade_id": "t1", "ticker": "X-A", "side": "yes", "price": 0.31, "size": 1.0,
         "fee": 0.0, "liquidity": "maker"}
    assert r.apply_real_fill(f, "rest")
    assert r.unbooked_pos == {"oid-9:t1": {"ticker": "X-A", "qty": 1.0}}
    assert json.load(open(os.path.join(r.root, R.UNBOOKED_POSITIONS))) == r.unbooked_pos
    r.apply_real_fill(f, "rest")                               # read again: counted once
    assert len(r.unbooked_pos) == 1
    r.real_ledger.quote_ids["oid-9"]["basket_id"] = b.basket_id  # the basket is back: booked, no longer aside
    assert r.apply_real_fill(f, "rest")
    assert r.unbooked_pos == {} and r.real_ledger.net_by_ticker().get("X-A") == 1.0


def test_a_booked_fill_read_again_after_its_basket_left_the_scan_is_not_held_aside(arbs):
    r, b = arbs
    r.real_ledger.quote_ids["oid-7"] = {"basket_id": b.basket_id, "ticker": "X-A", "side": "yes", "retired_at": None}
    f = {"order_id": "oid-7", "trade_id": "t1", "ticker": "X-A", "side": "yes", "price": 0.31, "size": 1.0,
         "fee": 0.0, "liquidity": "maker"}
    assert r.apply_real_fill(f, "rest")
    r.real_ledger.quote_ids["oid-7"]["basket_id"] = "gone:basket"   # the scan dropped it; a restart polls again
    assert r.apply_real_fill(f, "rest")
    assert r.unbooked_pos == {} and r.real_ledger.net_by_ticker().get("X-A") == 1.0



def test_a_basket_with_a_quote_resting_is_marked_on_every_book_update(arbs, monkeypatch):
    import types
    r, b = arbs
    monkeypatch.setattr(r, "liq", types.SimpleNamespace(on_book=lambda *a: None))
    monkeypatch.setattr(r.cfg.runtime, "record_prints", False, raising=False)
    upd = types.SimpleNamespace(ticker="X-A", bid=0.40, ask=0.45, bid_depth=1, ask_depth=1)
    r.quotes[(b.basket_id, 0)] = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.31,
                                         size=5.0, cid="c", edge=0.04, max_price=0.32, pending=False)

    async def go():
        r._on_quote_inner(upd)
        r.quoted.clear(); r.dirty.clear()
        r._on_quote_inner(upd)                              # deeper levels only, but our quote rests: now
        assert b.basket_id in r.quoted and not r.lazy
    asyncio.run(go())


def test_live_model_sizes_stop_at_live_quote_size(arbs, monkeypatch):
    import types
    r, b = arbs
    seen = []

    class Stub:
        def value(self, f, x, live, n, maker_rate=0.0):
            seen.append(n)
            return (n, 1.0)                                       # bigger is worth more

    r.exit_model = Stub()
    monkeypatch.setattr(R.exit_cost, "live_features", lambda *a, **k: {})
    monkeypatch.setattr(R.pricing, "others_cost", lambda vs, j, n: n)
    monkeypatch.setattr(R.pricing, "prices_at", lambda vs, j, payout, edges, o: list(edges))
    vs = [types.SimpleNamespace(maker_rate=0.0)] * 2
    now_ts = pd.Timestamp.now(tz="UTC")
    assert r.model_pick(b, 0, vs, now_ts, True, 100.0)[1] == 100.0 and max(seen) == 100.0
    seen.clear(); r._model_cache.clear(); r.model_state.clear()
    assert r.model_pick(b, 0, vs, now_ts, True, 200.0)[1] == 200.0 and max(seen) == 200.0



def test_order_error_breaker_clears_after_a_quiet_cooldown(arbs):
    r, b = arbs
    import requests
    exc = requests.HTTPError('400 for x: {"code":9,"message":"The server was unable to process your request."}')
    for _ in range(r.cfg.runtime.max_order_errors):
        r._order_err(b.basket_id, "quote X-A", exc, "X-A")
    assert r.blocked() == "order_error_breaker"
    r.order_err_at -= R.ORDER_ERROR_COOLDOWN_S - 1
    assert r.blocked() == "order_error_breaker"
    r.order_err_at -= 2
    assert r.blocked() is None and r.order_errors == 0
    assert any("breaker cleared" in a["msg"] for a in r.alerts)


def test_a_quote_fill_sends_its_completion_before_the_fill_handler_returns(arbs):
    r, b = arbs
    log = []
    live = _live_groups(r, b, log)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    q = R.Quote(basket_id=live.basket_id, leg=0, ticker="X-A", side="yes", price=0.3, size=5, cid="a",
                edge=0.05, max_price=0.3, pending=False, exchange_id="oa")
    r.quotes[q.key] = q
    sent = []

    def place_ioc(o):
        sent.append(o)
        return None

    class C:
        def allow_now(self, *a, **k):
            return True

        def note_rate_limited(self, *a):
            pass
    C.place_ioc = staticmethod(place_ioc)
    r.gw.client = lambda t: C()
    del r.gw.ioc                                             # the real gateway path: a write thread

    async def go():
        r._quote_fill(q, 2, 0.6, None, "ws")
        r._exit_fast()
        t0 = time.monotonic()
        while not sent and time.monotonic() - t0 < 1.0:     # the write thread, not the event loop
            time.sleep(0.001)
        n = len(sent)
        await drain(r)
        return n
    assert asyncio.run(go()) == 1 and sent[0]
    r.live = False
