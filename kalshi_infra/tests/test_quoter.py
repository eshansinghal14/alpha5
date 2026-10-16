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
    base = dict(order_lots=1.0, max_position=10.0, constant_edge=0.01, constant_edge_ref_s=None, edge_fraction=1.0,
                lean_dollars=0.02, max_markets=None, atm_hysteresis=0,
                enable_taking=True, enable_quoting=True, quoting_edge=0.0, max_improve_ticks=1, quote_levels_behind=0,
                skip_quote_depth_ahead=None, quote_keep_inside=0.0, quote_ahead_grace_s=1.0,
                cancels_token=20.0, requote_delay_s=1.0)
    base.update(q)
    return types.SimpleNamespace(
        quoting=P.QuotingParams(**base),
        trading_time=P.TradingTimeParams(real_trading_delay_s=0.0, real_trading_cutoff_s=0.0),
        risk=P.RiskParams(max_loss_usd=80.0, max_signal_age_s=1.0),
        lip=P.LipSettings(**LIP_OFF))


LIP_OFF = dict(enable_lip_quoting=False, lip_cheap_max_price=0.2, lip_bid_lots=0.0, lip_offer_lots=0.0,
               lip_constant_edge=None, lip_max_markets=None,
               lip_requeue_min_ahead=None, lip_min_time_left_s=0.0, lip_hold_s=1.0)


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


def _const_at(ref, left_s):
    """The constant edge the quoter applied with `left_s` seconds to close."""
    q, ctx = Quoter(cfg(constant_edge=0.03, constant_edge_ref_s=ref), Model(), FREE,
                    close_time=T0 + pd.Timedelta(seconds=left_s)), Ctx()
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.30, 0.70))
    r = q.debug_log[-1]
    assert r["bid_edge"] == pytest.approx(r["constant_edge"] + 0.01)   # band 0.01, no lean, no fee
    return r["constant_edge"]


def test_constant_edge_flat_all_event_when_ref_is_empty():
    assert _const_at(None, 600) == pytest.approx(0.03)
    assert _const_at(None, 20) == pytest.approx(0.03)


def test_constant_edge_scales_sqrt_inside_ref_only():
    assert _const_at(180, 600) == pytest.approx(0.03)               # before the window: flat
    assert _const_at(180, 180) == pytest.approx(0.03)
    assert _const_at(180, 90) == pytest.approx(0.03 * 2 ** 0.5)     # half the ref left
    assert _const_at(180, 60) == pytest.approx(0.03 * 3 ** 0.5)


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


def test_quotes_return_as_soon_as_signal_is_fresh_again():
    m = Model()
    q = Quoter(cfg(), m, FREE, close_time=None)
    assert not q.signal_stale()
    m.age = 5.0
    assert q.signal_stale()
    m.age = 0.0
    assert not q.signal_stale()      # no recovery hold


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
CLOSE = T0 + pd.Timedelta(minutes=10)
# Yes side: a 0.10 longshot (cheap band). No side: best no bid 0.89 (expensive:
# no LIP there).
YES = {0.10: 500.0, 0.09: 300.0}
NO = {0.89: 120.0, 0.88: 400.0}


def lip_cfg(**lip):
    c = cfg()
    base = dict(LIP_OFF, enable_lip_quoting=True, lip_bid_lots=400.0, lip_offer_lots=400.0, lip_min_time_left_s=180.0)
    base.update(lip)
    c.lip = P.LipSettings(**base)
    return c


class LipCtx(Ctx):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lip_targets = {}
        self.lip_live = {}

    def set_lip_quote(self, ticker, side, t):
        self.lip_targets[(ticker, side)] = t

    def lip_quote(self, ticker, side):
        return self.lip_live.get((ticker, side))


@pytest.fixture(autouse=True)
def no_lip_floor(request, monkeypatch):
    """LIP tests that aren't about the $1 floor use a toy program whose payouts
    sit under it; `floor` in a test's name keeps the real one (and the
    cent-a-period test for dropping a side)."""
    if "floor" not in request.node.name:
        monkeypatch.setattr(quoter_mod, "LIP_MIN_PAYOUT_USD", 0.0)
        monkeypatch.setattr(quoter_mod, "LIP_PAYOUT_TICK_USD", 0.0)


