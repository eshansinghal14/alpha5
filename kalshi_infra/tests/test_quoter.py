"""Generic Quoter behaviour beyond the SPX replay parity: take confirmation,
freshness gate, block reasons, fair clip, lean, and the LIP-farming quote."""
import dataclasses
import types

import pandas as pd
import pytest

from kalshi_backtest.core import Order, Portfolio
from kalshi_infra.kalshi.fees import schedule
from kalshi_infra.trading import params as P
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.lip import LipParams, LipTracker
from kalshi_infra.trading.quoter import Quoter

T0 = pd.Timestamp("2026-10-08 20:00:00", tz="UTC")
FREE = schedule("quadratic", 0)          # zero fees: edge arithmetic only
KALSHI = schedule("quadratic", 1)        # 0.07 taker, 0 maker


def cfg(lip_lots=0.0, prop_max_lip=1.0, lip_min_depth_ahead=0.0, **q):
    base = dict(order_lots=1.0, max_position=10.0, constant_edge=0.01, edge_fraction=1.0,
                lean_multiplier=1.0, lean_dollars=0.02, lean_exponent=2.0, fair_clip_eps=0.0,
                inflight_timeout_s=0.5, take_confirm_s=0.0, max_markets=None, atm_hysteresis=0,
                enable_quoting=True, quoting_edge=0.0, max_improve_ticks=1, requote_improve_ticks=2.0,
                min_quote_life_s=0.0, requote_delay_s=1.0)
    base.update(q)
    return types.SimpleNamespace(
        quoting=P.QuotingParams(**base),
        close=P.CloseParams(real_trading_cutoff_s=0.0),
        risk=P.RiskParams(max_loss_usd=80.0, max_signal_age_s=1.0, fresh_recovery_s=0.0),
        lip=P.LipSettings(lip_lots=lip_lots, prop_max_lip=prop_max_lip, lip_min_depth_ahead=lip_min_depth_ahead))


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
    def __init__(self, can_amend=True):
        self.portfolio = Portfolio(fee_multiplier=0.0, maker_fee_multiplier=0.0)
        self.books = {}
        self.orders = []
        self.cancels = []
        self.amends = []
        self.forced = []
        self.time = T0
        self.last_event = None
        self.can_amend = can_amend
        self._id = 0

    def book(self, t):
        return self.books.get(t)

    def position(self, t):
        return self.portfolio.position(t)

    def buy(self, ticker, side, size, price=None, order_type="limit", intent="take", role="edge"):
        self._id += 1
        self.orders.append((ticker, side, price, intent, role, size))
        return Order(self._id, ticker, side, order_type, price, size, self.time, intent)

    def cancel(self, oid, force=False):
        self.cancels.append(oid)
        if force:
            self.forced.append(oid)

    def amend(self, oid, size):
        if self.can_amend:
            self.amends.append((oid, size))
        return self.can_amend


def step(q, ctx, fairs=None, book=None, dt=0.0):
    ctx.time = ctx.time + pd.Timedelta(seconds=dt)
    if book is not None:
        ctx.books[book.ticker] = book
    ctx.last_event = FairUpdate(fairs) if fairs is not None else ctx.books["A"]
    q(ctx)


def takes(ctx):
    return [o for o in ctx.orders if o[3] == "take"]


def quotes(ctx, role):
    return [o for o in ctx.orders if o[3] == "quote" and o[4] == role]


def test_take_when_fair_clears_ask():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    # reservation = 0.60 - (0.01 const + 0.01 ci) = 0.58 >= 0.55
    assert [o[:3] for o in takes(ctx)] == [("A", "yes", 0.55)]


def test_kalshi_taker_fee_is_charged_into_the_edge():
    q, ctx = Quoter(cfg(), Model(), KALSHI, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.58))
    r = q.debug_log[-1]
    assert r["taker_fee_bid"] == KALSHI.taker(0.58) == 0.0171
    assert takes(ctx) == []          # 0.60 - 0.02 - 0.0171 < 0.58


