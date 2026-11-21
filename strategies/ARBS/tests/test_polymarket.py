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
    calls, checks = [], [0]

    class C:
        shard = 100
        budget = None

        def allow_now(self, kind, force=False, reason=""):
            calls.append(force)
            return force

        def can_afford(self, kind="order", reserve=0.0):
            checks[0] += 1
            return checks[0] > 3                 # no room for 3 checks, then the bucket has room

        def tokens_now(self):
            return -100.0                        # overdrawn: even urgent cancels wait

        def cancel(self, oid, ticker):
            return True
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()
    gw.cancel_wait = {}

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    got, exc, sent = asyncio.run(gw.cancel("pm-a", "o1", "not_hot"))
    assert got is True and exc is None and calls == [False, True] and checks[0] == 4   # refused once, sent with room

    calls.clear()
    C.allow_now = lambda self, kind, force=False, reason="": calls.append(force) or force   # never room
    C.tokens_now = lambda self: -10.0            # within the overdraw
    asyncio.run(gw.cancel("pm-a", "o1", "not_hot"))
    assert calls[-1] is True                     # sent anyway after the wait


def _batch_gw(fail=None):
    from strategies.ARBS import execution
    sent = []

    class C:
        shard = 100
        budget = None

        def allow_now(self, kind, force=False, reason=""):
            return True

        def cancel(self, oid, ticker):
            sent.append([oid])
            return True

        def cancel_many(self, orders):
            if fail is not None:
                raise fail
            sent.append([o for o, _ in orders])
            return [o for o, _ in orders]

        def note_rate_limited(self, why):
            pass
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()
    gw.cancel_wait = {}

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    return gw, sent


def test_polymarket_cancels_in_one_pass_go_in_batches_urgent_first():
    gw, sent = _batch_gw()

    async def burst():
        cs = [gw.cancel("pm-a", f"r{i}", "not_hot") for i in range(5)] + \
             [gw.cancel("pm-a", f"u{i}", "no_edge") for i in range(30)]
        return await asyncio.gather(*cs)
    got = asyncio.run(burst())
    assert all(g == (True, None, True) for g in got)
    assert [len(s) for s in sent] == [20, 15]                  # 35 cancels, 2 requests
    assert all(o.startswith("u") for o in sent[0])             # urgent ones take the first batch


def test_a_failed_polymarket_batch_falls_back_to_single_cancels():
    gw, sent = _batch_gw(fail=ValueError("bad order in batch"))

    async def burst():
        return await asyncio.gather(*(gw.cancel("pm-a", f"o{i}", "no_edge") for i in range(3)))
    got = asyncio.run(burst())
    assert all(g == (True, None, True) for g in got) and sorted(sent) == [["o0"], ["o1"], ["o2"]]


def test_kalshi_cancels_are_forced_only_within_the_overdraw():
    from strategies.ARBS import execution
    calls, tokens = [], [-5.0]

    class C:
        shard = 0

        def allow_now(self, kind, force=False, reason=""):
            calls.append(force)
            return force or tokens[0] >= 2.0

        def tokens_now(self):
            return tokens[0]

        def cancel(self, oid, ticker):
            return True
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    asyncio.run(gw.cancel("KX-A", "o1", "pull"))
    assert calls == [False, True]                # short of tokens but within the overdraw: forced at once

    calls.clear()
    tokens[0] = -50.0                            # past it: waits for refill, then goes unforced

    async def refill():
        await asyncio.sleep(0.03)
        tokens[0] = 10.0

    async def both():
        return await asyncio.gather(gw.cancel("KX-A", "o1", "pull"), refill())
    (got, exc, sent), _ = asyncio.run(both())
    assert got is True and calls == [False, True]           # refused once, then sent once the bucket allows


