"""
cf_rate tests. Not a full statistical validation vs CF (that needs
real multi-venue data) -- these just check the shape of the output
and the degenerate branches.
"""
import os
import sys

import pytest


from kalshi_strategies.KXCRYPTOLEAD15M.cf_rate import compute_cf_spot
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import VenueBook, OrderBookLevel


def _book(price_center=100.0, size=10.0, venue="coinbase", coin="BTC",
          receive_ts=1000.0, is_resyncing=False):
    bids = tuple(OrderBookLevel(price_center - 0.1 * i - 0.05, size)
                 for i in range(5))
    asks = tuple(OrderBookLevel(price_center + 0.1 * i + 0.05, size)
                 for i in range(5))
    return VenueBook(venue=venue, product=f"{coin}-USD", coin=coin,
                     bids=bids, asks=asks, receive_ts=receive_ts,
                     is_resyncing=is_resyncing)


def test_single_healthy_venue_returns_rate():
    r = compute_cf_spot("BTC", [_book()])
    assert r is not None
    assert 99 < r.price < 101
    assert r.utilized_depth > 0


def test_resyncing_venue_dropped():
    r = compute_cf_spot("BTC", [_book(is_resyncing=True)])
    # One venue, which is resyncing -> no good books. Fallback to None
    # (no median mid either; the venue is excluded).
    assert r is None


def test_fallback_median_when_only_one_bad_venue():
    """One venue, not resyncing, but one-sided -> fallback median."""
    vb = VenueBook(venue="coinbase", product="BTC-USD", coin="BTC",
                   bids=(OrderBookLevel(100.0, 10),),
                   asks=(),
                   receive_ts=1000.0, is_resyncing=False)
    r = compute_cf_spot("BTC", [vb])
    assert r is None   # no mid can be computed; fallback requires a mid


def test_outlier_venue_dropped():
    """Three venues; one far away from the median is dropped."""
    good = _book(venue="coinbase", price_center=100.0, receive_ts=1000.0)
    good2 = _book(venue="kraken", price_center=100.1, receive_ts=1000.0)
    outlier = _book(venue="bitstamp", price_center=130.0, receive_ts=1000.0)
    r = compute_cf_spot("BTC", [good, good2, outlier])
    assert r is not None
    assert "bitstamp" in r.drop_reasons


def test_crossed_single_venue_returns_fallback_mid():
    """A crossed single-venue book must NOT blank the composite -- it
    yields a fallback mid of 0.5*(best_bid + best_ask). This is the
    2026-10-06 BTC bug: a stale crossing level dropped the only venue
    and returned None for minutes.

    best_bid=100.10 (crosses) best_ask=100.00 -> mid 100.05."""
    crossed = VenueBook(
        venue="coinbase", product="BTC-USD", coin="BTC",
        bids=(OrderBookLevel(100.10, 5.0),),
        asks=(OrderBookLevel(100.00, 5.0),),
        receive_ts=1000.0, is_resyncing=False)
    assert crossed.is_crossed()
    r = compute_cf_spot("BTC", [crossed])
    assert r is not None
    assert r.is_fallback
    assert abs(r.price - 100.05) < 1e-9
    assert "crossed(mid-only)" in r.drop_reasons["coinbase"]


def test_utilized_depth_stays_near_touch_with_deep_garbage():
    """A deep book with far-away junk levels (Coinbase level2 ships asks
    out to >$100M and bids down to $0.01) must still price AT the touch.

    Regression for the inverted utilized-depth criterion: the test was
    `mid_spread = 0.5*(bid(v) - ask(v)) <= threshold`, which is <= 0
    everywhere for an uncrossed book and so was vacuously true -- utilized
    depth ran to the whole thinner side and the weighted mid averaged in
    levels far from the touch (observed 2026-10-07 on a real BTC book:
    composite 92963 vs true BBO mid 85529, an 8% error). The criterion is
    |mid(v) - spot| <= deviation * spot."""
    spot = 100.0
    # 50 tight levels near the touch (0.01 wide), each size 5...
    bids = [OrderBookLevel(spot - 0.01 * (i + 1), 5.0) for i in range(50)]
    asks = [OrderBookLevel(spot + 0.01 * (i + 1), 5.0) for i in range(50)]
    # ...plus a lone deep "garbage" level 50% away on each side with huge
    # size (the kind of resting order that dragged the broken average).
    bids.append(OrderBookLevel(spot * 0.5, 1000.0))
    asks.append(OrderBookLevel(spot * 1.5, 1000.0))
    vb = VenueBook(venue="coinbase", product="BTC-USD", coin="BTC",
                   bids=tuple(bids), asks=tuple(asks), receive_ts=1000.0)
    r = compute_cf_spot("BTC", [vb])
    assert r is not None
    # The composite must sit at the touch (within a tick), NOT be dragged
    # toward the 50/150 garbage. BTC utilized-depth deviation is 0.5%.
    assert abs(r.price - spot) < 0.5, f"composite {r.price} dragged off touch {spot}"


def test_clean_venue_wins_over_crossed_one():
    """Two venues, one clean one crossed. The clean one drives the
    depth-weighted rate (not a fallback), and both contribute to the
    median sanity check without the crossed one blanking anything."""
    clean = _book(venue="kraken", price_center=100.0, receive_ts=1000.0)
    crossed = VenueBook(
        venue="coinbase", product="BTC-USD", coin="BTC",
        bids=(OrderBookLevel(100.10, 5.0),),
        asks=(OrderBookLevel(100.00, 5.0),),
        receive_ts=1000.0, is_resyncing=False)
    r = compute_cf_spot("BTC", [clean, crossed], min_venues=1)
    assert r is not None
    # Clean venue present -> not a fallback; price near 100.0.
    assert not r.is_fallback
    assert abs(r.price - 100.0) < 0.5
