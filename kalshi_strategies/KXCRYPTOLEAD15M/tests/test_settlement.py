"""
SettlementTracker tests.
"""
import math
import os
import sys

import pytest


from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBar
from kalshi_strategies.KXCRYPTOLEAD15M.settlement_tracker import EventWindow, SettlementTracker
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS


def _bar(coin, ts_s, price):
    return CompositeBar(coin=coin, ts_s=ts_s, price=price,
                        utilized_depth=1.0, n_venues_in=1,
                        n_venues_dropped=0, is_fallback=False,
                        fallback_mid=price, nowcast_count=1)


def test_projected_returns_fill_unformed_end_with_last_bar_per_source():
    S, T = 1_000_000.0, 1_000_900.0
    tr = SettlementTracker(EventWindow(event_ticker="E", S=S, T=T))
    for ts in range(int(S - 60), int(S)):
        for c in COINS:
            tr.ingest(_bar(c, float(ts), 100.0))
    # 30s into the end average: mid at 101, replica at 102.
    for ts in range(int(T - 60), int(T - 30)):
        for c in COINS:
            tr.ingest(CompositeBar(c, float(ts), 102.0, 1.0, 1, 0, False, 101.0, 1))
    now = T - 30
    assert math.isclose(tr.projected_returns(now, "mid")["BTC"], 0.01, abs_tol=1e-12)
    assert math.isclose(tr.projected_returns(now, "cf")["BTC"], 0.02, abs_tol=1e-12)


def test_basic_winner():
    """BTC up 0.1%, others flat -> BTC wins."""
    S, T = 1_000_000.0, 1_000_900.0
    w = EventWindow(event_ticker="E", S=S, T=T)
    tr = SettlementTracker(w)
    # Start TWAPs: 60 flat samples each for all coins.
    for ts in range(int(S - 60), int(S)):
        for c in COINS:
            tr.ingest(_bar(c, float(ts), 100.0))
    # End TWAPs: BTC at 100.1 (0.1% up), others at 100.0.
    for ts in range(int(T - 60), int(T)):
        tr.ingest(_bar("BTC", float(ts), 100.1))
        for c in COINS[1:]:
            tr.ingest(_bar(c, float(ts), 100.0))
    outcome = tr.finalize(T + 1)
    assert outcome is not None
    assert outcome.winners == ["BTC"]
    assert math.isclose(outcome.returns_pct["BTC"], 0.1, abs_tol=1e-6)


def test_tie_payout_split():
    """Two coins tie at the top -> payout_cents_each = 50."""
    S, T = 1_000_000.0, 1_000_900.0
    w = EventWindow(event_ticker="E", S=S, T=T)
    tr = SettlementTracker(w)
    for ts in range(int(S - 60), int(S)):
        for c in COINS:
            tr.ingest(_bar(c, float(ts), 100.0))
    # BTC and ETH both at +0.1%; others flat.
    for ts in range(int(T - 60), int(T)):
        for c in ("BTC", "ETH"):
            tr.ingest(_bar(c, float(ts), 100.1))
        for c in ("SOL", "XRP", "HYPE"):
            tr.ingest(_bar(c, float(ts), 100.0))
    outcome = tr.finalize(T + 1)
    assert outcome is not None
    assert set(outcome.winners) == {"BTC", "ETH"}
    assert outcome.payout_cents_each == 50


def test_partial_start_quality_flag():
    """Only 30 of 60 start samples -> start_quality=='partial'."""
    S, T = 1_000_000.0, 1_000_900.0
    w = EventWindow(event_ticker="E", S=S, T=T)
    tr = SettlementTracker(w)
    for ts in range(int(S - 30), int(S)):  # only 30 samples
        for c in COINS:
            tr.ingest(_bar(c, float(ts), 100.0))
    snap = tr.snapshot(S + 0.1)   # just after S
    assert snap.start_quality["BTC"] == "partial"


def test_backfill_resolves_start_quality():
    """Backfilling the start window from persisted bars (what the live
    runner does on adopting an event mid-window) must make start_quality
    resolve to 'ok' -- without it, start_locked stays False and the pricer
    gets x=0, collapsing the fair to a variance prior that sits far from
    the Kalshi mid (observed 2026-10-07: BTC theo 0.17 vs market 0.035)."""
    S, T = 1_000_000.0, 1_000_900.0
    w = EventWindow(event_ticker="E", S=S, T=T)
    tr = SettlementTracker(w)
    # No live ingest at all -- start_quality is 'missing' for every coin.
    snap0 = tr.snapshot(S + 300.0)
    assert all(q == "missing" for q in snap0.start_quality.values())
    # Now backfill a full 60-sample start window from "persisted" bars.
    bars = {c: [_bar(c, float(ts), 100.0) for ts in range(int(S - 60), int(S))]
            for c in COINS}
    tr.backfill(bars)
    snap1 = tr.snapshot(S + 300.0)
    assert all(q == "ok" for q in snap1.start_quality.values())
    # And backfill is idempotent (dedup on ts_s): re-applying doesn't
    # double-count or change quality.
    tr.backfill(bars)
    assert tr.snapshot(S + 300.0).start_quality["BTC"] == "ok"


def test_rounding_at_boundary():
    """Returns are rounded to 1e-5 (3 decimals in percent)."""
    S, T = 1_000_000.0, 1_000_900.0
    w = EventWindow(event_ticker="E", S=S, T=T)
    tr = SettlementTracker(w)
    for ts in range(int(S - 60), int(S)):
        for c in COINS:
            tr.ingest(_bar(c, float(ts), 100.0))
    # BTC: tiny positive, within rounding -> should still round to a
    # multiple of 0.001 (percent) == 1e-5 in return units.
    for ts in range(int(T - 60), int(T)):
        tr.ingest(_bar("BTC", float(ts), 100.0004))  # +0.0004%, rounds to 0.0
        for c in COINS[1:]:
            tr.ingest(_bar(c, float(ts), 100.0))
    outcome = tr.finalize(T + 1)
    # +0.0004% rounds to +0.000% -- a 5-way tie.
    assert len(outcome.winners) == 5
    assert outcome.payout_cents_each == 20