def test_urgent_polymarket_cancels_go_now_within_the_overdraw(monkeypatch):
    from strategies.ARBS import execution
    monkeypatch.setattr(execution, "PM_CANCEL_WAIT_S", 5.0)
    calls, tokens, ip = [], [0.0], [None]

    class B:                                     # the per-IP bucket (tokens unless set apart)
        def tokens_now(self):
            return tokens[0] if ip[0] is None else ip[0]

    class C:
        shard = 100
        budget = B()

        def allow_now(self, kind, force=False, reason=""):
            calls.append(force)
            return force                         # never room without force

        def tokens_now(self):
            return tokens[0]

        def can_afford(self, kind="order", reserve=0.0):
            return tokens[0] >= 10 + reserve

        def cancel(self, oid, ticker):
            return True
    gw = execution.Gateway.__new__(execution.Gateway)
    gw.client = lambda t: C()
    gw.cancel_wait = {}

    async def write(fn, *a):
        return fn(*a)
    gw.write = write
    t0 = time.monotonic()
    for reason in ("pull", "no_edge", "move_refused", "2 broken baskets", "exit"):
        calls.clear()
        asyncio.run(gw.cancel("pm-a", "o1", reason))
        assert calls == [False, True], reason    # forced on the first refusal
    assert time.monotonic() - t0 < 2.5             # none waited its 5 s (loose: flaked at 1.0 under load)

    # Past the overdraw nothing is forced, however long it waits: forcing
    # each of a stop's 2,000 cancels drew 15,708 429s (11-16).
    monkeypatch.setattr(execution, "PM_CANCEL_WAIT_S", 0.05)
    for reason in ("pull", "not_hot"):
        calls.clear()
        tokens[0] = -60.0

        async def go():
            async def refill():
                await asyncio.sleep(0.3)
                tokens[0] = -10.0
            t = asyncio.get_running_loop().create_task(refill())
            t1 = time.monotonic()
            await gw.cancel("pm-a", "o1", reason)
            await t
            return time.monotonic() - t1
        assert asyncio.run(go()) >= 0.28, reason     # Windows' ~15 ms timer can wake the refill early
        assert calls[-1] is True and calls.count(True) == 1

    # This process's quoting cap spent but the per-IP bucket has room: a pull
    # goes at once, a routine cancel still waits its turn.
    tokens[0], ip[0] = -60.0, 100.0
    calls.clear()
    t1 = time.monotonic()
    asyncio.run(gw.cancel("pm-a", "o1", "pull"))
    assert calls == [False, True] and time.monotonic() - t1 < 0.04
    assert gw.cancel_wait["pull"][0] >= 1                  # held cancels are timed


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


def test_a_stop_takes_polymarket_quotes_down_with_one_cancel_all(pm_arbs):
    r, b = pm_arbs
    calls = []

    async def cancel_all(reason, raise_=False):
        calls.append(reason)
        await asyncio.sleep(0.05)
        return True
    r.pm_cancel_all = cancel_all

    async def go():
        assert r._pm_stop_all("2 broken baskets") and r._pm_stop_all("2 broken baskets")   # in flight: skip PM
        await asyncio.sleep(0.1)
        assert not r._pm_stop_all("2 broken baskets")      # done: leftovers one by one
        assert r._pm_stop_all("3 broken baskets")          # a new stop sends another
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert calls == ["2 broken baskets", "3 broken baskets"]


def test_polymarket_closed_market_reject_is_held_like_a_pause(pm_arbs):
    r, b = pm_arbs
    exc = Exception('400 Bad Request for https://api.polymarket.us/v1/orders: '
                    '{"code":9, "message":"This market is closed for trading.", "details":[]}')
    r._order_err(b.basket_id, "complete IOC pm-ev-a", exc, "pm-ev-a")
    assert r.order_errors == 0 and r.market_paused("pm-ev-a")


def _fut(ev, title, name, rule, bid=None, ask=None):
    return {"slug": f"tec-{ev}-{name}", "event": ev, "event_title": title, "category": "sports", "rules": rule,
            "sports_type": "SPORTS_MARKET_TYPE_FUTURE", "end": "2027-02-15T00:00:00Z", "bid": bid, "ask": ask}


def test_futures_event_is_traded_only_when_one_market_can_win():
    from strategies.ARBS import pm_scan
    split = "If X ties with one or more other players, Yes contract holders receive $1.00 divided by the number of tied players."
    assert pm_scan.exclusive([_fut("e", "NBA MVP", "a", split), _fut("e", "NBA MVP", "b", split)])
    assert pm_scan.exclusive([_fut("e", "AFC Champion", "a", "If Buffalo are the AFC Champions, Yes.")])
    # each named person pays in full; many nominees; a separate tie market
    assert not pm_scan.exclusive([_fut("e", "TIME Person of the Year 2026", "a",
                                       "If multiple persons are named, each such person will be considered.")])
    assert not pm_scan.exclusive([_fut("e", "Oscar Nominees: Best Director", "a", "If X is nominated, Yes.")])
    assert not pm_scan.exclusive([_fut("e", "2027 Ryder Cup Winner", "tie", "Yes if the 2027 Ryder Cup ends in a tie.")])
    assert not pm_scan.exclusive([_fut("e", "NBA Teams 50+ Regular Season Wins", "a", "")])
    ladder = [{**_fut("e", "Traffic through the Malacca Strait next week?", f"ge{n}",
                      f"Yes if the total number of transit calls ... is at least {n}."), "title": f"{n}+ Ships"}
              for n in (1450, 1500)]
    assert not pm_scan.exclusive(ladder)
    assert not pm_scan.exclusive([{**r, "title": ""} for r in ladder])        # the rule alone
    assert not pm_scan.exclusive([{**r, "rules": ""} for r in ladder])        # the title alone


