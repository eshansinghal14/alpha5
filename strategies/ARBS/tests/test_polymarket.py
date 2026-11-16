import asyncio
import json
import time

import pandas as pd
import pytest

from backtest.book import L2Book
from infra.polymarket.feed import apply_book, trade_of
from infra.polymarket.orders import parse_execution
from strategies.ARBS import pm_scan
from strategies.ARBS import runner as R
from strategies.ARBS.spec import Basket, Leg
from strategies.ARBS.venues import Feeds, is_pm, pm_result


def test_venue_by_ticker_case():
    assert is_pm("atc-unl-ita-fra-2026-11-12-ita") and not is_pm("KXUEFANLGAME-26NOV12ITAFRA-ITA")


def test_book_offers_become_no_bids():
    b = L2Book("m", 4)
    apply_book(b, {"bids": [{"px": {"value": "0.40"}, "qty": "10"}],
                   "offers": [{"px": {"value": "0.43"}, "qty": "7"}, {"px": {"value": "0.45"}, "qty": "2"}]},
               pd.Timestamp.now(tz="UTC"))
    assert b.yes_bids == {0.4: 10.0} and b.no_bids == {0.57: 7.0, 0.55: 2.0}
    assert b.best_ask() == 0.43


def test_trade_priced_in_the_makers_side():
    now = pd.Timestamp.now(tz="UTC")
    buy_yes = trade_of({"marketSlug": "m", "price": {"value": "0.43"}, "quantity": {"value": "5"},
                        "taker": {"intent": "ORDER_INTENT_BUY_LONG"}}, now)
    buy_no = trade_of({"marketSlug": "m", "price": {"value": "0.40"}, "quantity": {"value": "5"},
                       "taker": {"intent": "ORDER_INTENT_BUY_SHORT"}}, now)
    assert (buy_yes[0].price, buy_yes[0].taker_side) == (0.57, "yes")     # met a NO bid at 0.57
    assert (buy_no[0].price, buy_no[0].taker_side) == (0.40, "no")        # met a YES bid at 0.40


def test_no_fill_is_priced_in_no_terms():
    f = parse_execution({"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "2.5", "lastPx": {"value": "0.62"},
                         "tradeId": "t1", "aggressor": False,
                         "order": {"id": "o1", "marketSlug": "m", "intent": "ORDER_INTENT_BUY_SHORT",
                                   "cumQuantity": 2.5}})
    assert (f["side"], f["price"], f["size"], f["liquidity"], f["cum"]) == ("no", 0.38, 2.5, "maker", 2.5)


def test_settlement_price_to_result():
    assert pm_result(1.0) == ("yes", 1.0) and pm_result(0.0) == ("no", 0.0)
    assert pm_result(0.37) == ("scalar", 0.37) and pm_result(None) == ("", None)


def _pm(slug, event, title, question, team=None, abbr=None, start="2026-11-14T15:00:00Z"):
    return {"slug": slug, "event": event, "event_title": title, "question": question, "series": "unl",
            "sports_type": "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME", "game_start": start,
            "sides": [{"team": team, "abbr": abbr}] if team else [{}]}


def _soccer():
    ev = "atc-unl-wal-por-2026-11-14"
    pm = {s["slug"]: s for s in (
        _pm(f"{ev}-wal", ev, "Wales vs. Portugal", "Will Wales win?", "Wales", "wal"),
        _pm(f"{ev}-draw", ev, "Wales vs. Portugal", "Will it end in a draw?"),
        _pm(f"{ev}-por", ev, "Wales vs. Portugal", "Will Portugal win?", "Portugal", "por"))}
    k = {"event_ticker": "KXUEFANLGAME-26NOV14WALPOR", "series_ticker": "KXUEFANLGAME",
         "markets": [{"ticker": f"KXUEFANLGAME-26NOV14WALPOR-{x}", "yes_sub_title": n, "status": "active"}
                     for x, n in (("WAL", "Wales"), ("POR", "Portugal"), ("TIE", "Tie"))]}
    return pm, k


