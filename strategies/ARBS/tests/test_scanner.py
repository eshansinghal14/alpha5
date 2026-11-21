"""Scanner rules on hand-built events (no network)."""
import json

import pandas as pd

from strategies.ARBS import scanner
from strategies.ARBS.spec import Basket, Leg, parse_spec

NOW = pd.Timestamp("2026-10-28", tz="UTC")


def mkt(ticker, yes=0.5, vol=10, **kw):
    return {"ticker": ticker, "status": "active", "market_type": "binary", "yes_bid_dollars": f"{yes - 0.01:.2f}",
            "yes_ask_dollars": f"{yes + 0.01:.2f}", "volume_24h_fp": str(vol),
            "expected_expiration_time": "2026-11-15T00:00:00Z", **kw}


def event(et, crt, markets, series=None, shard=0):
    return {"event_ticker": et, "series_ticker": series or et.split("-")[0], "collateral_return_type": crt,
            "exchange_index": shard, "markets": markets}


def run(events, days=90, max_markets=1000, known_tickers=frozenset(), held=frozenset(), exclude=frozenset(),
        **kw):
    """scan then select, as the runner sees it."""
    args = dict(now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={})
    doc = scanner.scan(events, **{**args, **kw})
    return scanner.select(doc, now=NOW, days=days, max_markets=max_markets, known_tickers=known_tickers,
                          held=held, exclude=exclude)


def baskets(doc):
    return {b.basket_id: b for b in parse_spec(doc)}


def buckets(et):
    return [mkt(f"{et}-T1", 0.2, strike_type="less", cap_strike=1),
            mkt(f"{et}-B2", 0.5, strike_type="between", floor_strike=1, cap_strike=2),
            mkt(f"{et}-T2", 0.3, strike_type="greater", floor_strike=2)]


def test_mecnet_lists_no_on_the_likeliest_k():
    e = event("EV-1", "MECNET", [mkt("EV-1-A", 0.1), mkt("EV-1-B", 0.6), mkt("EV-1-C", 0.3)])
    bs = baskets(run([e]))
    assert set(bs) == {"mecnet:EV-1:no:B+C", "mecnet:EV-1:no:A+B+C"}
    b = bs["mecnet:EV-1:no:A+B+C"]
    assert b.payout == 2 and {l.side for l in b.legs} == {"no"}


def test_ladder_pairs_strikes_within_span():
    ms = [mkt(f"L-1-T{k}", strike_type="greater", floor_strike=k) for k in range(5)]
    ms.append(mkt("L-1-X", strike_type="greater_or_equal", floor_strike=2))
    bs = baskets(run([event("L-1", "DIRECNET", ms)], ladder_span=2))
    b = bs["ladder:L-1:T2=>T0"]
    assert b.payout == 1 and b.legs == (Leg("L-1-T0", "yes"), Leg("L-1-T2", "no"))
    assert "ladder:L-1:T3=>T0" not in bs              # 3 rungs up, past the span
    assert not any("T2=>X" in k or "X=>T2" in k for k in bs)   # equal strikes never paired


def test_less_ladder_runs_downward():
    ms = [mkt(f"D-1-T{k}", strike_type="less", cap_strike=k) for k in (1, 2)]
    b = baskets(run([event("D-1", "DIRECNET", ms)]))["ladder:D-1:T1=>T2"]
    # below 1 implies below 2: YES <2 + NO <1 pays at least 1.
    assert b.legs == (Leg("D-1-T2", "yes"), Leg("D-1-T1", "no"))


def test_covering_buckets_trade_as_complete_set_unless_excluded():
    e = event("BK-1", "MECNET", buckets("BK-1"))
    bs = baskets(run([e]))
    assert bs["complete_set:BK-1:buy_all_yes"].payout == 1 and bs["complete_set:BK-1:buy_all_no"].payout == 2
    assert not any(b.type == "complete_set" for b in parse_spec(run([e], complete_set_exclude=["BK"])))


def test_coverage_rejects_overlap_and_missing_tail():
    ms = buckets("BK-1")
    ms[1]["floor_strike"] = 0.5                       # overlaps the lower tail
    assert scanner.coverage_step(ms) is None
    assert scanner.coverage_step(buckets("BK-1")[1:]) is None
    ms = buckets("BK-1")
    ms[1]["floor_strike"] = 1.01                      # reported to the cent
    assert abs(scanner.coverage_step(ms) - 0.01) < 1e-9


