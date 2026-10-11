"""CoinRaceModel gates and Fair shape, offline (no network)."""
import time

import pandas as pd

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBar, CompositeBus
from kalshi_strategies.KXCRYPTOLEAD15M.cov_forecaster import ConstantCovForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook
from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config

PX = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 1.4, "HYPE": 84.0}


def _event(S, T):
    ms = [{"ticker": f"EV-{c}", "open_time": pd.Timestamp(S, unit="s", tz="UTC").isoformat(),
           "close_time": pd.Timestamp(T, unit="s", tz="UTC").isoformat()} for c in COINS]
    return EventInfo("EV", pd.Timestamp(S, unit="s", tz="UTC"), pd.Timestamp(T, unit="s", tz="UTC"), ms)


def _book(coin, mono, mult=1.0):
    p = PX[coin] * mult
    return VenueBook(venue="coinbase", product=f"{coin}-USD", coin=coin,
                     bids=(OrderBookLevel(p * 0.9999, 5.0),), asks=(OrderBookLevel(p * 1.0001, 5.0),),
                     receive_ts=mono)


def _model(now, **pricing):
    cfg = config(**pricing)
    m = CoinRaceModel(_event(now - 300, now + 600), cfg, bus=CompositeBus(coins=COINS, persist=False),
                      coinbase=None, forecaster=ConstantCovForecaster())
    # Lock the start TWAP at today's prices: 60 bars over [S-60, S).
    S = m.window.S
    m.tracker.backfill({c: [CompositeBar(c, S - 60 + i, PX[c], 1.0, 1, 0, False, PX[c], 1) for i in range(61)]
                        for c in COINS})
    return m


def _feed(m, mono, mults=None):
    for c in COINS:
        m.bus.observe(_book(c, mono, (mults or {}).get(c, 1.0)))


def test_fairs_sum_to_one_with_band():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic())
    fairs = m.reprice(now)
    assert set(fairs) == set(m.tickers) and all(f is not None for f in fairs.values())
    assert abs(sum(f.prob for f in fairs.values()) - 1.0) < 1e-9
    k = m.cfg.pricing.ci_vol_multiplier
    for f in fairs.values():
        tv = f.extras["theo_vol"]
        assert abs((f.hi - f.prob) - k * tv) < 1e-12 and abs((f.prob - f.lo) - k * tv) < 1e-12


def test_leader_is_priced_up():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic(), {"SOL": 1.01})
    fairs = m.reprice(now)
    assert max(fairs, key=lambda t: fairs[t].prob) == m.ticker_of["SOL"]


def test_down_coin_withholds_every_fair():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    for c in COINS[:-1]:
        m.bus.observe(_book(c, mono))
    fairs = m.reprice(now)
    assert all(f is None for f in fairs.values())
    assert m.withheld_reason.startswith("coin_down")
    assert m.signal_age_s() is None


def test_cross_coin_skew_withholds():
    now = time.time()
    m = _model(now, recovery_s=0.0, max_cross_coin_skew_s=0.5)
    mono = time.monotonic()
    for c in COINS:
        m.bus.observe(_book(c, mono - (2.0 if c == "BTC" else 0.0)))
    fairs = m.reprice(now)
    assert all(f is None for f in fairs.values()) and m.withheld_reason == "cross_coin_skew"


def test_wakeup_only_on_mid_change_and_age_follows_live_books():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    woke = []
    m.set_wakeup(lambda: woke.append(1))
    mono = time.monotonic()
    for c in COINS:
        m._on_book(_book(c, mono - 5.0))
    m.reprice(now)
    n = len(woke)
    m._on_book(_book("BTC", mono))            # same mid: no wakeup
    assert len(woke) == n
    # Fair is current (no mid moved), so its age tracks the books, which a
    # heartbeat freshens in place.
    for vbs in m.bus.venue_books_per_coin().values():
        for vb in vbs:
            vb.receive_ts = time.monotonic()
    assert m.signal_age_s() < 0.5
    m._on_book(_book("BTC", time.monotonic(), 1.01))   # mid moved
    assert len(woke) == n + 1
    assert m.signal_age_s() >= 4.9            # dirty: age of the inputs under the fair


def test_spot_feed_carries_state_across_events():
    """The session feed outlives events: a new event's model sees the same
    forecaster/health, gets wakeups once attached, and its tracker is seeded
    from bars sampled while it was being built."""
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    now = time.time()
    cfg = config(recovery_s=0.0)
    spot = SpotFeed(cfg, persist=False, venues=False)
    spot.forecaster = ConstantCovForecaster()
    spot.forecaster_warmed = True
    a = CoinRaceModel(_event(now - 300, now + 600), cfg, spot=spot)
    spot.attach(a)
    woke = []
    a.set_wakeup(lambda: woke.append("a"))
    mono = time.monotonic()
    for c in COINS:
        spot._on_book(_book(c, mono))
    assert woke and a.forecaster is spot.forecaster and a.health is spot.health
    spot.detach(a)
    b = CoinRaceModel(_event(now - 60, now + 840), cfg, spot=spot)
    spot.sample(now - 30)      # lands in b's start window while b is unattached
    spot.attach(b)
    b.set_wakeup(lambda: woke.append("b"))
    spot._on_book(_book("BTC", mono, 1.01))
    assert woke[-1] == "b"
    assert any(bar.ts_s == int(now - 30) for bar in b.tracker._bars["BTC"])


