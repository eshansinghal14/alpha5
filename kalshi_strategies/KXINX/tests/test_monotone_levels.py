import numpy as np

from kalshi_strategies.KXINX import bl_pricing as pricing
from kalshi_strategies.KXINX.live_pricing import compute_live_bucket_probs


def test_already_monotone_is_unchanged():
    p = np.array([0.99, 0.8, 0.5, 0.2, 0.01])
    np.testing.assert_allclose(pricing.monotone_prob_above(p, np.full(5, 0.02)), p)


def test_violation_pooled_by_weight():
    # Tight 1.0 next to a wide 0.64: the wide one gives way.
    out = pricing.monotone_prob_above(np.array([0.64, 1.0, 0.9]), np.array([0.30, 0.01, 0.01]))
    assert np.all(np.diff(out) <= 1e-12)
    assert out[1] > 0.99 and out[0] > 0.99


def test_clipped_to_unit_interval_and_nan_preserved():
    out = pricing.monotone_prob_above(np.array([1.16, np.nan, 0.55, -0.22]), np.full(4, 0.05))
    assert np.isnan(out[1])
    assert out[0] == 1.0 and out[3] == 0.0


def _markets(edges):
    return [{"ticker": f"B{lo}", "floor_strike": lo, "cap_strike": hi - 0.0001}
            for lo, hi in zip(edges[:-1], edges[1:])]


def _chain(strikes, fwd, vol_pts, noise=None):
    """Undiscounted normal-model call/put prices; optional additive noise on calls."""
    from scipy.stats import norm
    d = (fwd - strikes) / vol_pts
    call = (fwd - strikes) * norm.cdf(d) + vol_pts * norm.pdf(d)
    if noise is not None:
        call = call + noise
    put = call - (fwd - strikes)
    return call, put


def test_live_buckets_sum_to_one_with_noisy_chain():
    strikes = np.arange(7600.0, 7905.0, 5.0)
    rng = np.random.default_rng(0)
    call, put = _chain(strikes, 7752.0, 12.0, noise=rng.normal(0, 0.6, strikes.size))
    mk = _markets(list(range(7650, 7876, 25)))
    args = (mk, strikes, call, call - 0.1, call + 0.1, strikes, put, put - 0.1, put + 0.1)
    old = compute_live_bucket_probs(*args)
    new = compute_live_bucket_probs(*args, monotone=True)
    s_old = sum(v[1] for v in old.values())
    s_new = sum(v[1] for v in new.values())
    assert abs(s_new - 1.0) < 0.02
    assert abs(s_new - 1.0) <= abs(s_old - 1.0) + 1e-9
    # bands untouched
    for t in new:
        assert new[t][0] <= new[t][1] <= new[t][2]