def test_filters_and_market_budget():
    far = event("FAR-1", "MECNET", [mkt("FAR-1-A"), mkt("FAR-1-B", expected_expiration_time="2027-06-01T00:00:00Z")])
    mine = event("KXINXU-1", "DIRECNET", [mkt("KXINXU-1-T1", strike_type="greater", floor_strike=1),
                                         mkt("KXINXU-1-T2", strike_type="greater", floor_strike=2)])
    big = event("BIG-1", "MECNET", [mkt("BIG-1-A", vol=500), mkt("BIG-1-B", vol=500)])
    other = event("OS-1", "MECNET", [mkt("OS-1-A", vol=900), mkt("OS-1-B", vol=900)], shard=3)
    small = event("SM-1", "MECNET", [mkt("SM-1-A", vol=1), mkt("SM-1-B", vol=1), mkt("SM-1-C", vol=1)])
    doc = run([far, mine, other, small, big], max_markets=4, skip_series={"KXINXU"})
    # FAR settles past 90 days, KXINXU belongs to another strategy, OS is on an
    # unfunded shard, and SM's 3 markets don't fit beside BIG's 2.
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:BIG-1"}
    doc = run([far, small, big], max_markets=4, known_tickers={"SM-1-A", "SM-1-B"})
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:BIG-1", "mecnet:SM-1"}
    doc = run([far, small, big], max_markets=4, exclude={"BIG"})
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:SM-1"}


def test_budget_goes_by_volume_per_market():
    # LAD trades more in total (1,200 vs 600) but less per market (200 vs 300).
    lad = event("LAD-1", "MECNET", [mkt(f"LAD-1-{c}", vol=200) for c in "ABCDEF"])
    game = event("G-1", "MECNET", [mkt("G-1-A", vol=300), mkt("G-1-B", vol=300)])
    doc = run([lad, game], max_markets=6)
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:G-1"}


def test_scan_lists_everything_and_select_keeps_held():
    far = event("FAR-1", "MECNET", [mkt("FAR-1-A"), mkt("FAR-1-B", expected_expiration_time="2027-06-01T00:00:00Z")])
    big = event("BIG-1", "MECNET", [mkt("BIG-1-A", vol=500), mkt("BIG-1-B", vol=500)])
    doc = scanner.scan([far, big], now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[],
                       overrides={})
    assert doc["n_markets"] == 4 and [r["volume_24h"] for r in doc["relationships"]] == [20.0, 1000.0]
    got = scanner.select(doc, now=NOW, days=90, max_markets=0, held={"mecnet:FAR-1:no:A+B"})
    assert [r["relationship_id"] for r in got["relationships"]] == ["mecnet:FAR-1"]
    got = scanner.select(doc, now=NOW, days=90, max_markets=None)              # no count: every market
    assert [r["relationship_id"] for r in got["relationships"]] == ["mecnet:BIG-1"]   # FAR settles past 90 days
    got = scanner.select(doc, now=NOW, days=None, max_markets=None)            # no horizon: FAR too
    assert sorted(r["relationship_id"] for r in got["relationships"]) == ["mecnet:BIG-1", "mecnet:FAR-1"]


def test_scan_lists_only_the_events_its_process_owns():
    evs = [event(f"EV{i}-1", "MECNET", [mkt(f"EV{i}-1-A"), mkt(f"EV{i}-1-B")]) for i in range(6)]
    args = dict(now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={})
    got = scanner.scan(evs, owns=lambda ev: ev in ("EV1-1", "EV4-1"), **args)
    assert {r["relationship_id"] for r in got["relationships"]} == {"mecnet:EV1-1", "mecnet:EV4-1"}


def test_series_override_sets_settlement():
    e = event("KXMIDTERMMOV-X", "DIRECNET", [mkt("KXMIDTERMMOV-X-P1", strike_type="greater", floor_strike=1,
                                                 expected_expiration_time="2027-11-03T15:00:00Z"),
                                             mkt("KXMIDTERMMOV-X-P2", strike_type="greater", floor_strike=2)])
    assert not run([e])["relationships"]
    b = next(iter(baskets(run([e], overrides={"KXMIDTERMMOV": "2026-12"})).values()))
    assert b.settles == pd.Timestamp("2026-12-31 23:59:59.999999999", tz="UTC")


