"""
Three-tier feed-health monitor tests.

Covers:
  * Fresh book -> OK (after recovery window).
  * Soft-stale -> DEGRADED (still usable, event still prices).
  * Hard-stale / no book -> DOWN (event invalid).
  * Recovery hold-off: a coin that just left DOWN reports DOWN until
    recovery_s has elapsed.
  * Per-coin hard bound: HYPE tolerates longer staleness (30s CF bound).
  * event_usable: one DOWN coin blocks; a DEGRADED coin does not.
"""
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.spot_feed.health import FeedHealthMonitor, OK, DEGRADED, DOWN
from kalshi_crypto.spot_feed.base import VenueBook, OrderBookLevel
from kalshi_crypto.series import COINS


def _book(coin: str, receive_ts: float, *, resyncing=False, one_sided=False,
          crossed=False, venue="coinbase") -> VenueBook:
    if one_sided:
        bids = (OrderBookLevel(100.0, 5.0),)
        asks = ()
    elif crossed:
        bids = (OrderBookLevel(101.0, 5.0),)
        asks = (OrderBookLevel(100.0, 5.0),)
    else:
        bids = (OrderBookLevel(99.95, 5.0),)
        asks = (OrderBookLevel(100.05, 5.0),)
    return VenueBook(venue=venue, product=f"{coin}-USD", coin=coin,
                     bids=bids, asks=asks, receive_ts=receive_ts,
                     is_resyncing=resyncing)


def _monitor():
    return FeedHealthMonitor(soft_stale_s=3.0, hard_stale_s=15.0, recovery_s=5.0)


def test_fresh_book_becomes_ok_after_recovery():
    m = _monitor()
    now = 1000.0
    # Fresh book, observed repeatedly across the recovery window.
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    # After >= recovery_s (5s) continuously not-DOWN, status is OK.
    assert m.status("BTC", now=now + 6) == OK
    assert m.is_healthy("BTC", now=now + 6)
    assert m.coin_usable("BTC", now=now + 6)


def test_within_recovery_window_reports_down():
    m = _monitor()
    now = 1000.0
    m.observe({"BTC": [_book("BTC", receive_ts=now)]}, now=now)
    # Only 2s since first not-DOWN observation < recovery_s=5.
    assert m.status("BTC", now=now + 2) == DOWN
    assert not m.coin_usable("BTC", now=now + 2)


def test_soft_stale_is_degraded_and_usable():
    m = _monitor()
    now = 1000.0
    # Warm up to OK first.
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    # Now a book that is 4s old (between soft=3 and hard=15).
    t_check = now + 10
    m.observe({"BTC": [_book("BTC", receive_ts=t_check - 4.0)]}, now=t_check)
    assert m.status("BTC", now=t_check) == DEGRADED
    assert m.coin_usable("BTC", now=t_check)      # still usable
    assert not m.is_healthy("BTC", now=t_check)   # but not strictly OK


def test_hard_stale_is_down():
    m = _monitor()
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    t_check = now + 30
    # Book 20s old > hard_stale_s=15.
    m.observe({"BTC": [_book("BTC", receive_ts=t_check - 20.0)]}, now=t_check)
    assert m.status("BTC", now=t_check) == DOWN
    assert not m.coin_usable("BTC", now=t_check)


def test_no_book_is_down():
    m = _monitor()
    now = 1000.0
    m.observe({"BTC": []}, now=now)
    assert m.status("BTC", now=now) == DOWN


def test_resyncing_or_one_sided_book_not_counted():
    m = _monitor()
    now = 1000.0
    # Only a resyncing book -> no usable book -> DOWN.
    m.observe({"BTC": [_book("BTC", receive_ts=now, resyncing=True)]}, now=now)
    assert m.status("BTC", now=now) == DOWN
    # Only a one-sided book -> DOWN.
    m.observe({"ETH": [_book("ETH", receive_ts=now, one_sided=True)]}, now=now)
    assert m.status("ETH", now=now) == DOWN


def test_crossed_book_still_counts_as_usable():
    """A crossed-but-fresh book has a usable mid, so it must NOT mark the
    coin DOWN -- matches compute_cf_spot's mid-only tolerance. This is the
    other half of the 2026-10-06 BTC fix: health and the composite agree
    on what 'usable' means."""
    m = _monitor()
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t, crossed=True)]}, now=now + t)
    # Fresh crossed book, warmed past recovery -> OK (usable).
    assert m.status("BTC", now=now + 6) == OK
    assert m.coin_usable("BTC", now=now + 6)


def test_hype_tolerates_longer_staleness():
    """HYPE's CF methodology staleness is 30s, so its hard bound is
    30s, not the 15s global. A 20s-old HYPE book is DEGRADED, not DOWN."""
    m = _monitor()
    now = 1000.0
    for t in range(0, 7):
        m.observe({"HYPE": [_book("HYPE", receive_ts=now + t)]}, now=now + t)
    t_check = now + 40
    m.observe({"HYPE": [_book("HYPE", receive_ts=t_check - 20.0)]}, now=t_check)
    # 20s < 30s HYPE hard bound -> DEGRADED, still usable.
    assert m.status("HYPE", now=t_check) == DEGRADED
    assert m.coin_usable("HYPE", now=t_check)


