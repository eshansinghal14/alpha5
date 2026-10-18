import math

import numpy as np

from kalshi_strategies.CRYPTO15M.pricer import implied_mu, prob_up

RATE = (0.5e-4) ** 2      # 0.5bp per sqrt(s)


def test_atm_is_half_and_monotone():
    ps = [prob_up(x * 1e-4, 0.0, 600.0, RATE, nu=5.0).prob for x in (-10, -2, 0, 2, 10)]
    assert abs(ps[2] - 0.5) < 1e-12
    assert all(a < b for a, b in zip(ps, ps[1:]))


def test_matches_monte_carlo():
    rng = np.random.default_rng(0)
    nu, tau, x = 5.0, 300.0, 3e-4
    z = rng.standard_t(nu, 400_000) * math.sqrt((nu - 2) / nu)
    sd = math.sqrt(RATE * (tau - 40))
    mc = float(np.mean(x + sd * z >= 0))
    assert abs(prob_up(x, 0.0, tau, RATE, nu=nu).prob - mc) < 3e-3


def test_last_minute_weights_observed_average():
    # Halfway through the end average with the observed half 2bp up and spot
    # back at the strike: mu = I, and sd collapses to the remaining 30s.
    r = prob_up(0.0, 1e-4, 30.0, RATE, nu=None)
    assert r.phase == "last_minute" and abs(r.mu - 1e-4) < 1e-15
    assert abs(r.sd - math.sqrt(RATE * 30 ** 3 / 10800)) < 1e-15
    assert abs(r.prob - 0.8970) < 1e-3     # Phi(1e-4 / 0.79e-4)


def test_basis_floors_sd_at_expiry():
    r = prob_up(0.0, 0.3e-4, 0.5, RATE, nu=None, basis_sd=0.4e-4)
    assert 0.7 < r.prob < 0.8


def test_theo_vol_is_slope_times_sigma_when_linear():
    tau, lat = 600.0, 2.0
    r = prob_up(0.0, 0.0, tau, RATE, nu=None, reaction_s=lat)
    slope = 1.0 / (math.sqrt(2 * math.pi) * r.sd)
    # 24-node quadrature of E|z| is ~1.6% off at the kink; fine for a band.
    assert abs(r.theo_vol / (slope * math.sqrt(RATE * lat)) - 1.0) < 0.03


def test_implied_mu_inverts():
    r = prob_up(4e-4, 0.0, 400.0, RATE, nu=5.0)
    assert abs(implied_mu(r.prob, r.sd, 5.0) - r.mu) < 1e-9