def test_run_carries_over_held_relationships(tmp_path, monkeypatch):
    from strategies.ARBS.config import ArbsConfig
    from infra.config import ConfigStore
    from strategies.ARBS.config import CONFIG_PATH
    cfg = ConfigStore(ArbsConfig, CONFIG_PATH, None).config
    spec = str(tmp_path / "s.json")
    monkeypatch.setattr(scanner, "other_series", lambda: set())
    monkeypatch.setattr(scanner, "fetch_starts", lambda now: {})
    monkeypatch.setattr(scanner, "fetch_frequencies", lambda: {})
    monkeypatch.setattr(scanner, "fetch_events",
                        lambda: [event("EV-1", "MECNET", [mkt("EV-1-A"), mkt("EV-1-B")])])
    scanner.run(cfg, spec)
    monkeypatch.setattr(scanner, "fetch_events", lambda: [])
    scanner.run(cfg, spec, held={"mecnet:EV-1:no:A+B"})
    with open(spec) as fh:
        assert [r["relationship_id"] for r in json.load(fh)["relationships"]] == ["mecnet:EV-1"]
    scanner.run(cfg, spec)
    with open(spec) as fh:
        assert json.load(fh)["relationships"] == []


def test_quoting_stops_at_kickoff():
    # occurrence_datetime is the game's expected end; the milestone start is kickoff.
    e = event("GM-1", "MECNET", [mkt("GM-1-A", occurrence_datetime="2026-10-28T22:00:00Z"),
                                 mkt("GM-1-B", occurrence_datetime="2026-10-28T22:00:00Z")])
    b = next(iter(baskets(run([e], starts={"GM-1": "2026-10-28T20:00:00Z"})).values()))
    assert not b.is_live(pd.Timestamp("2026-10-28T19:59:00Z"))
    assert b.is_live(pd.Timestamp("2026-10-28T20:00:00Z"))
    b = next(iter(baskets(run([e])).values()))
    assert b.is_live(pd.Timestamp("2026-10-28T22:00:00Z"))


def test_starts_include_esports(monkeypatch):
    page = {"milestones": [
        {"category": "Esports", "start_date": "2026-10-29T10:20:00Z", "related_event_tickers": ["KXCS2MAP-X-1"]},
        {"category": "Elections", "start_date": "2020-01-01T00:00:00Z", "related_event_tickers": ["KXHOUSE-X"]}]}
    monkeypatch.setattr(scanner, "kalshi_get", lambda path, params: page)
    assert scanner.fetch_starts(pd.Timestamp("2026-10-29", tz="UTC")) == {"KXCS2MAP-X-1": "2026-10-29T10:20:00Z"}


def test_daily_series_live_from_the_start_of_its_day():
    e = event("KXHIGHNY-26OCT30", "MECNET", [mkt("KXHIGHNY-26OCT30-A", occurrence_datetime="2026-10-31T14:00:00Z"),
                                             mkt("KXHIGHNY-26OCT30-B", occurrence_datetime="2026-10-31T14:00:00Z")],
              series="KXHIGHNY") | {"strike_date": "2026-10-31T05:00:00Z"}
    b = next(iter(baskets(run([e], freqs={"KXHIGHNY": "daily"})).values()))
    assert not b.is_live(pd.Timestamp("2026-10-30T04:59:00Z"))
    assert b.is_live(pd.Timestamp("2026-10-30T05:00:00Z"))          # its measurement day began
    b = next(iter(baskets(run([e], freqs={"KXHIGHNY": "one_off"})).values()))
    assert not b.is_live(pd.Timestamp("2026-10-30T20:00:00Z"))      # not a daily series: occurrence only


def test_daily_rule_skips_events_with_a_kickoff():
    e = event("KXCS2MAP-26OCT31X-1", "MECNET", [mkt("KXCS2MAP-26OCT31X-1-A", occurrence_datetime="2026-10-31T22:00:00Z"),
                                                mkt("KXCS2MAP-26OCT31X-1-B", occurrence_datetime="2026-10-31T22:00:00Z")],
              series="KXCS2MAP") | {"strike_date": "2026-11-01T04:00:00Z"}
    b = next(iter(baskets(run([e], freqs={"KXCS2MAP": "daily"},
                              starts={"KXCS2MAP-26OCT31X-1": "2026-10-31T20:00:00Z"})).values()))
    assert not b.is_live(pd.Timestamp("2026-10-31T12:00:00Z"))       # not from the day's start
    assert b.is_live(pd.Timestamp("2026-10-31T20:00:00Z"))           # from kickoff