def test_soccer_game_pairs_each_outcome_across_exchanges():
    pm, k = _soccer()
    doc = pm_scan.scan(pm, [k], now=pd.Timestamp("2026-11-13", tz="UTC"))
    types = sorted(r["type"] for r in doc["relationships"])
    assert types == ["complete_set", "cross_venue", "cross_venue", "cross_venue", "mutually_exclusive_no"]
    cross = {r["relationship_id"]: r for r in doc["relationships"] if r["type"] == "cross_venue"}
    b = cross["cross:KXUEFANLGAME-26NOV14WALPOR-TIE"]["baskets"][0]
    assert b["legs"] == [{"ticker": "atc-unl-wal-por-2026-11-14-draw", "side": "yes"},
                         {"ticker": "KXUEFANLGAME-26NOV14WALPOR-TIE", "side": "no"}]
    assert cross["cross:KXUEFANLGAME-26NOV14WALPOR-POR"]["baskets"][1]["legs"][0]["ticker"].endswith("-por")
    cs = next(r for r in doc["relationships"] if r["type"] == "complete_set")
    assert [b["payout"] for b in cs["baskets"]] == [1, 2]
    assert cs["live_from_utc"].startswith("2026-11-14T15:00")


def test_a_game_with_one_team_matching_is_not_paired():
    pm, k = _soccer()
    k["markets"][1]["yes_sub_title"] = "Spain"
    k["markets"][1]["ticker"] = "KXUEFANLGAME-26NOV14WALPOR-ESP"
    doc = pm_scan.scan(pm, [k], now=pd.Timestamp("2026-11-13", tz="UTC"))
    assert not [r for r in doc["relationships"] if r["type"] == "cross_venue"]


def test_weather_buckets_cover_and_pair_by_station():
    day = "2026-11-14"
    pm = {f"tc-temp-nychigh-{day}-{k}": {"slug": f"tc-temp-nychigh-{day}-{k}"}
          for k in ("lt48f", "gte48lt49f", "gte50lt51f", "gte52f")}
    k = {"event_ticker": "KXHIGHNY-26NOV14", "series_ticker": "KXHIGHNY", "markets": [
        {"ticker": "KXHIGHNY-26NOV14-T48", "strike_type": "less", "cap_strike": 48, "status": "active"},
        {"ticker": "KXHIGHNY-26NOV14-B50.5", "strike_type": "between", "floor_strike": 50, "cap_strike": 51,
         "status": "active"},
        {"ticker": "KXHIGHNY-26NOV14-T51", "strike_type": "greater", "floor_strike": 51, "status": "active"}]}
    days = pm_scan.weather_days(pm)
    assert days[0]["covered"]
    assert pm_scan.pair_weather([k], days) == {"KXHIGHNY-26NOV14-T48": f"tc-temp-nychigh-{day}-lt48f",
                                                "KXHIGHNY-26NOV14-B50.5": f"tc-temp-nychigh-{day}-gte50lt51f",
                                                "KXHIGHNY-26NOV14-T51": f"tc-temp-nychigh-{day}-gte52f"}


