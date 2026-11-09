import dataclasses
import time

import pytest

from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM import farm
from kalshi_strategies.LIPFARM.farm import parse_book, plan_market, remove_ours

FETCH_FLOW = farm.fetch_flow          # the real one; an autouse fixture stubs it
P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
CFG = farm.Cfg(cash_reserve=0.0, num_markets=5, min_lots_frac=0.0, sentinel_keep=2.0, dump_slip_ticks=2, chase_ticks=2, max_chase_ticks=5,
               adverse_lots=300, recover_s=0.0, event_pull_s=0.0, deep_keep=0.0, exit_depth_frac=10.0, exit_depth_slip=0.10, wide_spread=0.20, min_leg_price=0.0, decided_min=0.97,
               min_ahead=0.0, max_position=1000.0, write_tokens_per_s=5.0, scan_markets=1, scan_rotate=0, refresh_s=30,
               programs_refresh_s=600, flow_days=4.0, max_trades_day=50.0, loss_per_trade=3.0, loss_cap=30.0,
               min_lip_to_loss=2.0,
               skip_categories="Sports,Mentions", skip_series="KXRT", min_close_h=48.0, max_pool_share=1.0, loss_min_trades=0.0, wide_exit_frac=0.0, wide_exit_slip=0.10, flow_long_days=0.0, adverse_frac=1.0,
               realized_loss_days=0.0, realized_loss_prior=5.0, arbs_yield_ratio=0.0)


