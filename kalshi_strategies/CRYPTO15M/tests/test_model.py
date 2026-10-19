"""UpDownModel gates and Fair shape, offline (no network, nothing persisted)."""
import math
import time

import pandas as pd

from kalshi_infra.config import load
from kalshi_infra.kalshi.markets import EventInfo
from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH, UpDownConfig
from kalshi_strategies.CRYPTO15M.model import UpDownModel
from kalshi_strategies.CRYPTO15M.shared_feed import extend, implied_ema
from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBar
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook

PX = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 1.4, "HYPE": 84.0}


class _Book:
    def __init__(self, ticker, bid, ask):
        self.ticker, self.bid, self.ask = ticker, bid, ask


def _cfg(**pricing):
    cfg = load(UpDownConfig, CONFIG_PATH, "KXBTC15M")
    # Realized vol unless a test asks for implied: the live config is implied for
    # every coin, which withholds the fair until Kalshi mids have been read.
    for k, v in {"min_rv_seconds": 0, "recovery_s": 0.0, "vol_source": "realized", **pricing}.items():
        setattr(cfg.pricing, k, v)
    return cfg


def _setup(now, coin="BTC", strike=None, cf_mult=1.0, **pricing):
    """Event opened 300s ago; CF and 1s bars at PX * cf_mult for the last 400s,
    so the start average and the basis are both in."""
    cfg = _cfg(**pricing)
    spot = SpotFeed(cfg, persist=False, venues=False)
    S, T = int(now) - 300, int(now) + 600
    m = {"ticker": f"KX{coin}15M-EV-00", "floor_strike": strike}
    ev = EventInfo("EV", pd.Timestamp(S, unit="s", tz="UTC"), pd.Timestamp(T, unit="s", tz="UTC"), [m])
    model = UpDownModel(ev, cfg, coin, spot)
    mono = time.monotonic()
    for t in range(int(now) - 400, int(now)):
        spot.cf.add(coin, t, PX[coin] * cf_mult, mono=mono)
        _feed(spot, coin, ts=mono)
        spot.bus.feeds[coin].sample(t)
    spot.attach(model, coins=(coin,))
    return model, spot


def _feed(spot, coin, mult=1.0, ts=None):
    p = PX[coin] * mult
    spot._on_book(VenueBook(venue="coinbase", product=f"{coin}-USD", coin=coin,
                            bids=(OrderBookLevel(p * 0.99999, 1.0),), asks=(OrderBookLevel(p * 1.00001, 1.0),),
                            receive_ts=time.monotonic() if ts is None else ts))


def test_atm_on_cf_start_and_up_move():
    now = time.time()
    m, spot = _setup(now)
    _feed(spot, "BTC")
    f = m.reprice(now)[m.ticker]
    assert f is not None and abs(f.prob - 0.5) < 0.01 and f.lo < f.prob < f.hi
    assert m.start_src == "cf"
    _feed(spot, "BTC", 1.001)
    assert m.reprice(now)[m.ticker].prob > 0.6


def test_basis_puts_mid_on_cf_level():
    # Mid sits 2bp over CF the whole time: the basis removes it, fair stays ATM.
    now = time.time()
    m, spot = _setup(now, cf_mult=1 / 1.0002)
    _feed(spot, "BTC")
    f = m.reprice(now)[m.ticker]
    assert abs(m.basis * 1e4 - 2.0) < 0.01 and abs(f.prob - 0.5) < 0.01


def test_other_coin_book_does_not_wake():
    now = time.time()
    m, spot = _setup(now)
    _feed(spot, "BTC")
    m.reprice(now)
    assert not m._dirty
    _feed(spot, "ETH", 1.01)
    spot._on_cf_tick("ETH", int(now))
    assert not m._dirty
    spot._on_cf_tick("BTC", int(now))
    assert m._dirty


def test_tail_and_close_blocks():
    now = time.time()
    m, spot = _setup(now)
    _feed(spot, "BTC")
    f = m.reprice(now)[m.ticker]
    m.on_book(_Book(m.ticker, 0.48, 0.52))
    assert m.side_block(m.ticker, "yes", f, now) is None
    m.on_book(_Book(m.ticker, 0.03, 0.05))
    assert m.side_block(m.ticker, "yes", f, now) == "book_in_tail"
    assert m.side_block(m.ticker, "yes", f, m.window.T - 5) == "near_close"


def test_burst_pulls_own_coin_only():
    now = time.time()
    m, spot = _setup(now)
    spot._pull("ETH", "coinbase", time.monotonic())
    assert m.quote_block(m.ticker, now) is None
    spot._pull("BTC", "coinbase", time.monotonic())
    assert m.quote_block(m.ticker, now) == "spot_burst"


def test_implied_without_observations_withholds():
    now = time.time()
    m, spot = _setup(now, vol_source="implied")
    _feed(spot, "BTC")
    assert m.reprice(now)[m.ticker] is None and m.withheld_reason == "no_implied_vol"