def test_take_confirm_requires_signal_to_hold():
    q, ctx = Quoter(cfg(take_confirm_s=0.75), Model(), FREE, close_time=None), Ctx()
    f = {"A": Fair(0.60, 0.59, 0.61)}
    step(q, ctx, f, Book("A", 0.40, 0.55))
    step(q, ctx, f, dt=0.5)
    assert takes(ctx) == []
    step(q, ctx, f, dt=0.3)
    assert len(takes(ctx)) == 1
    step(q, ctx, f, dt=0.1)          # at most one take per window
    assert len(takes(ctx)) == 1


def test_take_confirm_resets_when_signal_lapses():
    q, ctx = Quoter(cfg(take_confirm_s=0.75), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.5)
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, dt=0.5)
    assert takes(ctx) == []


def test_edge_quote_first_tick_outside_and_capped_inside_touch():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    got = {(o[1], o[2]) for o in quotes(ctx, "edge")}
    assert got == {("yes", 0.41), ("no", 0.41)}


def test_stale_signal_pulls_quotes_and_logs_reason():
    m = Model()
    q, ctx = Quoter(cfg(), m, FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert q.live_quote_order_ids()
    m.age = 5.0
    step(q, ctx, dt=0.1)
    assert not q.live_quote_order_ids() and len(ctx.cancels) == 2
    assert q.debug_log[-1]["block_bid"] == "stale_signal" and q.block_counts["stale_signal"] == 1


def test_fresh_recovery_applies_after_a_stale_episode_not_at_start():
    c = cfg()
    c.risk = P.RiskParams(max_loss_usd=80.0, max_signal_age_s=1.0, fresh_recovery_s=60.0)
    m = Model()
    q = Quoter(c, m, FREE, close_time=None)
    assert not q.signal_stale()
    m.age = 5.0
    assert q.signal_stale()
    m.age = 0.0
    assert q.signal_stale()          # inside the recovery hold


def test_series_side_block_vetoes_one_side():
    m = Model()
    m.block[("A", "yes")] = "series_says_no"
    q, ctx = Quoter(cfg(), m, FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.70, 0.69, 0.71)}, Book("A", 0.40, 0.55))
    assert takes(ctx) == [] and all(o[1] == "no" for o in ctx.orders)
    assert q.debug_log[-1]["block_bid"] == "series_says_no" and q.debug_log[-1]["block_ask"] is None


def test_max_markets_blocks_far_markets():
    q, ctx = Quoter(cfg(max_markets=1), Model(), FREE, close_time=None), Ctx()
    ctx.books["B"] = Book("B", 0.40, 0.60)
    step(q, ctx, {"A": Fair(0.52, 0.51, 0.53), "B": Fair(0.90, 0.89, 0.91)}, Book("A", 0.40, 0.60))
    by = {r["ticker"]: r for r in q.debug_log}
    assert by["A"]["block_bid"] is None and by["B"]["block_bid"] == "outside_atm_set"


def test_fair_clip_applies_to_quoting_not_log():
    q, ctx = Quoter(cfg(fair_clip_eps=0.01), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.001, 0.0, 0.002)}, Book("A", 0.01, 0.03))
    r = q.debug_log[-1]
    assert r["prob"] == 0.01 and r["prob_raw"] == 0.001


def test_lean_charged_only_to_growing_side():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    ctx.portfolio.position("A").yes_qty = 10.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    r = q.debug_log[-1]
    assert r["lean_bid"] == pytest.approx((1.0 * 0.01 + 0.02) * 1.0)
    assert r["lean_ask"] == 0.0


# ---------------------------------------------------------------------------
# LIP-farming quote
# ---------------------------------------------------------------------------

LIP = LipParams(target_size=300.0, discount_factor=0.5, period_reward_usd=9.0, snapshots_per_period=900)


def lip_book(yes_bids, no_bids, bid, ask):
    return Book("A", bid, ask, ladder=types.SimpleNamespace(yes_bids=dict(yes_bids), no_bids=dict(no_bids)))


def lip_quoter(**lip):
    return (Quoter(cfg(enable_quoting=True, order_lots=1.0, **lip), Model(), FREE, close_time=None,
                   lip=LipTracker({"A": LIP})), Ctx())


