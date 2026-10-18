"""UpDownModel gates and Fair shape, offline (no network, nothing persisted)."""
import time

import pandas as pd

from kalshi_infra.config import load
from kalshi_infra.kalshi.markets import EventInfo
from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH, UpDownConfig
from kalshi_strategies.CRYPTO15M.model import UpDownModel
from kalshi_strategies.CRYPTO15M.shared_feed import SharedSpotFeed
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook

PX = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 1.4, "HYPE": 84.0}


class _Book:
    def __init__(self, ticker, bid, ask):
        self.ticker, self.bid, self.ask = ticker, bid, ask


def _cfg(**pricing):
    cfg = load(UpDownConfig, CONFIG_PATH, "KXBTC15M")
    for k, v in {"min_rv_seconds": 0, "recovery_s": 0.0, **pricing}.items():
        setattr(cfg.pricing, k, v)
    return cfg


def _setup(now, coin="BTC", strike=None, cf_mult=1.0, **pricing):
    """Event opened 300s ago; CF and 1s bars at PX * cf_mult for the last 400s,
    so the start average and the basis are both in."""
    cfg = _cfg(**pricing)
    spot = SharedSpotFeed(cfg, persist=False, venues=False)
    S, T = int(now) - 300, int(now) + 600
    m = {"ticker": f"KX{coin}15M-EV-00", "floor_strike": strike}
    ev = EventInfo("EV", pd.Timestamp(S, unit="s", tz="UTC"), pd.Timestamp(T, unit="s", tz="UTC"), [m])
    model = UpDownModel(ev, cfg, coin, spot)
    mono = time.monotonic()
    for t in range(int(now) - 400, int(now)):
        spot.cf.add(coin, t, PX[coin] * cf_mult, mono=mono)
        _feed(spot, coin, ts=mono)
        spot.bus.feeds[coin].sample(t)
    spot.attach(model)
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


def test_withheld_before_start():
    now = time.time()
    m, spot = _setup(now)
    _feed(spot, "BTC")
    assert m.reprice(m.window.S - 10)[m.ticker] is None and m.withheld_reason == "before_start"