def lip_q(close=CLOSE, own_in_book=False, max_loss=1e6, **lip):
    c = lip_cfg(**lip)
    c.risk = P.RiskParams(max_loss_usd=max_loss, max_signal_age_s=1.0)
    return (Quoter(c, Model(), FREE, close_time=close, lip=LipTracker({"A": LIPP})),
            LipCtx(own_in_book=own_in_book))


def lip_step(q, ctx, yes=YES, no=NO, fair=0.10, dt=0.0):
    # Book: yes bid = best yes bid; yes ask = 1 - best no bid.
    bid = max(yes) if yes else None
    ask = round(1 - max(no), 2) if no else None
    step(q, ctx, {"A": Fair(fair, fair - 0.01, fair + 0.01)}, lb(yes, no, bid, ask), dt=dt)


def test_lip_rests_at_best_bid_cheap_side_only():
    q, ctx = lip_q()
    lip_step(q, ctx)
    y = ctx.lip_targets[("A", "yes")]
    assert (y.price, y.size) == (0.10, 400.0)          # cheap side
    assert ctx.lip_targets[("A", "no")] is None        # 0.89: no expensive-side LIP


def test_lip_ignores_fair_and_edge():
    """Fair 0.50 puts the edge reservation far below the 0.10 best bid; the
    LIP quote is placed regardless."""
    q, ctx = lip_q()
    lip_step(q, ctx, fair=0.50)
    assert ctx.lip_targets[("A", "yes")].price == 0.10


def test_lip_cheap_side_tapers_past_the_cutoff():
    """400 lots up to 0.20, linear to 0 at 0.50: 0.30 -> 266, 0.45 -> 66."""
    q, ctx = lip_q()
    lip_step(q, ctx, yes={0.30: 500.0, 0.29: 300.0}, no={0.65: 500.0}, fair=0.32)
    assert ctx.lip_targets[("A", "yes")].size == 266.0
    lip_step(q, ctx, yes={0.45: 500.0, 0.44: 300.0}, no={0.50: 500.0}, fair=0.47, dt=0.1)
    assert ctx.lip_targets[("A", "yes")].size == 66.0


def test_lip_off_at_and_above_half():
    q, ctx = lip_q()
    lip_step(q, ctx, yes={0.50: 300.0}, no={0.49: 300.0})
    assert ctx.lip_targets[("A", "yes")] is None           # 0.50: taper ends
    q, ctx = lip_q()
    lip_step(q, ctx, yes={0.10: 500.0}, no={0.80: 2000.0, 0.79: 300.0})
    assert ctx.lip_targets[("A", "no")] is None            # 0.80: no expensive-side LIP


def test_lip_off_near_close():
    q, ctx = lip_q(close=T0 + pd.Timedelta(seconds=179))
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None


def test_lip_off_publishes_none():
    q, ctx = lip_q(enable_lip_quoting=False)
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_zero_lots_disables_lip():
    q, ctx = lip_q(lip_bid_lots=0.0, lip_offer_lots=0.0)
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None


def test_lip_pulled_on_stale_signal():
    q, ctx = lip_q()
    lip_step(q, ctx)
    q.model.age = 5.0
    step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None


