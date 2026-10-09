"""Generic Quoter behaviour that isn't covered by the SPX replay parity:
take confirmation, close phase, freshness gate, block reasons, fair clip."""
import dataclasses
import types

import pandas as pd
import pytest

from kalshi_backtest.core import Order, Portfolio
from kalshi_infra.trading import params as P
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.quoter import Quoter

T0 = pd.Timestamp("2026-10-08 20:00:00", tz="UTC")


def cfg(**q):
    base = dict(order_size=1.0, max_position=10.0, constant_edge=0.01, edge_fraction=1.0,
                lean_multiplier=1.0, lean_dollars=0.02, lean_exponent=2.0, fair_clip_eps=0.0,
                inflight_timeout_s=0.5, take_confirm_s=0.0, max_markets=None, atm_hysteresis=0,
                enable_quoting=True, quoting_edge=0.0, max_improve_ticks=1, requote_improve_ticks=2.0,
                min_quote_life_s=0.0, requote_delay_s=1.0)
    base.update(q)
    close = P.CloseParams(last_phase_s=0.0, last_phase_max_half_band=None, last_phase_otm_fair_max=None,
                          real_trading_cutoff_s=0.0, close_buffer_s=0.0)
    risk = types.SimpleNamespace(max_signal_age_s=1.0, near_expiry_s=0.0, max_signal_age_near_expiry_s=1.0,
                                 fresh_recovery_s=0.0)
    return types.SimpleNamespace(quoting=P.QuotingParams(**base), close=close, risk=risk,
                                 fees=P.FeeParams(taker_fee_multiplier=0.0, maker_fee_multiplier=0.0,
                                                  taker_fee_edge_multiplier=1.0))


class Model:
    def __init__(self):
        self.age = 0.0
        self.block = {}

    def on_market_event(self, t, s):
        return None

    def signal_age_s(self):
        return self.age

    def side_block(self, ticker, side, fair, now_s):
        return self.block.get((ticker, side))


@dataclasses.dataclass
class Book:
    ticker: str
    bid: float
    ask: float
    time: pd.Timestamp = T0
    ladder: object = None


class Ctx:
    def __init__(self):
        self.portfolio = Portfolio(fee_multiplier=0.0, maker_fee_multiplier=0.0)
        self.books = {}
        self.orders = []
        self.cancels = []
        self.time = T0
        self.last_event = None
        self._id = 0

    def book(self, t):
        return self.books.get(t)

    def position(self, t):
        return self.portfolio.position(t)

    def buy(self, ticker, side, size, price=None, order_type="limit", intent="take"):
        self._id += 1
        self.orders.append((ticker, side, price, intent))
        return Order(self._id, ticker, side, order_type, price, size, self.time, intent)

    def cancel(self, oid):
        self.cancels.append(oid)


def step(q, ctx, fairs=None, book=None, dt=0.0):
    ctx.time = ctx.time + pd.Timedelta(seconds=dt)
    if book is not None:
        ctx.books[book.ticker] = book
    ctx.last_event = FairUpdate(fairs) if fairs is not None else ctx.books["A"]
    q(ctx)


def takes(ctx):
    return [o for o in ctx.orders if o[3] == "take"]


def test_take_when_fair_clears_ask():
    q, ctx = Quoter(cfg(), Model(), close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    # reservation = 0.60 - (0.01 const + 0.01 ci) = 0.58 >= 0.55
    assert takes(ctx) == [("A", "yes", 0.55, "take")]


def test_take_confirm_requires_signal_to_hold():
    q, ctx = Quoter(cfg(take_confirm_s=0.75), Model(), close_time=None), Ctx()
    f = {"A": Fair(0.60, 0.59, 0.61)}
    step(q, ctx, f, Book("A", 0.40, 0.55))
    step(q, ctx, f, dt=0.5)
    assert takes(ctx) == []
    step(q, ctx, f, dt=0.3)
    assert len(takes(ctx)) == 1
    step(q, ctx, f, dt=0.1)          # at most one take per window
    assert len(takes(ctx)) == 1


def test_take_confirm_resets_when_signal_lapses():
    q, ctx = Quoter(cfg(take_confirm_s=0.75), Model(), close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.5)   # no longer clears
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, dt=0.5)   # 1.0s since first, but only 0s since reset
    assert takes(ctx) == []


