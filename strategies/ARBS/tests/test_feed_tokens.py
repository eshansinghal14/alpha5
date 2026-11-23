"""Feed-drop pulls, per-market feed readiness, move/pull journal reasons and
the live grow hold."""
import asyncio
import dataclasses

import pandas as pd

from strategies.ARBS import runner as R
from strategies.ARBS.depth import AskWatch
from strategies.ARBS.feed import MultiFeed
from strategies.ARBS.tests.test_runner import _eval, arbs, book  # noqa: F401  (arbs is a fixture)


def _snapshot(t):
    return {"type": "orderbook_snapshot", "msg": {"market_ticker": t, "yes_dollars_fp": [["0.40", "5"]],
                                                  "no_dollars_fp": [["0.50", "5"]]}}


def test_ask_watch_says_why_it_fired():
    w, k = AskWatch(), ("X", "no")
    assert w.fires(k, 0.0, 0.45, 10.0, 0.25, 0.5) is None
    assert w.fires(k, 0.1, 0.45, 4.0, 0.25, 0.5) == "size"
    assert w.fires(k, 0.2, 0.44, 4.0, 0.25, 0.5) == "rise"
    assert w.fires(k, 0.3, None, 0.0, 0.25, 0.5) == "gone"


def test_a_market_is_ready_once_its_snapshot_lands_after_a_reconnect():
    f = MultiFeed(["A", "B"], lambda *a: None, lambda *a: None, label="t")
    assert not f.ready("A")
    f._handle_message(_snapshot("A"))
    assert f.ready("A") and not f.ready("B")
    f._set_up(True)
    assert f.ready("B")
    f._set_up(False)                       # the connection dropped: nothing is current
    assert not f.ready("A") and not f.ready("B")


def test_reconnect_subscribes_the_named_markets_first():
    f = MultiFeed(["A", "B", "C"], lambda *a: None, lambda *a: None, label="t")
    f.first = lambda: ["C", "Z", "C"]
    assert f._ordered() == ["C", "A", "B"]

    def broken():
        raise RuntimeError("bad")
    f.first = broken
    assert f._ordered() == ["A", "B", "C"]


def _quotes(r, b):
    """A live basket's arb quote, a pre-game one and a deep one, resting."""
    live = dataclasses.replace(b, basket_id="t:live", legs=(R.Leg("L-A", "yes"), R.Leg("L-B", "yes")),
                               live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.baskets[live.basket_id] = live
    out = []
    for bid, tk, deep in ((b.basket_id, "X-A", True), (b.basket_id, "X-B", False), (live.basket_id, "L-A", False)):
        q = R.Quote(basket_id=bid, leg=0 if tk.endswith("A") else 1, ticker=tk, side="yes", price=0.3, size=5,
                    cid=tk, edge=0.02, max_price=0.3, exchange_id="o-" + tk, pending=False, deep=deep)
        r.quotes[q.key] = q
        out.append(q)
    return live


def test_feed_drop_pull_takes_live_quotes_first_and_deep_ones_last(arbs):
    r, b = arbs
    _quotes(r, b)
    r.feed.kalshi.up = False
    order = []

    async def cancel(q, reason):
        order.append((q.ticker, q.deep))
        r._retire(q)
    r.cancel_quote = cancel
    asyncio.run(r.pull_all("feed_down", venues={"kalshi"}))
    assert order == [("L-A", False), ("X-B", False), ("X-A", True)]


def test_feed_drop_pull_leaves_quotes_whose_markets_are_fed_again(arbs):
    r, b = arbs
    _quotes(r, b)
    r.feed.kalshi.up = False
    r.feed.per_market = True
    for t in b.tickers:
        r.feed.kalshi._handle_message(_snapshot(t))
    gone = []

    async def cancel(q, reason):
        gone.append(q.ticker)
        r._retire(q)
    r.cancel_quote = cancel
    asyncio.run(r.pull_all("feed_down", venues={"kalshi"}))
    assert gone == ["L-A"]
    assert r.feed.up_for(b.tickers) and not r.blocked()
    r.feed.per_market = False
    assert not r.feed.up_for(b.tickers) and r.blocked() == "feed_down"


def test_move_rows_carry_why_and_the_edge_limit(arbs):
    r, b = arbs
    rows = []
    r.journal = rows.append
    r.cfg.trading = dataclasses.replace(r.cfg.trading, requote_cooldown_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 10)])
    _eval(r, b)
    px = r.quotes[(b.basket_id, 0)].price
    book(r, "X-B", [(0.40, 10)], [(0.33, 10)])      # B's ask 0.67: A's limit falls under its quote
    _eval(r, b)
    m = [x for x in rows if x["kind"] == "move" and x["ticker"] == "X-A" and not x.get("deep")][-1]
    assert m["why"] == "limit" and m["to"] <= m["max"] < px


def test_a_live_quote_grows_only_once_its_room_has_held(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=5,
                                        requote_cooldown_s=0.0, live_grow_hold_s=3.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 1)])       # B's YES ask 0.55: 1 offered
    _eval(r, live)
    q = r.quotes[(live.basket_id, 0)]
    assert q.size == 1
    book(r, "X-B", [(0.40, 10)], [(0.45, 1), (0.44, 40)])
    _eval(r, live)
    assert q.size == 1                               # room grew, but not for 3 s yet
    r.grow_since[q.key] -= 3.5
    _eval(r, live)
    assert q.size == 5 and q.key not in r.grow_since


def test_a_grow_hold_of_zero_grows_at_once(arbs):
    r, b = arbs
    live = dataclasses.replace(b, live_from=pd.Timestamp("2026-01-01", tz="UTC"))
    r.set_baskets([live])
    r.hot = [live.basket_id]
    r.cfg.trading = dataclasses.replace(r.cfg.trading, quote_live_events=True, live_quote_size=5,
                                        requote_cooldown_s=0.0, live_grow_hold_s=0.0)
    book(r, "X-A", [(0.30, 10)], [(0.35, 10)])
    book(r, "X-B", [(0.40, 10)], [(0.45, 1)])
    _eval(r, live)
    book(r, "X-B", [(0.40, 10)], [(0.45, 1), (0.44, 40)])
    _eval(r, live)
    assert r.quotes[(live.basket_id, 0)].size == 5
