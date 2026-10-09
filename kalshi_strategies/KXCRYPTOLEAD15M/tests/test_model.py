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


def test_signal_age_is_oldest_coins_newest_book():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    for c in COINS:
        m.bus.observe(_book(c, mono - (0.3 if c == "HYPE" else 0.0)))
    m.reprice(now)
    assert m.signal_age_s() >= 0.3
