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
