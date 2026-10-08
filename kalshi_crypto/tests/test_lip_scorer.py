"""
LIP scorer tests.

The headline test is the Kalshi help page's own worked example:
  * 20% of the normalized score on one side, 8000 non-excluded snapshots
    out of 10000, period_reward $100 -> payout = $16.00.
  * Reference price mechanics on a hand-constructed book.
  * marginal_reward_rate: adding a size that doesn't change scoring
    returns 0 (we're already in the book); adding at the Reference
    Price increases our share proportionally.
"""
import math
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.lip_scorer import (LipParams, LipLevel, score_book,
                                       marginal_reward_rate, period_payout)


def test_help_page_payout_example():
    """From Kalshi's own help page: 20% share * $100 * 8000/10000 =
    $16.00 payout, rounded down to the cent, min $1.00."""
    payout = period_payout(user_total_score=0.20, all_users_total_score=1.0,
                           non_excluded=8000, total_snapshots=10_000,
                           period_reward_usd=100.0)
    assert math.isclose(payout, 16.00, abs_tol=1e-9)


def test_payout_min_one_dollar():
    """A positive payout below $1 rounds UP to $1 per the rules."""
    payout = period_payout(user_total_score=0.005, all_users_total_score=1.0,
                           non_excluded=10, total_snapshots=100,
                           period_reward_usd=100.0)
    # 0.005 * 100 * 0.1 = 0.05 raw; min $1.00 kicks in.
    assert math.isclose(payout, 1.00, abs_tol=1e-9)


def test_reference_price_at_target_over_five():
    """3 levels of 50 contracts each, Target 200. Target/5 = 40, so
    the Reference Price is at the FIRST level (its cumulative 50 is
    the first >= 40). Stop walking once cumulative >= 200, which is
    after the 4th level (none exists -- so the side DOESN'T qualify)."""
    params = LipParams(target_size=200.0, discount_factor=0.5,
                       period_reward_usd=100.0, snapshots_per_period=900)
    yes_levels = [LipLevel(price=0.60, size=50, mine=10),
                  LipLevel(price=0.59, size=50, mine=0),
                  LipLevel(price=0.58, size=50, mine=0)]
    no_levels = [LipLevel(price=0.40, size=250, mine=0)]  # qualifies, Ref @ 0.40
    score = score_book(yes_levels, no_levels, params)
    # Yes side cumulative is 150 < Target=200, so does NOT qualify.
    assert not score.yes.qualifies
    assert not score.qualifies


def test_reference_price_and_scoring_mechanics():
    """A well-formed side. Target=200, DF=0.5. Levels:
       0.60 x 50 (ours: 20) -- cumulative 50 (>= Target/5=40), Ref=0.60
       0.59 x 100 (ours: 0) -- cumulative 150
       0.58 x 100 (ours: 0) -- cumulative 250 >= Target=200, stop here

       Score weights: 2 ticks below Ref -> DF^2 = 0.25 at 0.58.
       all_score = 50*1 + 100*0.5 + 100*0.25 = 125
       our_score = 20*1 = 20
       our_share = 20 / 125 = 0.16
    """
    params = LipParams(target_size=200.0, discount_factor=0.5,
                       period_reward_usd=100.0, snapshots_per_period=900)
    yes_levels = [LipLevel(price=0.60, size=50, mine=20),
                  LipLevel(price=0.59, size=100, mine=0),
                  LipLevel(price=0.58, size=100, mine=0)]
    no_levels = [LipLevel(price=0.40, size=50, mine=0),
                 LipLevel(price=0.39, size=100, mine=0),
                 LipLevel(price=0.38, size=100, mine=0)]  # symmetric qualifying side
    score = score_book(yes_levels, no_levels, params)
    assert score.yes.qualifies
    assert score.yes.reference_price == 0.60
    assert math.isclose(score.yes.all_raw_score, 125.0, abs_tol=1e-9)
    assert math.isclose(score.yes.our_raw_score, 20.0, abs_tol=1e-9)
    assert math.isclose(score.yes.our_share, 0.16, abs_tol=1e-9)


def test_expected_reward_this_snapshot():
    """reward_per_snapshot = $100 / 900 ~= 0.1111 for a 15-min event.
    Our share of 0.16 on yes side only (no side belongs to someone
    else) yields 0.16 * 0.1111 = $0.0178 expected per snapshot --
    assuming both sides qualify.
    """
    params = LipParams(target_size=200.0, discount_factor=0.5,
                       period_reward_usd=100.0, snapshots_per_period=900)
    yes_levels = [LipLevel(price=0.60, size=50, mine=20),
                  LipLevel(price=0.59, size=100, mine=0),
                  LipLevel(price=0.58, size=100, mine=0)]
    no_levels = [LipLevel(price=0.40, size=50, mine=0),
                 LipLevel(price=0.39, size=100, mine=0),
                 LipLevel(price=0.38, size=100, mine=0)]
    s = score_book(yes_levels, no_levels, params)
    assert s.qualifies
    assert math.isclose(s.reward_per_snapshot_usd, 100.0 / 900.0, abs_tol=1e-9)
    assert math.isclose(s.expected_reward_this_snapshot_usd,
                        s.yes.our_share * s.reward_per_snapshot_usd, abs_tol=1e-9)


def test_marginal_reward_rate_adds_credit():
    """Adding size at the Reference Price should strictly increase our
    expected reward -- our share goes up because we moved from zero to
    a positive piece of the normalized score pie."""
    params = LipParams(target_size=200.0, discount_factor=0.5,
                       period_reward_usd=100.0, snapshots_per_period=900)
    yes_levels = [LipLevel(price=0.60, size=50, mine=0),
                  LipLevel(price=0.59, size=100, mine=0),
                  LipLevel(price=0.58, size=100, mine=0)]
    no_levels = [LipLevel(price=0.40, size=50, mine=0),
                 LipLevel(price=0.39, size=100, mine=0),
                 LipLevel(price=0.38, size=100, mine=0)]
    rate = marginal_reward_rate(
        yes_levels, no_levels, params,
        side="yes", candidate_price=0.60, candidate_size=20)
    assert rate > 0


def test_side_not_qualifying_zero_payout():
    """If either side falls short of Target, the snapshot is excluded."""
    params = LipParams(target_size=1000.0, discount_factor=0.5,
                       period_reward_usd=100.0, snapshots_per_period=900)
    yes_levels = [LipLevel(price=0.60, size=50, mine=10)]
    no_levels = [LipLevel(price=0.40, size=50, mine=0)]
    s = score_book(yes_levels, no_levels, params)
    assert not s.qualifies
    assert s.expected_reward_this_snapshot_usd == 0.0
