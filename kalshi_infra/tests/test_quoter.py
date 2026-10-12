"""Generic Quoter: takes, freshness gate, block reasons, fair clip, lean, and
the desired quote ladder it publishes to the order manager."""
import dataclasses
import types

import pandas as pd
import pytest

from kalshi_backtest.core import Order, Portfolio
from kalshi_infra.kalshi.fees import schedule
from kalshi_infra.trading import params as P
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading import quoter as quoter_mod
from kalshi_infra.trading.quoter import FAIR_CLIP_EPS, Quoter

T0 = pd.Timestamp("2026-10-08 20:00:00", tz="UTC")
FREE = schedule("quadratic", 0)          # zero fees: edge arithmetic only
KALSHI = schedule("quadratic", 1)        # 0.07 taker, 0 maker


@pytest.fixture(autouse=True)
def immediate_takes(monkeypatch):
    """Edge arithmetic tests take on the first step; the confirm tests set it back."""
    monkeypatch.setattr(quoter_mod, "TAKE_HOLD_S", 0.0)


def cfg(**q):
    base = dict(order_lots=1.0, max_position=10.0, constant_edge=0.01, edge_fraction=1.0,
                lean_dollars=0.02, max_markets=None, atm_hysteresis=0,
                enable_taking=True, enable_quoting=True, quoting_edge=0.0, max_improve_ticks=1, quote_levels_behind=0,
                skip_quote_depth_ahead=None, quote_keep_inside=0.0, quote_ahead_grace_s=1.0,
                cancels_token=20.0, requote_delay_s=1.0)
    base.update(q)
    return types.SimpleNamespace(
        quoting=P.QuotingParams(**base),
        trading_time=P.TradingTimeParams(real_trading_delay_s=0.0, real_trading_cutoff_s=0.0),
        risk=P.RiskParams(max_loss_usd=80.0, max_signal_age_s=1.0, fresh_recovery_s=0.0),
        lip=P.LipSettings(enable_lip_quoting=False, lip_lots=0.0, prop_max_lip=1.0, lip_min_depth_ahead=0.0, lip_hold_s=1.0))


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
    """Records takes and the latest desired ladder per side. `ours` is what
    the ctx reports as our live quotes; `own_orders_in_book` mirrors real
    mode (Kalshi's book includes them)."""

    def __init__(self, own_in_book=False):
        self.portfolio = Portfolio(fee_multiplier=0.0, maker_fee_multiplier=0.0)
        self.books = {}
        self.orders = []
        self.desired = {}
        self.ours = {}
        self.on_book = {}
        self.time = T0
        self.last_event = None
        self.own_orders_in_book = own_in_book
        self._id = 0

    def book(self, t):
        return self.books.get(t)

    def position(self, t):
        return self.portfolio.position(t)

    def buy(self, ticker, side, size, price=None, order_type="limit", intent="take", role="edge"):
        self._id += 1
        self.orders.append((ticker, side, price, intent, role, size))
        return Order(self._id, ticker, side, order_type, price, size, self.time, intent)

    def set_quote_ladder(self, ticker, side, d):
        self.desired[(ticker, side)] = d

    def quote_ladder(self, ticker, side):
        return list(self.ours.get((ticker, side), []))

    def quotes_on_book(self, ticker, side):
        return list(self.on_book.get((ticker, side), self.ours.get((ticker, side), [])))


def step(q, ctx, fairs=None, book=None, dt=0.0):
    ctx.time = ctx.time + pd.Timedelta(seconds=dt)
    if book is not None:
        ctx.books[book.ticker] = book
    ctx.last_event = FairUpdate(fairs) if fairs is not None else ctx.books["A"]
    q(ctx)


def takes(ctx):
    return [o for o in ctx.orders if o[3] == "take"]


def ladder(ctx, side, t="A"):
    d = ctx.desired.get((t, side))
    return [] if d is None else d.prices


