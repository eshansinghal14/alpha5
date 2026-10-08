"""
Theo freshness: the fair is recomputed from the latest mid on every
wakeup (no cache to go stale), the deltas refresh on their own cadence,
the kill switch pulls quotes when the price feed is behind, and an
in-window event without a start TWAP is never priced off x = 0.

Regression for 2026-10-08: the fair was cached on the composite loop,
which stalled under a Kalshi burst near the close; BTC theo sat at 0.10
for ~6s while the market ran to 0.84.
"""
import os
import sys
import time
import types

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_PROJECT_ROOT,
           os.path.join(_PROJECT_ROOT, "kalshi_backtest"),
           os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "live"),
           os.path.join(_PROJECT_ROOT, "kalshi_infra")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kalshi_crypto.config import CoinRaceConfig
from kalshi_crypto.composite import CompositeBar, CompositeBus
from kalshi_crypto.cov_forecaster import ConstantCovForecaster
from kalshi_crypto.event_state import EventState
from kalshi_crypto.lip_scorer import LipParams
from kalshi_crypto.series import COINS, market_ticker
from kalshi_crypto.settlement_tracker import EventWindow, SettlementTracker
from kalshi_crypto.spot_feed.base import OrderBookLevel, VenueBook
from kalshi_crypto.strategy import COMPOSITE_EVENT_KIND, CoinRaceStrategy


def _make_strategy(with_start_twap=True):
    now = time.time()
    window = EventWindow(event_ticker="E", S=now - 120, T=now + 600)
    tickers = {c: market_ticker("E", c) for c in COINS}
    markets = {c: {"ticker": tickers[c], "open_time": "", "close_time": ""} for c in COINS}
    lip = {c: LipParams(target_size=1000, discount_factor=0.5,
                        period_reward_usd=20.0, snapshots_per_period=900) for c in COINS}
    tracker = SettlementTracker(window)
    if with_start_twap:
        for ts in range(int(window.S - 60), int(window.S)):
            for c in COINS:
                tracker.ingest(CompositeBar(coin=c, ts_s=float(ts), price=100.0,
                                            utilized_depth=1.0, n_venues_in=1,
                                            n_venues_dropped=0, is_fallback=False,
                                            fallback_mid=100.0, nowcast_count=1))
    es = EventState(event_ticker="E", window=window, markets=markets,
                    tickers=tickers, tracker=tracker, lip_params=lip)
    strat = CoinRaceStrategy(event_state=es, config=CoinRaceConfig(mc_n=4000),
                             forecaster=ConstantCovForecaster(sigma_per_sec=1e-4, rho=0.6),
                             record_debug=False)
    bus = CompositeBus(coins=COINS, persist=False)
    strat.attach_composite(bus)
    return strat, bus


def _feed(bus, now_mono, mids):
    for c in COINS:
        m = mids.get(c, 100.0)
        bus.observe(VenueBook(venue="coinbase", product=f"{c}-USD", coin=c,
                              bids=(OrderBookLevel(m - 0.01, 5.0),),
                              asks=(OrderBookLevel(m + 0.01, 5.0),),
                              receive_ts=now_mono))


class _Ctx:
    def __init__(self, t, evt):
        self.time = pd.Timestamp(t, unit="s", tz="UTC")
        self.last_event = evt
        self.portfolio = types.SimpleNamespace(fills=[])
    def book(self, ticker): return None
    def position(self, ticker):
        return types.SimpleNamespace(yes_qty=0.0, no_qty=0.0)
    def buy(self, **kw): return types.SimpleNamespace(order_id=1)
    def cancel(self, oid): pass


def _warm(strat, bus, mono, t0):
    for i in range(8):
        mono[0] += 1.0
        _feed(bus, mono[0], {})
        strat(_Ctx(t0 + i, {"kind": COMPOSITE_EVENT_KIND, "ts": t0 + i}))