@pytest.fixture
def pm_arbs(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LIVE_ROOT", str(tmp_path))
    from strategies.ARBS import SERIES
    r = R.ArbsRunner(SERIES["ARBSPM"], live_trading=False)
    b = Basket(basket_id="cross:K-A:pm_no", relationship_id="cross:K-A", type="cross_venue", cluster="cross:soccer",
               legs=(Leg("pm-ev-a", "no"), Leg("K-A", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.set_baskets([b])
    return r, b


def test_pm_fill_counts_once_from_websocket_and_reconcile(pm_arbs):
    r, b = pm_arbs
    r.live = True
    r.real_ledger.quote_ids["o1"] = {"basket_id": b.basket_id, "ticker": "pm-ev-a", "side": "no", "retired_at": None}
    ws = {"ticker": "pm-ev-a", "side": "no", "price": 0.40, "size": 3.0, "fee": 0.0, "liquidity": "maker",
          "order_id": "o1", "trade_id": "t1", "cum": 3.0}
    assert r.apply_real_fill(ws, "ws")
    rec = {"ticker": "pm-ev-a", "side": "no", "price": 0.40, "size": 3.0, "fee": 0.0, "liquidity": "maker",
           "order_id": "o1", "trade_id": "cum:3.0", "cum": 3.0}
    assert r.apply_real_fill(rec, "reconcile")
    assert r.real_ledger.net_by_ticker() == {"pm-ev-a": -3.0}
    more = dict(rec, size=5.0, cum=5.0, trade_id="cum:5.0")
    r.apply_real_fill(more, "reconcile")
    assert r.real_ledger.net_by_ticker() == {"pm-ev-a": -5.0}


def test_pm_process_rests_no_quote_on_a_kalshi_leg(pm_arbs):
    r, b = pm_arbs
    assert r.pm_process
    r.feed._books = {"pm-ev-a": L2Book("pm-ev-a", 4), "K-A": L2Book("K-A", 4)}
    r.feed.up = True
    now = pd.Timestamp.now(tz="UTC")
    for t, yes, no in (("pm-ev-a", [(0.55, 50)], [(0.40, 50)]), ("K-A", [(0.35, 50)], [(0.30, 50)])):
        r.feed.ladder(t).apply_snapshot("yes", yes, time=now)
        r.feed.ladder(t).apply_snapshot("no", no, time=now)
    for t in b.tickers:
        r.meta[t] = R.Meta(ticker=t, event="e", status="active", shard=100 if is_pm(t) else 0, taker_rate=0.07,
                           maker_rate=0.0)
    r.hot = [b.basket_id]
    import dataclasses
    r.cfg.trading = dataclasses.replace(r.cfg.trading, min_edge=0.0, quote_edge_add=0.0, depth_age_s=0.0)

    async def go():
        r.manage_quotes(b, r.views(b), time.time())
        await asyncio.sleep(0)
    asyncio.run(go())
    assert {q.ticker for q in r.quotes.values()} == {"pm-ev-a"}


def test_audit_takes_other_processes_kalshi_legs_off(pm_arbs, tmp_path):
    r, b = pm_arbs
    (tmp_path / "ARBS").mkdir()
    (tmp_path / "ARBS" / "ledger.json").write_text(json.dumps({"baskets": {"x": {"legs": {
        "K-A": {"side": "no", "qty": 40.0, "cost": 1.0}}}}}))
    r.real_ledger.record(b.basket_id, b.tickers, "K-A", "yes", 5, 2.0, time.time())
    r.real_ledger.record(b.basket_id, b.tickers, "pm-ev-a", "no", 5, 2.0, time.time())

    class Kalshi:
        def positions_all(self):
            return {"K-A": -35.0}             # ARBS's 40 NO and our 5 YES

    class Poly:
        def positions_all(self):
            return {"pm-ev-a": -5.0}

    async def call(fn, *a):
        return fn(*a)
    r.gw.venue_clients, r.gw.call = lambda: [Kalshi(), Poly()], call
    assert asyncio.run(r.audit()) and r.audit_diff is None


def test_feeds_route_by_venue():
    f = Feeds(["K-A", "pm-a"], lambda b: None, lambda t, x: None, label="t")
    assert f.ladder("pm-a") is f.pm._books["pm-a"] and f.ladder("K-A") is f.kalshi._books["K-A"]
    f.add(["pm-b", "K-B"])
    assert "pm-b" in f.pm._books and "K-B" in f.kalshi._books


def test_selection_ranks_by_recorded_volume(tmp_path):
    now = pd.Timestamp("2026-11-15T18:00Z")
    d = tmp_path / "trades" / "2026-11-15"
    d.mkdir(parents=True)
    ms = now.value // 10**6
    pd.DataFrame({"recv_ns": [now.value - 10**9] * 3 + [now.value - 25 * 3600 * 10**9],
                  "slug": ["busy-a", "busy-b", "quiet-a", "quiet-a"], "qty": [50.0, 50.0, 1.0, 999.0]}
                 ).to_parquet(d / f"{ms}.parquet")
    pd.DataFrame({"recv_ns": [1], "slug": ["quiet-a"], "qty": [999.0]}).to_parquet(d / f"{ms - 26 * 3600 * 1000}.parquet")
    vol = pm_scan.volume_24h(now, root=str(tmp_path))
    assert vol == {"busy-a": 50.0, "busy-b": 50.0, "quiet-a": 1.0}

    def rel(rid, a, b):
        return {"relationship_id": rid, "expected_settlement_utc": "2026-11-16T14:00Z", "cluster": "pm:soccer",
                "baskets": [{"basket_id": f"{rid}:x", "legs": [{"ticker": a, "side": "yes"}, {"ticker": b, "side": "yes"}]}]}
    doc = {"relationships": [rel("quiet", "quiet-a", "quiet-b"), rel("busy", "busy-a", "busy-b")]}
    pm_scan.add_volume(doc, vol)
    from strategies.ARBS.scanner import select
    sel = select(doc, now=now, days=3, max_markets=2)
    assert [r["relationship_id"] for r in sel["relationships"]] == ["busy"]


def test_fill_while_disarmed_is_booked_real(pm_arbs):
    r, b = pm_arbs
    assert not r.live
    r.real_ledger.quote_ids["o2"] = {"basket_id": b.basket_id, "ticker": "pm-ev-a", "side": "no", "retired_at": None}
    r._on_fill({"ticker": "pm-ev-a", "side": "no", "price": 0.40, "size": 2.0, "fee": 0.0, "liquidity": "maker",
                "order_id": "o2", "trade_id": "t2", "cum": 2.0})
    assert r.real_ledger.net_by_ticker() == {"pm-ev-a": -2.0}
    assert r.shadow_ledger.net_by_ticker() == {}


def test_cancel_echo_books_the_final_fill_and_ends_reads(pm_arbs):
    r, b = pm_arbs
    r.live = True
    r.real_ledger.quote_ids["o3"] = {"basket_id": b.basket_id, "ticker": "pm-ev-a", "side": "no",
                                     "retired_at": time.time()}
    r.real_ledger.quote_ids["o4"] = {"basket_id": b.basket_id, "ticker": "pm-ev-a", "side": "no",
                                     "retired_at": time.time()}
    r._on_gone("o3", "EXECUTION_TYPE_CANCELED", None, cum=2.0, avg=0.4)
    assert r.real_ledger.net_by_ticker() == {"pm-ev-a": -2.0} and r.real_ledger.quote_ids["o3"]["final"]
    asked = []

    class Poly:
        def reconcile(self, open_ids, closed_ids):
            asked.append((open_ids, closed_ids))
            return [{"order_id": "o4", "done": True, "cum": 0.0, "avg": None, "state": "ORDER_STATE_CANCELED"}]

    async def call(fn, *a):
        return fn(*a)
    r.gw.call = call
    asyncio.run(r.reconcile_pm(Poly()))
    asyncio.run(r.reconcile_pm(Poly()))
    assert asked == [([], ["o4"])]           # o3 final from its echo; o4 final after one read


def test_failed_arm_stays_disarmed(pm_arbs):
    r, b = pm_arbs
    r.gw.clients = {100: object()}

    async def balances():
        return {100: 5000.0}, 0.0

    async def boom():
        assert not r.live                    # nothing real is quoted while the arm checks run
        raise RuntimeError("429")
    r.shard_balances, r.poll_fills = balances, boom
    assert not asyncio.run(r.arm()) and not r.live


def test_client_reconcile_reads_few_closed_orders_and_stops_when_low(monkeypatch, tmp_path):
    from infra.kalshi.orders import SharedWriteBudget
    from infra.polymarket import limits
    from infra.polymarket.orders import PolymarketOrderClient
    monkeypatch.setattr(limits, "_BUDGET", SharedWriteBudget(1000.0, 0, path=str(tmp_path / "b")))
    c = PolymarketOrderClient()
    read = []
    c.open_orders = lambda: [{"id": "a", "cumQuantity": "1", "avgPx": {"value": "0.3"}, "state": "ORDER_STATE_NEW",
                              "intent": "ORDER_INTENT_BUY_LONG", "marketSlug": "m"}]

    def order(oid, pay="read"):
        read.append(oid)
        if oid == "c3":
            raise limits.PMBudgetLow("low")
        return {"cumQuantity": "0", "state": "ORDER_STATE_CANCELED", "intent": "ORDER_INTENT_BUY_LONG"}
    c.order = order
    out = c.reconcile(["a"], [f"c{i}" for i in range(10)], max_reads=5)
    assert read == ["c0", "c1", "c2", "c3"]
    assert [o["order_id"] for o in out] == ["a", "c0", "c1", "c2"] and out[1]["done"] and not out[0]["done"]


def test_reads_refused_below_the_keep_and_429_empties(monkeypatch, tmp_path):
    from infra.kalshi.orders import SharedWriteBudget
    from infra.polymarket import limits
    monkeypatch.setattr(limits, "_BUDGET", SharedWriteBudget(25.0, 0, path=str(tmp_path / "b")))
    limits.pay(keep=10.0)
    with pytest.raises(limits.PMBudgetLow):
        limits.pay(keep=10.0)
    limits.pay(force=True)
    limits.note_status(429, "get")
    assert limits.budget().tokens_now() <= 0.5 and limits.stats()["http_429"]["get"] >= 1


def test_echo_without_a_price_is_not_final(pm_arbs):
    r, b = pm_arbs
    r.real_ledger.quote_ids["o5"] = {"basket_id": b.basket_id, "ticker": "pm-ev-a", "side": "no",
                                     "retired_at": time.time()}
    r._on_gone("o5", "EXECUTION_TYPE_CANCELED", None, cum=2.0, avg=None)
    assert not r.real_ledger.quote_ids["o5"].get("final")      # reconcile still reads it
    r._on_gone("o6", "EXECUTION_TYPE_CANCELED", None, cum=0.0, avg=None)   # not ours: ignored


def test_fill_for_an_unlisted_basket_alerts_without_marking_it_booked(pm_arbs):
    r, b = pm_arbs
    r.real_ledger.quote_ids["o7"] = {"basket_id": "gone:basket", "ticker": "pm-ev-a", "side": "no",
                                     "retired_at": None}
    f = {"ticker": "pm-ev-a", "side": "no", "price": 0.4, "size": 1.0, "fee": 0.0, "liquidity": "maker",
         "order_id": "o7", "trade_id": "t7", "cum": 1.0}
    assert r.apply_real_fill(f, "ws")
    assert r.real_ledger.net_by_ticker() == {} and "o7:t7" not in r.real_ledger.seen_trades
    assert not r.real_ledger.quote_ids["o7"].get("booked")
    assert any("no longer listed" in a["msg"] for a in r.alerts)


def test_resolved_market_settles_from_its_outcome_prices():
    from strategies.ARBS.venues import pm_resolved_price
    m = {"status": "MARKET_STATUS_RESOLVED", "outcomes": '["Yes","No"]', "outcomePrices": '["0","1"]'}
    assert pm_resolved_price(m) == 0.0 and pm_result(pm_resolved_price(m)) == ("no", 0.0)
    assert pm_resolved_price({**m, "outcomePrices": '["0.33","0.67"]'}) == 0.33
    # Open: outcomePrices are live prices, not a result.
    assert pm_resolved_price({**m, "status": "MARKET_STATUS_OPEN", "outcomePrices": '["0.42","0.58"]'}) is None


def test_polymarket_cancels_wait_for_the_bucket_then_go(monkeypatch):
    from strategies.ARBS import execution
    monkeypatch.setattr(execution, "PM_CANCEL_WAIT_S", 0.2)
    calls = []

    class C:
        shard = 100

        def allow_now(self, kind, force=False, reason=""):
            calls.append(force)
            return force or len(calls) > 3       # refused 3 times, then the bucket has room

        def tokens_now(self):
            return -100.0                        # overdrawn: even urgent cancels wait

        def cancel(self, oid, ticker):
            return True
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    got, exc, sent = asyncio.run(gw.cancel("pm-a", "o1", "not_hot"))
    assert got is True and exc is None and calls == [False, False, False, False]

    calls.clear()
    C.allow_now = lambda self, kind, force=False, reason="": calls.append(force) or force   # never room
    asyncio.run(gw.cancel("pm-a", "o1", "not_hot"))
    assert calls[-1] is True                     # sent anyway after the wait


def test_urgent_polymarket_cancels_go_now_within_the_overdraw(monkeypatch):
    from strategies.ARBS import execution
    monkeypatch.setattr(execution, "PM_CANCEL_WAIT_S", 5.0)
    calls, tokens = [], [0.0]

    class C:
        shard = 100

        def allow_now(self, kind, force=False, reason=""):
            calls.append(force)
            return force                         # never room without force

        def tokens_now(self):
            return tokens[0]

        def cancel(self, oid, ticker):
            return True
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    t0 = time.monotonic()
    for reason in ("pull", "no_edge", "move_refused", "2 broken baskets", "exit"):
        calls.clear()
        asyncio.run(gw.cancel("pm-a", "o1", reason))
        assert calls == [False, True], reason    # forced on the first refusal
    assert time.monotonic() - t0 < 1.0

    calls.clear()
    tokens[0] = -60.0                            # past the overdraw: waits like a routine one
    monkeypatch.setattr(execution, "PM_CANCEL_WAIT_S", 0.2)
    asyncio.run(gw.cancel("pm-a", "o1", "pull"))
    assert len(calls) > 2 and calls[-1] is True


def test_polymarket_move_is_one_modify_unless_partly_filled(monkeypatch, tmp_path):
    from backtest.core import Order
    from infra.kalshi.orders import SharedWriteBudget
    from infra.polymarket import limits
    from infra.polymarket.orders import PolymarketOrderClient
    monkeypatch.setattr(limits, "_BUDGET", SharedWriteBudget(limits.TOKENS_PER_S, 0, path=str(tmp_path / "b")))
    c = PolymarketOrderClient()
    sent = []
    c._json = lambda method, path, **kw: sent.append((method, path, kw.get("body"))) or {}
    c.cancel = lambda oid, t: sent.append(("cancel", oid)) or True
    c.place_resting = lambda o: sent.append(("create", o.price, o.size)) or type("A", (), {"exchange_order_id": "new"})()
    o = Order(order_id=1, ticker="pm-a", side="no", order_type="limit", price=0.40, size=5.0,
              decision_time=pd.Timestamp.now(tz="UTC"), intent="quote")
    assert c.amend("o1", o, 5.0, 0.0, price=0.40) == {"order": {"order_id": "o1"}}
    ((method, path, body),) = sent
    assert path == "/v1/order/o1/modify" and body["price"]["value"] == "0.6000" and body["quantity"] == 5.0
    assert body["participateDontInitiate"] is True
    sent.clear()
    assert c.amend("o1", o, 3.0, 2.0, price=0.40) == {"order": {"order_id": "new"}}
    assert sent == [("cancel", "o1"), ("create", 0.40, 3.0)]


def test_replace_execution_reaches_on_replace_in_own_side_terms():
    from infra.polymarket.feed import PolyFeed
    got = []
    f = PolyFeed(["pm-a"], lambda b: None, lambda t, tr: None, label="t", on_fill=lambda f: None,
                 on_replace=lambda oid, px: got.append((oid, px)))
    f.handle_private({"orderSubscriptionUpdate": {"execution": {
        "type": "EXECUTION_TYPE_REPLACE", "order": {"id": "o1", "marketSlug": "pm-a", "state": "ORDER_STATE_REPLACED",
                                                    "intent": "ORDER_INTENT_BUY_SHORT",
                                                    "price": {"value": "0.62", "currency": "USD"}}}}})
    assert got == [("o1", 0.38)]


def test_unconfirmed_polymarket_move_goes_back_to_its_old_price(pm_arbs, monkeypatch):
    from strategies.ARBS import pricing
    r, b = pm_arbs
    r.live = True
    monkeypatch.setattr(R, "PM_MODIFY_ECHO_S", 0.1)

    async def amend(*a):
        return {"order": {"order_id": "o1"}}, None, True
    r.gw.amend = amend
    plan = pricing.QuotePlan(leg=0, price=0.42, max_price=0.45, edge=0.03, others_cost=0.5)

    async def go(echo):
        q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.40, size=5.0, cid="c",
                    edge=0.03, max_price=0.45, exchange_id="o1", pending=False)
        r.quotes[q.key] = q
        await r.move_quote(q, plan)
        assert q.price == pytest.approx(0.42)
        if echo:
            r._on_replace("o1", 0.42)
        await asyncio.sleep(0.2)
        return q
    assert asyncio.run(go(True)).price == pytest.approx(0.42)      # confirmed
    q = asyncio.run(go(False))
    assert q.price == pytest.approx(0.40) and r.n["pm_moves_unconfirmed"] == 1   # silently not made


def test_cross_venue_take_sends_polymarket_first_and_kalshi_only_for_its_fill(pm_arbs):
    from strategies.ARBS import pricing
    from strategies.ARBS.runner import RealFill
    r, b = pm_arbs                                   # legs: pm-ev-a NO, K-A YES; not a live event
    r.feed._books = {"pm-ev-a": L2Book("pm-ev-a", 4), "K-A": L2Book("K-A", 4)}
    r.feed.up = True
    now = pd.Timestamp.now(tz="UTC")
    for t, yes, no in (("pm-ev-a", [(0.55, 50)], [(0.40, 50)]), ("K-A", [(0.35, 50)], [(0.30, 50)])):
        r.feed.ladder(t).apply_snapshot("yes", yes, time=now)
        r.feed.ladder(t).apply_snapshot("no", no, time=now)
    for t in b.tickers:
        r.meta[t] = R.Meta(ticker=t, event="e", status="active", shard=100 if is_pm(t) else 0, taker_rate=0.07,
                           maker_rate=0.0)
    sent, pm_fill = [], [0.0]

    async def ioc(ticker, side, limit, size, why, view):
        sent.append((ticker, size))
        got = pm_fill[0] if is_pm(ticker) else size
        return (RealFill(size=got, price=limit, fee=0.0, exchange_order_id="x") if got else None), None
    r._ioc = ioc
    plan = pricing.TakePlan(size=5.0, limits=[0.45, 0.50], edge=0.05, cost=4.75)
    asyncio.run(r._take(b, plan, []))
    assert sent == [("pm-ev-a", 5.0)]                # Polymarket missed: Kalshi never sent
    sent.clear()
    pm_fill[0] = 3.0
    asyncio.run(r._take(b, plan, []))
    assert sent == [("pm-ev-a", 5.0), ("K-A", 3.0)]
