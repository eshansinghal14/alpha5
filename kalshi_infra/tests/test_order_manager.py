"""OrderManager: slot state, priorities, the cancels_token reserve, moves,
keep-inside, too-far-ahead, trims, LIP, and the invariant that no quote stays
live too far inside the edge."""
import pytest

from kalshi_infra.trading import order_manager as om
from kalshi_infra.trading.order_manager import Desired, LipTarget, OrderManager


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Ex:
    """Records writes; creates/amends are acked only when the test says so.
    `bucket` None = unmetered; otherwise each write debits it."""

    def __init__(self, bucket=None):
        self.creates, self.cancels, self.amends = [], [], []
        self.bucket = bucket
        self._id = 0

    def next_order_id(self):
        self._id += 1
        return self._id

    def _debit(self, n):
        if self.bucket is not None:
            self.bucket -= n

    def create(self, q):
        self.creates.append(q)
        self._debit(om.COST_CREATE)
        return True

    def amend(self, q, price, remaining):
        self.amends.append((q.order_id, price, remaining))
        self._debit(om.COST_AMEND)
        return True

    def cancel(self, q, reason):
        self.cancels.append((q.order_id, reason))
        self._debit(om.COST_CANCEL)

    def bucket_tokens(self):
        return self.bucket


def mk(bucket=None, reserve=0.0, keep=0.0):
    c = Clock()
    ex = Ex(bucket)
    return OrderManager(ex, cancels_token=reserve, keep_inside=keep, clock=c), ex, c


def ack_all(m, ex):
    for q in ex.creates:
        if q.order_id in m.quotes and m.quotes[q.order_id].exchange_id is None:
            m.on_created(q.order_id, f"x{q.order_id}")


def answer_amends(m, ex, ok=True):
    for oid, *_ in ex.amends:
        if oid in m.quotes and m.quotes[oid].state == om.AMENDING:
            m.on_amended(oid, ok=ok)


def D(prices, edge=0.48, room=10.0, lots=1.0, cap=None):
    return Desired(list(prices), lots, edge, room, cap)


def live_px(m, side="yes", t="A"):
    return sorted((q.price for q in m.live(t, side) if q.state != om.CANCELLING), reverse=True)


# ---------------------------------------------------------------------------
# Edge ladder
# ---------------------------------------------------------------------------

def test_creates_full_ladder_nearest_first():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39]))
    m.pump()
    assert [q.price for q in ex.creates] == [0.41, 0.40, 0.39]


def test_touch_level_of_every_market_goes_before_any_second_level():
    m, ex, _ = mk(bucket=40.0, reserve=20.0)        # two creates fit above the reserve
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.set_desired("B", "yes", D([0.31, 0.30]))
    m.pump()
    assert sorted(q.price for q in ex.creates) == [0.31, 0.41]


def test_cancels_token_is_kept_free_by_creates():
    m, ex, _ = mk(bucket=45.0, reserve=20.0)
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39, 0.38]))
    m.pump()
    assert len(ex.creates) == 2 and ex.bucket >= 20.0
    assert m.counts["create_deferred:reserve"] >= 1


def test_cancels_may_use_the_whole_bucket():
    m, ex, _ = mk(bucket=4.0, reserve=20.0)        # below the reserve: cancels still go
    for oid, p in ((1, 0.41), (2, 0.40)):
        m.quotes[oid] = om.Quote(oid, "A", "yes", p, 1.0, state=om.RESTING, exchange_id=f"x{oid}")
    m.pull_all()
    assert len(ex.cancels) == 2 and ex.bucket == 0.0


def test_cancels_token_follows_live_config():
    m, ex, _ = mk(bucket=35.0, reserve=20.0)
    v = {"r": 20.0}
    m.cancels_token_fn = lambda: v["r"]
    m.set_desired("A", "yes", D([0.41, 0.40]))
    assert m.pump() == 1
    v["r"] = 0.0
    assert m.pump() == 1


def test_no_duplicate_create_while_in_flight():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41]))
    m.pump()
    m.pump()
    assert len(ex.creates) == 1


def test_touch_improvement_moves_the_deepest_spare_up_one_write():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41, 0.40], edge=0.49))
    m.pump()
    assert [(o, p) for o, p, _ in ex.amends] == [(3, 0.42)] and len(ex.creates) == 3 and ex.cancels == []
    answer_amends(m, ex)
    assert live_px(m) == [0.42, 0.41, 0.40]