def lb(yes_bids, no_bids, bid, ask):
    return Book("A", bid, ask, ladder=types.SimpleNamespace(yes_bids=dict(yes_bids), no_bids=dict(no_bids)))


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


def test_take_hold_requires_signal_to_hold(monkeypatch):
    monkeypatch.setattr(quoter_mod, "TAKE_HOLD_S", 0.5)
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    f = {"A": Fair(0.60, 0.59, 0.61)}
    step(q, ctx, f, Book("A", 0.40, 0.55))
    step(q, ctx, f, dt=0.3)
    assert takes(ctx) == []
    step(q, ctx, f, dt=0.25)
    assert len(takes(ctx)) == 1
    step(q, ctx, f, dt=0.25)         # at most one take per window
    assert len(takes(ctx)) == 1


def test_take_hold_resets_when_signal_lapses(monkeypatch):
    monkeypatch.setattr(quoter_mod, "TAKE_HOLD_S", 0.5)
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.3)
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, dt=0.3)
    assert takes(ctx) == []


def test_quotes_never_go_through_buy():
    q, ctx = Quoter(cfg(quote_levels_behind=2), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ctx.orders == []          # the order manager sends quotes, not the quoter


def test_top_level_first_tick_outside_and_capped_inside_touch():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ladder(ctx, "yes") == [0.41] and ladder(ctx, "no") == [0.41]


def test_ladder_is_consecutive_ticks_behind_the_top():
    q, ctx = Quoter(cfg(quote_levels_behind=2), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ladder(ctx, "yes") == [0.41, 0.40, 0.39]
    assert ladder(ctx, "no") == [0.41, 0.40, 0.39]          # own-side (no) prices


def test_edge_limit_is_first_tick_outside_the_reservation():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    # quote reservation 0.50 - (0.01 + 0.01) = 0.48 -> best acceptable yes bid 0.48
    assert ctx.desired[("A", "yes")].edge_limit == pytest.approx(0.48)


def test_no_self_chase_when_we_are_alone_at_the_touch():
    """Real mode: our own 1 lot is the best bid at 0.41. The top must stay
    0.41 (others' touch 0.40 + 1 tick), not climb to 0.42 over ourselves."""
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx(own_in_book=True)
    ctx.ours[("A", "yes")] = [(0.41, 1.0)]
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.41: 1.0, 0.40: 100.0}, {0.40: 100.0}, 0.41, 0.60))
    assert ladder(ctx, "yes")[0] == 0.41


def test_shadow_does_not_net_our_size_out_of_the_book():
    """Shadow quotes aren't in Kalshi's book, so the displayed touch is
    everyone else's: 0.41 shown -> top 0.42 (still outside the 0.48 edge)."""
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx(own_in_book=False)
    ctx.ours[("A", "yes")] = [(0.41, 1.0)]
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.41: 1.0, 0.40: 100.0}, {0.40: 100.0}, 0.41, 0.60))
    assert ladder(ctx, "yes")[0] == 0.42


def test_unacked_create_is_not_netted_out_of_the_book():
    """Real mode: our 1-lot create at 0.41 hasn't landed, so the 1 lot showing
    at 0.41 is someone else's. Netting ours out would hide it and drop the top
    to 0.41 (others' 0.40 + 1); it must stay 0.42."""
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx(own_in_book=True)
    ctx.ours[("A", "yes")] = [(0.41, 1.0)]
    ctx.on_book[("A", "yes")] = []                     # in flight: not on Kalshi's book yet
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.41: 1.0, 0.40: 100.0}, {0.40: 100.0}, 0.41, 0.60))
    assert ladder(ctx, "yes")[0] == 0.42


def test_skip_depth_ahead_drops_deep_levels_not_the_touch():
    """Others: 300 at 0.40, 200 at 0.39. Cap 250: 0.41 (0 ahead) and 0.40 (300
    ahead -> over) -> only 0.41 is wanted; nothing deeper either."""
    q, ctx = Quoter(cfg(quote_levels_behind=3, skip_quote_depth_ahead=250.0), Model(), FREE,
                    close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.40: 300.0, 0.39: 200.0}, {0.40: 100.0}, 0.40, 0.60))
    assert ladder(ctx, "yes") == [0.41]