def test_lip_size_net_of_position_on_that_side():
    """Both sides cheap (yes 0.10, no 0.15). 150 yes held: yes quotes 400 - 150,
    no quotes 400 + 150 (the band is the max position on that side)."""
    q, ctx = lip_q()
    ctx.portfolio.position("A").yes_qty = 150.0
    cheap_no = {0.15: 120.0, 0.14: 400.0}
    lip_step(q, ctx, no=cheap_no, fair=0.12)
    assert ctx.lip_targets[("A", "yes")].size == 250.0
    assert ctx.lip_targets[("A", "no")].size == 550.0
    ctx.portfolio.position("A").yes_qty = 400.0
    lip_step(q, ctx, no=cheap_no, fair=0.12, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_follows_others_best_not_our_own_quote():
    """Real mode: the book shows our 400-lot LIP quote at 0.11 as the best yes
    bid. Others' best is 0.10, so the target is 0.10, not 0.11."""
    q, ctx = lip_q(own_in_book=True)
    ctx.on_book[("A", "yes")] = [(0.11, 400.0)]
    lip_step(q, ctx, yes={0.11: 400.0, 0.10: 500.0})
    assert ctx.lip_targets[("A", "yes")].price == 0.10


def test_lip_capped_inside_far_touch():
    # Best yes bid 0.10 with the yes ask at 0.11 (no bid 0.89): fine. Make the
    # far side cross: no bid 0.91 -> yes ask 0.09 -> cap at 0.08. Others' 50 at
    # 0.10 is under target, so our 0.08 still scores. The no side needs the 300
    # target on its own (no expensive-side LIP of ours to make it up).
    q, ctx = lip_q()
    lip_step(q, ctx, yes={0.10: 50.0}, no={0.91: 400.0})
    assert ctx.lip_targets[("A", "yes")].price == 0.08


# Others under target at the best bid, so a quote capped one or two ticks back
# still scores (behind a level already at target it would earn nothing).
THIN_YES = {0.10: 50.0, 0.09: 100.0}


def test_lip_constant_edge_quotes_best_bid_with_edge_to_spare():
    """Best yes bid 0.10 with fair 0.13: 3c under fair, more than a 2c edge ->
    quoted at the best bid itself (not stepped down)."""
    q, ctx = lip_q(lip_constant_edge=0.02)
    lip_step(q, ctx, fair=0.13)
    assert ctx.lip_targets[("A", "yes")].price == 0.10


def test_lip_constant_edge_caps_best_bid_at_fair_minus_edge():
    # Best yes bid 0.10, fair 0.11: only 1c under -> quote at 0.11 - 0.02 = 0.09.
    q, ctx = lip_q(lip_constant_edge=0.02)
    lip_step(q, ctx, yes=THIN_YES, fair=0.11)
    assert ctx.lip_targets[("A", "yes")].price == 0.09
    assert q.block_counts.get("lip_capped_at_fair", 0) >= 1


def test_lip_capped_price_still_has_to_clear_the_floor():
    """Capped below the best bid the quote earns DF^N less; with the real $1
    floor (test name has 'floor') it isn't placed if that can't pay."""
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5), lip_constant_edge=0.0)
    lip_step(q, ctx, yes={0.10: 50.0, 0.09: 100.0, 0.08: 600.0}, fair=0.08)   # capped 0.08, scores DF^1
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_below_floor", 0) >= 1


def test_lip_capped_behind_a_full_level_earns_nothing_and_is_not_quoted():
    """FLOOR_YES meets the 300 target by 0.09; capped at 0.08 the quote scores
    nothing, so it isn't placed."""
    q, ctx = lip_q(lip_constant_edge=0.0)
    lip_step(q, ctx, yes=FLOOR_YES, fair=0.08)
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_no_reward", 0) >= 1


def test_lip_constant_edge_at_exactly_the_edge_quotes():
    q, ctx = lip_q(lip_constant_edge=0.02)
    lip_step(q, ctx, fair=0.12)
    assert ctx.lip_targets[("A", "yes")].price == 0.10


def test_lip_constant_edge_rounds_the_cap_down():
    q, ctx = lip_q(lip_constant_edge=0.01)
    lip_step(q, ctx, yes=THIN_YES, fair=0.105)                 # cap 0.095 -> 0.09
    assert ctx.lip_targets[("A", "yes")].price == 0.09
    lip_step(q, ctx, yes=THIN_YES, fair=0.115, dt=0.1)         # cap 0.105 -> 0.10, the best bid
    assert ctx.lip_targets[("A", "yes")].price == 0.10


def test_lip_constant_edge_zero_never_bids_above_fair():
    q, ctx = lip_q(lip_constant_edge=0.0)
    lip_step(q, ctx, yes=THIN_YES, fair=0.085)                 # best bid 0.10 > fair -> 0.08
    assert ctx.lip_targets[("A", "yes")].price == 0.08


def test_lip_one_sided_book_farms_nothing():
    """No bids at all on the no side: Kalshi excludes every snapshot, so a
    yes LIP quote earns nothing whatever its size -- not quoted, even with the
    $1 floor already met (shortfall 0)."""
    q, ctx = lip_q()
    q.lip.accrued_usd["A"] = 1.5
    lip_step(q, ctx, no={})
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None
    assert q.block_counts.get("lip_no_reward", 0) >= 1


