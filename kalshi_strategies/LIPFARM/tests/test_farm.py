import dataclasses
import time

import pytest

from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM import farm
from kalshi_strategies.LIPFARM.farm import parse_book, plan_market, remove_ours

FETCH_FLOW = farm.fetch_flow          # the real one; an autouse fixture stubs it
P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
CFG = farm.Cfg(max_capital=5000, num_markets=5, min_lots_frac=0.0, sentinel_keep=2.0, dump_slip_ticks=2, chase_ticks=2, max_chase_ticks=5,
               adverse_lots=300, exit_depth_frac=10.0, exit_depth_slip=0.03, wide_spread=0.20, min_leg_price=0.0, decided_min=0.97,
               min_ahead=0.0, max_position=1e9, write_tokens_per_s=5.0, scan_markets=1, scan_rotate=0, refresh_s=30,
               programs_refresh_s=600, flow_days=4.0, max_sweeps_day=0.5, max_trades_day=20.0,
               skip_categories="Sports,Mentions")


@pytest.fixture(autouse=True)
def _quiet_flow(monkeypatch):
    """No trade reads in tests: every market reads as having no flow."""
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days, opened=None: {"trades_day": 0.0, "sweeps_day": 0.0, "days": days,
                                                             "at": time.time()})


def test_decided_quotes_only_the_expensive_side():
    others = {"yes": {0.97: 200.0}, "no": {0.02: 5000.0}}
    pl = plan_market("T", others, P, CFG)
    assert pl.mode == "decided" and pl.legs == (("yes", 0.97),) and pl.capital == 970.0
    # yes side: our 1000 of 1200; no side: none of ours -> half of 1000/1200 per snapshot.
    assert abs(pl.rate - (1000 / 1200) / 2 * 0.001) < 1e-12


def test_decided_needs_the_other_side_to_qualify_on_others_alone():
    assert plan_market("T", {"yes": {0.97: 200.0}, "no": {0.02: 999.0}}, P, CFG) is None


def test_no_plan_at_99_or_in_between():
    assert plan_market("T", {"yes": {0.99: 200.0}, "no": {0.005: 5000.0}}, P, CFG) is None   # top price
    tapered = dataclasses.replace(P, price_ranges=((0.0, 0.1, 0.001), (0.1, 0.9, 0.01), (0.9, 1.0, 0.001)))
    assert plan_market("T", {"yes": {0.99: 200.0}, "no": {0.005: 5000.0}}, tapered, CFG).legs == (("yes", 0.99),)
    assert plan_market("T", {"yes": {0.60: 5000.0}, "no": {0.30: 5000.0}}, P, CFG) is None   # 10c spread


def test_wide_quotes_both_touches():
    others = {"yes": {0.11: 50.0}, "no": {0.37: 20.0}}
    pl = plan_market("T", others, P, CFG)
    assert pl.mode == "wide" and set(pl.legs) == {("yes", 0.11), ("no", 0.37)}
    assert abs(pl.capital - 480.0) < 1e-9


def test_wide_drops_a_side_at_its_position_cap():
    others = {"yes": {0.11: 50.0, 0.05: 2000.0}, "no": {0.37: 5000.0}}   # yes qualifies without us
    cfg = dataclasses.replace(CFG, max_position=1000)
    pl = plan_market("T", others, P, cfg, held={"yes": 1000.0})
    assert pl.legs == (("no", 0.37),)


def test_min_ahead_needs_others_at_our_price():
    others = {"yes": {0.97: 200.0}, "no": {0.02: 5000.0}}
    assert plan_market("T", others, P, dataclasses.replace(CFG, min_ahead=200)) is not None
    assert plan_market("T", others, P, dataclasses.replace(CFG, min_ahead=201)) is None


def test_subcent_prices_survive_parsing():
    yes, no = parse_book({"yes_dollars": [["0.9850", "10"]], "no_dollars": [["0.0040", "5"]]})
    assert yes == {0.985: 10.0} and no == {0.004: 5.0}
    assert remove_ours(no, 0.004, 5.0) == {}


def _held(f, side="yes", price=0.97, size=600.0):
    f.state["fills"] = [{"ticker": "Z-1-A", "side": side, "price": price, "size": size, "fee": 0.0,
                         "order_id": "oid0", "trade_id": "t0"}]
    f.state["hedged_oids"] = ["oid0"]