def test_implied_prices_once_ema_has_data():
    now = time.time()
    # implied_lag_s 0: the 10bp move below is set up in one go, not a live jump.
    # Wide SE / curvature gates: at 1bp/sqrt(s) and 600s left a 0.70 mid is a
    # weak vol read (SE ~1.0); this checks the plumbing, not the gates.
    m, spot = _setup(now, vol_source="implied", implied_min_obs=2.5, implied_lag_s=0.0,
                     implied_max_obs_se=2.0, implied_max_curvature=5.0)
    _feed(spot, "BTC", 1.0010)
    m.reprice(now)
    # Kalshi priced 10bp up at 0.70: implies some vol; three seconds of it fill the EMA.
    for k in range(3):
        m.on_book(_Book(m.ticker, 0.69, 0.71))
        m._iv_last_sec = None
        m._dirty = True
        f = m.reprice(now + k)[m.ticker]
    assert f is not None and m.vol_src == "implied" and abs(f.prob - 0.70) < 0.02


def test_spot_jump_downweights_implied():
    now = time.time()
    m, spot = _setup(now, vol_source="implied", implied_min_obs=1.0, implied_lag_s=5.0)
    _feed(spot, "BTC", 1.0010)     # 10bp since the last bars: Kalshi may lag it
    m.on_book(_Book(m.ticker, 0.69, 0.71))
    m.reprice(now)
    o = implied_ema(spot, "BTC").last
    assert o is not None and (o.reason is not None or o.se_log > 0.5)


def test_vol_off_and_burst_pull_quotes():
    now = time.time()
    m, spot = _setup(now, vol_source="implied")
    assert m.quote_block(m.ticker, now) is None
    extend(spot, "BTC", "vol_off", time.monotonic() + 30)
    assert m.quote_block(m.ticker, now) == "vol_off"
    extend(spot, "BTC", "vol_burst", time.monotonic() + 30)
    assert m.quote_block(m.ticker, now) == "vol_burst"
    extend(spot, "ETH", "vol_off", time.monotonic() + 30)       # another coin's pull
    spot.coin_state["BTC"].clear()
    assert m.quote_block(m.ticker, now) is None


def test_burst_ratio_trips_on_last_minute_vol():
    now = int(time.time())
    m, spot = _setup(now, vol_burst_long_s=300.0)
    feed = spot.bus.feeds["BTC"]
    feed._bars.clear()
    rng = __import__("numpy").random.default_rng(1)
    px = PX["BTC"]
    for i, t in enumerate(range(now - 400, now)):
        px *= math.exp(rng.normal(0, (4e-4 if i >= 340 else 0.5e-4)))
        feed._bars.append(CompositeBar("BTC", float(t), px, 1.0, 1, 0, False, px, 1))
    m._burst_check()
    assert m.burst_ratio is not None and m.burst_ratio > 2.0
    assert m.quote_block(m.ticker, now) == "vol_burst"


def test_one_feed_serves_race_and_single_coin_models():
    """In the shared process an ETH book wakes the race and the ETH model, a
    BTC book only the race; a burst pull reaches both; health keeps the
    first-attached model's thresholds."""
    from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel
    from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config as race_config
    from kalshi_strategies.KXCRYPTOLEAD15M.tests.test_model import _event as race_event
    now = time.time()
    rcfg = race_config(recovery_s=0.0)
    spot = SpotFeed(rcfg, persist=False, venues=False)
    race = CoinRaceModel(race_event(now - 300, now + 600), rcfg, cf=spot.cf, spot=spot)
    spot.attach(race)
    cfg = _cfg(soft_stale_s=99.0)
    S, T = int(now) - 300, int(now) + 600
    ev = EventInfo("EV", pd.Timestamp(S, unit="s", tz="UTC"), pd.Timestamp(T, unit="s", tz="UTC"),
                   [{"ticker": "KXETH15M-EV-00", "floor_strike": None}])
    eth = UpDownModel(ev, cfg, "ETH", spot)
    spot.attach(eth, coins=("ETH",))
    assert spot.health.soft_stale_s == rcfg.pricing.soft_stale_s
    woke = []
    race.set_wakeup(lambda: woke.append("race"))
    eth.set_wakeup(lambda: woke.append("eth"))
    _feed(spot, "ETH", 1.001)
    assert sorted(woke) == ["eth", "race"]
    woke.clear()
    _feed(spot, "BTC", 1.001)
    assert woke == ["race"]
    woke.clear()
    spot._on_cf_tick("ETH", int(now))
    assert sorted(woke) == ["eth", "race"]
    woke.clear()
    spot._pull("BTC", "coinbase", time.monotonic())
    assert sorted(woke) == ["eth", "race"]                     # every model hears a pull
    assert race.quote_block(race.tickers[0], now) == "spot_burst"
    assert eth.quote_block(eth.ticker, now) is None            # but ETH pulls on ETH bursts only
    spot.detach(eth)
    woke.clear()
    _feed(spot, "ETH", 1.002)
    assert woke == ["race"] and spot.model is race


def test_withheld_before_start():
    now = time.time()
    m, spot = _setup(now)
    _feed(spot, "BTC")
    assert m.reprice(m.window.S - 10)[m.ticker] is None and m.withheld_reason == "before_start"