def test_touch_falling_away_leaves_the_quote_and_never_amends_down():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.45, 0.44]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.44, 0.43]))
    m.pump()
    assert ex.amends == [] and ex.cancels == []
    assert [q.price for q in ex.creates] == [0.45, 0.44, 0.43]


def test_quote_inside_edge_is_cancelled():
    m, ex, _ = mk(bucket=2.0, reserve=20.0)
    m.quotes[1] = om.Quote(1, "A", "yes", 0.41, 1.0, state=om.RESTING, exchange_id="x1")
    m.set_desired("A", "yes", D([0.38], edge=0.39))
    assert ex.cancels == [(1, "inside_edge")]


def test_keep_inside_leaves_a_quote_within_the_threshold():
    m, ex, _ = mk(keep=0.01)
    m.quotes[1] = om.Quote(1, "A", "yes", 0.41, 1.0, state=om.RESTING, exchange_id="x1")
    m.set_desired("A", "yes", D([0.40], edge=0.40))
    assert ex.cancels == []
    m.set_desired("A", "yes", D([0.39], edge=0.39))
    assert ex.cancels == [(1, "inside_edge")]


def test_keep_inside_never_places_or_moves_inside_the_edge():
    m, ex, _ = mk(keep=0.02)
    m.set_desired("A", "yes", D([0.41, 0.40], edge=0.40))
    m.pump()
    assert [q.price for q in ex.creates] == [0.40]


def test_unquotable_side_cancels_everything_touch_first():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", None)
    assert [oid for oid, _ in ex.cancels] == [1, 2, 3]


def test_full_pull_touch_first_then_drains_and_blocks_creates():
    m, ex, _ = mk()
    oid = 0
    for p in (0.41, 0.40, 0.39):
        for side in ("yes", "no"):
            oid += 1
            m.quotes[oid] = om.Quote(oid, "A", side, p, 1.0, state=om.RESTING, exchange_id=f"x{oid}")
    ex.bucket = 6.0
    m.pull_all("pull")
    px = sorted((m.quotes[o].price for o, _ in ex.cancels), reverse=True)
    assert px == [0.41, 0.41, 0.40]
    m.set_desired("A", "yes", D([0.45]))
    m.pump()
    assert ex.creates == [] and m.counts.get("create_deferred:cancels_pending", 0) >= 1
    ex.bucket = 100.0
    m.pump()
    assert len(ex.cancels) == 6


def test_room_drops_deepest_first():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39], room=3.0))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39], room=1.0))
    assert [oid for oid, r in ex.cancels if r == "room"] == [3, 2]


def test_room_blocked_levels_are_not_queued_but_movable_ones_are():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40], room=2.0))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41], edge=0.49, room=2.0))
    assert [(p, s) for _, _, s, p, _ in m.missing()] == [(0.42, "yes")]
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39], room=2.0))
    assert m.missing() == []


def test_create_stops_when_room_runs_out():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39], room=2.0))
    m.pump()
    assert [q.price for q in ex.creates] == [0.41, 0.40]


def test_cancel_before_ack_is_sent_on_ack():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41]))
    m.pump()
    m.set_desired("A", "yes", D([0.38], edge=0.39))
    assert ex.cancels == [] and m.quotes[1].state == om.CANCELLING
    m.on_created(1, "x1")
    assert ex.cancels == [(1, "after_ack")]


def test_ack_landing_inside_the_edge_is_pulled():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41]))
    m.pump()
    m.desired[("A", "yes")] = D([0.38], edge=0.39)
    m.on_created(1, "x1")
    assert ex.cancels == [(1, "inside_edge_on_ack")]


def test_cancel_waits_for_amend_in_flight_then_goes():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41], edge=0.49))
    m.pump()
    assert [(o, p) for o, p, _ in ex.amends] == [(2, 0.42)]
    m.set_desired("A", "yes", D([0.38], edge=0.39))
    assert (2, "inside_edge") not in ex.cancels
    m.on_amended(2, ok=True)
    assert (2, "inside_edge") in ex.cancels


def test_amend_404_drops_the_quote():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41], edge=0.49))
    m.pump()
    m.on_amended(2, ok=False, gone=True)
    assert 2 not in m.quotes


def test_failed_amend_restores_old_price():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41], edge=0.49))
    m.pump()
    m.on_amended(2, ok=False)
    assert m.quotes[2].price == 0.40 and m.quotes[2].state == om.RESTING