def test_futures_scan_quotes_no_on_the_likeliest_like_kalshis_mecnet():
    from strategies.ARBS import pm_scan
    pm = {r["slug"]: r for r in (_fut("afc", "AFC Champion", "buf", "", 0.30, 0.32),
                                 _fut("afc", "AFC Champion", "kc", "", 0.40, 0.42),
                                 _fut("afc", "AFC Champion", "mia", "", 0.01, 0.02),
                                 _fut("afc", "AFC Champion", "ne", "", 0.10, 0.12))}
    doc = pm_scan.scan(pm, [], now=pd.Timestamp("2026-11-16", tz="UTC"), cross_venue=False, max_legs=3)
    (rel,) = doc["relationships"]
    assert rel["type"] == "mutually_exclusive_no" and rel["cluster"] == "pm:futures:sports"
    assert rel["expected_settlement_utc"].startswith("2027-02-15") and rel["live_from_utc"] is None
    got = {b["basket_id"]: (b["payout"], [l["ticker"] for l in b["legs"]]) for b in rel["baskets"]}
    assert got == {"mecnet:afc:no:buf+kc": (1, ["tec-afc-kc", "tec-afc-buf"]),
                   "mecnet:afc:no:buf+kc+ne": (2, ["tec-afc-kc", "tec-afc-buf", "tec-afc-ne"])}
    assert all(l["side"] == "no" for b in rel["baskets"] for l in b["legs"])


def test_replace_that_beats_the_modify_answer_still_confirms_the_move(pm_arbs, monkeypatch):
    from strategies.ARBS import pricing
    r, b = pm_arbs
    r.live = True
    monkeypatch.setattr(R, "PM_MODIFY_ECHO_S", 0.1)

    async def amend(*a):
        r._on_replace("o1", 0.42)                 # the stream echo, before the HTTP answer
        return {"order": {"order_id": "o1"}}, None, True
    r.gw.amend = amend
    plan = pricing.QuotePlan(leg=0, price=0.42, max_price=0.45, edge=0.03, others_cost=0.5)

    async def go():
        q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.40, size=5.0, cid="c",
                    edge=0.03, max_price=0.45, exchange_id="o1", pending=False)
        r.quotes[q.key] = q
        await r.move_quote(q, plan)
        await asyncio.sleep(0.2)
        return q
    q = asyncio.run(go())
    assert q.price == pytest.approx(0.42) and r.n.get("pm_moves_unconfirmed", 0) == 0
    assert "o1" not in r.pm_replaced


def test_a_feed_drop_pulls_only_that_venues_quotes(pm_arbs):
    r, b = pm_arbs                                   # legs: pm-ev-a NO, K-A YES
    r.live = True
    r.feed.kalshi._tickers, r.feed.pm._tickers = ["K-A"], ["pm-ev-a"]
    r.feed.kalshi.up, r.feed.kalshi.down_since = False, 0.0
    r.feed.pm.up, r.feed.pm.down_since = True, None
    assert r.feed.down_venues(5.0, 100.0) == {"kalshi"}
    assert not r.feed.up_for(["K-A"]) and r.feed.up_for(["pm-ev-a"])
    assert r.blocked() is None                       # not a stop for every basket
    for leg, t in ((0, "pm-ev-a"), (1, "K-A")):
        q = R.Quote(basket_id=b.basket_id, leg=leg, ticker=t, side="no", price=0.4, size=5.0, cid=f"c{leg}",
                    edge=0.03, max_price=0.45, exchange_id=f"o{leg}", pending=False)
        r.quotes[q.key] = q
    pulled, swept = [], []

    async def cancel_quote(q, reason):
        pulled.append(q.ticker)
        r._retire(q)

    async def pm_cancel_all(reason, raise_=False):
        raise AssertionError("polymarket quotes pulled for a kalshi drop")

    async def sweep(reason, venues=None):
        swept.append(venues)
    r.cancel_quote, r.pm_cancel_all, r.sweep = cancel_quote, pm_cancel_all, sweep
    asyncio.run(r.pull_all("feed_down", venues={"kalshi"}))
    assert pulled == ["K-A"] and swept == [{"kalshi"}]
    assert [q.ticker for q in r.quotes.values()] == ["pm-ev-a"]


