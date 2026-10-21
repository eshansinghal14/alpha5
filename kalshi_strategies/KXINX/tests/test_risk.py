import numpy as np

from kalshi_strategies.KXINX import risk
from kalshi_strategies.KXINX.risk import Leg


def test_offsetting_series_cancel():
    # Long "above 7800" on KXINXU, long every KXINX bucket below 7800: one of
    # the two always pays, so the worst case is just the premium paid.
    legs = [Leg("U-T7800", 10, 0, 6.0, 7800, None, "greater_or_equal"),
            Leg("X-B7787", 10, 0, 3.0, 7775, 7799.9999, "between"),
            Leg("X-L7775", 10, 0, 1.0, None, 7775, "less")]
    worst, _ = risk.worst_case(legs)
    assert np.isclose(worst, 10 - 10.0)


def test_same_direction_adds_up():
    # Short yes (long no) on both "above 7800" and the 7800-7825 bucket:
    # a close of 7810 loses both.
    legs = [Leg("U", 0, 10, 4.0, 7800, None, "greater_or_equal"),
            Leg("X", 0, 10, 7.0, 7800, 7824.9999, "between")]
    worst, at = risk.worst_case(legs)
    assert np.isclose(worst, -11.0)
    assert 7800 <= at <= 7824.9999


def test_strict_vs_inclusive_boundary():
    # Long yes on "greater 7808.69" loses at exactly 7808.69.
    legs = [Leg("D", 10, 0, 5.0, 7808.69, None, "greater")]
    pnl = risk.settlement_pnl(legs, np.array([7808.69, 7808.70]))
    assert np.isclose(pnl[0], -5.0) and np.isclose(pnl[1], 5.0)


def test_blocks_only_sides_that_worsen():
    legs = [Leg("U", 20, 0, 18.0, 7800, None, "greater_or_equal")]
    markets = {"U": (7800, None, "greater_or_equal"),
               "X": (7775, 7799.9999, "between"),
               "L": (None, 7800, "less")}
    blocks = risk.blocked_sides(legs, markets, limit_usd=10.0,
                                prices={"U": 0.9, "X": 0.08, "L": 0.1})
    # Worst case: any close below 7800 (-18). U:no and L:yes pay in exactly
    # that region, so they stay open. X:yes only covers 7775-7800 and costs
    # money below 7775, so it worsens the worst case and is blocked.
    assert "U:yes" in blocks
    assert "U:no" not in blocks
    assert "L:yes" not in blocks
    assert "X:yes" in blocks
    assert all("combined_risk" in r for r in blocks.values())


def test_limit_off_blocks_nothing():
    legs = [Leg("U", 200, 0, 180.0, 7800, None, "greater_or_equal")]       # -180 worst case
    assert risk.blocked_sides(legs, {"U": (7800, None, "greater_or_equal")}, None, {}) == {}


def test_under_limit_blocks_nothing():
    legs = [Leg("U", 2, 0, 1.0, 7800, None, "greater_or_equal")]
    assert risk.blocked_sides(legs, {"U": (7800, None, "greater_or_equal")}, 100.0, {}) == {}
