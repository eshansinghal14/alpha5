"""
Quoter tests: edge arithmetic, event-level consistency, desired-quote
tick rounding.
"""
import math
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.config import CoinRaceConfig
from kalshi_crypto.quoter import (crypto_fee, decompose_edge,
                                   desired_quote_level, apply_event_constraints)


def test_crypto_fee_quadratic_kernel():
    """fee = ceil(1 * contracts * p * (1-p) * 10000) / 10000. At p=0.5
    this is ceil(0.25 * 10000) / 10000 = 0.25 per contract."""
    assert math.isclose(crypto_fee(0.5, 1.0), 0.25, abs_tol=1e-9)
    # Symmetric about 0.5.
    assert math.isclose(crypto_fee(0.3, 1.0), crypto_fee(0.7, 1.0), abs_tol=1e-9)


def test_decompose_edge_lip_floor():
    """LIP credit floors at adverse_buffer -- it never pushes the
    required edge below constant_edge."""
    cfg = CoinRaceConfig(constant_edge=0.01, edge_fraction=1.0,
                         ci_vol_multiplier=1.0, lean_dollars=0.0,
                         lean_multiplier=0.0)
    d = decompose_edge(
        fair=0.5, theo_vol=0.0,
        net_position=0.0, eff_yes=0.0, eff_no=0.0, max_pos=10.0,
        market_bid=0.49, market_ask=0.51,
        lip_credit_bid=100.0,   # unlimited credit -- should still floor
        lip_credit_ask=100.0,
        config=cfg,
    )
    # Edge total == adverse_buffer == constant_edge.
    assert math.isclose(d.bid_edge_total, 0.01, abs_tol=1e-9)
    assert math.isclose(d.ask_edge_total, 0.01, abs_tol=1e-9)
    assert math.isclose(d.bid_reservation, 0.49, abs_tol=1e-9)
    assert math.isclose(d.ask_reservation, 0.51, abs_tol=1e-9)


def test_maker_edge_excludes_taker_fee():
    """A resting quote pays no fee on this series, so the MAKER edge must
    NOT include the taker fee the crossing edge carries. With zero CI /
    lean, the taker edge is constant_edge + taker_fee while the maker edge
    is just constant_edge."""
    cfg = CoinRaceConfig(constant_edge=0.01, edge_fraction=1.0,
                         ci_vol_multiplier=0.0, lean_dollars=0.0,
                         lean_multiplier=0.0)
    d = decompose_edge(
        fair=0.5, theo_vol=0.0,
        net_position=0.0, eff_yes=0.0, eff_no=0.0, max_pos=10.0,
        market_bid=0.49, market_ask=0.51,
        lip_credit_bid=0.0, lip_credit_ask=0.0,
        config=cfg,
    )
    taker_fee = crypto_fee(0.51, 1.0)   # fee at the ask we'd cross
    assert taker_fee > 0.0
    # Taker edge carries the fee...
    assert math.isclose(d.bid_edge_total, 0.01 + taker_fee, abs_tol=1e-9)
    # ...maker edge does not (maker fee is 0 on KXCRYPTOLEAD15M).
    assert math.isclose(d.maker_fee_bid, 0.0, abs_tol=1e-9)
    assert math.isclose(d.maker_bid_edge_total, 0.01, abs_tol=1e-9)
    assert math.isclose(d.maker_bid_reservation, 0.49, abs_tol=1e-9)


def test_lip_credit_reduces_maker_edge():
    """LIP credit lowers the MAKER (resting-quote) edge toward the adverse
    buffer -- it is earned by resting, which is exactly the quoting case."""
    cfg = CoinRaceConfig(constant_edge=0.01, edge_fraction=1.0,
                         ci_vol_multiplier=1.0, lean_dollars=0.0,
                         lean_multiplier=0.0)
    # theo_vol gives a CI edge of 0.05; a 0.02 LIP credit should shave the
    # maker edge from (0.01 + 0.05) = 0.06 down to 0.04.
    d = decompose_edge(
        fair=0.5, theo_vol=0.05,
        net_position=0.0, eff_yes=0.0, eff_no=0.0, max_pos=10.0,
        market_bid=0.49, market_ask=0.51,
        lip_credit_bid=0.02, lip_credit_ask=0.02,
        config=cfg,
    )
    assert math.isclose(d.maker_bid_edge_total, 0.04, abs_tol=1e-9)
    # And it still floors at the adverse buffer under unlimited credit.
    d2 = decompose_edge(
        fair=0.5, theo_vol=0.05,
        net_position=0.0, eff_yes=0.0, eff_no=0.0, max_pos=10.0,
        market_bid=0.49, market_ask=0.51,
        lip_credit_bid=100.0, lip_credit_ask=100.0,
        config=cfg,
    )
    assert math.isclose(d2.maker_bid_edge_total, 0.01, abs_tol=1e-9)


def test_desired_quote_rounds_outside_reservation():
    """YES bid at 0.573 reservation -> quote at 0.57 (floor), not 0.58."""
    px = desired_quote_level("yes", 0.573, market_bid=0.55, market_ask=0.60,
                             allowed=True, max_improve_ticks=5)
    assert math.isclose(px, 0.57, abs_tol=1e-9)
    # And symmetric: NO bid at ask-reservation 0.627 -> yes_ask=0.63 rounded UP,
    # so no-side price = 0.37.
    px = desired_quote_level("no", 0.627, market_bid=0.55, market_ask=0.60,
                             allowed=True, max_improve_ticks=5)
    # yes_price ceil(0.627) = 0.63, no = 1 - 0.63 = 0.37.
    assert math.isclose(px, 0.37, abs_tol=1e-9)


def test_desired_quote_caps_inside_opposite_touch():
    """YES bid never crosses -- it is capped a tick inside the ask."""
    px = desired_quote_level("yes", 0.99, market_bid=0.55, market_ask=0.60,
                             allowed=True, max_improve_ticks=100)
    # Would want to quote at ~0.98, but ask=0.60 caps at 0.59.
    assert math.isclose(px, 0.59, abs_tol=1e-9)


def test_event_constraint_sum_bids_under_one():
    """5 yes bids summing to 1.05 -> the highest one gets pulled."""
    bids = {"BTC": 0.30, "ETH": 0.25, "SOL": 0.20, "XRP": 0.15, "HYPE": 0.15}
    asks = {"BTC": 0.50, "ETH": 0.50, "SOL": 0.50, "XRP": 0.50, "HYPE": 0.50}
    out_bids, out_asks, reasons = apply_event_constraints(bids, asks)
    assert sum(v for v in out_bids.values() if v is not None) < 1.0
    assert "BTC" in reasons


def test_event_constraint_sum_asks_over_one():
    """5 YES asks summing < 1.0 means the counterparty can lock-in; the
    constraint equivalently says no-bid sum must be low enough that
    (1 - no_bid) summed > 1. If all 5 no-bids are 0.95, yes-asks sum
    to 0.25 and we need to pull the highest no-bid."""
    bids = {"BTC": 0.10, "ETH": 0.10, "SOL": 0.10, "XRP": 0.10, "HYPE": 0.10}
    asks = {"BTC": 0.95, "ETH": 0.95, "SOL": 0.95, "XRP": 0.95, "HYPE": 0.95}
    out_bids, out_asks, reasons = apply_event_constraints(bids, asks)
    # At least one coin got pulled.
    assert any(v is None for v in out_asks.values())