@pytest.fixture(autouse=True)
def _quiet_flow(monkeypatch):
    """No trade reads in tests: every market reads as having no flow."""
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days, opened=None: {"trades_day": 0.0, "days": days,
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
    cfg = dataclasses.replace(CFG, exit_depth_frac=1.0, exit_depth_slip=0.10)
    # Only bids past a sweep's 3-tick reach count: yes has 400 four ticks down; no's 9000 is one tick down.
    o = {"yes": {0.80: 1500.0, 0.76: 400.0}, "no": {0.05: 1500.0, 0.04: 9000.0}}
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
    others = {"yes": {0.70: 50.0, 0.66: 2000.0}, "no": {0.06: 20.0, 0.05: 5000.0}}
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
    deep = {"yes": {0.40: 50.0, 0.36: 9000.0}, "no": {0.35: 50.0, 0.31: 9000.0}}
    for target in (1000, 2500):
        Pt = dataclasses.replace(P, target_size=target)
        assert plan_market("T", deep, Pt, CFG, {}, farm.exit_lots(deep, CFG, target)).lots == target


def _tr(ts, side, yes_px):
    return {"created_time": f"2026-11-05T12:00:{ts:06.3f}Z", "taker_side": side, "yes_price_dollars": f"{yes_px:.2f}"}


def test_narrow_market_needs_exit_liquidity_and_is_sized_by_it():
    others = {"yes": {0.45: 500.0, 0.41: 300.0}, "no": {0.50: 500.0, 0.46: 600.0}}
    assert plan_market("T", others, P, CFG) is None                     # 5c spread: needs exit
    pl = plan_market("T", others, P, dataclasses.replace(CFG, exit_depth_frac=1.0), {},
                     exit=farm.exit_lots(others, dataclasses.replace(CFG, exit_depth_frac=1.0), 1000))
    assert pl.mode == "narrow" and set(pl.legs) == {("yes", 0.45), ("no", 0.50)} and pl.lots == 300
    # No depth under a touch: that side isn't backed, so it isn't quoted.
    thin = {"yes": {0.45: 1500.0, 0.44: 900.0}, "no": {0.50: 500.0, 0.46: 600.0}}     # 0.44 is inside a sweep's reach
    pl = plan_market("T", thin, P, CFG, {}, exit=farm.exit_lots(thin, CFG, 1000))
    assert pl.legs == (("no", 0.50),)
    # min_leg_price holds for narrow legs too.
    cheap = {"yes": {0.05: 1500.0, 0.01: 600.0}, "no": {0.90: 1500.0, 0.86: 600.0}}
    pl = plan_market("T", cheap, P, dataclasses.replace(CFG, min_leg_price=0.2), {},
                     exit=farm.exit_lots(cheap, CFG, 1000))
    assert pl.mode == "narrow" and pl.legs == (("no", 0.90),)


def test_a_side_we_hold_is_not_bid_again():
    others = {"yes": {0.40: 2000.0, 0.36: 2000.0}, "no": {0.45: 2000.0, 0.41: 2000.0}}
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


def test_wide_bids_as_deep_as_keeps_most_of_the_lip():
    cfg = dataclasses.replace(CFG, deep_keep=0.8, min_leg_price=0.2)
    # T130 11-05 without our bids: 184 lots above 9c, then thin down to 5c.
    others = {"yes": {0.10: 89.0, 0.04: 3926.0}, "no": {0.57: 11.0, 0.56: 173.0, 0.09: 2.5, 0.06: 27.0, 0.05: 3025.0}}
    pl = plan_market("T", others, P, cfg, {}, 1000)
    assert pl.mode == "wide" and pl.big["no"] == 0.06 and pl.legs == (("no", 0.06),) and pl.layout["no"] == "deep"
    assert pl.capital == pytest.approx(60.0) and farm.desired_orders(pl) == {"T|no": (0.06, 1000, True)}
    touch = plan_market("T", others, P, dataclasses.replace(cfg, deep_keep=0.0), {}, 1000)
    assert pl.rate >= 0.8 * touch.rate
    # Others past Target/5 above it: deep scores nothing, so it stays up top.
    crowded = dict(others, no={0.57: 300.0, 0.56: 173.0, 0.09: 2.5, 0.06: 27.0, 0.05: 3025.0})
    assert plan_market("T", crowded, P, cfg, {}, 1000).big["no"] >= 0.56



def test_a_side_goes_deep_on_its_own_lip_not_the_markets():
    cfg = dataclasses.replace(CFG, deep_keep=0.8, min_leg_price=0.2)
    # ANTHVREQ 11-05: the yes touch is crowded (12.6k), so yes earns little anywhere; the no side
    # carries the market. Yes must not be pushed to 1c just because the no side's LIP dominates.
    others = {"yes": {0.36: 12662.0, 0.26: 292.0, 0.15: 1.0}, "no": {0.26: 23.0, 0.15: 3001.0}}
    pl = plan_market("T", others, P, cfg, {}, 1000)
    assert pl.big["yes"] >= 0.26


def test_expected_loss_rises_toward_the_close():
    pl = farm.Plan("T", "wide", (("yes", 0.4), ("no", 0.5)), 1e-3, 900.0, 0.002, 1000)
    base = farm.expected_loss_day(4.0, pl, CFG)
    assert farm.expected_loss_day(4.0, pl, CFG, 90.0) == pytest.approx(base)
    assert farm.expected_loss_day(4.0, pl, CFG, 3.0) == pytest.approx(3.3 * base)
    assert farm.expected_loss_day(4.0, pl, CFG, 0.5) == pytest.approx(4.0 * base)


def test_pool_share_counts_only_the_sides_we_quote():
    one = farm.Plan("T", "decided", (("yes", 0.97),), 1e-3, 970.0, 4e-3, 1000)      # 1e-3 of a 2e-3 side
    two = farm.Plan("T", "wide", (("yes", 0.4), ("no", 0.5)), 1e-3, 900.0, 4e-3, 1000)
    assert farm.pool_share(one) == pytest.approx(0.5) and farm.pool_share(two) == pytest.approx(0.25)


def test_exit_ticks_skips_what_a_sweep_takes_and_prices_the_hole():
    assert farm.exit_ticks({0.39: 5000.0, 0.36: 1000.0}, 0.40, 1000) == pytest.approx(4.0)    # 0.39 is swept
    assert farm.exit_ticks({0.36: 500.0}, 0.40, 1000) == pytest.approx((500 * 4 + 500 * 20) / 1000)
    assert farm.exit_ticks({}, 0.05, 100) == pytest.approx(5.0)                                # capped at the price


def test_expected_loss_charges_a_hole_under_the_bid_more():
    pl = farm.Plan("T", "wide", (("yes", 0.4),), 1e-3, 400.0, 0.002, 1000)
    deep = farm.expected_loss_day(4.0, pl, CFG, others={"yes": {0.36: 2000.0}, "no": {}})
    hole = farm.expected_loss_day(4.0, pl, CFG, others={"yes": {}, "no": {}})
    flat = farm.expected_loss_day(4.0, pl, CFG)
    assert deep == pytest.approx(flat * 4 / 7.6) and hole == pytest.approx(flat * 20 / 7.6)


def test_a_market_that_never_trades_is_still_charged_for_its_gap():
    pl = farm.Plan("T", "wide", (("yes", 0.4),), 1e-3, 400.0, 0.002, 1000)
    cfg = dataclasses.replace(CFG, loss_min_trades=0.3)
    assert farm.expected_loss_day(0.0, pl, CFG) == 0.0
    assert farm.expected_loss_day(0.0, pl, cfg) == pytest.approx(farm.expected_loss_day(0.3, pl, CFG))


def test_a_decided_market_pays_for_its_odds_of_flipping():
    pl = farm.Plan("T", "decided", (("no", 0.97),), 1e-3, 776.0, 0.002, 800)
    others = {"no": {0.94: 200.0}, "yes": {0.02: 29000.0}}          # mid (0.97 + 0.98) / 2
    assert farm.flip_loss_day(pl, others, 87.0) == pytest.approx(800 * 0.97 * 0.025 / 87.0)
    wide = farm.Plan("T", "wide", (("no", 0.40),), 1e-3, 320.0, 0.002, 800)
    assert farm.flip_loss_day(wide, others, 87.0) == 0.0


def test_wide_bids_are_sized_to_the_exit_under_them():
    cfg = dataclasses.replace(CFG, wide_exit_frac=1.0, wide_exit_slip=0.10, min_lots_frac=0.2)
    # yes: 600 lots of others' bids 2-9c under its 0.40 touch; no: only at its own price, nothing under.
    others = {"yes": {0.40: 50.0, 0.38: 400.0, 0.31: 200.0, 0.20: 5000.0}, "no": {0.35: 1500.0}}
    pl = plan_market("T", others, P, cfg)
    assert pl.mode == "wide" and [s for s, _ in pl.legs] == ["yes"] and pl.lots == 600
    assert plan_market("T", {"yes": {0.40: 50.0}, "no": {0.35: 1500.0}}, P, cfg) is None   # no exit anywhere


def test_exit_depth_inside_a_sweeps_reach_does_not_count():
    # WVCOAL 11-06 before the sweep: 1,712 lots 1-3 ticks under the 66c touch, which one 3,353-lot sell took.
    o = {"yes": {0.30: 500.0}, "no": {0.66: 2457.0, 0.65: 1487.0, 0.64: 25.0, 0.63: 200.0}}
    assert farm.exit_lots(o, dataclasses.replace(CFG, exit_depth_frac=1.0), 1000)["no"] == 0


def test_loss_scales_by_the_series_at_its_distance_from_fifty():
    tab = {"bucket_mult": [1.5, 1.0, 0.8, 0.4], "series": {"KXBAD": [6.0, 4.0, 3.0, 1.0]}}
    assert farm.loss_mult("KXBAD-26NOV08-T205", 0.43, tab) == 6.0          # 7c from 50
    assert farm.loss_mult("KXBAD-26NOV08-T205", 0.97, tab) == 1.0          # 47c from 50
    assert farm.loss_mult("KXOTHER-1-A", 0.30, tab) == 1.0                 # no tape: its bucket's all-market value


def test_expected_loss_uses_each_legs_series_multiplier(monkeypatch):
    tab = {"bucket_mult": [1.0, 1.0, 1.0, 1.0], "series": {"KXBAD": [4.0, 4.0, 1.0, 1.0]}}
    monkeypatch.setattr(farm, "series_loss_table", lambda: tab)
    pl = farm.Plan("KXBAD-1-A", "narrow", (("yes", 0.45), ("no", 0.90)), 1e-3, 0.0, 0.002, 200)
    flat = farm.expected_loss_day(2.0, dataclasses.replace(pl, ticker="KXOK-1-A"), CFG)
    assert farm.expected_loss_day(2.0, pl, CFG) == pytest.approx(flat * (4.0 + 1.0) / 2.0)


def test_series_loss_counts_a_sweep_that_does_not_come_back():
    from kalshi_strategies.LIPFARM import series_loss as sl
    t0 = 1_000_000.0
    # Takers buy no 3 times in 1 s: yes bids at 0.44, 0.43, 0.41 cleared (600 lots); yes stays at 0.35 after.
    tr = [(t0, "no", 0.44, 300.0), (t0 + 0.2, "no", 0.43, 200.0), (t0 + 0.5, "no", 0.41, 100.0)]
    tr += [(t0 + 600 * k, "no", 0.35, 5.0) for k in range(1, 14)]
    assert sl.adverse_losses(tr) == [(0.44, pytest.approx(600 * 0.09))]
    back = tr[:3] + [(t0 + 600, "no", 0.44, 5.0)] + [(t0 + 600 * k, "no", 0.35, 5.0) for k in range(2, 14)]
    assert sl.adverse_losses(back) == []                                   # back within 30 min: not adverse
    assert [sl.bucket(p) for p in (0.45, 0.30, 0.15, 0.97)] == [0, 1, 2, 3]


def test_realized_loss_spreads_a_series_loss_over_its_quoted_days_plus_the_prior():
    assert farm.realized_loss_day(30.0, 5.0, 5.0) == pytest.approx(3.0)
    assert farm.realized_loss_day(-20.0, 5.0, 5.0) == 0.0          # a series that made money charges nothing
    assert farm.realized_loss_day(30.0, 0.0, 0.0) == 0.0