def test_lip_side_that_adds_nothing_is_dropped_other_side_kept():
    """Both sides cheap (yes 0.10, no 0.15). Others' 500 at the yes best bid
    meet the target alone and the cap puts us a tick back: our yes adds
    nothing. The no side still earns."""
    q, ctx = lip_q(lip_constant_edge=0.0)
    lip_step(q, ctx, yes={0.10: 500.0, 0.09: 300.0}, no={0.15: 120.0, 0.14: 400.0}, fair=0.095)
    assert ctx.lip_targets[("A", "yes")] is None
    assert ctx.lip_targets[("A", "no")] is not None


def _resting_lip(q, ctx, fair=0.10):
    lip_step(q, ctx, fair=fair)
    t = ctx.lip_targets[("A", "yes")]
    ctx.lip_live[("A", "yes")] = (t.price, t.size)
    return t


def _soft_pull(q, on: bool):
    """A soft pull: the payout floor. A $100 target that nothing reaches
    makes A's side qualify for no snapshot, so its best case is $0 < $1."""
    q.lip.params["A"] = dataclasses.replace(LIPP, target_size=100_000.0) if on else LIPP
    quoter_mod.LIP_MIN_PAYOUT_USD = 1.0 if on else 0.0


def test_lip_soft_pull_held_for_debounce():
    q, ctx = lip_q()
    t = _resting_lip(q, ctx)
    _soft_pull(q, True)
    lip_step(q, ctx, dt=0.5)                                  # pull starts here
    assert ctx.lip_targets[("A", "yes")] == t
    lip_step(q, ctx, dt=1.5)
    assert ctx.lip_targets[("A", "yes")] == t                # 1.5s in: still held
    lip_step(q, ctx, dt=0.6)
    assert ctx.lip_targets[("A", "yes")] is None             # 2.1s in: pulled


def test_lip_soft_pull_that_clears_resets_the_clock():
    q, ctx = lip_q()
    t = _resting_lip(q, ctx)
    _soft_pull(q, True)
    lip_step(q, ctx, dt=1.5)
    _soft_pull(q, False)
    lip_step(q, ctx, dt=0.1)
    _soft_pull(q, True)
    lip_step(q, ctx, dt=1.5)
    assert ctx.lip_targets[("A", "yes")] == t                # a new 1.5s, not 3.1s


def test_lip_spot_burst_pulls_resting_quote_at_once():
    q, ctx = lip_q()
    _resting_lip(q, ctx)
    q.model.quote_block = lambda ticker, now_s: "spot_burst"
    lip_step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None
    assert "lip_pull_held" not in q.block_counts


def test_lip_spot_burst_pulls_the_exit_too():
    q, ctx = lip_q(lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is not None
    q.model.quote_block = lambda ticker, now_s: "spot_burst"
    _cheap_step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_hard_pulls_skip_the_debounce():
    q, ctx = lip_q()
    _resting_lip(q, ctx)
    q.model.block[("A", "yes")] = "basis_tie"
    lip_step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None
    q, ctx = lip_q()
    _resting_lip(q, ctx)
    q.model.age = 5.0                                         # stale signal
    step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_soft_pull_with_nothing_resting_is_immediate():
    q, ctx = lip_q()
    _soft_pull(q, True)
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_held_quote_never_grows_after_a_partial_fill():
    q, ctx = lip_q()
    t = _resting_lip(q, ctx)
    ctx.lip_live[("A", "yes")] = (t.price, 150.0)            # 250 of 400 filled
    _soft_pull(q, True)
    lip_step(q, ctx, dt=0.5)
    assert ctx.lip_targets[("A", "yes")].size == 150.0


def test_lip_obeys_side_block_and_enable_quoting():
    q, ctx = lip_q()
    cheap_no = {0.15: 120.0, 0.14: 400.0}
    q.model.block[("A", "yes")] = "basis_tie"
    lip_step(q, ctx, no=cheap_no, fair=0.12)
    assert ctx.lip_targets[("A", "yes")] is None
    assert ctx.lip_targets[("A", "no")] is not None
    q.cfg.quoting = dataclasses.replace(q.cfg.quoting, enable_quoting=False)
    lip_step(q, ctx, no=cheap_no, fair=0.12, dt=0.1)
    assert ctx.lip_targets[("A", "no")] is None


def test_lip_pulled_on_spot_burst():
    q, ctx = lip_q()
    q.model.quote_block = lambda ticker, now_s: "spot_burst"
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("A", "no")] is None