def test_kalshi_event_reprices_from_latest_mid(monkeypatch):
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    assert strat._last_fair_result is not None
    btc = COINS.index("BTC")
    before = float(strat._last_fair_result.probs[btc])
    # BTC rips 1% in the book; NO composite tick, only a Kalshi book event.
    mono[0] += 0.05
    _feed(bus, mono[0], {"BTC": 101.0})
    strat(_Ctx(t0 + 8.05, types.SimpleNamespace(ticker=strat.event_state.tickers["BTC"])))
    after = float(strat._last_fair_result.probs[btc])
    assert after > before + 0.3, (before, after)


def test_deltas_refresh_on_their_own_cadence(monkeypatch):
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    calls = []
    orig = strat.pricer.fairs_at
    monkeypatch.setattr(strat.pricer, "fairs_at",
                        lambda *a, **k: (calls.append(k.get("compute_delta")), orig(*a, **k))[1])
    evt = types.SimpleNamespace(ticker=strat.event_state.tickers["ETH"])
    last = strat._last_delta_s
    strat(_Ctx(last + 0.1, evt))                       # Kalshi event: fair only
    strat(_Ctx(last + 0.2, {"kind": COMPOSITE_EVENT_KIND, "ts": last + 0.2}))  # < 0.5s: fair only
    strat(_Ctx(last + 0.7, {"kind": COMPOSITE_EVENT_KIND, "ts": last + 0.7}))  # >= 0.5s: deltas
    assert calls == [False, False, True], calls


def test_kill_switch_when_price_feed_behind(monkeypatch):
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    assert not strat.theo_stale()
    mono[0] += strat.config.theo_max_age_s + 0.5      # no books arrive
    assert strat.theo_stale()


def test_no_pricing_in_window_without_start_twap(monkeypatch):
    strat, bus = _make_strategy(with_start_twap=False)
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    _warm(strat, bus, mono, time.time())
    assert strat._last_fair_result is None


def test_last_minute_mu_is_expected_final_average(monkeypatch):
    """30s of the end window observed at +1%, price still +1%: the pricer's
    mu must be the expected final average (+1%), not 1.5% -- I is the
    partial SUM of observed returns / 60, not the partial average."""
    import math
    import kalshi_crypto.strategy as sm
    now = time.time()
    window = EventWindow(event_ticker="E", S=now - 870, T=now + 30)
    tracker = SettlementTracker(window)
    bar = lambda c, t, p: CompositeBar(coin=c, ts_s=float(t), price=p, utilized_depth=1.0,
                                       n_venues_in=1, n_venues_dropped=0, is_fallback=False,
                                       fallback_mid=p, nowcast_count=1)
    for ts in range(int(window.S - 60), int(window.S)):
        for c in COINS:
            tracker.ingest(bar(c, ts, 100.0))
    for ts in range(int(window.T - 60), int(now)):
        for c in COINS:
            tracker.ingest(bar(c, ts, 101.0))
    tickers = {c: market_ticker("E", c) for c in COINS}
    es = EventState(event_ticker="E", window=window,
                    markets={c: {"ticker": tickers[c]} for c in COINS},
                    tickers=tickers, tracker=tracker, lip_params={})
    strat = CoinRaceStrategy(event_state=es, config=CoinRaceConfig(mc_n=2000),
                             forecaster=ConstantCovForecaster(sigma_per_sec=1e-4, rho=0.6),
                             record_debug=False)
    bus = CompositeBus(coins=COINS, persist=False)
    strat.attach_composite(bus)
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    for _ in range(8):
        mono[0] += 1.0
        _feed(bus, mono[0], {c: 101.0 for c in COINS})
        strat.feed_health.observe(bus.venue_books_per_coin(), now=mono[0])
    strat._down_coins = []
    strat._reprice(now, compute_delta=False)
    mu = strat._last_fair_result.mu
    assert abs(mu[0] - math.log(1.01)) < 2e-4, mu