def test_a_polymarket_feed_drop_takes_its_quotes_down_with_one_cancel_all(pm_arbs):
    r, b = pm_arbs
    r.live = True
    r.feed.kalshi._tickers, r.feed.pm._tickers = ["K-A"], ["pm-ev-a"]
    r.feed.kalshi.up, r.feed.pm.up = True, False
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.4, size=5.0, cid="c",
                edge=0.03, max_price=0.45, exchange_id="o1", pending=False)
    r.quotes[q.key] = q
    stops, cancels = [], []
    r._pm_stop_all = lambda reason: stops.append(reason) or True

    async def cancel_quotes(qs, reason):
        cancels.append(reason)
    r.cancel_quotes = cancel_quotes

    async def go():
        r.evaluate(b)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert stops == ["feed_down"] and cancels == []


def test_broken_basket_stop_counts_only_baskets_broken_30s(pm_arbs):
    import types
    r, b = pm_arbs
    r.feed.kalshi._tickers, r.feed.pm._tickers = ["K-A"], ["pm-ev-a"]
    r.feed.up = True
    now = time.time()
    r.ledger.baskets = {"x": types.SimpleNamespace(broken_since=now - 300), "y": types.SimpleNamespace(broken_since=now - 1)}
    assert r.blocked() is None                       # y is mid-exit
    r.ledger.baskets["y"].broken_since = now - R.BROKEN_STOP_S - 1
    assert r.blocked() == "2 broken baskets"


def test_a_basket_with_a_resting_quote_is_evaluated_before_quoteless_ones(pm_arbs):
    r, b = pm_arbs
    b2 = Basket(basket_id="cross:K-B:pm_no", relationship_id="cross:K-B", type="cross_venue", cluster="cross:soccer",
                legs=(Leg("pm-ev-b", "no"), Leg("K-B", "yes")), payout=1.0, quote_until=None, resume_quoting=None)
    r.set_baskets([b, b2])
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.4, size=5.0, cid="c",
                edge=0.03, max_price=0.45, exchange_id="o1", pending=False)
    r.quotes[q.key] = q
    assert r.quotes.n_by_basket == {b.basket_id: 1}
    order = []
    r.evaluate = lambda bk: order.append(bk.basket_id)

    async def go():
        r.dirty.clear()
        for _ in range(3):
            r.mark(b2.basket_id)
        r.mark(b.basket_id)                         # marked last, evaluated first
        t = asyncio.get_running_loop().create_task(r.eval_loop())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())
    assert order[:2] == [b.basket_id, b2.basket_id]
    r.quotes.pop(q.key)
    assert r.quotes.n_by_basket == {}


def _pending_409():
    import requests
    resp = requests.Response()
    resp.status_code = 409
    return requests.HTTPError('409 Conflict: {"code":10,"message":"Order pending"}', response=resp)


def test_lost_polymarket_create_waits_out_order_pending(pm_arbs, monkeypatch):
    r, b = pm_arbs
    r.live = True
    monkeypatch.setattr(R, "LOST_LOOKUP_S", 0.0)
    monkeypatch.setattr(R, "PM_LOST_RETRY_S", 0.0)
    answers = [_pending_409(), _pending_409(), None]

    async def call(fn, *a):
        x = answers.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    r.gw.call = call
    r.gw.client = lambda t: type("C", (), {"find_by_client_id": None})()
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.4, size=5.0, cid="c1",
                edge=0.03, max_price=0.45)
    r.quotes[q.key] = q
    asyncio.run(r._find_lost(q))
    assert r.halt is None and q.key not in r.quotes and answers == []      # resolved as never placed


def test_unresolved_lost_polymarket_create_cancels_all_instead_of_halting(pm_arbs, monkeypatch):
    r, b = pm_arbs
    r.live = True
    monkeypatch.setattr(R, "LOST_LOOKUP_S", 0.0)
    monkeypatch.setattr(R, "PM_LOST_RETRY_S", 0.0)

    async def call(fn, *a):
        raise _pending_409()
    r.gw.call = call
    r.gw.client = lambda t: type("C", (), {"find_by_client_id": None})()
    swept = []

    async def cancel_all(reason, raise_=False):
        swept.append(reason)
        return True
    r.pm_cancel_all = cancel_all
    q = R.Quote(basket_id=b.basket_id, leg=0, ticker="pm-ev-a", side="no", price=0.4, size=5.0, cid="c1",
                edge=0.03, max_price=0.45)
    r.quotes[q.key] = q
    asyncio.run(r._find_lost(q))
    assert r.halt is None and swept == ["lost quote on pm-ev-a"] and q.key not in r.quotes