def test_live_before_settlement_unless_a_kickoff():
    lead = pd.Timedelta(hours=6)
    news = event("KXPEAK-26NOV08", "DIRECNET", [mkt("KXPEAK-26NOV08-T6", strike_type="greater", floor_strike=6),
                                                mkt("KXPEAK-26NOV08-T7", strike_type="greater", floor_strike=7)])
    b = next(iter(baskets(run([news], settle_lead=lead)).values()))
    assert not b.is_live(pd.Timestamp("2026-11-14T17:59:00Z"))
    assert b.is_live(pd.Timestamp("2026-11-14T18:00:00Z"))          # 6h before 11-15 00:00
    assert not next(iter(baskets(run([news])).values())).is_live(pd.Timestamp("2026-11-14T23:00:00Z"))
    game = event("GM-1", "MECNET", [mkt("GM-1-A"), mkt("GM-1-B")])
    b = next(iter(baskets(run([game], settle_lead=lead, starts={"GM-1": "2026-11-14T20:00:00Z"})).values()))
    assert not b.is_live(pd.Timestamp("2026-11-14T19:00:00Z"))      # kickoff decides, not settlement


def test_live_early_by_series():
    e = event("KXITFMATCH-26NOV12AB", "MECNET", [mkt("KXITFMATCH-26NOV12AB-A"), mkt("KXITFMATCH-26NOV12AB-B")])
    start = {"KXITFMATCH-26NOV12AB": "2026-11-12T15:43:00Z"}
    b = next(iter(baskets(run([e], starts=start, early_h={"KXITFMATCH": 2.0})).values()))
    assert b.is_live(pd.Timestamp("2026-11-12T13:43:00Z")) and not b.is_live(pd.Timestamp("2026-11-12T13:42:00Z"))
    b = next(iter(baskets(run([e], starts=start, early_h={"KXATPMATCH": 2.0})).values()))
    assert not b.is_live(pd.Timestamp("2026-11-12T15:24:00Z"))


def test_live_window_by_category():
    win = {"Elections": ["2026-11-03T22:00:00Z", "2026-11-06T00:00:00Z"]}
    vote = event("SENATEX-26", "MECNET", buckets("SENATEX-26")) | {"category": "Elections"}
    game = event("KXGAME-26", "MECNET", buckets("KXGAME-26")) | {"category": "Sports"}
    args = dict(mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={}, live_windows=win)
    got = baskets(scanner.scan([vote, game], now=NOW, **args))
    night = pd.Timestamp("2026-11-03T23:00:00Z")
    assert all(b.is_live(night) == ("SENATEX" in bid) for bid, b in got.items())
    assert all(not b.is_live(pd.Timestamp("2026-11-03T21:00:00Z")) for b in got.values())
    later = baskets(scanner.scan([vote], now=pd.Timestamp("2026-11-07", tz="UTC"), **args))
    assert not any(b.is_live(pd.Timestamp("2026-11-08", tz="UTC")) for b in later.values())


def test_cheap_events_do_not_count_against_the_market_cap_but_do_against_the_total():
    a = event("A-1", "MECNET", [mkt("A-1-X", vol=100), mkt("A-1-Y", vol=100)])
    b = event("B-1", "MECNET", [mkt("B-1-X", vol=10), mkt("B-1-Y", vol=10)])
    c = event("C-1", "MECNET", [mkt("C-1-X", vol=1), mkt("C-1-Y", vol=1)])
    doc = scanner.scan([a, b, c], now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={})
    ids = lambda got: sorted(r["relationship_id"] for r in got["relationships"])  # noqa: E731
    assert ids(scanner.select(doc, now=NOW, days=90, max_markets=2)) == ["mecnet:A-1"]
    got = scanner.select(doc, now=NOW, days=90, max_markets=2, cheap={"A-1", "C-1"})
    assert ids(got) == ["mecnet:A-1", "mecnet:B-1", "mecnet:C-1"]          # only B's 2 markets count
    got = scanner.select(doc, now=NOW, days=90, max_markets=2, cheap={"A-1", "C-1"}, max_total=4)
    assert ids(got) == ["mecnet:A-1", "mecnet:B-1"]                         # 6 markets in all is past 4


def test_a_held_basket_past_the_market_cap_does_not_shut_out_cheap_events():
    a = event("A-1", "MECNET", [mkt("A-1-X", vol=100), mkt("A-1-Y", vol=100)])
    b = event("B-1", "MECNET", [mkt("B-1-X", vol=10), mkt("B-1-Y", vol=10)])
    c = event("C-1", "MECNET", [mkt("C-1-X", vol=1), mkt("C-1-Y", vol=1)])
    doc = scanner.scan([a, b, c], now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={})
    held = {next(r for r in doc["relationships"] if r["relationship_id"] == "mecnet:B-1")["baskets"][0]["basket_id"]}
    got = scanner.select(doc, now=NOW, days=90, max_markets=2, cheap={"C-1"}, held=held)
    assert sorted(r["relationship_id"] for r in got["relationships"]) == ["mecnet:A-1", "mecnet:B-1", "mecnet:C-1"]


