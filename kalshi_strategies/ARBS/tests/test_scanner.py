"""Scanner rules on hand-built events (no network)."""
import json

import pandas as pd

from kalshi_strategies.ARBS import scanner
from kalshi_strategies.ARBS.spec import Basket, Leg, merge, parse_spec

NOW = pd.Timestamp("2026-10-28", tz="UTC")


def mkt(ticker, yes=0.5, vol=10, **kw):
    return {"ticker": ticker, "status": "active", "market_type": "binary", "yes_bid_dollars": f"{yes - 0.01:.2f}",
            "yes_ask_dollars": f"{yes + 0.01:.2f}", "volume_24h_fp": str(vol),
            "expected_expiration_time": "2026-11-15T00:00:00Z", **kw}


def event(et, crt, markets, series=None, shard=0):
    return {"event_ticker": et, "series_ticker": series or et.split("-")[0], "collateral_return_type": crt,
            "exchange_index": shard, "markets": markets}


def run(events, **kw):
    args = dict(now=NOW, days=90, max_markets=1000, mecnet_max_legs=6, ladder_span=3, complete_set_series=[],
                overrides={})
    return scanner.scan(events, **{**args, **kw})


def baskets(doc):
    return {b.basket_id: b for b in parse_spec(doc)}


def buckets(et):
    return [mkt(f"{et}-T1", 0.2, strike_type="less", cap_strike=1),
            mkt(f"{et}-B2", 0.5, strike_type="between", floor_strike=1, cap_strike=2),
            mkt(f"{et}-T2", 0.3, strike_type="greater", floor_strike=2)]


def test_mecnet_lists_no_on_the_likeliest_k():
    e = event("EV-1", "MECNET", [mkt("EV-1-A", 0.1), mkt("EV-1-B", 0.6), mkt("EV-1-C", 0.3)])
    bs = baskets(run([e])[0])
    assert set(bs) == {"mecnet:EV-1:no:B+C", "mecnet:EV-1:no:B+C+A"}
    b = bs["mecnet:EV-1:no:B+C+A"]
    assert b.payout == 2 and {l.side for l in b.legs} == {"no"}


def test_ladder_pairs_strikes_within_span():
    ms = [mkt(f"L-1-T{k}", strike_type="greater", floor_strike=k) for k in range(5)]
    ms.append(mkt("L-1-X", strike_type="greater_or_equal", floor_strike=2))
    bs = baskets(run([event("L-1", "DIRECNET", ms)], ladder_span=2)[0])
    b = bs["ladder:L-1:T2=>T0"]
    assert b.payout == 1 and b.legs == (Leg("L-1-T0", "yes"), Leg("L-1-T2", "no"))
    assert "ladder:L-1:T3=>T0" not in bs              # 3 rungs up, past the span
    assert not any("T2=>X" in k or "X=>T2" in k for k in bs)   # equal strikes never paired


def test_less_ladder_runs_downward():
    ms = [mkt(f"D-1-T{k}", strike_type="less", cap_strike=k) for k in (1, 2)]
    b = baskets(run([event("D-1", "DIRECNET", ms)])[0])["ladder:D-1:T1=>T2"]
    # below 1 implies below 2: YES <2 + NO <1 pays at least 1.
    assert b.legs == (Leg("D-1-T2", "yes"), Leg("D-1-T1", "no"))


def test_complete_set_only_once_series_approved():
    e = event("BK-1", "MECNET", buckets("BK-1"))
    doc, review = run([e])
    assert "BK" in review["series"] and review["series"]["BK"]["max_gap"] == 0
    assert not any(b.type == "complete_set" for b in parse_spec(doc))
    doc, review = run([e], complete_set_series=["BK"])
    bs = baskets(doc)
    assert bs["complete_set:BK-1:buy_all_yes"].payout == 1 and bs["complete_set:BK-1:buy_all_no"].payout == 2
    assert not review["series"]


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
    doc, _ = run([far, mine, other, small, big], max_markets=4, skip_series={"KXINXU"})
    # FAR settles past 90 days, KXINXU belongs to another strategy, OS is on an
    # unfunded shard, and SM's 3 markets don't fit beside BIG's 2.
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:BIG-1"}
    doc, _ = run([far, small, big], max_markets=4, known_tickers={"SM-1-A", "SM-1-B"})
    assert {b.relationship_id for b in parse_spec(doc)} == {"mecnet:BIG-1", "mecnet:SM-1"}


def test_series_override_sets_settlement():
    e = event("KXMIDTERMMOV-X", "DIRECNET", [mkt("KXMIDTERMMOV-X-P1", strike_type="greater", floor_strike=1,
                                                 expected_expiration_time="2027-11-03T15:00:00Z"),
                                             mkt("KXMIDTERMMOV-X-P2", strike_type="greater", floor_strike=2)])
    assert not run([e])[0]["relationships"]
    b = next(iter(baskets(run([e], overrides={"KXMIDTERMMOV": "2026-12"})[0]).values()))
    assert b.settles == pd.Timestamp("2026-12-31 23:59:59.999999999", tz="UTC")


def test_merge_keeps_hand_spec_basket_with_same_legs():
    hand = Basket("hand:x", "hand", "mutual_exclusion", "c", (Leg("A", "no"), Leg("B", "no")), 1.0, None, None)
    scan = Basket("mecnet:E:no:B+A", "mecnet:E", "mutually_exclusive_no", "E", (Leg("B", "no"), Leg("A", "no")),
                  1.0, None, None)
    other = Basket("mecnet:E:no:A+C", "mecnet:E", "mutually_exclusive_no", "E", (Leg("A", "no"), Leg("C", "no")),
                   1.0, None, None)
    assert [b.basket_id for b in merge([hand], [scan, other])] == ["hand:x", "mecnet:E:no:A+C"]


def test_run_carries_over_held_relationships(tmp_path, monkeypatch):
    from kalshi_strategies.ARBS.config import ArbsConfig
    from kalshi_infra.config import ConfigStore
    from kalshi_strategies.ARBS.config import CONFIG_PATH
    cfg = ConfigStore(ArbsConfig, CONFIG_PATH, None).config
    spec, review = str(tmp_path / "s.json"), str(tmp_path / "r.json")
    monkeypatch.setattr(scanner, "other_series", lambda: set())
    monkeypatch.setattr(scanner, "fetch_events",
                        lambda: [event("EV-1", "MECNET", [mkt("EV-1-A"), mkt("EV-1-B")])])
    scanner.run(cfg, {}, set(), spec, review)
    monkeypatch.setattr(scanner, "fetch_events", lambda: [])
    scanner.run(cfg, {}, set(), spec, review, held={"mecnet:EV-1:no:A+B"})
    with open(spec) as fh:
        assert [r["relationship_id"] for r in json.load(fh)["relationships"]] == ["mecnet:EV-1"]
    scanner.run(cfg, {}, set(), spec, review)
    with open(spec) as fh:
        assert json.load(fh)["relationships"] == []
