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
    assert k == len(pn.PERPS) and gap == pytest.approx(0.0, abs=1e-6) and px == pytest.approx(100.0)


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


def test_increment_is_binance_move_since_spot_last_moved():
    n = pn.PerpNowcast()
    t = _warm(n)
    assert n.increment("ETH", t, 100.0)[1] == pytest.approx(0.0, abs=1e-9)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)       # Binance +1bp first
    px, inc = n.increment("ETH", t + 0.05, 100.0)
    assert inc == pytest.approx(1.0, abs=1e-6) and px > 100.0
    s1 = 100.0 * math.exp(1e-4)
    n.on_spot("ETH", s1)                                                       # spot catches up
    assert n.increment("ETH", t + 0.1, s1)[1] == pytest.approx(0.0, abs=1e-9)  # not counted twice


def test_increment_never_pulls_a_spot_move_back():
    """Spot moves first: nowcast() would subtract it until Binance agrees,
    the increment leaves it alone."""
    n = pn.PerpNowcast()
    t = _warm(n)
    s1 = 100.0 * math.exp(1.5e-4)
    n.on_spot("ETH", s1)
    _, gap, _ = n.nowcast("ETH", t + 0.01, s1)
    _, inc = n.increment("ETH", t + 0.01, s1)
    assert gap == pytest.approx(-1.5, abs=0.05) and inc == pytest.approx(0.0, abs=1e-9)


def test_increment_capped_and_stale_binance_is_spot():
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(10e-4), t + 0.05)
    assert n.increment("ETH", t + 0.05, 100.0)[1] == pytest.approx(pn.CAP_BP)
    px, inc = n.increment("ETH", t + 0.05 + pn.FRESH_S + 1, 100.0)
    assert inc is None and px == 100.0


def test_binance_alone_moves_the_nowcast_before_the_others():
    """The others haven't moved yet: the median wouldn't, Binance does."""
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    _, gap, k = n.nowcast("ETH", t + 0.05, 100.0)
    assert k == 2 and gap == pytest.approx(1.0, abs=0.05)


def test_binance_far_from_okx_falls_back_to_spot():
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(10e-4), t + 0.05)   # bad Binance print
    n.on_perp("okx_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    px, gap, k = n.nowcast("ETH", t + 0.05, 100.0)
    assert gap is None and px == 100.0 and k == 2


def test_binance_stale_falls_back_to_okx_then_spot():
    n = pn.PerpNowcast()
    t = _warm(n)
    late = t + pn.FRESH_S + 1
    n.on_perp("okx_perp", "ETH", 100.05 * math.exp(1e-4), late)
    _, gap, k = n.nowcast("ETH", late, 100.0)
    assert k == 1 and gap == pytest.approx(1.0, abs=0.05)
    n2 = pn.PerpNowcast()
    _warm(n2)
    px, gap, k = n2.nowcast("ETH", t + pn.FRESH_S + 5, 100.0)
    assert k == 0 and gap is None and px == 100.0                        # nothing fresh: spot


def _vb(venue, coin, bid, ask, t, resync=False):
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook
    return VenueBook(venue=venue, product=coin, coin=coin, bids=(OrderBookLevel(bid, 1.0),),
                     asks=(OrderBookLevel(ask, 1.0),), receive_ts=t, is_resyncing=resync)


def test_spot_feed_feeds_perp_book_mids_not_trades():
    """Perp books reach the nowcast through SpotFeed._on_book as their mid;
    they stay out of the spot median."""
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config
    spot = SpotFeed(config(recovery_s=0.0), persist=False, venues=False)
    spot.bus.feeds["ETH"].venues = ("coinbase",)           # as live: perps never enter the median
    for i in range(30):
        t = 100.0 + i * 0.1
        spot._on_book(_vb("coinbase", "ETH", 99.99, 100.01, t))
        for v in pn.PERPS:
            spot._on_book(_vb(v, "ETH", 100.04, 100.06, t))
    assert spot.bus.mid_nowcast("ETH") == pytest.approx(100.0)
    t = 103.0
    for v in pn.PERPS:                                     # perp books up 1bp
        m = 100.05 * math.exp(1e-4)
        spot._on_book(_vb(v, "ETH", m - 0.01, m + 0.01, t))
    _, gap, k = spot.perp_nowcast.nowcast("ETH", t, 100.0)
    assert k == 2 and gap == pytest.approx(1.0, abs=0.05)
    spot._on_book(_vb("binance_perp", "ETH", 200.0, 201.0, t + 0.01, resync=True))   # resyncing: ignored
    _, gap, _ = spot.perp_nowcast.nowcast("ETH", t + 0.01, 100.0)
    assert gap == pytest.approx(1.0, abs=0.05)


def test_depth_priced_coin_prices_on_the_replica_and_bars_match():
    """A depth-priced coin's pricing mid and bar mid are the CF replica of
    its book; a thin top level no longer sets it alone."""
    from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeFeed
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook
    import time as _t
    bids = (OrderBookLevel(78.99, 0.1),) + tuple(OrderBookLevel(78.90 - 0.01 * i, 50.0) for i in range(24))
    asks = tuple(OrderBookLevel(79.01 + 0.01 * i, 50.0) for i in range(25))
    vb = VenueBook(venue="coinbase", product="HYPE-USD", coin="HYPE", bids=bids, asks=asks,
                   receive_ts=_t.monotonic())
    plain = CompositeFeed("HYPE", persist=False)
    deep = CompositeFeed("HYPE", persist=False, depth_priced=True)
    plain.observe(vb)
    deep.observe(vb)
    assert plain.mid_nowcast() == pytest.approx(79.0)
    assert deep.mid_nowcast() < 79.0 - 0.02                 # the 0.1-lot bid barely counts
    bar = deep.sample(1000.0)
    assert bar.mid == pytest.approx(deep.mid_nowcast()) and bar.mid == pytest.approx(bar.price)
    assert deep.mid_nowcast() == pytest.approx(bar.mid)     # sample() keeps the replica as the mid


def test_bad_okx_print_does_not_move_a_fresh_binance():
    n = pn.PerpNowcast()
    t = _warm(n)
    n.on_perp("binance_perp", "ETH", 100.05 * math.exp(1e-4), t + 0.05)
    n.on_perp("okx_perp", "ETH", 100.05 * math.exp(50e-4), t + 0.05)   # bad OKX print
    px, gap, _ = n.nowcast("ETH", t + 0.05, 100.0)
    assert gap is None and px == 100.0                                 # disagreement: spot, not OKX