def test_quote_first_tick_outside_reservation_and_capped_inside_touch():
    q, ctx = Quoter(cfg(), Model(), close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    quotes = [o for o in ctx.orders if o[3] == "quote"]
    # res bid = 0.50 - 0.01 = 0.49 -> capped at bid + 1 tick = 0.41; ask side 0.59 (no-buy at 0.41)
    assert ("A", "yes", 0.41, "quote") in quotes and ("A", "no", 0.41, "quote") in quotes


def test_stale_signal_pulls_quotes_and_logs_reason():
    m = Model()
    q, ctx = Quoter(cfg(), m, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert q.live_quote_order_ids()
    m.age = 5.0
    step(q, ctx, dt=0.1)
    assert not q.live_quote_order_ids() and len(ctx.cancels) == 2
    assert q.debug_log[-1]["block_bid"] == "stale_signal" and q.block_counts["stale_signal"] == 1


def test_none_fair_pulls_quotes():
    q, ctx = Quoter(cfg(), Model(), close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    step(q, ctx, {"A": None}, dt=0.1)
    assert not q.live_quote_order_ids() and q.debug_log[-1]["block_bid"] == "no_fair"


def test_series_side_block_vetoes_one_side():
    m = Model()
    m.block[("A", "yes")] = "series_says_no"
    q, ctx = Quoter(cfg(), m, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.70, 0.69, 0.71)}, Book("A", 0.40, 0.55))
    assert takes(ctx) == [] and all(o[1] == "no" for o in ctx.orders)
    assert q.debug_log[-1]["block_bid"] == "series_says_no" and q.debug_log[-1]["block_ask"] is None


@pytest.mark.parametrize("band,prob,expect", [
    (0.10, 0.50, "last_phase_band"),       # wide band in the last phase
    (0.01, 0.50, "last_phase_not_otm"),    # not near 0 or 1
    (0.01, 0.002, None),                   # far OTM: still quoted
])
def test_close_phase_rules(band, prob, expect):
    c = cfg()
    c.close = P.CloseParams(last_phase_s=150.0, last_phase_max_half_band=0.05, last_phase_otm_fair_max=0.005,
                            real_trading_cutoff_s=0.0, close_buffer_s=0.0)
    q, ctx = Quoter(c, Model(), close_time=T0 + pd.Timedelta(seconds=100)), Ctx()
    step(q, ctx, {"A": Fair(prob, prob - band, prob + band)}, Book("A", 0.001 if prob < 0.01 else 0.40, 0.60))
    assert q.debug_log[-1]["block_bid"] == expect


def test_close_phase_inactive_before_window():
    c = cfg()
    c.close = P.CloseParams(last_phase_s=150.0, last_phase_max_half_band=0.05, last_phase_otm_fair_max=0.005,
                            real_trading_cutoff_s=0.0, close_buffer_s=0.0)
    q, ctx = Quoter(c, Model(), close_time=T0 + pd.Timedelta(seconds=600)), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.40, 0.60)}, Book("A", 0.40, 0.60))
    assert q.debug_log[-1]["block_bid"] is None


def test_max_markets_blocks_far_markets():
    q, ctx = Quoter(cfg(max_markets=1), Model(), close_time=None), Ctx()
    ctx.books["B"] = Book("B", 0.40, 0.60)
    step(q, ctx, {"A": Fair(0.52, 0.51, 0.53), "B": Fair(0.90, 0.89, 0.91)}, Book("A", 0.40, 0.60))
    by = {r["ticker"]: r for r in q.debug_log}
    assert by["A"]["block_bid"] is None and by["B"]["block_bid"] == "outside_atm_set"


def test_fair_clip_applies_to_quoting_not_log():
    q, ctx = Quoter(cfg(fair_clip_eps=0.01), Model(), close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.001, 0.0, 0.002)}, Book("A", 0.01, 0.03))
    r = q.debug_log[-1]
    assert r["prob"] == 0.01 and r["prob_raw"] == 0.001


def test_lean_charged_only_to_growing_side():
    q, ctx = Quoter(cfg(), Model(), close_time=None), Ctx()
    ctx.portfolio.position("A").yes_qty = 10.0      # at the cap, long
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    r = q.debug_log[-1]
    assert r["lean_bid"] == pytest.approx((1.0 * 0.01 + 0.02) * 1.0)
    assert r["lean_ask"] == 0.0