def test_no_live_quote_inside_edge_after_set_desired():
    m, ex, _ = mk()
    for edge, ladder in ((0.48, [0.41, 0.40, 0.39]), (0.40, [0.40, 0.39, 0.38]),
                         (0.45, [0.44, 0.43, 0.42]), (0.38, [0.38, 0.37])):
        m.set_desired("A", "yes", D(ladder, edge=edge))
        m.pump(); ack_all(m, ex); answer_amends(m, ex)
        live = [q for q in m.live("A", "yes") if q.state not in (om.CANCELLING, om.AMENDING)]
        assert all(q.price <= edge + 1e-9 for q in live)


def test_cancel_failure_requeues_and_retries():
    m, ex, _ = mk()
    m.quotes[1] = om.Quote(1, "A", "yes", 0.41, 1.0, state=om.RESTING, exchange_id="x1")
    m.set_desired("A", "yes", D([], edge=0.39))
    m.on_cancel_failed(1, "inside_edge")
    m.pump()
    assert len(ex.cancels) == 2


def test_rejected_price_backs_off():
    m, ex, c = mk()
    m.set_desired("A", "yes", D([0.41]))
    m.pump()
    m.on_create_failed(1, rejected=True)
    m.pump()
    assert len(ex.creates) == 1
    c.t += om.REJECT_BACKOFF_S + 0.1
    m.pump()
    assert len(ex.creates) == 2


def test_cancel_stats_split_sparse_and_pull():
    m, ex, c = mk()
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.39], edge=0.40))
    c.t += 1
    m.pull_all("pull")
    s = m.summary()["cancels"]
    assert s["sparse_cancels"] == 1 and s["pull_cancels"] == 1


def test_fill_retires_quote_and_partial_keeps_it():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41], lots=3.0))
    m.pump(); ack_all(m, ex)
    m.on_fill(1, 1.0)
    assert m.quotes[1].remaining == pytest.approx(2.0)
    m.on_fill(1, 2.0)
    assert 1 not in m.quotes


def test_on_book_excludes_unacked_and_reports_amend_at_old_price():
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump()
    assert m.on_book("A", "yes") == []
    ack_all(m, ex)
    m.set_desired("A", "yes", D([0.42, 0.41], edge=0.49))
    m.pump()
    assert sorted(p for p, _ in m.on_book("A", "yes")) == [0.40, 0.41]


# ---------------------------------------------------------------------------
# Too far ahead of the touch
# ---------------------------------------------------------------------------

def test_quote_too_far_ahead_waits_for_grace_then_moves_down():
    m, ex, c = mk()
    m.ahead_grace_s = 1.0
    m.set_desired("A", "yes", D([0.45, 0.44, 0.43], cap=0.48))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.40, 0.39, 0.38], cap=0.41, room=3.0))
    m.pump()
    assert ex.amends == [] and ex.cancels == []
    c.t += 1.0
    m.set_desired("A", "yes", D([0.40, 0.39, 0.38], cap=0.41, room=3.0))
    m.pump()
    assert sorted(p for _, p, _ in ex.amends) == [0.38, 0.39, 0.40] and ex.cancels == []


def test_flicker_inside_grace_costs_nothing():
    m, ex, c = mk()
    m.ahead_grace_s = 1.0
    m.set_desired("A", "yes", D([0.45], cap=0.48))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.40], cap=0.41, room=1.0))
    c.t += 0.5
    m.set_desired("A", "yes", D([0.45], cap=0.48))
    c.t += 1.0
    m.pump()
    assert ex.amends == [] and ex.cancels == []


def test_too_far_ahead_with_full_ladder_is_cancelled():
    m, ex, c = mk()
    m.ahead_grace_s = 0.0
    m.set_desired("A", "yes", D([0.45, 0.40], cap=0.48))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.40], cap=0.41))
    m.pump()
    assert ex.cancels == [(1, "too_far_ahead")]


# ---------------------------------------------------------------------------
# Trim: quotes deeper than the ladder
# ---------------------------------------------------------------------------

def test_levels_deeper_than_the_ladder_are_trimmed_deepest_first():
    """Touch moved up 3 ticks with no room to move: the ladder is
    0.44-0.42; 0.41-0.39 are leftovers below it -> trimmed, deepest first."""
    m, ex, _ = mk()
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39], room=3.0))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.44, 0.43, 0.42], edge=0.49, room=3.0))
    m.pump()                                  # moves go first (spares below each new level)
    answer_amends(m, ex)
    assert live_px(m) == [0.44, 0.43, 0.42] and ex.cancels == []


