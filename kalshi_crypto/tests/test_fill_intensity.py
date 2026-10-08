"""
Fill-intensity model + expected-rest-time LIP credit tests.

Covers:
  * FillIntensityModel fallback (fixed lambda) and fitted-artifact lookup.
  * expected_rest_time_s closed form: -> tau as lambda->0, bounded by tau,
    -> 1/lambda for large lambda*tau.
  * g() queue adjustment in [0,1], monotone in ticks and queue.
  * lip_credit_per_contract: expected-rest credit < full-period upper bound,
    and recovers the upper bound as lambda->0.
"""
import math
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.fill_intensity import (FillIntensityModel,
                                          FALLBACK_LAMBDA_PER_S)
from kalshi_crypto.lip_scorer import LipParams, LipLevel
from kalshi_crypto.quoter import lip_credit_per_contract


def _params():
    return LipParams(target_size=1000, discount_factor=0.5,
                     period_reward_usd=20.0, snapshots_per_period=900)


def _book():
    yes = [LipLevel(0.20, 500.0, 0.0), LipLevel(0.19, 600.0, 0.0)]
    no = [LipLevel(0.75, 500.0, 0.0), LipLevel(0.74, 600.0, 0.0)]
    return yes, no


def test_fallback_model_fixed_lambda():
    m = FillIntensityModel.fallback()
    assert not m.fitted
    assert math.isclose(m.lambda_side("yes", 300.0, 0.2), FALLBACK_LAMBDA_PER_S)
    assert math.isclose(m.lambda_side("no", 10.0, 0.9), FALLBACK_LAMBDA_PER_S)


def test_expected_rest_time_limits():
    m = FillIntensityModel.fallback()
    # lambda -> 0: E[rest] -> tau (the full-period upper bound).
    assert math.isclose(m.expected_rest_time_s(0.0, 300.0), 300.0, abs_tol=1e-9)
    # bounded by tau.
    assert m.expected_rest_time_s(0.02, 300.0) <= 300.0 + 1e-9
    # large lambda*tau: E[rest] -> 1/lambda.
    assert math.isclose(m.expected_rest_time_s(1.0, 10_000.0), 1.0, abs_tol=1e-6)
    # monotone decreasing in lambda.
    assert (m.expected_rest_time_s(0.01, 300.0)
            > m.expected_rest_time_s(0.05, 300.0))


def test_queue_adjustment_bounds_and_monotonicity():
    m = FillIntensityModel.fallback()
    # At the touch with an empty queue, g == 1.
    assert math.isclose(m.g(0.0, 0.0), 1.0, abs_tol=1e-9)
    # Always in [0, 1].
    for ticks in (0, 1, 3):
        for q in (0, 10, 100, 1000):
            g = m.g(ticks, q)
            assert 0.0 <= g <= 1.0
    # Decreasing in ticks-from-best and in queue-ahead.
    assert m.g(0, 0) > m.g(1, 0) > m.g(2, 0)
    assert m.g(0, 0) > m.g(0, 50) > m.g(0, 500)


def test_credit_expected_below_upper_bound():
    """The expected-rest-time credit must be strictly below the old
    full-period upper bound whenever lambda > 0."""
    yes, no = _book()
    p = _params()
    m = FillIntensityModel.fallback()
    ub = lip_credit_per_contract(yes, no, p, side="yes",
                                 candidate_price=0.20, candidate_size=1.0)
    exp = lip_credit_per_contract(yes, no, p, side="yes",
                                  candidate_price=0.20, candidate_size=1.0,
                                  tau=900.0, fill_model=m,
                                  ticks_from_best=0.0, queue_ahead=0.0)
    assert 0.0 < exp < ub


def test_credit_recovers_upper_bound_as_lambda_to_zero():
    """With a near-zero fill intensity the expected credit converges to
    the full-period upper bound (order never fills, rests the whole time)."""
    yes, no = _book()
    p = _params()
    m = FillIntensityModel.fallback()
    m.fallback_lambda = 1e-9
    ub = lip_credit_per_contract(yes, no, p, side="yes",
                                 candidate_price=0.20, candidate_size=1.0)
    exp = lip_credit_per_contract(yes, no, p, side="yes",
                                  candidate_price=0.20, candidate_size=1.0,
                                  tau=900.0, fill_model=m)
    assert math.isclose(exp, ub, rel_tol=1e-4)


def test_credit_zero_size_is_zero():
    yes, no = _book()
    p = _params()
    m = FillIntensityModel.fallback()
    assert lip_credit_per_contract(yes, no, p, side="yes",
                                   candidate_price=0.20, candidate_size=0.0,
                                   tau=900.0, fill_model=m) == 0.0
