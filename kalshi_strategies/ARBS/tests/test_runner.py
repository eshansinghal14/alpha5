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


def test_quote_fill_completes_the_other_leg(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])     # A ask 0.65
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])     # B ask 0.55 -> gap < 0, no take

    async def go():
        r.evaluate(b)
        await drain(r)
        qs = r.basket_quotes(b.basket_id)
        assert {q.leg for q in qs} == {0, 1}          # every leg quoted at once
        q = r.quotes[(b.basket_id, 0)]
        # A print through our quote fills it in shadow.
        r._on_trade(q.ticker, Trade(ticker=q.ticker, time=pd.Timestamp.now(tz="UTC"), price=q.price, size=5,
                                    taker_side="no" if q.side == "yes" else "yes"))
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    pos = r.shadow_ledger.baskets[b.basket_id]
    assert pos.bundles(b.tickers) == 5 and not pos.unhedged(b.tickers)
    assert not r.basket_quotes(b.basket_id)          # the other leg's quote is pulled while completing


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


def test_broken_basket_alerts_when_cap_below_ask(arbs):
    r, b = arbs
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.10, 10)], [(0.20, 10)])     # B ask 0.80
    r.shadow_ledger.record(b.basket_id, b.tickers, "X-A", "yes", 5, 5 * 0.40, time.time())

    async def go():
        r.evaluate(b)
        await drain(r)
    asyncio.run(go())
    assert r.shadow_ledger.baskets[b.basket_id].unhedged(b.tickers) == {"X-A": 5}
    assert any("cannot complete" in a["msg"] for a in r.alerts)
    assert r.summary()["holdings"][0]["completion"]["short"] == ["X-B"]


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
    worst = b.payout + r.cfg.completion.max_loss_per_bundle
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