def test_skip_depth_ahead_off_keeps_full_ladder():
    q, ctx = Quoter(cfg(quote_levels_behind=3, skip_quote_depth_ahead=None), Model(), FREE,
                    close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.40: 300.0, 0.39: 200.0}, {0.40: 100.0}, 0.40, 0.60))
    assert ladder(ctx, "yes") == [0.41, 0.40, 0.39, 0.38]


def test_touch_improvement_raises_the_top():
    """Others step up from 0.40 to 0.43: the top follows to 0.44 (still
    outside the 0.48 edge)."""
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ladder(ctx, "yes") == [0.41]
    step(q, ctx, book=Book("A", 0.43, 0.60), dt=0.1)
    assert ladder(ctx, "yes") == [0.44]


def test_max_ahead_price_is_others_touch_plus_improve_ticks():
    q, ctx = Quoter(cfg(max_improve_ticks=1), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ctx.desired[("A", "yes")].max_ahead_price == pytest.approx(0.41)   # 0.40 + 1 tick
    assert ctx.desired[("A", "no")].max_ahead_price == pytest.approx(0.41)


def test_max_ahead_none_when_nobody_else_bids():
    """Real mode, we are the only bid: there's no one to be ahead of."""
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx(own_in_book=True)
    ctx.ours[("A", "yes")] = [(0.41, 1.0)]
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.41: 1.0}, {0.40: 100.0}, 0.41, 0.60))
    assert ctx.desired[("A", "yes")].max_ahead_price is None


def test_stale_signal_publishes_nothing_quotable_and_logs_reason():
    m = Model()
    q, ctx = Quoter(cfg(), m, FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ladder(ctx, "yes")
    m.age = 5.0
    step(q, ctx, dt=0.1)
    assert ctx.desired[("A", "yes")] is None and ctx.desired[("A", "no")] is None
    r = q.debug_log[-1]
    assert r["block_bid"] == "stale_signal" and q.block_counts["stale_signal"] == 1
    assert r["prob_raw"] == 0.50     # fair still logged under a block (no theo gap)


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
    assert takes(ctx) == []
    assert ctx.desired[("A", "yes")].edge_limit is None and ladder(ctx, "no")
    assert q.debug_log[-1]["block_bid"] == "series_says_no" and q.debug_log[-1]["block_ask"] is None


def test_max_markets_blocks_far_markets():
    q, ctx = Quoter(cfg(max_markets=1), Model(), FREE, close_time=None), Ctx()
    ctx.books["B"] = Book("B", 0.40, 0.60)
    step(q, ctx, {"A": Fair(0.52, 0.51, 0.53), "B": Fair(0.90, 0.89, 0.91)}, Book("A", 0.40, 0.60))
    by = {r["ticker"]: r for r in q.debug_log}
    assert by["A"]["block_bid"] is None and by["B"]["block_bid"] == "outside_atm_set"


def test_fair_clip_applies_to_quoting_not_log():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.0, 0.0, 0.002)}, Book("A", 0.01, 0.03))
    r = q.debug_log[-1]
    assert r["prob"] == FAIR_CLIP_EPS and r["prob_raw"] == 0.0


def test_lean_adds_to_growing_side_and_subtracts_from_reducing_side():
    q, ctx = Quoter(cfg(), Model(), FREE, close_time=None), Ctx()
    ctx.portfolio.position("A").yes_qty = 10.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    r = q.debug_log[-1]
    assert r["lean_bid"] == pytest.approx(0.02)
    assert r["lean_ask"] == pytest.approx(-0.02)


def test_every_live_level_counts_toward_lean():
    """Three resting yes lots at max_position 3 = full ratio on the bid."""
    q, ctx = Quoter(cfg(max_position=3.0), Model(), FREE, close_time=None), Ctx()
    ctx.ours[("A", "yes")] = [(0.41, 1.0), (0.40, 1.0), (0.39, 1.0)]
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    r = q.debug_log[-1]
    assert r["eff_yes"] == 3.0 and r["lean_bid"] == pytest.approx(0.02)