def test_trim_cancels_leftovers_when_nothing_else_waits():
    m, ex, _ = mk()
    for oid, p in ((1, 0.44), (2, 0.43), (3, 0.42), (4, 0.39), (5, 0.38)):
        m.quotes[oid] = om.Quote(oid, "A", "yes", p, 1.0, state=om.RESTING, exchange_id=f"x{oid}")
    m.set_desired("A", "yes", D([0.44, 0.43, 0.42], room=10.0))
    m.pump()
    assert [o for o, r in ex.cancels if r == "trim"] == [5, 4]


def test_trim_waits_while_edge_levels_are_still_missing():
    m, ex, _ = mk(bucket=40.0, reserve=20.0)
    for oid, p in ((1, 0.44), (2, 0.38)):
        m.quotes[oid] = om.Quote(oid, "A", "yes", p, 1.0, state=om.RESTING, exchange_id=f"x{oid}")
    m.set_desired("A", "yes", D([0.44, 0.43, 0.42, 0.41], room=10.0))
    m.pump()
    # 0.38 is a spare below the missing levels: it's MOVED up first, not trimmed.
    assert not [o for o, r in ex.cancels if r == "trim"]


def test_trim_never_touches_quotes_above_the_ladder():
    m, ex, _ = mk()
    m.quotes[1] = om.Quote(1, "A", "yes", 0.45, 1.0, state=om.RESTING, exchange_id="x1")
    m.set_desired("A", "yes", D([0.42, 0.41]))
    m.pump(); ack_all(m, ex)
    assert not [o for o, r in ex.cancels if r == "trim"]


# ---------------------------------------------------------------------------
# LIP
# ---------------------------------------------------------------------------

def test_lip_goes_after_best_level_but_before_levels_behind_it():
    m, ex, _ = mk(bucket=20.0, reserve=0.0)          # two writes' worth
    m.set_desired("A", "yes", D([0.41, 0.40, 0.39]))
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump()
    assert [(q.role, q.price) for q in ex.creates] == [("edge", 0.41), ("lip", 0.30)]
    ex.bucket = 20.0
    m.pump()
    assert [q.price for q in ex.creates[2:]] == [0.40, 0.39]


def test_every_sides_best_level_before_any_lip():
    m, ex, _ = mk(bucket=20.0, reserve=0.0)
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.set_desired("B", "yes", D([0.31, 0.30]))
    m.set_lip("A", "yes", LipTarget(0.20, 20.0))
    m.pump()
    assert sorted(q.price for q in ex.creates) == [0.31, 0.41] and all(q.role == "edge" for q in ex.creates)


def test_lip_creates_before_lip_moves():
    m, ex, c = mk()
    m.lip_hold_s = 0.0
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    ex.bucket = 10.0                                   # one write
    m.set_lip("A", "yes", LipTarget(0.29, 20.0))       # A wants a move
    m.set_lip("B", "yes", LipTarget(0.25, 20.0))       # B has no LIP quote yet
    m.pump()
    assert ex.amends == [] and ex.creates[-1].ticker == "B"


def test_lip_off_cancels_at_once():
    m, ex, _ = mk()
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    m.set_lip("A", "yes", None)
    assert ex.cancels == [(1, "lip_off")]


def test_lip_move_waits_for_hold_then_one_amend():
    m, ex, c = mk()
    m.lip_hold_s = 1.0
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    m.set_lip("A", "yes", LipTarget(0.29, 15.0))
    m.pump()
    assert ex.amends == []
    c.t += 1.0
    m.pump()
    assert ex.amends == [(1, 0.29, 15.0)] and ex.cancels == [] and len(ex.creates) == 1


def test_pull_all_cancels_lip_too():
    m, ex, _ = mk()
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    m.pull_all("pull")
    assert ex.cancels == [(1, "pull")] and m.lip == {}


def test_lip_size_isnt_counted_as_edge_room():
    m, ex, _ = mk()
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.41, 0.40], room=2.0))
    m.pump()
    assert [q.price for q in ex.creates if q.role == "edge"] == [0.41, 0.40]


