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


def test_best_quote_sticks_to_current_leg_within_margin():
    legs = [lv([(0.60, 9)], bid=0.30), lv([(0.55, 9)], bid=0.30)]
    a = pricing.best_quote(legs, 1.0, 0.02, 1)
    other = 1 - a.leg
    assert pricing.best_quote(legs, 1.0, 0.02, 1, current_leg=other, switch_margin=0.5).leg == other


def test_completion_two_legs_limit_is_cap_j():
    # Bought 5 of leg 0 for $2.00 incl. fees; payout 1, max loss 0.05.
    legs = [lv([(0.50, 9)]), lv([(0.55, 9)])]
    c = pricing.completion(1.0, 0.05, 5, [5, 0], 2.00, legs)
    cap = (5 * 1.05 - 2.00) / 5
    assert c.need == [0, 5] and c.caps[1] == pytest.approx(cap)
    assert tc(c.limits[1]) <= cap + 1e-9 and tc(round(c.limits[1] + 0.01, 2)) > cap
    assert c.limits[0] is None and c.short == []


def test_completion_refuses_past_max_loss():
    legs = [lv([(0.50, 9)]), lv([(0.80, 9)])]
    c = pricing.completion(1.0, 0.05, 5, [5, 0], 2.00, legs)    # cap 0.65 < 0.80 ask
    assert c.limits == [None, None] and c.short == [1]


def test_completion_multi_leg_limits_sum_within_budget():
    legs = [lv([(0.10, 9)]), lv([(0.20, 9)]), lv([(0.30, 9)])]
    c = pricing.completion(1.0, 0.05, 2, [2, 0, 0], 0.22, legs)
    worst = 0.22 + 2 * (tc(c.limits[1]) + tc(c.limits[2]))
    assert worst <= 2 * 1.05 + 1e-9
    assert c.limits[1] >= 0.20 and c.limits[2] >= 0.30


def test_completion_shallow_book_prices_deepest_level():
    legs = [lv([(0.50, 9)]), lv([(0.30, 2)])]
    c = pricing.completion(1.0, 0.05, 5, [5, 0], 2.50, legs)
    assert c.limits[1] is not None and c.limits[1] >= 0.30