# Program: $9 per 900s period, target 300. Yes side others 100 @0.10 + 300
# @0.09; our 400 at the best bid. Only the 0.10 level scores (cum 500 >= 300
# there): our share 400/500 = 0.8. No side (0.89): no LIP, share 0.
# Rate = 0.8 / 2 * 9/900 = 0.004 $/s. LIP stops 180s before close.
FLOOR_YES = {0.10: 100.0, 0.09: 300.0}
RATE = 0.8 / 2 * 9 / 900


def test_lip_floor_blocks_when_best_case_payout_too_small():
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))   # 120s -> $0.48
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_below_floor", 0) >= 1


def test_lip_floor_passes_with_enough_time_left():
    q, ctx = lip_q()                                      # 420s -> $1.68
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is not None
    assert q._lip_proj["A"] == pytest.approx(420 * RATE, rel=1e-6)


def test_lip_floor_counts_what_is_already_accrued():
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))
    q.lip.accrued_usd["A"] = 0.75                                               # 0.75 + 0.48 >= 1.00
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is not None


def test_lip_floor_shortfall_shrinks_with_accrued():
    """$0.50 already earned: the rest needs $0.50. 120s left x $0.004/s = $0.48."""
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))
    q.lip.accrued_usd["A"] = 0.50
    lip_step(q, ctx, yes=FLOOR_YES)                       # need 0.50, have 0.48
    assert ctx.lip_targets[("A", "yes")] is None
    q.lip.accrued_usd["A"] = 0.53                          # need 0.47; 0.48 left
    lip_step(q, ctx, yes=FLOOR_YES, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is not None


def test_lip_floor_same_threshold_to_start_and_keep():
    """$0.53 accrued, $0.48 still to earn, shortfall $0.47: a new quote starts
    on exactly what a resting one needs to stay (no x1.2 start margin)."""
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))
    q.lip.accrued_usd["A"] = 0.53
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is not None
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))
    q.lip.accrued_usd["A"] = 0.53
    ctx.lip_live[("A", "yes")] = (0.10, 400.0)
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is not None


def test_lip_floor_once_period_pays_a_side_needs_only_a_cent():
    """Over $1 already earned: no $1 floor, but the side must still add a
    payable cent. 1s of window x $0.004/s is $0.004: dropped. 5s is $0.02: quoted."""
    q, ctx = lip_q(close=T0 + pd.Timedelta(seconds=181))
    q.lip.accrued_usd["A"] = 1.05
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_no_reward", 0) >= 1
    q, ctx = lip_q(close=T0 + pd.Timedelta(seconds=185))
    q.lip.accrued_usd["A"] = 1.05
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is not None


def test_lip_floor_counts_size_behind_a_level_already_at_target():
    """Others' 500 at the best bid meet the 300 target alone; our 400 there
    still scores (whole level qualifies), share 400/900. 420s x that rate is
    $0.93, under $1, so the floor blocks on size, not on zero credit."""
    q, ctx = lip_q()
    lip_step(q, ctx)
    assert q._lip_proj.get("A") is None
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_below_floor", 0) >= 1
    # Others' 400 alone meet target; share 400/800 -> $1.05, over $1.
    q2, ctx2 = lip_q()
    lip_step(q2, ctx2, yes={0.10: 400.0, 0.09: 300.0})
    assert ctx2.lip_targets[("A", "yes")] is not None
    assert q2._lip_proj["A"] == pytest.approx(420 * (400 / 800) / 2 * 9 / 900, rel=1e-6)


def test_lip_floor_projection_caps_at_the_period_total():
    """The per-account cap limits the period sum, not each second: $0.90 cap,
    already $0.50 accrued, so at most $0.40 more however high the rate."""
    q, ctx = lip_q()
    q.lip.params["A"] = LipParams(target_size=300.0, discount_factor=0.5, period_reward_usd=9.0,
                                  snapshots_per_period=900, max_reward_usd=0.90)
    q.lip.accrued_usd["A"] = 0.50
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is None
    assert q.block_counts.get("lip_below_floor", 0) >= 1


def _two_markets(**lip):
    base = dict(lip_max_markets=1)
    base.update(lip)
    q = Quoter(lip_cfg(**base), Model(), FREE, close_time=CLOSE, lip=LipTracker({"A": LIPP, "B": LIPP}))
    return q, LipCtx()


def _book(t, best_size):
    yes = {0.10: best_size, 0.09: 300.0}
    return Book(t, 0.10, 0.11, ladder=types.SimpleNamespace(yes_bids=yes, no_bids=dict(NO)))


