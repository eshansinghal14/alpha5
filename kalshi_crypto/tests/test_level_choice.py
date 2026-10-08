"""
choose_quote_level: picks the resting price that maximises EV = LIP earned
while resting + P(fill) x (fair - price - adverse), rather than the first
tick outside the reservation.
"""
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.fill_intensity import FillIntensityModel
from kalshi_crypto.lip_scorer import LipLevel, LipParams, score_book
from kalshi_crypto.quoter import choose_quote_level, max_rest_price

PARAMS = LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=20.0,
                   snapshots_per_period=900)


def _book():
    # YES bids: reference price 0.25 (cum 692 >= 200 there), qualifies (>=1000).
    yes = [LipLevel(0.29, 120, 0), LipLevel(0.26, 2, 0), LipLevel(0.25, 570, 0),
           LipLevel(0.22, 1, 0), LipLevel(0.21, 120, 0), LipLevel(0.18, 400, 0)]
    no = [LipLevel(0.59, 119, 0), LipLevel(0.56, 124, 0), LipLevel(0.50, 120, 0),
          LipLevel(0.45, 700, 0)]
    return yes, no


def _quiet():
    m = FillIntensityModel.fallback()
    m.fallback_lambda = 1e-6          # almost never filled -> LIP dominates
    return m


def test_lip_dominated_picks_lowest_full_credit_level():
    """With fills negligible, the best price is the LOWEST level that still
    gets full LIP credit -- the reference price -- not the top of the
    allowed range and not below the reference."""
    yes, no = _book()
    ref = score_book(yes, no, PARAMS).yes.reference_price
    ch = choose_quote_level("yes", 0.40, yes, yes, no, PARAMS, max_price=0.30,
                            adverse_edge=0.01, size=1.0, tau=600.0, fill_model=_quiet())
    assert ref == pytest.approx(0.25)
    assert ch.price == pytest.approx(ref)
    assert ch.lip_usd > 0


def test_fill_ev_can_justify_quoting_above_reference():
    """If the queue fills often and fair is well above the quotes, earning
    the spread beats sitting at the reference."""
    yes, no = _book()
    busy = FillIntensityModel.fallback()
    busy.fallback_lambda = 0.05
    ch = choose_quote_level("yes", 0.60, yes, yes, no, PARAMS, max_price=0.30,
                            adverse_edge=0.01, size=1.0, tau=600.0, fill_model=busy)
    assert ch.price is not None and ch.fill_ev > 0


def test_no_quote_when_every_level_loses():
    """Fair below every candidate (fills lose money) and no LIP -> None."""
    yes, no = _book()
    busy = FillIntensityModel.fallback()
    busy.fallback_lambda = 0.05
    no_lip = LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=0.0)
    ch = choose_quote_level("yes", 0.05, yes, yes, no, no_lip, max_price=0.30,
                            adverse_edge=0.01, size=1.0, tau=600.0, fill_model=busy)
    assert ch.price is None


def test_max_rest_price_never_crosses():
    assert max_rest_price("yes", 0.25, 0.27, 1) == pytest.approx(0.26)
    assert max_rest_price("yes", 0.25, 0.26, 1) == pytest.approx(0.25)
    # NO side: own best = 1 - ask, opposite = 1 - bid.
    assert max_rest_price("no", 0.25, 0.30, 1) == pytest.approx(0.71)
