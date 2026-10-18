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


def test_fast_ema_tracks_a_step_and_recalibrates_slow():
    e = ImpliedVolEMA()
    a = observe(_mid(15e-4), 0.005, 15e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    for t in range(600):
        e.add(float(t), a, 900.0, 60.0)
    s2 = 1.6 * S
    b = observe(_mid(15e-4, s=s2), 0.005, 15e-4, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW)
    for t in range(600, 720):
        e.add(float(t), b, 900.0, 60.0)
    lf, se, n = e.fast_stats(720.0, 60.0)
    slow = e.value(720.0, 900.0, 1)
    assert abs(math.exp(lf) / s2 - 1) < 0.15 and slow < 1.25 * S and se < 0.05
    e.recalibrate(720.0, 900.0, 60.0)
    assert abs(e.value(720.0, 900.0, 1) / math.exp(lf) - 1) < 1e-9 and e.n_recal == 1


def test_weights_at_reference_vol_are_unbiased():
    """Readings at 0.8x and 1.25x the true vol, same spot. Weighted at their own
    vol the two get different weights (which one wins depends on the point), so
    the mean is off the true vol; at the EMA's vol they weigh the same and the
    mean is exact."""
    x = 12e-4
    lo, hi = 0.8 * S, 1.25 * S
    own = [observe(_mid(x, s=v), 0.005, x, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, **KW) for v in (lo, hi)]
    ref = [observe(_mid(x, s=v), 0.005, x, 1.0, TAU, 0.0, 5.0, spot_err=1e-4, ref_s=S, **KW) for v in (lo, hi)]
    assert all(o.reason is None for o in own + ref)
    assert abs(own[0].se_log - own[1].se_log) > 1e-4
    assert abs(ref[0].se_log - ref[1].se_log) < 1e-12

    def mean(obs):
        e = ImpliedVolEMA()
        for t in range(50):
            for o in obs:
                e.add(float(t), o, 900.0)
        return e.value(50.0, 900.0, 1)

    assert abs(mean(ref) / S - 1) < 1e-9
    assert abs(mean(own) / S - 1) > 1e-4


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