def _step2(q, ctx, a, b, dt=0.0):
    ctx.time = ctx.time + pd.Timedelta(seconds=dt)
    ctx.books["A"], ctx.books["B"] = _book("A", a), _book("B", b)
    ctx.last_event = FairUpdate({"A": Fair(0.10, 0.09, 0.11), "B": Fair(0.10, 0.09, 0.11)})
    q(ctx)


def test_lip_max_markets_keeps_the_best_projection():
    q, ctx = _two_markets()
    _step2(q, ctx, a=250.0, b=100.0)      # shares 400/650 vs 400/500: B
    assert ctx.lip_targets[("B", "yes")] is not None
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_max_markets_member_holds_until_beaten_by_25pct():
    q, ctx = _two_markets()
    _step2(q, ctx, a=100.0, b=150.0)               # A 0.80 vs B 0.73: A
    assert ctx.lip_targets[("A", "yes")] is not None
    _step2(q, ctx, a=150.0, b=120.0, dt=0.1)        # B 0.77 vs A 0.73: +6%, A keeps it
    assert ctx.lip_targets[("A", "yes")] is not None and ctx.lip_targets[("B", "yes")] is None
    _step2(q, ctx, a=250.0, b=50.0, dt=0.1)         # B 0.89 vs A 0.62: +44%, swap
    assert ctx.lip_targets[("B", "yes")] is not None and ctx.lip_targets[("A", "yes")] is None


def test_lip_floor_drops_a_side_worth_under_a_cent_once_the_market_pays():
    """Both sides cheap. The no side alone takes the market past $1: 300 of
    the 320 at 0.15 (which meets the 300 target), 0.94/2 x $0.01/s x 420s =
    $1.97. The yes side behind a 1M-lot wall adds ~$2e-4 over the window:
    dropped, no side kept."""
    q, ctx = lip_q(lip_bid_lots=300.0, lip_offer_lots=300.0)
    lip_step(q, ctx, yes={0.10: 1_000_000.0}, no={0.15: 20.0, 0.14: 400.0}, fair=0.12)
    assert ctx.lip_targets[("A", "yes")] is None
    assert ctx.lip_targets[("A", "no")] is not None
    assert q.block_counts.get("lip_no_reward", 0) >= 1


def test_lip_floor_keeps_a_small_side_the_market_needs_to_reach_a_dollar():
    """Same walled yes side, but the market is short of $1 without it
    (300 of 2,300 on the no side, ~$0.27): the side rule keeps it and the $1
    floor judges the whole market instead."""
    q, ctx = lip_q(lip_bid_lots=300.0, lip_offer_lots=300.0)
    lip_step(q, ctx, yes={0.10: 1_000_000.0}, no={0.15: 2000.0, 0.14: 400.0}, fair=0.12)
    assert q.block_counts.get("lip_no_reward", 0) == 0
    assert q.block_counts.get("lip_below_floor", 0) >= 1


def test_lip_floor_resting_quote_still_pulled_under_one_dollar():
    q, ctx = lip_q(close=T0 + pd.Timedelta(minutes=5))   # $0.48
    ctx.lip_live[("A", "yes")] = (0.10, 400.0)
    lip_step(q, ctx, yes=FLOOR_YES)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_floor_slot_passes_to_next_market_that_clears_it():
    """A holds the one slot; when A's best case falls under $1, B (still over)
    takes it on the same decision pass."""
    q, ctx = _two_markets()
    _step2(q, ctx, a=100.0, b=150.0)               # A $1.68, B $1.53: A
    assert ctx.lip_targets[("A", "yes")] is not None and ctx.lip_targets[("B", "yes")] is None
    _step2(q, ctx, a=600.0, b=150.0, dt=0.1)        # A $0.84 < $1
    assert ctx.lip_targets[("A", "yes")] is None
    assert ctx.lip_targets[("B", "yes")] is not None
    assert q.block_counts.get("lip_below_floor", 0) >= 1


def test_lip_floor_no_slot_when_no_market_clears_it():
    q, ctx = _two_markets()
    _step2(q, ctx, a=600.0, b=700.0)                # $0.84 and $0.76
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("B", "yes")] is None
    assert q._lip_set == set()