def lip_yes(ctx):
    return [o for o in quotes(ctx, "lip") if o[1] == "yes"]


# Yes bids: 100 @0.40, 100 @0.39, 100 @0.38 (target 300 reached at 0.38).
# Reference = first level with cumulative >= 60 -> 0.40. Edge reservation 0.49,
# so the edge quote sits at 0.41 and every LIP level from 0.41 down is outside the edge.
YES = {0.40: 100.0, 0.39: 100.0, 0.38: 100.0}
NO = {0.40: 400.0}


def test_lip_quote_rests_outside_edge_at_max_reward():
    q, ctx = lip_quoter(lip_lots=50.0, prop_max_lip=1.0)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lip_book(YES, NO, 0.40, 0.60))
    (o,) = lip_yes(ctx)
    assert o[2] <= 0.41 and o[5] <= 50.0


def test_lower_prop_moves_deeper():
    a, ca = lip_quoter(lip_lots=50.0, prop_max_lip=1.0)
    b, cb = lip_quoter(lip_lots=50.0, prop_max_lip=0.2)
    for q, c in ((a, ca), (b, cb)):
        step(q, c, {"A": Fair(0.50, 0.49, 0.51)}, lip_book(YES, NO, 0.40, 0.60))
    assert lip_yes(cb)[0][2] < lip_yes(ca)[0][2]


def test_min_depth_ahead_respected():
    q, ctx = lip_quoter(lip_lots=50.0, prop_max_lip=0.01, lip_min_depth_ahead=150.0)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lip_book(YES, NO, 0.40, 0.60))
    (o,) = lip_yes(ctx)
    ahead = sum(z for p, z in YES.items() if p >= o[2] - 1e-9) + 1.0   # + our edge quote
    assert ahead >= 150.0


def test_no_level_with_enough_depth_means_no_lip_quote():
    q, ctx = lip_quoter(lip_lots=50.0, prop_max_lip=0.01, lip_min_depth_ahead=10_000.0)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lip_book(YES, NO, 0.40, 0.60))
    assert lip_yes(ctx) == []


def test_size_cut_to_fewest_lots_with_same_reward():
    # Discount 1.0 and a book already at target ABOVE our level: extra lots
    # past the target cutoff earn nothing, so fewer lots are rested.
    params = LipParams(target_size=150.0, discount_factor=1.0, period_reward_usd=9.0, snapshots_per_period=900)
    # LIP lots count against the position cap (they can fill), so give room.
    q = Quoter(cfg(max_position=500.0, lip_lots=200.0, prop_max_lip=1.0), Model(), FREE, close_time=None,
               lip=LipTracker({"A": params}))
    ctx = Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lip_book({0.40: 100.0}, NO, 0.40, 0.60))
    (o,) = lip_yes(ctx)
    assert o[5] < 200.0


def test_lip_quote_withdrawn_with_forced_cancel_when_fair_disappears():
    q, ctx = lip_quoter(lip_lots=50.0, prop_max_lip=1.0)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lip_book(YES, NO, 0.40, 0.60))
    lip_ids = [qq["order_id"] for k, qq in q._quotes.items() if k[2] == "lip"]
    assert lip_ids
    step(q, ctx, {"A": None}, dt=0.1)
    assert set(lip_ids) <= set(ctx.forced)


def test_lip_resize_up_amends_and_down_requeues():
    q, ctx = lip_quoter(lip_lots=20.0, prop_max_lip=1.0)
    book = lip_book(YES, NO, 0.40, 0.60)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, book)
    (first,) = lip_yes(ctx)
    q.cfg.lip.lip_lots = 40.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.1)
    key = ("A", "yes", "lip")
    if q._quotes[key]["size"] > first[5]:
        assert ctx.amends, "an increase at the same level should amend"
    n_cancels = len(ctx.cancels)
    q.cfg.lip.lip_lots = 5.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.1)
    assert len(ctx.cancels) > n_cancels and q._quotes[key]["size"] <= 5.0
