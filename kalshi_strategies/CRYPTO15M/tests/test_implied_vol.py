import math

from kalshi_strategies.CRYPTO15M.implied_vol import ImpliedVolEMA, observe
from kalshi_strategies.CRYPTO15M.pricer import prob_up

KW = dict(mid_noise_floor=0.005, max_obs_se=0.5, max_curvature=0.5, s_bounds=(1e-5, 5e-4))
S = 1e-4            # 1bp / sqrt(s)
TAU = 600.0


def _mid(x, s=S, b=0.0):
    return prob_up(x, 0.0, TAU, s * s, nu=5.0, basis_sd=b).prob


def test_recovers_vol_off_the_money():
    m = _mid(10e-4)
    o = observe(m, 0.005, 10e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    assert o.reason is None and abs(o.s / S - 1) < 1e-6


def test_atm_carries_no_weight():
    m = _mid(0.2e-4)
    o = observe(m, 0.005, 0.2e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    assert o.reason is not None     # near 0.5 the vol is unidentified


def test_recent_spot_move_downweights():
    m = _mid(10e-4)
    calm = observe(m, 0.005, 10e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    moved = observe(m, 0.005, 10e-4, 1.0, TAU, 0.0, 5.0, spot_err=5e-4, **KW)
    assert moved.reason is not None or moved.se_log > 2 * calm.se_log


def test_mid_on_wrong_side_is_level_not_vol():
    o = observe(0.40, 0.005, 10e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    assert o.reason == "sign"


def test_ema_needs_min_obs_and_decays():
    e = ImpliedVolEMA()
    good = observe(_mid(10e-4), 0.005, 10e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    for t in range(5):
        e.add(float(t), good, 900.0)
    assert e.value(5.0, 900.0, min_obs=20) is None
    for t in range(5, 40):
        e.add(float(t), good, 900.0)
    assert abs(e.value(40.0, 900.0, min_obs=20) / S - 1) < 1e-6
    # An hour with nothing accepted: decayed below min_obs, so no vol.
    assert e.value(40.0 + 3600, 900.0, min_obs=20) is None


def test_precision_weighting_favours_informative_obs():
    e = ImpliedVolEMA()
    sharp = observe(_mid(15e-4), 0.005, 15e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    s2 = 2 * S
    vague = observe(_mid(6e-4, s=s2), 0.005, 6e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    assert sharp.reason is None and vague.reason is None and sharp.se_log < vague.se_log
    for t in range(30):
        e.add(float(t), sharp, 900.0)
        e.add(float(t), vague, 900.0)
    v = e.value(30.0, 900.0, min_obs=1)
    assert S < v < math.sqrt(S * s2)   # pulled toward the sharper observation