def test_cross_gaps_are_journaled_from_both_tops(pm_arbs, monkeypatch):
    import types
    r, b = pm_arbs                                   # cross:K-A:pm_no, legs pm-ev-a NO, K-A YES
    assert r.cross_of.get("K-A") == [("K-A", "pm-ev-a")] and r.cross_of.get("pm-ev-a") == [("K-A", "pm-ev-a")]
    rows = []
    monkeypatch.setattr(r, "journal", lambda row: rows.append(row))
    for t in ("K-A", "pm-ev-a"):
        r.meta[t] = R.Meta(ticker=t, event="e", status="active", shard=100 if is_pm(t) else 0, taker_rate=0.0,
                           maker_rate=0.0)
    # K YES ask 0.40, PM YES bid 0.55 -> PM NO ask 0.45: K YES + PM NO = 0.85, a 15c gap with no fees
    r._on_quote(types.SimpleNamespace(ticker="K-A", bid=0.38, ask=0.40, bid_depth=5.0, ask_depth=7.0))
    r._on_quote(types.SimpleNamespace(ticker="pm-ev-a", bid=0.55, ask=0.57, bid_depth=9.0, ask_depth=3.0))
    gaps = [x for x in rows if x["kind"] == "cross_gap"]
    assert len(gaps) == 1 and gaps[0]["dir"] == "k_yes+pm_no" and gaps[0]["edge"] == pytest.approx(0.15)
    assert gaps[0]["k_size"] == 7.0 and gaps[0]["pm_size"] == 9.0
    r._on_quote(types.SimpleNamespace(ticker="pm-ev-a", bid=0.55, ask=0.57, bid_depth=9.0, ask_depth=3.0))
    assert len([x for x in rows if x["kind"] == "cross_gap"]) == 1          # same edge within a second: once


def test_replay_reads_polymarket_legs_off_their_prefixed_slugs():
    from strategies.ARBS import replay
    ev = "unl-bih-rou-2026-11-17"
    books = {f"atc-{ev}-bih": {}, f"atc-{ev}-draw": {}, f"atc-{ev}-rou": {}, "KXUEFANLGAME-26NOV17BIHROU-BIH": {},
             f"atc-{ev}-dh1-bih": {}}
    # home/away are the row's own market and the event's other team
    assert sorted(replay.legs_of(f"mecnet:{ev}:no:draw+home", books, f"atc-{ev}-bih")) == \
        [(f"atc-{ev}-bih", "no"), (f"atc-{ev}-draw", "no")]
    assert sorted(replay.legs_of(f"mecnet:{ev}:no:away+home", books, f"atc-{ev}-rou")) == \
        [(f"atc-{ev}-bih", "no"), (f"atc-{ev}-rou", "no")]
    # a complete set leaves the half-game markets out
    assert sorted(t for t, _ in replay.legs_of(f"complete_set:{ev}:buy_all_yes", books)) == \
        [f"atc-{ev}-bih", f"atc-{ev}-draw", f"atc-{ev}-rou"]
    # Kalshi ids read as before
    assert replay.legs_of("mecnet:KXE-1:no:A+B", {"KXE-1-A": {}, "KXE-1-B": {}}) == [("KXE-1-A", "no"), ("KXE-1-B", "no")]


def test_polymarket_trains_apart_from_kalshi(tmp_path):
    from strategies.ARBS import shadow_check
    for s in ("ARBS", "ARBS0B", "ARBSPM"):
        (tmp_path / s).mkdir()
    k = [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in shadow_check.family_roots(str(tmp_path / "ARBS"))]
    pm = [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in shadow_check.family_roots(str(tmp_path / "ARBSPM"))]
    assert "ARBSPM" not in k and "ARBS0B" in k and pm == ["ARBSPM"]


def test_pm_deep_quotes_are_reach_only_and_switched_off(pm_arbs, monkeypatch):
    r, b = pm_arbs
    monkeypatch.setattr(R, "PM_REACH", False)
    cancelled = []
    monkeypatch.setattr(r, "cancel_quote", lambda q, why: cancelled.append((q.ticker, why)))
    for j, t in enumerate(("pm-ev-a", "K-A")):
        q = R.Quote(basket_id=b.basket_id, leg=j, ticker=t, side=b.legs[j].side, price=0.2, size=5.0, cid=f"c{j}",
                    edge=0.05, max_price=0.25, pending=False, deep=True)
        q.reach = True
        r.quotes[q.key] = q
    r.manage_deep(b, None, {}, 0.0, time.time(), True, False)
    # off: the PM leg's reach quote comes down; a Kalshi leg never holds one from this process
    assert sorted(cancelled) == [("K-A", "venue"), ("pm-ev-a", "deep_off")]