def test_room_counts_fills_not_resting_quotes():
    """`room` is what may rest in total; the manager subtracts what already
    rests. A 9-lot long position leaves 1 lot of yes room."""
    q, ctx = Quoter(cfg(quote_levels_behind=3), Model(), FREE, close_time=None), Ctx()
    ctx.portfolio.position("A").yes_qty = 9.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ctx.desired[("A", "yes")].room == pytest.approx(1.0)
    assert ctx.desired[("A", "no")].room == pytest.approx(19.0)


def test_requote_delay_after_a_maker_fill_adds_nothing_but_keeps_the_edge_limit():
    q, ctx = Quoter(cfg(requote_delay_s=1.0), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    o = Order(99, "A", "yes", "limit", 0.41, 1.0, ctx.time, "quote")
    ctx.portfolio.record_fill(o, 0.41, 1.0, ctx.time, "maker")
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.1)
    d = ctx.desired[("A", "yes")]
    # Long 1 now, so the bid carries a little lean: edge 0.47, not 0.48.
    assert d.prices == [] and d.edge_limit == pytest.approx(0.47)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=1.0)
    assert ladder(ctx, "yes") == [0.41]


def test_taking_disabled_sends_no_takes_but_still_quotes():
    q, ctx = Quoter(cfg(enable_taking=False), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    assert takes(ctx) == []
    assert ctx.desired[("A", "yes")].edge_limit is not None


def test_quoting_disabled_publishes_unquotable():
    q, ctx = Quoter(cfg(enable_quoting=False), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.40, 0.60))
    assert ctx.desired[("A", "yes")].edge_limit is None


# ---------------------------------------------------------------------------
# LIP target
# ---------------------------------------------------------------------------

from kalshi_infra.trading.lip import LipParams, LipTracker  # noqa: E402

LIPP = LipParams(target_size=300.0, discount_factor=0.5, period_reward_usd=9.0, snapshots_per_period=900)
# Yes bids 100 @0.40, 100 @0.39, 100 @0.38 (target reached at 0.38); edge 0.48.
YES = {0.40: 100.0, 0.39: 100.0, 0.38: 100.0}
NO = {0.40: 400.0}


def lip_cfg(**lip):
    c = cfg()
    base = dict(enable_lip_quoting=True, lip_lots=20.0, prop_max_lip=1.0, lip_min_depth_ahead=0.0, lip_hold_s=1.0)
    base.update(lip)
    c.lip = P.LipSettings(**base)
    return c


def lips(ctx):
    return ctx.lip_targets


class LipCtx(Ctx):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lip_targets = {}
        self.lip_live = {}

    def set_lip_quote(self, ticker, side, t):
        self.lip_targets[(ticker, side)] = t

    def lip_quote(self, ticker, side):
        return self.lip_live.get((ticker, side))


def lip_q(**lip):
    return Quoter(lip_cfg(**lip), Model(), FREE, close_time=None, lip=LipTracker({"A": LIPP})), LipCtx()


def test_lip_target_published_outside_edge():
    q, ctx = lip_q()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb(YES, NO, 0.40, 0.60))
    t = ctx.lip_targets[("A", "yes")]
    assert t is not None and t.price <= 0.48 and 1 <= t.size <= 20


def test_lip_off_publishes_none():
    q, ctx = lip_q(enable_lip_quoting=False)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb(YES, NO, 0.40, 0.60))
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_pulled_on_stale_signal():
    q, ctx = lip_q()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb(YES, NO, 0.40, 0.60))
    q.model.age = 5.0
    step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None


