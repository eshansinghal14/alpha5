"""Coin Race on vols backed out of the single-coin 15M markets."""
import math
import time

import numpy as np

from kalshi_strategies.CRYPTO15M.pricer import prob_up
from kalshi_strategies.KXCRYPTOLEAD15M.implied_rates import ImpliedRates, single_coin_tickers
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config


def test_single_coin_tickers():
    t = single_coin_tickers("KXCRYPTOLEAD15M-26OCT182215")
    assert t["BTC"] == "KXBTC15M-26OCT182215-15" and t["HYPE"] == "KXHYPE15M-26OCT182215-15"
    assert single_coin_tickers("KXCRYPTOLEAD15M-26OCT190000")["ETH"] == "KXETH15M-26OCT190000-00"


def test_rates_from_15m_mids():
    """Each coin's 15M mid priced at a known vol: the EMA recovers it."""
    p = config(implied_min_obs=5.0).pricing
    iv = ImpliedRates()
    true = {"BTC": 0.5e-4, "ETH": 0.7e-4, "SOL": 0.9e-4, "XRP": 0.8e-4, "HYPE": 1.1e-4}
    tau = 600.0
    # Spot 0.8 sd above the strike for each coin: mids ~0.78 (t5), inside the read range.
    x = {c: 0.8 * s * math.sqrt(tau - 40) for c, s in true.items()}
    mono = time.monotonic()
    for c, s in true.items():
        m = prob_up(x[c], 0.0, tau, s * s, nu=p.t_nu, basis_sd=p.implied_basis_sd_bp * 1e-4).prob
        assert 0.7 < m < 0.85
        iv.mids[c] = (m - 0.0025, m + 0.0025, mono)
    now = time.time()
    for k in range(10):
        for c in COINS:
            iv.observe(now + k, c, x[c], tau, 0.0, p)
    r = iv.rates(now + 10, p)
    for c, s in true.items():
        assert r[c] is not None and abs(math.sqrt(r[c]) / s - 1) < 0.1, (c, r[c])


def test_short_coin_has_no_rate_and_stale_mid_is_ignored():
    p = config(implied_min_obs=5.0, implied_max_mid_age_s=1.0).pricing
    iv = ImpliedRates()
    iv.mids["BTC"] = (0.70, 0.71, time.monotonic() - 5.0)       # 5s old: ignored
    now = time.time()
    for k in range(10):
        iv.observe(now + k, "BTC", 15e-4, 600.0, 0.0, p)
    assert iv.rates(now + 10, p)["BTC"] is None and iv.ema["BTC"].n_seen == 0


def _race(now, **pricing):
    from kalshi_strategies.KXCRYPTOLEAD15M.tests.test_model import _feed, _model
    m = _model(now, vol_source="implied", implied_min_obs=5.0, recovery_s=0.0, **pricing)
    m.spot = type("S", (), {})()
    m.spot.implied_rates = iv = ImpliedRates()
    m.spot.sample = lambda now_s: None
    _feed(m, time.monotonic())
    return m, iv


def _fill(iv, coins, now, s=1e-4):
    for c in coins:
        for k in range(6):
            iv.ema[c].add(now - 6 + k, type("O", (), {"s": s, "se_log": 0.1, "reason": None})(), 120.0)


def test_coin_without_implied_falls_back_to_realized():
    now = time.time()
    m, iv = _race(now, implied_fallback_realized=True)
    _fill(iv, [c for c in COINS if c != "ETH"], now)
    out = m._price(now, None)
    assert all(f is not None for f in out.values()), m.withheld_reason
    eth = out[m.ticker_of["ETH"]]
    har = m.pricer._rates_corr(600.0, now)[0][COINS.index("ETH")]
    assert eth.extras["vol_src"] == "realized" and abs(eth.extras["vol_bp"] - math.sqrt(har) * 1e4) < 1e-6
    btc = out[m.ticker_of["BTC"]]
    assert btc.extras["vol_src"] == "implied" and abs(btc.extras["vol_bp"] - 1.0) < 1e-6


def test_last_estimate_held_then_realized():
    """Readings stop: the last estimate prices for implied_hold_s, then the HAR fallback."""
    now = time.time()
    m, iv = _race(now, implied_fallback_realized=True, implied_hold_s=180.0, implied_ema_s=120.0)
    _fill(iv, COINS, now, s=0.7e-4)
    out = m._price(now, None)
    assert m.vol_src["ETH"] == "implied" and abs(out[m.ticker_of["ETH"]].extras["vol_bp"] - 0.7) < 1e-6
    # 150s with no readings: the 6 decay below implied_min_obs (5) -> held.
    out = m._price(now + 150, None)
    assert m.vol_src["ETH"] == "held" and abs(out[m.ticker_of["ETH"]].extras["vol_bp"] - 0.7) < 1e-6
    assert iv.held_age(now + 150, m.cfg.pricing)["ETH"] is not None
    # Past the hold: HAR.
    out = m._price(now + 400, None)
    har = m.pricer._rates_corr(600.0, now + 400)[0][COINS.index("ETH")]
    assert m.vol_src["ETH"] == "realized" and abs(out[m.ticker_of["ETH"]].extras["vol_bp"] - math.sqrt(har) * 1e4) < 1e-6


def test_no_fallback_withholds():
    now = time.time()
    m, iv = _race(now, implied_fallback_realized=False)
    _fill(iv, [c for c in COINS if c != "ETH"], now)
    out = m._price(now, None)
    assert all(f is None for f in out.values()) and m.withheld_reason == "no_implied_vol:ETH"


def test_pricer_rate_override():
    from kalshi_strategies.KXCRYPTOLEAD15M.cov_forecaster import ConstantCovForecaster
    from kalshi_strategies.KXCRYPTOLEAD15M.pricer import CoinRacePricer
    p = config().pricing
    pr = CoinRacePricer(ConstantCovForecaster(), p)
    x = np.array([5e-4, 0, 0, 0, 0])
    kw = dict(now_s=0.0, S=0.0, T=900.0, start_locked=True, compute_delta=False)
    base = pr.fairs_at(x, np.zeros(5), 600.0, **kw)
    low = pr.fairs_at(x, np.zeros(5), 600.0, rate_override=np.full(5, (0.2e-4) ** 2), **kw)
    high = pr.fairs_at(x, np.zeros(5), 600.0, rate_override=np.full(5, (3e-4) ** 2), **kw)
    # Less vol: the 5bp leader is surer; more vol: closer to 1/5.
    assert low.probs[0] > base.probs[0] > high.probs[0] > 0.2
    assert np.allclose(pr._last_rate, (3e-4) ** 2)
    again = pr.fairs_at(x, np.zeros(5), 600.0, rate_override=np.full(5, (0.2e-4) ** 2), **kw)
    assert np.allclose(again.probs, low.probs)                  # draws rebuilt for the new rates