def test_no_fair_when_one_coin_resumes_before_the_others(monkeypatch):
    """2026-10-08 16:34:33: after a Coinbase stall, BTC's book resumed
    before the other four, so x compared a moved BTC with four frozen
    coins and theo jumped 0.28 -> 0.63. With the cross-coin skew guard the
    fair is withheld until all five are fresh again."""
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    assert strat._last_fair_result is not None
    # 5s stall: no books for anyone. Then ONLY BTC updates, +0.2%.
    mono[0] += 5.0
    bus.observe(VenueBook(venue="coinbase", product="BTC-USD", coin="BTC",
                          bids=(OrderBookLevel(100.19, 5.0),), asks=(OrderBookLevel(100.21, 5.0),),
                          receive_ts=mono[0]))
    strat._down_coins = []
    strat._reprice(t0 + 13, compute_delta=False)
    assert strat._last_fair_result is None
    # Everyone catches up -> fair returns.
    _feed(bus, mono[0] + 0.05, {"BTC": 100.2})
    mono[0] += 0.05
    strat._reprice(t0 + 13.05, compute_delta=False)
    assert strat._last_fair_result is not None


def test_take_needs_signal_to_hold(monkeypatch):
    strat, _ = _make_strategy()
    k = ("T", "take_yes")
    assert not strat._take_ok(k, 100.0)            # first sight
    assert not strat._take_ok(k, 100.3)            # still inside confirm window
    assert strat._take_ok(k, 100.8)                # held 0.8s -> one take
    assert not strat._take_ok(k, 100.85)           # no second take in the window


def test_backlogged_message_counts_as_stale():
    """A frame that sat 4s in our buffers after a loop stall must NOT look
    fresh just because we processed it now (2026-10-08 ...1300)."""
    from kalshi_crypto.spot_feed.coinbase import CoinbaseAdapter
    a = CoinbaseAdapter(coins=["BTC"])
    now_wall = time.time()
    mono = time.monotonic()
    # Establish the baseline offset with a prompt message (100ms network+skew).
    assert a._data_age(now_wall - 0.10, mono) == 0.0
    # A message stamped 4.1s ago processed now: ~4s of OUR delay.
    age = a._data_age(time.time() - 4.10, time.monotonic())
    assert 3.9 < age < 4.2, age


def test_quotes_stay_off_until_fresh_for_recovery_window(monkeypatch):
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    for _ in range(3):                         # get past the initial hold
        mono[0] += 1.0
        _feed(bus, mono[0], {})
        strat._reprice(t0 + 9, compute_delta=False)
        strat.theo_stale()
    assert not strat.theo_stale()
    mono[0] += strat.config.theo_max_age_s + 0.5        # feed goes quiet
    assert strat.theo_stale()
    _feed(bus, mono[0], {})                              # fresh again...
    strat._reprice(t0 + 12, compute_delta=False)
    assert strat.theo_stale()                            # ...but still held off
    mono[0] += strat.config.fresh_recovery_s * 0.5
    _feed(bus, mono[0], {}); strat._reprice(t0 + 13, compute_delta=False)
    assert strat.theo_stale()
    mono[0] += strat.config.fresh_recovery_s * 0.6
    _feed(bus, mono[0], {}); strat._reprice(t0 + 14, compute_delta=False)
    assert not strat.theo_stale()


def test_near_expiry_staleness_limit_is_tighter(monkeypatch):
    strat, bus = _make_strategy()
    import kalshi_crypto.strategy as sm
    mono = [10_000.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: mono[0])
    t0 = time.time()
    _warm(strat, bus, mono, t0)
    strat._fresh_since = mono[0] - 100.0                 # long past recovery
    age = (strat.config.theo_max_age_near_expiry_s + strat.config.theo_max_age_s) / 2
    strat._last_price_mono = mono[0] - age
    strat.event_state.window.T = time.time() + 600       # mid-event: fine
    assert not strat.theo_stale()
    strat._fresh_since = mono[0] - 100.0
    strat.event_state.window.T = time.time() + 60        # last minute: stale
    assert strat.theo_stale()