def test_lip_never_trimmed_or_moved_as_edge_spare():
    m, ex, _ = mk()
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    m.set_desired("A", "yes", D([0.41, 0.40]))
    m.pump(); ack_all(m, ex)
    assert ex.amends == [] and not ex.cancels


def _resting_lip(m, ex):
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    return m.lip_quote("A", "yes")


def test_lip_requeue_is_one_size_bump_amend():
    m, ex, _ = mk()
    q = _resting_lip(m, ex)
    assert m.requeue_lip(q, urgent=True)
    assert ex.amends == [(q.order_id, 0.30, pytest.approx(20.0 + om.LIP_REQUEUE_BUMP))]
    assert ex.cancels == [] and m.counts["lip_requeue"] == 1


def test_lip_requeue_rate_limited_per_side():
    m, ex, c = mk()
    q = _resting_lip(m, ex)
    assert m.requeue_lip(q, urgent=True)
    answer_amends(m, ex)
    c.t += om.LIP_REQUEUE_MIN_S - 0.1
    assert not m.requeue_lip(q, urgent=True)
    c.t += 0.2
    assert m.requeue_lip(q, urgent=True)


def test_lip_requeue_non_urgent_needs_spare_tokens():
    m, ex, _ = mk(bucket=25.0, reserve=10.0)       # 15 spare: one amend, not amend + 2 creates
    q = _resting_lip(m, ex)
    ex.bucket = 25.0
    assert not m.requeue_lip(q, urgent=False)
    assert m.requeue_lip(q, urgent=True)


def test_requeued_lip_bump_is_not_amended_back():
    m, ex, c = mk()
    m.lip_hold_s = 0.0
    q = _resting_lip(m, ex)
    m.requeue_lip(q, urgent=True); answer_amends(m, ex)
    n = len(ex.amends)
    c.t += 1.0
    m.pump()
    assert len(ex.amends) == n                    # target 20.0, resting 20.01: left alone


def test_on_book_includes_lip():
    m, ex, _ = mk()
    m.set_lip("A", "yes", LipTarget(0.30, 20.0))
    m.pump(); ack_all(m, ex)
    assert m.on_book("A", "yes") == [(0.30, 20.0)]


# ---------------------------------------------------------------------------
# Level cap: levels left behind a stepped-back ladder go before new ones
# ---------------------------------------------------------------------------

def _resting(m, prices, t="A", side="yes"):
    for i, p in enumerate(prices, start=100):
        m.quotes[i] = om.Quote(i, t, side, p, 1.0, state=om.RESTING, exchange_id=f"x{i}")


def test_excess_levels_cancelled_most_aggressive_first_before_any_create():
    # 2026-10-12 17:25:51 BTC: ladder stepped back 0.56/0.55 -> 0.52/0.51 while
    # 0.56/0.55/0.54 still rested, and all five were swept.
    m, ex, _ = mk()
    _resting(m, [0.56, 0.55, 0.54])
    m.set_desired("A", "yes", Desired([0.52, 0.51], 1.0, 0.56, 10.0, None, max_levels=2))
    assert [r for _, r in ex.cancels] == ["excess_level"]
    assert m.quotes[ex.cancels[0][0]].price == 0.56          # the one a sweep hits first
    m.pump()
    # The two still above the ladder are moved down onto it (no new orders).
    assert ex.creates == []
    assert sorted(p for _, p, _ in ex.amends) == [0.51, 0.52]


def test_no_create_while_side_is_at_its_level_cap():
    m, ex, _ = mk()
    _resting(m, [0.60, 0.59])                                   # at the cap, ahead of the ladder
    m.set_desired("A", "yes", Desired([0.55, 0.54], 1.0, 0.60, 10.0, None, max_levels=2))
    m.pump()
    assert ex.creates == []
    assert len(m.live("A", "yes")) <= 2


def test_level_cap_never_drops_a_wanted_level():
    m, ex, _ = mk()
    _resting(m, [0.52, 0.51, 0.49])
    m.set_desired("A", "yes", Desired([0.52, 0.51], 1.0, 0.56, 10.0, None, max_levels=2))
    assert [m.quotes[o].price for o, _ in ex.cancels] == [0.49]


def test_no_level_cap_keeps_old_behaviour():
    m, ex, _ = mk()
    _resting(m, [0.56, 0.55, 0.54])
    m.set_desired("A", "yes", Desired([0.52, 0.51], 1.0, 0.56, 10.0, None))
    assert ex.cancels == []