def test_lip_slot_passes_on_when_holder_loses_its_target():
    """A loses its LIP quote to the fair cap (fair 0.005: capped under the
    minimum price); B takes the slot."""
    q, ctx = _two_markets(lip_constant_edge=0.0)
    _step2(q, ctx, a=100.0, b=150.0)
    assert ctx.lip_targets[("A", "yes")] is not None
    ctx.time = ctx.time + pd.Timedelta(seconds=0.1)
    ctx.books["A"], ctx.books["B"] = _book("A", 100.0), _book("B", 150.0)
    ctx.last_event = FairUpdate({"A": Fair(0.005, 0.0, 0.01), "B": Fair(0.10, 0.09, 0.11)})
    q(ctx)
    assert ctx.lip_targets[("A", "yes")] is None and ctx.lip_targets[("B", "yes")] is not None


def _lip_fill(ctx, side, size, price):
    o = Order(777, "A", side, "limit", price, size, ctx.time, "quote")
    o.role = "lip"
    ctx.portfolio.record_fill(o, price, size, ctx.time, "maker", fee=0.0)
    ctx.portfolio.fills[-1].role = "lip"


# A book cheap on both sides (yes 0.10 bid, no 0.15 bid) so an exit has a
# legal price under 0.50; fair between them.
CHEAP_YES = {0.10: 120.0, 0.09: 400.0}
CHEAP_NO = {0.15: 120.0, 0.14: 400.0}


def _cheap_step(q, ctx, dt=0.0, **kw):
    lip_step(q, ctx, **{"yes": CHEAP_YES, "no": CHEAP_NO, "fair": 0.12, "dt": dt, **kw})


def test_lip_fill_is_offered_back_at_best_on_other_side():
    """Filled 120 no on the LIP no bid: offer 120 back on the yes side, marked
    front. One tick in front of the 0.10 best yes bid is 0.11, under the far
    touch (no bid 0.15 -> yes ask 0.85), so the exit rests at 0.11 plus band."""
    q, ctx = lip_q(lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx)
    t = ctx.lip_targets[("A", "yes")]
    assert t is not None and t.size == 120.0 and t.front is True and t.price == 0.11


def test_lip_exit_steps_in_front_when_spread_allows():
    q, ctx = lip_q(lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx, yes={0.06: 500.0})
    assert ctx.lip_targets[("A", "yes")].price == 0.07


def test_lip_exit_never_rests_at_or_over_half():
    """120 yes bought at 0.10: offering it back is a no bid near 0.89, over
    0.50, so no exit and no LIP of any kind on the no side."""
    q, ctx = lip_q()
    _lip_fill(ctx, "yes", 120.0, 0.10)
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "no")] is None
    assert q.block_counts.get("lip_exit_over_half", 0) >= 1


def test_lip_never_quotes_at_or_over_half_whatever_the_sizes():
    q, ctx = lip_q(lip_bid_lots=500.0, lip_offer_lots=500.0)
    lip_step(q, ctx, yes={0.10: 500.0}, no={0.80: 2000.0, 0.79: 300.0})
    assert ctx.lip_targets[("A", "no")] is None
    lip_step(q, ctx, yes={0.55: 500.0}, no={0.40: 2000.0}, fair=0.57, dt=0.1)
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_exit_adds_band_size_on_a_cheap_side():
    """9 no bought on a cheap no side (0.15): the yes exit at the cheap 0.11
    offers the 9 back plus the yes band, 400 + 9."""
    q, ctx = lip_q()
    _lip_fill(ctx, "no", 9.0, 0.15)
    _cheap_step(q, ctx)
    t = ctx.lip_targets[("A", "yes")]
    assert t.size == 409.0 and t.front is True


def test_lip_exit_one_sided_book_offers_inventory_only():
    """120 no bought on LIP; then every yes bid leaves. The inventory is still
    offered back -- a tick inside the far touch (no bid 0.15 -> yes 0.84),
    over 0.50, so with no yes bids and the far touch too high: no exit."""
    q, ctx = lip_q(lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx, yes={})
    assert ctx.lip_targets[("A", "yes")] is None


