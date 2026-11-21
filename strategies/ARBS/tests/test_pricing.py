import pytest

from infra.kalshi.fees import fee
from strategies.ARBS import pricing
from strategies.ARBS.pricing import LegView

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
    from strategies.ARBS.pricing import Ticks, max_price_within, quote_leg
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


def test_second_level_size_is_what_the_other_legs_complete_without_their_best_level():
    legs = [lv([], bid=0.45), lv([(0.55, 100), (0.56, 30), (0.70, 500)])]
    # Without the 100 at 0.55, the 30 at 0.56 still complete a fill at 0.40; the 0.70 level loses.
    assert pricing.second_level_size(legs, 0, 1.0, 100.0, at=0.40) == 30
    assert pricing.second_level_size(legs, 0, 1.0, 20.0, at=0.40) == 20
    assert pricing.second_level_size(legs, 0, 1.0, 1000.0, at=0.25) == 530     # at 0.25 the 0.70 level completes too
    assert pricing.second_level_size(legs, 0, 1.0, 100.0, at=0.43) == 0        # 0.43 + 0.56 + its fee is over $1
    assert pricing.second_level_size([lv([], bid=0.45), lv([(0.55, 100)])], 0, 1.0, 100.0, at=0.40) == 0


def test_depth_size_binary_search_matches_the_linear_scan():
    import random
    from strategies.ARBS import pricing as P

    def linear(legs, j, payout, need, cap):
        first = P.quote_leg(legs, j, payout, need, 1.0)
        if first is None or cap < 1:
            return 0.0
        price = first.price
        marks = {float(cap)}
        for i, lv in enumerate(legs):
            if i == j:
                continue
            got = 0.0
            for _, q in lv.asks:
                got += q
                if got >= cap:
                    break
                marks.add(float(int(got + 1e-9)))
        best = 1.0
        for n in sorted(m for m in marks if m >= 1):
            plan = P.quote_leg(legs, j, payout, need, n)
            if plan is None or plan.max_price < price - 1e-9:
                break
            best = n
        return best

    rng = random.Random(7)
    for _ in range(3000):
        legs = []
        for _ in range(rng.randint(2, 5)):
            px = sorted({round(rng.uniform(0.02, 0.6), 2) for _ in range(rng.randint(1, 8))})
            legs.append(P.LegView(asks=[(p, float(rng.randint(1, 40))) for p in px],
                                  bid=round(rng.uniform(0.01, 0.3), 2) if rng.random() < 0.8 else None,
                                  taker_rate=0.07, maker_rate=0.0175))
        j, need, cap = rng.randrange(len(legs)), rng.uniform(0.0, 0.08), float(rng.randint(1, 300))
        payout = float(len(legs) - 1) if rng.random() < 0.3 else 1.0
        assert P.depth_size(legs, j, payout, need, cap) == linear(legs, j, payout, need, cap)


def test_depth_sizes_and_shared_walks_match_one_quote_leg_per_edge_and_size():
    import random
    from strategies.ARBS import pricing as P

    def linear(legs, j, payout, need, cap, at):
        first = P.quote_leg(legs, j, payout, need, 1.0)
        if first is None or cap < 1:
            return 0.0
        price = first.price if at is None else at
        if first.max_price < price - 1e-9:
            return 0.0
        marks = {float(cap)}
        for i, lv in enumerate(legs):
            if i == j:
                continue
            got = 0.0
            for _, q in lv.asks:
                got += q
                if got >= cap:
                    break
                marks.add(float(int(got + 1e-9)))
        best = 1.0
        for n in sorted(m for m in marks if m >= 1):
            plan = P.quote_leg(legs, j, payout, need, n)
            if plan is None or plan.max_price < price - 1e-9:
                break
            best = n
        return best

    tapered = P.Ticks(((0.0, 0.1, 0.001), (0.1, 0.9, 0.01), (0.9, 1.0, 0.001)))
    edges = (0.0, 0.01, 0.03, 0.05, 0.1, 0.2)
    rng = random.Random(11)
    for _ in range(2000):
        ticks = tapered if rng.random() < 0.3 else P.CENT
        legs = []
        for _ in range(rng.randint(2, 5)):
            px = sorted({round(rng.uniform(0.005, 0.6), 3 if ticks is tapered else 2) for _ in range(rng.randint(1, 8))})
            legs.append(P.LegView(asks=[(p, float(rng.randint(1, 40))) for p in px],
                                  bid=round(rng.uniform(0.01, 0.3), 2) if rng.random() < 0.8 else None,
                                  taker_rate=0.07, maker_rate=0.0175, ticks=ticks))
        j, cap = rng.randrange(len(legs)), float(rng.randint(0, 300))
        payout = float(len(legs) - 1) if rng.random() < 0.3 else 1.0
        at = None if rng.random() < 0.7 else round(rng.uniform(0.01, 0.4), 2)
        assert P.depth_sizes(legs, j, payout, edges, cap, at) == [linear(legs, j, payout, e, cap, at) for e in edges]
        n = float(rng.randint(1, 60))
        o = P.others_cost(legs, j, n)
        for e in edges:
            want = P.quote_leg(legs, j, payout, e, n)
            assert (None if o is None else P.plan_at(legs, j, payout, e, o)) == want
        if o is not None:
            assert P.prices_at(legs, j, payout, edges, o) == [
                None if (p := P.plan_at(legs, j, payout, e, o)) is None else p.price for e in edges]



TAPERED = pricing.Ticks(((0.0, 0.1, 0.001), (0.1, 0.9, 0.01), (0.9, 1.0, 0.001)))


def _walk(budget, rate, ticks):
    """max_price_within as it was: down the grid from the budget, a fee a step."""
    p = ticks.floor(min(ticks.hi, budget + pricing._EPS))
    while p >= ticks.lo - pricing._EPS:
        if p + fee(p, 1.0, rate) <= budget + pricing._EPS:
            return p
        p = ticks.floor(p - 1e-6)
    return None


def test_max_price_within_bisect_matches_the_walk_on_every_grid():
    import math
    import random
    rng = random.Random(0)
    for ticks in (pricing.CENT, TAPERED):
        pts = pricing._cost_grid(0.07, ticks)[0]
        for rate in (0.0, 0.0175, 0.035, 0.07, 0.14):
            budgets = [x + d for x in pts for d in (-1e-7, 0.0, 1e-9, 0.0001, 0.0175)]
            budgets += [rng.uniform(-0.2, 1.3) for _ in range(500)] + [math.inf, math.nan]
            for b in budgets:
                assert pricing.max_price_within(b, rate, ticks) == _walk(b, rate, ticks), (ticks, rate, b)


def test_kept_fees_and_ticks_match_their_formulas():
    for ticks in (pricing.CENT, TAPERED):
        for p in pricing._cost_grid(0.07, ticks)[0]:
            assert ticks.down(p) == ticks.floor(p - 1e-6) and ticks.up(p) == ticks._up(p)
            assert pricing.fee1(p, 0.07) == fee(p, 1.0, 0.07) and pricing.taker_cost(p, 0.07) == p + fee(p, 1.0, 0.07)