def test_event_usable_blocks_on_one_down_coin():
    m = _monitor()
    now = 1000.0
    # All five fresh and warmed up...
    for t in range(0, 7):
        m.observe({c: [_book(c, receive_ts=now + t)] for c in COINS}, now=now + t)
    t_ok = now + 6
    assert m.event_usable(COINS, now=t_ok)
    # Now HYPE goes hard-stale (40s old > its 30s bound).
    t_check = now + 60
    books = {c: [_book(c, receive_ts=t_check)] for c in COINS}
    books["HYPE"] = [_book("HYPE", receive_ts=t_check - 40.0)]
    m.observe(books, now=t_check)
    assert not m.event_usable(COINS, now=t_check)
    assert m.down_coins(COINS, now=t_check) == ["HYPE"]


def test_event_usable_tolerates_degraded_coin():
    m = _monitor()
    now = 1000.0
    for t in range(0, 7):
        m.observe({c: [_book(c, receive_ts=now + t)] for c in COINS}, now=now + t)
    # BTC goes soft-stale (4s) but the rest are fresh.
    t_check = now + 10
    books = {c: [_book(c, receive_ts=t_check)] for c in COINS}
    books["BTC"] = [_book("BTC", receive_ts=t_check - 4.0)]
    m.observe(books, now=t_check)
    assert m.status("BTC", now=t_check) == DEGRADED
    # A degraded coin does NOT block the event.
    assert m.event_usable(COINS, now=t_check)


def test_transient_down_blip_is_debounced():
    """A warmed-up coin that sees ONE empty-venue dispatch (a race between
    the Coinbase callback and the sampling loop) must NOT flap to DOWN and
    reset its recovery window -- the 2026-10-06/07 'no decisions' flap.
    The debounce holds the last good tier until the absence persists."""
    m = FeedHealthMonitor(soft_stale_s=3.0, hard_stale_s=15.0, recovery_s=5.0,
                          down_debounce_s=2.0)
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    assert m.status("BTC", now=now + 6) == OK
    # One empty-venue tick: transient, within debounce -> still usable.
    m.observe({"BTC": []}, now=now + 6.5)
    assert m.status("BTC", now=now + 6.5) == OK
    assert m.coin_usable("BTC", now=now + 6.5)
    # Fresh book right back -> stays OK, recovery window never reset.
    m.observe({"BTC": [_book("BTC", receive_ts=now + 7)]}, now=now + 7)
    assert m.status("BTC", now=now + 7) == OK


def test_persistent_absence_still_goes_down_after_debounce():
    """The debounce only suppresses a BLIP. A real outage (usable book
    gone for longer than down_debounce_s) still transitions to DOWN."""
    m = FeedHealthMonitor(soft_stale_s=3.0, hard_stale_s=15.0, recovery_s=5.0,
                          down_debounce_s=2.0)
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    assert m.status("BTC", now=now + 6) == OK
    # Empty venues held past the debounce window.
    m.observe({"BTC": []}, now=now + 6.5)
    m.observe({"BTC": []}, now=now + 8.0)   # 1.5s of absence < 2.0, still held
    assert m.status("BTC", now=now + 8.0) == OK
    m.observe({"BTC": []}, now=now + 9.0)   # 2.5s of absence >= 2.0 -> DOWN
    assert m.status("BTC", now=now + 9.0) == DOWN


def test_measured_hard_stale_is_not_debounced():
    """A DOWN from a MEASURED stale age is real, not a blip, so it
    transitions immediately even on a warmed-up coin -- the debounce is
    only for the absence-of-book case."""
    m = FeedHealthMonitor(soft_stale_s=3.0, hard_stale_s=15.0, recovery_s=5.0,
                          down_debounce_s=2.0)
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    assert m.status("BTC", now=now + 6) == OK
    # One 20s-old book: hard-stale, measured -> DOWN on the first tick.
    t_check = now + 30
    m.observe({"BTC": [_book("BTC", receive_ts=t_check - 20.0)]}, now=t_check)
    assert m.status("BTC", now=t_check) == DOWN


def test_snapshot_schema():
    m = _monitor()
    now = 1000.0
    for t in range(0, 7):
        m.observe({"BTC": [_book("BTC", receive_ts=now + t)]}, now=now + t)
    snap = m.snapshot(now=now + 6)
    assert "BTC" in snap
    row = snap["BTC"]
    for k in ("status", "usable", "not_down_since_s_ago", "last_reason",
              "down_transitions", "seconds_down"):
        assert k in row
    assert row["status"] == OK
    assert row["usable"] is True
