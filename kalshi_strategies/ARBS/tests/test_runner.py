"""Runner decisions in shadow mode on hand-built books (no network)."""
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


def test_quote_fill_rests_a_completion_bid_when_the_book_held(arbs):
    r, b = arbs
    _fill_quote_on_a(r, b)
    _eval(r, b)                                      # first pass pulled B's arb quote; this one rests
    assert r.shadow_ledger.baskets[b.basket_id].unhedged(b.tickers) == {"X-A": 5}
    q = r.quotes[(b.basket_id, 1)]
    assert (q.kind, q.side, q.size) == ("complete", "yes", 5)    # B's arb quote replaced by a completion bid


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
    live = dataclasses.replace(b, quote_until=pd.Timestamp("2026-01-01", tz="UTC"))
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


def test_quotes_on_every_leg_count_once_per_basket(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    worst = b.payout + 0.01 * r.cfg.completion.chase_ticks
    # Room for exactly one basket's quotes at quote_size 5, not two.
    r.cfg.trading = dataclasses.replace(r.cfg.trading, max_dollars=5 * worst + 0.01)

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    qs = r.basket_quotes(b.basket_id)
    assert {q.leg for q in qs} == {0, 1} and all(q.size == 5 for q in qs)
    assert r.committed() == pytest.approx(5 * worst)


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


def test_flip_exits_held_bundles_at_exit_edge(arbs):
    r, b = arbs
    f = _with_flip(r, b)
    r.cfg.trading = dataclasses.replace(r.cfg.trading, exit_edge=0.01, min_edge=0.05, max_dollars=0.0,
                                        enable_quoting=False)
    for t in b.tickers:
        r.shadow_ledger.record(b.basket_id, b.tickers, t, "yes", 3, 1.35, time.time())
    # NO asks 0.47 + 0.47 + fees 0.035: a 2.5c edge, above exit_edge, below min_edge.
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
    r.cfg.trading = dataclasses.replace(r.cfg.trading, exit_edge=0.01, min_edge=0.05, enable_quoting=False)
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
    r.cfg.trading = dataclasses.replace(r.cfg.trading, exit_edge=0.01, min_edge=0.05, max_dollars=0.0)
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
        r.meta[t].status = "closed"

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
