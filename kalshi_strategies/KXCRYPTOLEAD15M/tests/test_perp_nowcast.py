import math

import pytest

from kalshi_strategies.KXCRYPTOLEAD15M import perp_nowcast as pn


def _warm(n, spot=100.0, perp=100.05, venues=pn.PERPS, t0=0.0):
    """Perps sit a steady +5bp over spot (USDT/funding basis)."""
    for i in range(30):
        t = t0 + i * 0.1
        n.on_spot("ETH", spot)
        for v in venues:
            n.on_perp(v, "ETH", perp, t)
    return t0 + 2.9


def test_steady_basis_gives_no_correction():
    n = pn.PerpNowcast()
    t = _warm(n)
    px, gap, k = n.nowcast("ETH", t, 100.0)
    assert k == 3 and gap == pytest.approx(0.0, abs=1e-6) and px == pytest.approx(100.0)


def test_perps_lead_moves_the_nowcast_then_closes_as_spot_catches_up():
    n = pn.PerpNowcast()
    t = _warm(n)
    for v in pn.PERPS:                                   # perps up 1bp, spot not yet
        n.on_perp(v, "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    px, gap, _ = n.nowcast("ETH", t + 0.05, 100.0)
    assert gap == pytest.approx(1.0, abs=0.05) and px > 100.0
    n.on_spot("ETH", 100.0 * math.exp(1e-4))            # spot catches up
    px2, gap2, _ = n.nowcast("ETH", t + 0.3, 100.0 * math.exp(1e-4))
    assert abs(gap2) < 0.1


def test_gap_is_capped():
    n = pn.PerpNowcast()
    t = _warm(n)
    for v in pn.PERPS:
        n.on_perp(v, "ETH", 100.05 * math.exp(10e-4), t + 0.05)
    _, gap, _ = n.nowcast("ETH", t + 0.05, 100.0)
    assert gap == pytest.approx(pn.CAP_BP)


def test_one_fresh_venue_falls_back_to_spot():
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(1e-4), t + pn.FRESH_S + 1)
    px, gap, k = n.nowcast("ETH", t + pn.FRESH_S + 1, 100.0)
    assert k == 1 and gap is None and px == 100.0


def test_median_ignores_one_outlier_venue():
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    n.on_perp("bybit_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    n.on_perp("okx_perp", "ETH", 100.05 * math.exp(50e-4), t + 0.05)   # bad print
    _, gap, _ = n.nowcast("ETH", t + 0.05, 100.0)
    assert gap == pytest.approx(1.0, abs=0.05)
