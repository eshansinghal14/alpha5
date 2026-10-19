"""Correlation fitted to the race's own Kalshi mids."""
import dataclasses

import numpy as np

from kalshi_strategies.KXCRYPTOLEAD15M.corr_fit import CorrFit, _Fixed, fit_g
from kalshi_strategies.KXCRYPTOLEAD15M.pricer import CoinRacePricer
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config

R = np.full((5, 5), 0.85)
np.fill_diagonal(R, 1.0)
X = np.array([6e-4, 2e-4, 0.0, -2e-4, -4e-4])
RATE = np.full(5, (0.8e-4) ** 2)


def _fairs(p, g):
    stub = _Fixed()
    stub.R = R
    pr = CoinRacePricer(stub, dataclasses.replace(p, mc_n=20000))
    return pr.fairs_at(X, np.zeros(5), 600.0, now_s=0.0, S=0.0, T=900.0, start_locked=True, compute_delta=False,
                       rate_override=RATE, corr_mult=g).probs


def test_corr_mult_spreads_probabilities():
    p = config().pricing
    full, half = _fairs(p, 1.0), _fairs(p, 0.5)
    # Less correlation: the 6bp leader is less sure to stay ahead, the tail coins likelier.
    assert half[0] < full[0] and half[-1] > full[-1]
    assert abs(full.sum() - 1) < 1e-9 and abs(half.sum() - 1) < 1e-9


def test_fit_recovers_g_from_mids():
    p = config().pricing
    target = _fairs(p, 0.7)
    stub = _Fixed()
    pr = CoinRacePricer(stub, dataclasses.replace(p, mc_n=20000))
    g, err = fit_g(pr, stub, R, X, np.zeros(5), 600.0, RATE, 0.0, 900.0, 0.0, target, np.ones(5),
                   np.linspace(0.5, 1.0, 11))
    assert abs(g - 0.7) < 1e-9 and err < 0.003


def test_corrfit_ema_and_gates():
    p = config(corr_fit=True, corr_fit_s=0.0, corr_half_life_s=60.0, corr_mult_min=0.5, corr_mult_max=1.0,
               corr_fit_grid=11, corr_fit_mc_n=20000, corr_fit_max_spread=0.03, corr_fit_min_legs=3).pricing
    target = _fairs(p, 0.6)
    bid = {c: float(t) - 0.005 for c, t in zip(COINS, target)}
    ask = {c: float(t) + 0.005 for c, t in zip(COINS, target)}
    cf = CorrFit()
    assert cf.value(p) == 1.0
    cf.maybe_fit(0.0, p, R, X, np.zeros(5), 600.0, RATE, 0.0, 900.0, bid, ask, COINS)
    assert abs(cf.value(p) - 0.6) < 1e-9 and cf.n_fits == 1          # first fit is adopted outright
    wide = {c: v + 0.05 for c, v in ask.items()}
    cf.maybe_fit(10.0, p, R, X, np.zeros(5), 600.0, RATE, 0.0, 900.0, bid, wide, COINS)
    assert cf.n_fits == 1                                              # spreads too wide: no fit
    target2 = _fairs(p, 1.0)
    bid2 = {c: float(t) - 0.005 for c, t in zip(COINS, target2)}
    ask2 = {c: float(t) + 0.005 for c, t in zip(COINS, target2)}
    cf.maybe_fit(60.0, p, R, X, np.zeros(5), 600.0, RATE, 0.0, 900.0, bid2, ask2, COINS)
    assert abs(cf.value(p) - 0.8) < 1e-6                               # one half-life toward 1.0
    off = dataclasses.replace(p, corr_fit=False)
    assert cf.value(off) == 1.0


def test_race_prices_on_fitted_corr():
    import time
    from kalshi_strategies.KXCRYPTOLEAD15M.tests.test_model import _feed, _model
    now = time.time()
    m = _model(now, corr_fit=True, corr_mult_min=0.5, recovery_s=0.0)
    m.spot = type("S", (), {})()
    m.spot.sample = lambda now_s: None
    m.spot.corr_fit = cf = CorrFit()
    cf.g = 0.6
    cf._last_fit = now + 1e9            # no new fit in this test
    _feed(m, time.monotonic())
    out = m._price(now, None)
    assert all(f is not None for f in out.values()), m.withheld_reason
    assert all(abs(f.extras["corr_mult"] - 0.6) < 1e-12 for f in out.values())
    R0 = m.pricer._rates_corr(600.0, now)[1]
    assert np.allclose(m.pricer._last_corr, 0.6 * R0 + 0.4 * np.eye(5))