def test_lip_exit_inventory_never_cut_by_loss_budget():
    q, ctx = lip_q(max_loss=1.0, lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")].size >= 120.0


def test_lip_exit_shrinks_as_it_fills_and_ignores_edge_fills():
    q, ctx = lip_q(lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _lip_fill(ctx, "yes", 50.0, 0.11)                   # 50 of the exit filled
    _cheap_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")].size == 70.0
    o = Order(778, "A", "no", "limit", 0.15, 3.0, ctx.time, "quote")    # edge fill: not LIP inventory
    ctx.portfolio.record_fill(o, 0.15, 3.0, ctx.time, "maker", fee=0.0)
    _cheap_step(q, ctx, dt=0.1)
    assert ctx.lip_targets[("A", "yes")].size == 70.0


def test_lip_exit_continues_past_the_lip_time_cutoff():
    q, ctx = lip_q(close=T0 + pd.Timedelta(seconds=100), lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)
    _cheap_step(q, ctx)
    assert ctx.lip_targets[("A", "no")] is None
    assert ctx.lip_targets[("A", "yes")].size == 120.0


def test_loss_budget_shrinks_lip_entry_to_fit():
    """Worst case = every order fills and settles against us. Both sides
    cheap: yes 400 @0.10 risks $40 if A loses, no @0.15 risks $0.15/lot if A
    wins. With $45 of budget the no side gets what keeps both outcomes >= -45."""
    q, ctx = lip_q(max_loss=45.0)
    lip_step(q, ctx, yes={0.10: 120.0, 0.09: 400.0}, no={0.15: 120.0, 0.14: 400.0}, fair=0.12)
    y, n = ctx.lip_targets[("A", "yes")], ctx.lip_targets[("A", "no")]
    assert y.size == 400.0
    assert n is not None and n.size < 400.0
    worst = q._worst_case(ctx, {("A", "yes", "lip"): [(y.price, y.size)], ("A", "no", "lip"): [(n.price, n.size)]})
    assert worst >= -45.0 - 1e-6


def test_loss_budget_blocks_lip_when_exhausted():
    q, ctx = lip_q(max_loss=30.0)       # yes 400 @0.10 alone would risk $40
    lip_step(q, ctx)
    y = ctx.lip_targets[("A", "yes")]
    assert y is not None and y.size == 300.0              # $30 / $0.10
    assert q.block_counts.get("max_loss_budget", 0) >= 1


def test_loss_budget_is_per_event():
    q, ctx = lip_q(max_loss=30.0)
    # Earlier events' losses no longer shrink this event's budget.
    ctx._engine = types.SimpleNamespace(loss_offset=-20.0)
    lip_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")].size == 300.0    # $30 / $0.10


def test_loss_budget_never_cuts_a_lip_exit():
    q, ctx = lip_q(max_loss=1.0, lip_bid_lots=0.0)
    _lip_fill(ctx, "no", 120.0, 0.15)                      # $18 at risk already, over budget
    _cheap_step(q, ctx)
    assert ctx.lip_targets[("A", "yes")].size == 120.0     # the exit still rests in full


def test_loss_budget_shrinks_edge_ladder_and_takes():
    q, ctx = Quoter(cfg(order_lots=10.0, quote_levels_behind=1), Model(), FREE, close_time=None), Ctx()
    q.cfg.risk = P.RiskParams(max_loss_usd=5.0, max_signal_age_s=1.0)
    step(q, ctx, {"A": Fair(0.50, 0.49, 0.51)}, Book("A", 0.30, 0.70))
    d = ctx.desired[("A", "yes")]
    # Two yes levels near 0.48/0.47 at 10 lots would risk ~$9.5 if A loses.
    assert d.lots * sum(d.prices) <= 5.0 + 1e-6
    assert q.block_counts.get("max_loss_budget", 0) >= 1


def test_lips_per_side_band_and_taper():
    s = P.LipSettings(**dict(LIP_OFF, lip_bid_lots=400.0, lip_offer_lots=100.0))
    assert s.lots_at(0.05, "yes") == 400.0 and s.lots_at(0.20, "yes") == 400.0
    assert s.lots_at(0.05, "no") == 100.0 and s.lots_at(0.20, "no") == 100.0
    assert s.lots_at(0.35, "yes") == pytest.approx(200.0)
    assert s.lots_at(0.35, "no") == pytest.approx(50.0)
    assert s.lots_at(0.49, "yes") == pytest.approx(400.0 * 0.01 / 0.30)
    for side in ("yes", "no"):
        assert s.lots_at(0.50, side) == 0.0 and s.lots_at(0.80, side) == 0.0 and s.lots_at(0.95, side) == 0.0
    assert s.max_lots() == 400.0


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