def test_cheap_events_are_those_under_the_cpu_bar_every_recent_day(tmp_path):
    for p, by_day in (("P1", {"2026-11-18": {"A-1": 2.0, "B-1": 3.0}, "2026-11-19": {"A-1": 1.0, "B-1": 40.0}}),
                      ("P2", {"2026-11-19": {"A-1": 8.5, "D-1": 0.5}})):
        (tmp_path / p).mkdir()
        (tmp_path / p / scanner.parts.CPU).write_text(json.dumps(by_day))
    got = scanner.cheap_events(("P1", "P2"), root=str(tmp_path))
    # A: 9.5 s on 11-19 across both, under 10; B: 40 s once; D: under; E: never timed.
    assert got == {"A-1", "D-1"}


def test_select_skips_an_excluded_event():
    a = event("A-1", "MECNET", [mkt("A-1-X"), mkt("A-1-Y")])
    b = event("B-1", "MECNET", [mkt("B-1-X"), mkt("B-1-Y")])
    doc = scanner.scan([a, b], now=NOW, mecnet_max_legs=6, ladder_span=3, complete_set_exclude=[], overrides={})
    got = scanner.select(doc, now=NOW, days=90, max_markets=1000, exclude={"A-1"})
    assert [r["relationship_id"] for r in got["relationships"]] == ["mecnet:B-1"]


def _spread_event(shard=3):
    ms = [mkt(f"NBS-1-{t}{k}", 0.5, strike_type="greater", floor_strike=k - 0.5) for t in ("PHX", "UTA") for k in (2, 5, 8, 11)]
    return {**event("NBS-1", "", ms, series="KXNBASPREAD", shard=shard), "category": "Sports"}


def test_spreads_are_ladders_per_team_and_both_teams_covering_is_impossible():
    bs = baskets(run([_spread_event()], shards={3}))
    assert bs["spread:NBS-1:PHX8=>PHX2"].legs == (Leg("NBS-1-PHX2", "yes"), Leg("NBS-1-PHX8", "no"))
    assert not any("PHX" in k and "UTA" in k and "=>" in k for k in bs)        # no ladder across teams
    no = bs["spread:NBS-1:no:PHX2+UTA2"]
    assert no.payout == 1 and {l.side for l in no.legs} == {"no"}
    assert "spread:NBS-1:no:PHX11+UTA11" not in bs                             # past SPREAD_CROSS


def test_spreads_only_on_spread_shards_and_after_traded_markets_in_the_budget():
    assert not baskets(run([_spread_event(shard=0)], shards={0}))
    traded = event("EV-1", "MECNET", [mkt("EV-1-A", 0.4, vol=1), mkt("EV-1-B", 0.5, vol=1)], shard=3)
    sel = run([_spread_event(), traded], shards={3}, max_markets=2)
    assert [r["relationship_id"] for r in sel["relationships"]] == ["mecnet:EV-1"]


def test_runner_refuses_the_prefix_the_scanner_lists_spreads_under():
    from strategies.ARBS import runner
    assert runner.SPREAD_PREFIX == scanner.SPREAD_PREFIX and runner.RECORD_PREFIXES == scanner.RECORD_PREFIXES


def _totals(shard=3, exp="2026-11-15T00:00:00Z"):
    tail = "originally scheduled for Nov 14, 2026, then the market resolves to Yes."
    return [{**event(et, "", [mkt(f"{et}-{k}", 0.5, strike_type="greater", floor_strike=k - 0.5, expected_expiration_time=exp,
                                  rules_primary=f"If over {k - 0.5} total points are scored in the A vs B game {tail}")
                              for k in ks], shard=shard), "category": "Sports"}
            for et, ks in (("KXNBATOTAL-1", (200, 202)), ("KXNBATOTALPRECISE-1", (201, 202)))]


def test_unnetted_baskets_keep_to_locked_days_and_record_shards():
    def watched(doc):
        return [k for k in baskets(doc) if k.startswith(scanner.RECORD_PREFIXES)]
    assert {k.split(":")[0] for k in watched(run(_totals(), shards={3}, days=None, locked_days=120.0))}         == {"spread", "watch"}
    late = (NOW + pd.Timedelta(days=121)).isoformat()
    assert not watched(run(_totals(exp=late), shards={3}, days=None, locked_days=120.0))
    assert watched(run(_totals(exp=late), shards={3}, days=None, locked_days=None))
    assert not watched(run(_totals(shard=0), shards={0}, days=None, locked_days=120.0))