def _last_minute_model(now, mid_ret, cf_ret, **pricing):
    """Model 30s from close whose end-average bars give each coin `mid_ret`
    on the mid and `cf_ret` on the CF replica."""
    cfg = config(recovery_s=0.0, **pricing)
    m = CoinRaceModel(_event(now - 870, now + 30), cfg, bus=CompositeBus(coins=COINS, persist=False),
                      coinbase=None, forecaster=ConstantCovForecaster())
    S, T = m.window.S, m.window.T
    bars = {}
    for c in COINS:
        p = PX[c]
        start = [CompositeBar(c, S - 60 + i, p, 1.0, 1, 0, False, p, 1) for i in range(60)]
        end = [CompositeBar(c, T - 60 + i, p * (1 + cf_ret[c]), 1.0, 1, 0, False, p * (1 + mid_ret[c]), 1)
               for i in range(30)]
        bars[c] = start + end
    m.tracker.backfill(bars)
    return m


def test_basis_tie_blocks_both_sides_of_contenders_only():
    now = time.time()
    flat = {c: 0.0 for c in COINS}
    # Mid says HYPE by 1bp, CF says BTC by 1bp: KXCRYPTOLEAD15M-26OCT092330's shape.
    mid = {**flat, "HYPE": 0.0011, "BTC": 0.0010}
    cf = {**flat, "HYPE": 0.0010, "BTC": 0.0011}
    m = _last_minute_model(now, mid, cf)
    _feed(m, time.monotonic(), {"HYPE": 1.0011, "BTC": 1.0010})
    fairs = m.reprice(now)
    assert m.basis_tie == {"HYPE", "BTC"}
    for c in COINS:
        t = m.ticker_of[c]
        want = "basis_tie" if c in ("HYPE", "BTC") else None
        assert m.side_block(t, "yes", fairs[t], now) == want
        assert m.side_block(t, "no", fairs[t], now) == want


def test_clear_leader_on_both_sources_is_not_a_basis_tie():
    now = time.time()
    lead = {**{c: 0.0 for c in COINS}, "SOL": 0.003}
    m = _last_minute_model(now, lead, lead)
    _feed(m, time.monotonic(), {"SOL": 1.003})
    fairs = m.reprice(now)
    assert m.basis_tie == frozenset()
    assert all(m.side_block(t, "yes", f, now) is None for t, f in fairs.items())


def test_basis_tie_off_outside_last_minute():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic())
    m.reprice(now)
    assert m.basis_tie == frozenset()


def test_spot_burst_pulls_quotes_for_every_market_then_expires():
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    now = time.time()
    cfg = config(recovery_s=0.0, burst_usd_1s={c: 1000.0 for c in COINS}, burst_hold_s=1.5)
    spot = SpotFeed(cfg, persist=False, venues=False)
    spot.forecaster = ConstantCovForecaster()
    m = CoinRaceModel(_event(now - 300, now + 600), cfg, spot=spot)
    spot.attach(m)
    woke = []
    m.set_wakeup(lambda: woke.append(1))
    mono = time.monotonic()
    spot._on_print("BTC", {"price": "80000", "size": "0.005", "trade_id": 1}, mono, None)   # $400
    assert m.quote_block(m.ticker_of["ETH"], now) is None and not woke
    spot._on_print("BTC", {"price": "80000", "size": "0.01", "trade_id": 2}, mono + 0.2, None)  # $1200 in 1s
    assert woke
    assert all(m.quote_block(t, now) == "spot_burst" for t in m.tickers)
    spot.burst_until = time.monotonic() - 0.01
    assert m.quote_block(m.ticker_of["ETH"], now) is None


def test_signal_age_is_oldest_coins_newest_book():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    for c in COINS:
        m.bus.observe(_book(c, mono - (0.3 if c == "HYPE" else 0.0)))
    m.reprice(now)
    # (mono - 0.3) then (later - that) rounds to 0.2999999998 sometimes.
    assert m.signal_age_s() >= 0.3 - 1e-6


def test_burst_calibrator_tracks_its_own_quantile_and_falls_back_until_ready():
    import numpy as np
    from kalshi_strategies.KXCRYPTOLEAD15M import prints as P
    cal = P.BurstCalibrator(["BTC"], {"BTC": 123.0})
    assert cal.threshold("BTC") == 123.0                      # nothing sampled: static fallback
    n = int(P.MIN_CALIB_S / P.SAMPLE_S)
    rng = np.random.default_rng(0)
    cal.seed(rng.uniform(0, 1000, size=(n, 1)))
    assert cal.ready
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert 990 < cal.threshold("BTC") < 1000                  # p99.5 of U(0, 1000)
    cal.seed(rng.uniform(0, 10_000, size=(len(cal._buf[0]), 1)))   # volume regime 10x higher
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert cal.threshold("BTC") > 9_000


def test_burst_calibrator_keeps_last_value_when_a_coin_goes_quiet():
    import numpy as np
    from kalshi_strategies.KXCRYPTOLEAD15M import prints as P
    cal = P.BurstCalibrator(["XRP"], None)
    cal.seed(np.full((int(P.MIN_CALIB_S / P.SAMPLE_S), 1), 500.0))
    cal.adopt(cal.quantiles(cal.snapshot()))
    cal.seed(np.zeros((len(cal._buf[0]), 1)))
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert cal.threshold("XRP") == 500.0                      # a zero quantile would trip on any print


def test_detector_level_decays_without_new_prints():
    from kalshi_strategies.KXCRYPTOLEAD15M.prints import BurstDetector
    d = BurstDetector(["BTC"])
    assert d.add("BTC", 1000.0, 10.0) == 1000.0
    assert d.level("BTC", 10.5) == 1000.0
    assert d.level("BTC", 11.01) == 0.0
