import pytest

from kalshi_infra.kalshi.fees import fee
from kalshi_strategies.ARBS import pricing
from kalshi_strategies.ARBS.pricing import LegView

T = 0.07


def lv(asks, bid=None):
    return LegView(asks=list(asks), bid=bid, taker_rate=T, maker_rate=0.0)


def tc(p):
    return p + fee(p, 1, T)


def test_gap_is_payout_minus_taker_cost():
    legs = [lv([(0.40, 10)]), lv([(0.50, 10)])]
    assert pricing.gap(legs, 1.0) == pytest.approx(1 - tc(0.40) - tc(0.50))
    assert pricing.gap([lv([]), lv([(0.5, 1)])], 1.0) is None


def test_take_plan_stops_where_the_worst_bundle_misses_min_edge():
    legs = [lv([(0.40, 3), (0.45, 10)]), lv([(0.50, 10)])]
    p = pricing.take_plan(legs, 1.0, min_edge=0.05, max_size=10)
    assert p.size == 3 and p.limits == [0.40, 0.50]
    assert pricing.take_plan(legs, 1.0, min_edge=0.2, max_size=10) is None


def test_take_plan_capped_by_depth_and_max_size():
    legs = [lv([(0.30, 2)]), lv([(0.30, 10)])]
    assert pricing.take_plan(legs, 1.0, 0.01, 10).size == 2
    assert pricing.take_plan(legs, 1.0, 0.01, 1).size == 1


def test_take_plan_takes_only_the_touch_even_if_deeper_levels_clear_edge():
    legs = [lv([(0.30, 4), (0.31, 100)]), lv([(0.30, 100)])]
    p = pricing.take_plan(legs, 1.0, min_edge=0.01, max_size=50)
    assert p.size == 4 and p.limits == [0.30, 0.30]


def test_take_plan_touch_under_one_contract_may_reach_next_level():
    legs = [lv([(0.30, 0.4), (0.31, 100)]), lv([(0.30, 100)])]
    p = pricing.take_plan(legs, 1.0, min_edge=0.01, max_size=50)
    assert p.size == 1 and p.limits == [0.31, 0.30]


def test_max_price_within_includes_fee():
    p = pricing.max_price_within(0.50, T)
    assert tc(p) <= 0.50 and tc(round(p + 0.01, 2)) > 0.50


def test_quote_price_clears_required_edge_when_completed_at_asks():
    legs = [lv([(0.60, 5)], bid=0.30), lv([(0.55, 10)], bid=0.40)]
    q = pricing.quote_leg(legs, 0, 1.0, required_edge=0.03, size=5)
    assert q.price == 0.31                         # one tick above the bid
    assert 1.0 - tc(0.55) - q.max_price >= 0.03 - 1e-9
    assert q.edge >= 0.03


def test_quote_never_at_or_through_the_ask():
    legs = [lv([(0.20, 5)], bid=None), lv([(0.50, 10)])]
    q = pricing.quote_leg(legs, 0, 1.0, 0.0, 1)
    assert q.price <= 0.19


def test_quote_none_when_other_leg_lacks_depth():
    legs = [lv([(0.60, 5)], bid=0.30), lv([(0.55, 2)])]
    assert pricing.quote_leg(legs, 0, 1.0, 0.0, 5) is None


def test_exits_price_completing_and_unwinding():
    # Hold 5 YES of leg 0 bought for $2.00; leg 1's YES ask is 0.55, leg 0's NO ask 0.58.
    legs, others = [lv([(0.45, 9)]), lv([(0.55, 9)])], [lv([(0.58, 9)]), lv([(0.40, 9)])]
    e = pricing.exits(1.0, [5, 0], 2.00, legs, others)
    assert e.need == [0, 5] and e.excess == [5, 0]
    assert e.complete == pytest.approx(2.00 + 5 * 0.55 + fee(0.55, 5, T) - 5)
    assert e.unwind == pytest.approx(2.00 - (5 - 5 * 0.58 - fee(0.58, 5, T)))
    assert e.complete_limits == [None, 0.55] and e.unwind_limits == [0.58, None]
    assert e.cheaper() == ("complete", e.complete)


def test_exits_need_full_depth_and_open_markets():
    legs, others = [lv([(0.45, 9)]), lv([(0.55, 2), (0.60, 2)])], [None, lv([(0.40, 9)])]
    e = pricing.exits(1.0, [5, 0], 2.00, legs, others)
    assert e.complete is None and e.unwind is None and e.cheaper() is None
    e = pricing.exits(1.0, [5, 0], 2.00, [lv([]), lv([(0.55, 2), (0.60, 3)])], others)
    assert e.complete_limits[1] == 0.60


def test_rest_price_and_touch_size():
    assert pricing.rest_price(lv([(0.40, 5)], bid=0.30)) == 0.31
    assert pricing.rest_price(lv([(0.31, 5)], bid=0.30)) == 0.30
    assert pricing.touch_size([lv([(0.4, 25)]), lv([(0.5, 23), (0.6, 50)]), lv([(0.1, 40)])], 0) == 23


def test_tapered_grid():
    from kalshi_strategies.ARBS.pricing import Ticks, max_price_within, quote_leg
    t = Ticks.from_market([{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                           {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                           {"start": "0.9000", "end": "1.0000", "step": "0.0010"}])
    assert (t.lo, t.hi) == (0.001, 0.999)
    assert t.floor(0.9147) == 0.914 and t.floor(0.555) == 0.55
    assert t.up(0.099) == 0.1 and t.up(0.1) == 0.11 and t.down(0.1) == 0.099 and t.down(0.91) == 0.909
    assert max_price_within(0.92, T, t) == 0.914
    # Joins one 0.1c tick over a tail bid, under a tail ask.
    a = LegView(asks=[(0.095, 5)], bid=0.08, taker_rate=T, maker_rate=0.0, ticks=t)
    b = LegView(asks=[(0.50, 5)], bid=0.40, taker_rate=T, maker_rate=0.0)
    q = quote_leg([a, b], 0, 1.0, 0.01, 1)
    assert q.price == 0.081 and q.max_price == 0.094


def test_depth_size_grows_while_completion_through_the_book_meets_the_edge():
    # Quote A (rests at 0.43, a tick over its 0.42 bid); B offers 10 at 0.50,
    # 20 at 0.52, 50 at 0.60.
    b = lv([(0.50, 10), (0.52, 20), (0.60, 50)])
    a = pricing.LegView(asks=[(0.47, 100)], bid=0.42, taker_rate=0.07, maker_rate=0.0)
    first = pricing.quote_leg([a, b], 0, 1.0, 0.03, 1.0)
    assert first.price == pytest.approx(0.43)
    # 30 completes through 0.52 within the edge at 0.43; 31+ reaches 0.60.
    assert pricing.depth_size([a, b], 0, 1.0, 0.03, 50.0) == 30.0
    assert pricing.depth_size([a, b], 0, 1.0, 0.03, 5.0) == 5.0
    a_low = pricing.LegView(asks=[(0.47, 100)], bid=0.30, taker_rate=0.07, maker_rate=0.0)
    assert pricing.depth_size([a_low, b], 0, 1.0, 0.03, 50.0) == 50.0    # 0.31 leaves room even at 0.60