def test_exit_depth_counts_only_bids_below_ours(tmp_path, monkeypatch):
    cfg = dataclasses.replace(CFG, exit_depth_frac=1.0, exit_depth_slip=0.03)
    # no touch 0.05 with 1500 there and only 1c bids below: no exit depth on no.
    o = {"yes": {0.80: 1500.0, 0.78: 400.0}, "no": {0.05: 1500.0, 0.01: 9000.0}}
    assert farm.exit_lots(o, cfg, 1000) == {"yes": 400, "no": 0}
    P1 = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
    pl = plan_market("T", o, P1, dataclasses.replace(cfg, wide_spread=0.15), {}, farm.exit_lots(o, cfg, 1000))
    assert pl.mode == "wide" and pl.legs == (("yes", 0.80),) and pl.lots == 400      # the 5c no leg isn't quoted


def test_forced_spends_borrow_at_most_one_burst():
    bk = farm.TokenBucket(rate=0.0, capacity=60.0)
    for _ in range(50):
        bk.take(10.0, force=True)
    assert bk.tokens == -60.0


def test_wide_skips_legs_under_min_leg_price():
    others = {"yes": {0.70: 50.0, 0.68: 2000.0}, "no": {0.06: 20.0, 0.05: 5000.0}}
    cfg = dataclasses.replace(CFG, min_leg_price=0.10, exit_depth_frac=10.0)
    pl = plan_market("T", others, P, cfg, {}, farm.exit_lots(others, cfg, 1000))
    assert pl.mode == "wide" and pl.legs == (("yes", 0.70),)    # the 6c no leg isn't quoted


