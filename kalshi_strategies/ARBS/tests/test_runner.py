"""Runner decisions in shadow mode on hand-built books (no network)."""
import collections
import json
import asyncio
import dataclasses
import time

import pandas as pd
import pytest

from kalshi_backtest.core import Trade
from kalshi_strategies.ARBS import runner as R
from kalshi_strategies.ARBS.spec import Basket, Leg


@pytest.fixture
def arbs(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    from kalshi_strategies.ARBS import SERIES
    r = R.ArbsRunner(SERIES["ARBS"], live_trading=False)
    b = Basket(basket_id="t:b", relationship_id="t", type="complete_set", cluster="t",
               legs=(Leg("X-A", "yes"), Leg("X-B", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.all_baskets, r.baskets, r.tickers = [b], {b.basket_id: b}, ["X-A", "X-B"]
    r.by_ticker = {"X-A": [b.basket_id], "X-B": [b.basket_id]}
    from kalshi_backtest.book import L2Book
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
                                        enable_quoting=False)
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
    from kalshi_backtest.book import L2Book
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

    def load_meta():
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
    from kalshi_strategies.ARBS.ledger import Ledger
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


def _cheap_book(r):
    book(r, "X-A", [(0.35, 10)], [(0.60, 10)])      # YES A ask 0.40
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])      # YES B ask 0.50


def _live_takes(r, b):
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, take_live_events=True, live_quote_size=100)
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
    r.cfg.trading = dataclasses.replace(r.cfg.trading, live_quote_size=1, take_live_events=True)
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
    asyncio.run(r.move_quote(q, R.pricing.QuotePlan(leg=0, price=0.35, max_price=0.36, edge=0.0, others_cost=0.0)))
    assert q.key not in r.quotes and r.n["moves_refused"] == 1
    r.live = False


def test_chase_ignores_what_older_bundles_cost(arbs):
    r, b = arbs
    # 5 whole bundles bought dear, then a quote on A fills 1 more at 0.40,
    # priced against B's 0.50 ask: the book hasn't moved, so it rests.
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.70, time.time())
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-B", "yes", 5, 5 * 0.29, time.time())
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.45, 10)], [(0.50, 10)])
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="X-A", side="yes", price=0.40, size=1, cid="c",
                edge=1 - 0.40 - R.pricing.taker_cost(0.50, 0.07), max_price=0.40, pending=False)
    r.quotes[q.key] = q
    r._quote_fill(q, 1, 0.40, None, "shadow")
    r.cfg.completion = dataclasses.replace(r.cfg.completion, chase_ticks=50)
    r.exit_planned[b.basket_id] -= 0.01             # planned a cent better than today's book
    _eval(r, b)
    note = r.summary()["holdings"][0]["completion"]
    assert note["action"] == "rest" and note["why"] is None


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
    from kalshi_backtest.core import Trade
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


def test_write_cost_counts_top_of_book_changes(arbs):
    r, b = arbs
    now = time.time()
    quiet = r.write_cost(b, now)
    for i in range(60):
        r.top_moves.setdefault("X-A", R._Rate()).add(1.0, now)
    assert r.write_cost(b, now) > quiet
    r.charge(b.basket_id, 10_000.0)
    assert r.write_cost(b, now) == pytest.approx(10_000.0 / R.WRITE_COST_TAU_S, rel=0.01)


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
    from kalshi_backtest.core import Trade
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


def test_bad_fill_pauses_quoting_on_the_event(arbs):
    r, b = arbs
    r.cfg.trading = dataclasses.replace(r.cfg.trading, bad_fill_cooldown_s=60.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    assert r.basket_quotes(b.basket_id)
    r.cool(b)
    _eval(r, b)
    assert not r.basket_quotes(b.basket_id) and r.summary()["cooldowns"]["X"] > 0
    r.cooling.clear()
    _eval(r, b)
    assert r.basket_quotes(b.basket_id)


def _live_gap(r, b, a_yes_bids):
    """b as a live basket; A's arb quote sits at its 0.40 limit under bids at 0.45."""
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=2, live_edge_add=0.0,
                                        deep_quote_size=5, deep_min_gap_ticks=5, requote_cooldown_s=0.0)
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
    assert deep.price == pytest.approx(0.21) and deep.size == 5 and deep.edge > main.edge


def test_no_deep_quote_on_a_small_gap_or_a_slow_market(arbs):
    r, b = arbs
    live = _live_gap(r, b, [(0.45, 10), (0.37, 5)])        # 0.38 is 2 ticks behind 0.40
    assert (live.basket_id, 0) in r.quotes and (live.basket_id, 0, "deep") not in r.quotes
    r.set_baskets([b])
    r.hot = [b.basket_id]
    book(r, "X-A", [(0.45, 10), (0.20, 5)], [(0.35, 10)])
    _eval(r, b)
    assert (b.basket_id, 0) in r.quotes and not any(q.deep for q in r.quotes.values())


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
                                        deep_quote_size=5, deep_min_gap_ticks=5)
    book(r, "X-A", [(0.45, 10), (0.20, 5)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, live)
    assert (live.basket_id, 0) in r.quotes and not any(q.deep for q in r.quotes.values())


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
                                        live_edge_add=0.0, live_order_groups=True, deep_quote_size=0)
    ids = iter(range(1000))

    async def rest(ticker, side, price, size, cid, group=None):
        log.append(("rest", ticker, group))
        from kalshi_infra.kalshi.orders import RestingAck
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


def test_live_exit_waits_for_an_ungrouped_quotes_cancel(arbs):
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
    assert kinds.index("cancel_end") < kinds.index("ioc")
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