def test_lip_size_is_lip_lots_minus_position_on_that_side():
    q, ctx = lip_q(lip_lots=20.0)
    ctx.portfolio.position("A").yes_qty = 15.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb(YES, NO, 0.40, 0.60))
    t = ctx.lip_targets[("A", "yes")]
    assert t is None or t.size <= 5.0
    ctx.portfolio.position("A").yes_qty = 20.0
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_depth_ahead_counts_our_own_quotes():
    """Shadow: others have 115 at 0.40; our 5-lot edge quote at 0.41 makes 120
    ahead of a LIP quote at 0.40. With a 120 minimum, 0.40 qualifies only
    because our size counts."""
    import kalshi_infra.trading.quoter as Q
    seen = {}
    orig = Q.choose_lip_level

    def spy(**kw):
        if kw["side"] == "yes":
            seen["ahead"] = kw["depth_ahead_fn"](0.40)
        return orig(**kw)

    Q.choose_lip_level = spy
    try:
        q, ctx = lip_q(lip_lots=50.0, lip_min_depth_ahead=120.0, prop_max_lip=0.0)
        ctx.ours[("A", "yes")] = [(0.41, 5.0)]
        step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, lb({0.40: 115.0, 0.30: 300.0}, NO, 0.40, 0.60))
    finally:
        Q.choose_lip_level = orig
    assert seen["ahead"] == pytest.approx(120.0)


def test_model_quote_block_pulls_both_ladders_but_still_takes():
    m = Model()
    m.quote_block = lambda ticker, now_s: "spot_burst"
    q, ctx = Quoter(cfg(), m, FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.60, 0.59, 0.61)}, Book("A", 0.40, 0.55))
    assert ladder(ctx, "yes") == [] and ladder(ctx, "no") == []
    assert ctx.desired[("A", "yes")].edge_limit is None      # cancels anything already resting
    assert [o[:3] for o in takes(ctx)] == [("A", "yes", 0.55)]
    assert q.block_counts["spot_burst"] == 1


def _short(ctx, n):
    ctx.portfolio.record_fill(Order(900, "A", "no", "limit", 0.5, n, T0), 0.5, n, T0, "taker", fee=0.0)


def test_reducing_side_edge_cut_by_lean_at_the_cap():
    # Short 10 at a cap of 10, buying yes reduces it. Band 0.01 each side,
    # lean at full ratio = lean_dollars 0.02.
    q, ctx = Quoter(cfg(constant_edge=0.03), Model(), FREE, close_time=None), Ctx()
    _short(ctx, 10)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.30, 0.70))
    r = q.debug_log[-1]
    assert r["lean_bid"] == pytest.approx(-0.02)
    assert r["bid_edge"] == pytest.approx(0.03 + 0.01 - 0.02)
    assert r["ask_edge"] == pytest.approx(0.03 + 0.01 + 0.02)


def test_reducing_lean_scales_with_position_like_the_growing_lean():
    q, ctx = Quoter(cfg(constant_edge=0.03), Model(), FREE, close_time=None), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.30, 0.70))
    assert q.debug_log[-1]["lean_bid"] == 0.0
    _short(ctx, 5)                                              # half the cap, linear
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)})
    assert q.debug_log[-1]["lean_bid"] == pytest.approx(-0.02 * 0.5)


def test_reducing_edge_never_goes_through_fair():
    q, ctx = Quoter(cfg(constant_edge=0.0, lean_dollars=0.10), Model(), FREE, close_time=None), Ctx()
    _short(ctx, 10)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.30, 0.70))
    r = q.debug_log[-1]
    assert r["bid_edge"] == 0.0 and r["bid_reservation"] == pytest.approx(0.50)
    assert r["quote_bid_reservation"] == pytest.approx(0.50)


def test_reducing_quote_moves_up_to_the_touch_at_the_cap():
    q, ctx = Quoter(cfg(constant_edge=0.03), Model(), FREE, close_time=None), Ctx()
    _short(ctx, 10)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.47, 0.60))
    # Edge 0.03 + 0.01 - 0.02 = 0.02 below fair: 0.48, also the one-tick-inside cap on the 0.47 bid.
    assert ladder(ctx, "yes") == [0.48]
