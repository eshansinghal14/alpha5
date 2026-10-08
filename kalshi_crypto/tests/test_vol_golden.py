"""
Golden cases from rv_modeling/vol_fix_reference (Oct 1 2026, 10:00-12:00
UTC Coinbase mids): our online RobustHarForecaster must reproduce the
reference features and rates to ~1e-6, and CoinRacePricer the reference
fairs (200k t5 draws) to within MC tolerance, given the same inputs.
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.config import CoinRaceConfig
from kalshi_crypto.pricer import CoinRacePricer
from kalshi_crypto.robust_har import RobustHarForecaster

REF = os.path.join(_PROJECT_ROOT, "kalshi_crypto", "rv_modeling", "vol_fix_reference")
GOLD = json.load(open(os.path.join(REF, "golden_cases.json")))
COINS = GOLD["coins"]


@pytest.fixture(scope="module")
def replayed():
    d = pd.read_csv(os.path.join(REF, "golden_mids_20261001_1000-1200utc.csv"))
    P = np.log(d[COINS].values)
    # Reference x0 for the clip's EWMA: mean r^2 of the first 3600 rows
    # (10 leading zeros included). Production warms up from data instead.
    x0 = {}
    for i, c in enumerate(COINS):
        r = np.zeros(len(P)); r[10:] = P[10:, i] - P[:-10, i]
        x0[c] = float(np.mean(r[:3600] ** 2))
    f = RobustHarForecaster(init_base_var=x0)
    want = {c["row_index_t"] - 1: c for c in GOLD["cases"]}   # features use rows <= t-1
    out = {}
    for row in range(max(want) + 1):
        f.update(float(d["ts"].iloc[row]), dict(zip(COINS, d.iloc[row][COINS].astype(float))))
        if row in want:
            c = want[row]
            out[c["row_index_t"]] = (f.logF(), f._raw_rates(max(c["tau"], 1.0)))
    return out


@pytest.mark.parametrize("case", GOLD["cases"], ids=lambda c: f"tau{c['tau']}")
def test_features_and_rates_match_reference(case, replayed):
    logF, rate = replayed[case["row_index_t"]]
    np.testing.assert_allclose(logF, np.array(case["logF_[1m,15m,1h]"]), atol=1e-6)
    np.testing.assert_allclose(rate, np.array(case["rate_per_s"]), rtol=1e-6)


class _Fixed:
    """Forecaster stub that hands the pricer the golden rate and R."""
    def __init__(self, rate, R):
        self._rate, self._R = np.asarray(rate), np.asarray(R)
    def rate_per_s(self, h, t=None): return self._rate
    def correlation(self, t=None): return self._R
    def forecast_cov_rate(self, t):
        s = np.sqrt(self._rate); return self._R * np.outer(s, s)


@pytest.mark.parametrize("case", GOLD["cases"], ids=lambda c: f"tau{c['tau']}")
def test_pricer_matches_reference_fairs(case):
    T, tau = 10_000.0, float(case["tau"])
    p = CoinRacePricer(_Fixed(case["rate_per_s"], case["R"]),
                       CoinRaceConfig(mc_n=200_000, t_nu=5.0, shrinkage_lambda=0.0, mc_seed=7))
    r = p.fairs_at(np.array(case["x"]), np.array(case["I"]), tau,
                   now_s=T - tau, S=T - 900.0, T=T, start_locked=True, compute_delta=False)
    assert math.isclose(r.tau_eff, case["tau_eff"], abs_tol=1e-9)
    np.testing.assert_allclose(r.diagnostics["sd"], np.array(case["sd"]), rtol=1e-9)
    np.testing.assert_allclose(r.probs, np.array(case["fairs_t5_200k"]), atol=0.005)


def test_sd_shrinks_like_sqrt_tau_eff_and_fairs_converge():
    """Hold x fixed: each coin's sd scales as sqrt(tau_eff) and the fairs
    converge to the argmax of x as tau -> 0."""
    case = GOLD["cases"][1]
    p = CoinRacePricer(_Fixed(case["rate_per_s"], case["R"]),
                       CoinRaceConfig(mc_n=20_000, t_nu=5.0, shrinkage_lambda=0.0))
    x = np.array([0.0, 0.0013, -0.0013, -0.0009, -0.0003])
    T = 10_000.0
    prev = None
    for tau in (860.0, 600.0, 300.0, 120.0, 61.0, 45.0, 20.0, 5.0):
        I = x if tau < 60 else np.zeros(5)
        r = p.fairs_at(x, I, tau, now_s=T - tau, S=T - 900.0, T=T, start_locked=True,
                       compute_delta=False)
        ratio = r.diagnostics["sd"] / np.sqrt(r.tau_eff * np.asarray(case["rate_per_s"]))
        np.testing.assert_allclose(ratio, 1.0, rtol=1e-12)
        if prev is not None:
            assert r.probs[1] >= prev - 0.01
        prev = r.probs[1]
    assert prev > 0.999