def test_sentinel_layout_on_a_thin_touch_touch_layout_on_a_crowded_one():
    cfg = dataclasses.replace(CFG, sentinel_keep=0.8)
    thin = {"yes": {0.40: 50.0, 0.39: 300.0, 0.37: 2000.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    pl = plan_market("T", thin, P, cfg, {}, 1000)
    assert pl.layout["yes"] == "sentinel" and pl.big["yes"] == 0.39
    orders = farm.desired_orders(pl)
    assert orders["T|yes|s"] == (0.40, farm.SENTINEL_LOTS, True) and orders["T|yes"] == (0.39, 1000, True)
    crowded = {"yes": {0.40: 3000.0, 0.39: 300.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    pl = plan_market("T", crowded, P, cfg, {}, 1000)
    assert pl.layout["yes"] == "touch" and farm.desired_orders(pl)["T|yes"] == (0.40, 1000, False)


def test_layout_has_hysteresis():
    cfg = dataclasses.replace(CFG, sentinel_keep=0.8)
    thin = {"yes": {0.40: 50.0, 0.39: 300.0, 0.37: 2000.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    base = plan_market("T", thin, P, dataclasses.replace(cfg, sentinel_keep=0.0), {}, 1000)
    pl = plan_market("T", thin, P, cfg, {}, 1000)
    # Find a sentinel_keep just above what the sentinel layout keeps: a touch side stays touch,
    # a sentinel side stays sentinel.
    ratio = pl.rate / plan_market("T", thin, P, dataclasses.replace(cfg, sentinel_keep=2.0), {}, 1000).rate
    near = dataclasses.replace(cfg, sentinel_keep=ratio + 0.05)
    assert plan_market("T", thin, P, near, {}, 1000, current={"yes": "touch"}).layout["yes"] == "touch"
    assert plan_market("T", thin, P, near, {}, 1000, current={"yes": "sentinel"}).layout["yes"] == "sentinel"
    assert base.layout["yes"] == "sentinel"


def test_sides_sized_under_target_fifth_are_not_quoted():
    cfg = dataclasses.replace(CFG, min_lots_frac=0.2)
    others = {"yes": {0.97: 900.0}, "no": {0.02: 5000.0}}          # the yes side reaches Target with us
    assert plan_market("T", others, P, cfg, {}, 199) is None        # Target 1000: under 200 lots
    assert plan_market("T", others, P, cfg, {}, 200).lots == 200


def test_bid_size_follows_the_programs_target():
    deep = {"yes": {0.40: 50.0, 0.39: 9000.0}, "no": {0.35: 50.0, 0.34: 9000.0}}
    for target in (1000, 2500):
        Pt = dataclasses.replace(P, target_size=target)
        assert plan_market("T", deep, Pt, CFG, {}, farm.exit_lots(deep, CFG, target)).lots == target


def _tr(ts, side, yes_px):
    return {"created_time": f"2026-11-05T12:00:{ts:06.3f}Z", "taker_side": side, "yes_price_dollars": f"{yes_px:.2f}"}


def test_count_sweeps_counts_bursts_that_cleared_a_level():
    trades = [_tr(0.0, "yes", 0.88), _tr(0.2, "yes", 0.89), _tr(0.4, "yes", 0.90),   # through 3 levels: 1
              _tr(5.0, "no", 0.50), _tr(5.5, "no", 0.50),                           # one level only: 0
              _tr(9.0, "no", 0.50), _tr(9.5, "no", 0.49),                           # 1
              _tr(20.0, "yes", 0.50), _tr(22.0, "yes", 0.60)]                       # > 1 s apart: 0
    assert farm.count_sweeps(trades) == 2


def test_narrow_market_needs_exit_liquidity_and_is_sized_by_it():
    others = {"yes": {0.45: 500.0, 0.44: 300.0}, "no": {0.50: 500.0, 0.49: 600.0}}
    assert plan_market("T", others, P, CFG) is None                     # 5c spread: needs exit
    pl = plan_market("T", others, P, dataclasses.replace(CFG, exit_depth_frac=1.0), {},
                     exit=farm.exit_lots(others, dataclasses.replace(CFG, exit_depth_frac=1.0), 1000))
    assert pl.mode == "narrow" and set(pl.legs) == {("yes", 0.45), ("no", 0.50)} and pl.lots == 300
    # No depth under a touch: that side isn't backed, so it isn't quoted.
    thin = {"yes": {0.45: 1500.0}, "no": {0.50: 500.0, 0.49: 600.0}}
    pl = plan_market("T", thin, P, CFG, {}, exit=farm.exit_lots(thin, CFG, 1000))
    assert pl.legs == (("no", 0.50),)
    # min_leg_price holds for narrow legs too.
    cheap = {"yes": {0.05: 1500.0, 0.04: 600.0}, "no": {0.90: 1500.0, 0.89: 600.0}}
    pl = plan_market("T", cheap, P, dataclasses.replace(CFG, min_leg_price=0.2), {},
                     exit=farm.exit_lots(cheap, CFG, 1000))
    assert pl.mode == "narrow" and pl.legs == (("no", 0.90),)


def test_a_side_we_hold_is_not_bid_again():
    others = {"yes": {0.40: 2000.0, 0.39: 2000.0}, "no": {0.45: 2000.0, 0.44: 2000.0}}
    ex = farm.exit_lots(others, CFG, 1000)
    assert set(plan_market("T", others, P, CFG, {"yes": 25.0}, exit=ex).legs) == {("yes", 0.40), ("no", 0.45)}
    assert plan_market("T", others, P, CFG, {"yes": 797.0}, exit=ex).legs == (("no", 0.45),)


def test_wide_and_decided_quote_target_whatever_the_exit_depth():
    others = {"yes": {0.11: 2000.0}, "no": {0.37: 2000.0}}              # nothing under either touch
    pl = plan_market("T", others, P, CFG, {}, exit=farm.exit_lots(others, CFG, 1000))
    assert pl.mode == "wide" and pl.lots == 1000
    dec = {"yes": {0.97: 2000.0}, "no": {0.02: 5000.0}}
    assert plan_market("T", dec, P, CFG, {}, exit=farm.exit_lots(dec, CFG, 1000)).lots == 1000


def test_flow_of_a_young_market_is_over_its_own_age(monkeypatch):
    trades = [_tr(i, "yes", 0.5) for i in range(6)]
    monkeypatch.setattr(farm, "read", lambda path, params=None, auth=False: {"trades": trades, "cursor": None})
    assert FETCH_FLOW("T", 10.0)["trades_day"] == 0.6
    assert FETCH_FLOW("T", 10.0, opened=time.time() - 3 * 86400)["trades_day"] == pytest.approx(2.0)
    assert FETCH_FLOW("T", 10.0, opened=time.time() - 3600)["trades_day"] == 6.0          # at least a day
