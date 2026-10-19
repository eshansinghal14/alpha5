"""LiveEngine + Quoter + OrderManager end to end, synchronously (no event
loop, so every executor call resolves inline). Real mode runs against a fake
order client; shadow uses the engine's own queue model."""
import types

import pandas as pd
import pytest

from kalshi_backtest.book import BookView, L2Book
from kalshi_backtest.core import Trade
from kalshi_infra.kalshi.fees import schedule
from kalshi_infra.kalshi.orders import RestingAck
from kalshi_infra.trading import order_manager as om
from kalshi_infra.trading.engine import LiveEngine
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.quoter import Quoter
from kalshi_infra.tests.test_quoter import Model, cfg

FREE = schedule("quadratic", 0)


class FakeClient:
    """Kalshi stand-in: acks every create, records cancels, unlimited budget."""

    def __init__(self):
        self.creates, self.cancels, self.amends = [], [], []
        self._n = 0
        self.fail_cancel = False

    def amend(self, exchange_id, order, new_size, filled, price=None):
        self.amends.append((exchange_id, price))
        return {"order_id": exchange_id}

    def allow_now(self, kind="order", force=False, reason="untagged"):
        return True

    def tokens_now(self):
        return 1e9

    def note_rate_limited(self, reason="unknown"):
        pass

    def place_resting(self, order):
        self._n += 1
        self.creates.append((order.ticker, order.side, order.price, order.size))
        return RestingAck(exchange_order_id=f"ex{self._n}", fill=None)

    def place_ioc(self, order):
        return None

    def cancel(self, exchange_id, ticker=None):
        if self.fail_cancel:
            raise RuntimeError("boom")
        self.cancels.append(exchange_id)
        return True


def book(bid=0.40, ask=0.60, depth=100.0):
    lb = L2Book("A")
    lb.apply_snapshot("yes", [(bid, depth)], time=pd.Timestamp.now(tz="UTC"))
    lb.apply_snapshot("no", [(round(1 - ask, 2), depth)], time=pd.Timestamp.now(tz="UTC"))
    q = lb.to_quote(pd.Timestamp.now(tz="UTC"))
    return BookView.from_quote(q, ladder=lb)


def setup(real=False, **q):
    q.setdefault("cancels_token", 0.0)
    q.setdefault("quote_levels_behind", 0)
    c = cfg(**q)
    quoter = Quoter(c, Model(), FREE, close_time=None)
    client = FakeClient() if real else None
    eng = LiveEngine(quoter, risk=c.risk, quoting=c.quoting, fees=FREE, order_client=client, event_ticker="E")
    if real:
        eng.live_trading = True          # bypass the arm reconcile (no REST in tests)
    eng.update_book(book())
    return eng, quoter, client


def fair(eng, p=0.50, w=0.01):
    eng.dispatch(FairUpdate({"A": Fair(p, p - w, p + w)}), pd.Timestamp.now(tz="UTC"))


def live_prices(eng, side):
    return sorted((q.price for q in eng.om.live("A", side) if q.state != om.CANCELLING), reverse=True)


def xid(eng, side, price):
    """Exchange id of our live quote at (side, price)."""
    (q,) = [q for q in eng.om.live("A", side) if abs(q.price - price) < 1e-9]
    return q.exchange_id


def fill(eng, exch, side, price=0.41, trade="t1"):
    eng.on_real_fill({"ticker": "A", "side": side, "price": price, "size": 1.0, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker",
                      "order_id": exch, "trade_id": trade})


def test_shadow_rests_one_level_both_sides():
    eng, _, _ = setup(quote_levels_behind=2)
    fair(eng)
    assert live_prices(eng, "yes") == [0.41]
    assert live_prices(eng, "no") == [0.41]
    assert all(q.state == om.RESTING for q in eng.om.live())


def test_real_mode_sends_creates_and_tracks_exchange_ids():
    eng, _, client = setup(real=True)
    fair(eng)
    assert len(client.creates) == 2
    assert all(q.exchange_id and q.state == om.RESTING for q in eng.om.live())


def test_steady_fair_sends_nothing_more():
    eng, _, client = setup(real=True)
    fair(eng)
    n = len(client.creates)
    for _ in range(20):
        fair(eng)
        eng.dispatch(eng.books["A"], pd.Timestamp.now(tz="UTC"))
    assert len(client.creates) == n and client.cancels == []


def test_fair_drop_amends_the_inside_quote_down_not_cancel():
    eng, _, client = setup(real=True)
    fair(eng, 0.50)                      # yes top 0.41, edge 0.48
    top = xid(eng, "yes", 0.41)
    n = len(client.creates)
    fair(eng, 0.42)                      # 0.41 yes is now inside the edge
    new_top = live_prices(eng, "yes")
    assert top not in client.cancels and client.amends == [(top, new_top[0])]
    assert not [c for c in client.creates[n:] if c[1] == "yes"]
    assert len(new_top) == 1 and new_top[0] < 0.41


def test_fair_rise_keeps_old_levels_and_adds_new_ones():
    eng, _, client = setup(real=True)
    fair(eng, 0.50)
    yes_ids = {q.exchange_id for q in eng.om.live("A", "yes")}
    yes_before = set(live_prices(eng, "yes"))
    fair(eng, 0.60, w=0.01)              # no-side levels now inside; yes levels still outside
    assert yes_before <= set(live_prices(eng, "yes"))
    assert not yes_ids & set(client.cancels)


def test_stale_signal_pulls_every_quote():
    eng, quoter, client = setup(real=True)
    fair(eng)
    quoter.model.age = 5.0
    fair(eng)
    assert sorted(client.cancels) == ["ex1", "ex2"]
    assert eng.om.live() == []


def test_pull_quotes_then_nothing_recreated_until_next_decision():
    eng, _, client = setup(real=True)
    fair(eng)
    eng.pull_quotes()
    eng.pump_quotes()
    assert len(client.creates) == 2 and eng.om.live() == []
    fair(eng)
    assert len(client.creates) == 4


def test_disarm_cancels_on_kalshi_before_flipping_mode():
    eng, _, client = setup(real=True)
    fair(eng)
    eng.cancel_resting = lambda: None    # no REST sweep in tests
    eng.set_live_trading(False)
    assert len(client.cancels) == 2 and not eng.live_trading


def test_failed_cancel_stays_tracked_and_is_retried():
    eng, quoter, client = setup(real=True)
    fair(eng)
    client.fail_cancel = True
    quoter.model.age = 5.0
    fair(eng)
    assert len(eng.om.live()) == 2 and all(q.state == om.RESTING for q in eng.om.live())
    client.fail_cancel = False
    fair(eng)
    assert eng.om.live() == []


def test_real_fill_books_and_frees_the_level():
    eng, _, client = setup(real=True)
    fair(eng)
    fill(eng, xid(eng, "yes", 0.41), "yes")
    assert eng.portfolio.position("A").yes_qty == 1.0
    assert 0.41 not in live_prices(eng, "yes")


def test_duplicate_fill_is_booked_once():
    eng, _, _ = setup(real=True)
    fair(eng)
    x = xid(eng, "yes", 0.41)
    fill(eng, x, "yes")
    fill(eng, x, "yes")
    assert eng.portfolio.position("A").yes_qty == 1.0


def test_fill_after_cancel_still_books():
    """A fill that raced its cancel must still reach the ledger."""
    eng, quoter, _ = setup(real=True)
    fair(eng)
    x = xid(eng, "no", 0.41)
    quoter.model.age = 5.0
    fair(eng)                            # everything cancelled and confirmed
    assert eng.om.live() == []
    fill(eng, x, "no", trade="t9")
    assert eng.portfolio.position("A").no_qty == 1.0


def test_order_events_time_the_round_trips_and_flag_a_fill_that_raced_its_cancel():
    """Real mode logs create/cancel sent + answered with RTTs; a fill reported
    after we decided to cancel carries that decision's time and reason."""
    eng, quoter, client = setup(real=True)
    eng.order_events = []
    fair(eng)
    ev = eng.order_events
    assert sum(r["event"] == "create_sent" for r in ev) == 2
    acks = [r for r in ev if r["event"] == "create_ack"]
    assert len(acks) == 2 and all(r["status"] == "ok" and r["rtt_s"] >= 0 for r in acks)
    x = xid(eng, "no", 0.41)
    quoter.model.age = 5.0
    fair(eng)                                # stale: everything cancelled
    sent = [r for r in ev if r["event"] == "cancel_sent"]
    ca = [r for r in ev if r["event"] == "cancel_ack"]
    assert len(sent) == 2 and len(ca) == 2 and all(r["queued_s"] is not None for r in sent)
    assert all(r["rtt_s"] >= 0 and r["status"] == "ok" for r in ca)
    fill(eng, x, "no", trade="t9")
    (f,) = [r for r in ev if r["event"] == "fill"]
    assert f["state"] == "gone" and f["cancel_decided_t"] is not None and f["cancel_reason"]
    assert f["cancel_decided_t"] <= f["t"]


def test_order_events_off_by_default():
    eng, _, _ = setup(real=True)
    fair(eng)
    assert eng.order_events is None


def test_fill_wakes_requote_delay_and_ladder_refills_after_it():
    eng, quoter, client = setup(real=True, requote_delay_s=0.0)
    fair(eng)
    fill(eng, xid(eng, "yes", 0.41), "yes")
    n = len(client.creates)
    fair(eng)
    assert len(client.creates) > n     # the filled level is replaced


def test_shadow_quote_fills_off_trades_behind_the_queue():
    eng, _, _ = setup()
    fair(eng)
    now = pd.Timestamp.now(tz="UTC")
    # 0.41 yes is a new level (no depth ahead): a print there fills it.
    eng.on_trade("A", Trade(ticker="A", time=now, price=0.41, size=1.0, taker_side="no"), now)
    assert eng.portfolio.position("A").yes_qty == 1.0
    assert 0.41 not in live_prices(eng, "yes")


def test_shadow_quote_ignores_print_on_the_other_sides_queue():
    eng, _, _ = setup()
    fair(eng)
    now = pd.Timestamp.now(tz="UTC")
    # A yes taker at no 0.41 hit the NO queue there, not our yes bid at 0.41.
    eng.on_trade("A", Trade(ticker="A", time=now, price=0.41, size=1.0, taker_side="yes"), now)
    assert eng.portfolio.position("A").yes_qty == 0.0
    assert 0.41 in live_prices(eng, "yes")


def _lip_engine():
    eng, _, client = setup(real=True)
    eng.update_book(book(bid=0.10, ask=0.60, depth=500.0))
    eng.om.set_lip("A", "yes", om.LipTarget(0.10, 100.0))
    eng.pump_quotes()
    q = eng.om.lip_quote("A", "yes")
    assert q is not None and q.exchange_id is not None
    return eng, client, q


def test_real_lip_fill_does_not_put_the_same_size_back_up():
    """26OCT170115 SOL: a filled 97-lot LIP order was re-created at 97 by the
    pump 76ms later and filled again (0 -> -170)."""
    eng, client, q = _lip_engine()
    n = len([c for c in client.creates if c[3] == 100.0])
    eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.10, "size": 100.0, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker",
                      "order_id": q.exchange_id, "trade_id": "t1"})
    for _ in range(5):
        eng.pump_quotes()
    assert len([c for c in client.creates if c[3] == 100.0]) == n
    assert eng.om.lip_quote("A", "yes") is None


def test_requeue_lip_moves_front_of_queue_to_back():
    eng, client, q = _lip_engine()
    # 30 ahead at 0.10, 500 displayed: 370 behind us.
    out = eng.requeue_lip({q.exchange_id: 30.0}, min_ahead=200.0)
    assert out == [("A", "yes", 0.10, 30.0, 370.0, True)]
    assert client.cancels[-1] == q.exchange_id


def test_requeue_lip_leaves_deep_or_last_quotes():
    eng, client, q = _lip_engine()
    n = len(client.cancels)
    assert eng.requeue_lip({q.exchange_id: 300.0}, min_ahead=200.0)[0][-1] is False   # deep enough
    assert eng.requeue_lip({q.exchange_id: 400.0}, min_ahead=500.0)[0][-1] is False   # nobody behind
    assert len(client.cancels) == n


def test_position_cap_blocks_a_quote_that_does_not_fit():
    eng, _, client = setup(real=True, max_position=0.5, order_lots=1.0)
    fair(eng)
    assert live_prices(eng, "yes") == [] and live_prices(eng, "no") == []


def test_lost_create_is_found_and_cancelled_never_double_placed():
    """A create that times out (no HTTP answer) may still rest. The slot is
    held -- no second order on that level -- until the lookup says."""
    import requests
    eng, _, client = setup(real=True, quote_levels_behind=0)
    orig = client.place_resting
    calls = {"n": 0}

    def flaky(order):
        calls["n"] += 1
        if calls["n"] == 1:
            order.client_order_id = "cid1"                # the real client stamps this before sending
            orig(order)                                   # it DID land at Kalshi...
            raise requests.ConnectionError("timeout")     # ...but we never heard back
        return orig(order)

    client.place_resting = flaky
    client.find_by_client_id = lambda ev, cid: ("ex1", "A")
    fair(eng)
    assert "ex1" in client.cancels                        # found and pulled
    yes_creates = [c for c in client.creates if c[1] == "yes"]
    assert len(yes_creates) == 1 or "ex1" in client.cancels


def test_lost_create_lookup_failure_keeps_slot_held():
    import requests
    eng, _, client = setup(real=True, quote_levels_behind=0)

    sent = []

    def boom(order):
        order.client_order_id = f"cid{order.order_id}"
        sent.append(order.order_id)
        raise requests.ConnectionError("timeout")

    client.place_resting = boom

    def lookup_fails(ev, cid):
        raise RuntimeError("api down")

    client.find_by_client_id = lookup_fails
    fair(eng)
    fair(eng)
    fair(eng)
    # One create per side, then nothing: both slots are held, unknown.
    assert len(sent) == 2
    assert len(eng.om.live()) == 2 and all(q.state == om.CANCELLING for q in eng.om.live())


def test_rejected_create_frees_slot_and_backs_off_that_price():
    import requests
    eng, _, client = setup(real=True, quote_levels_behind=0)

    class Resp:
        status_code = 400

    def reject(order):
        e = requests.HTTPError("400 bad")
        e.response = Resp()
        raise e

    client.place_resting = reject
    fair(eng)
    assert eng.om.live() == []
    fair(eng)                                   # inside REJECT_BACKOFF_S: not retried
    assert eng.om.counts.get("create", 0) == 2


def test_post_only_reject_is_not_an_order_error():
    import requests
    eng, _, client = setup(real=True, quote_levels_behind=0)

    class Resp:
        status_code = 400
        text = '{"error":{"code":"invalid_order","message":"post only order would cross"}}'

    def reject(order):
        e = requests.HTTPError("400 bad")
        e.response = Resp()
        raise e

    client.place_resting = reject
    for _ in range(8):                          # more than MAX_CONSECUTIVE_ORDER_ERRORS
        fair(eng)
    assert eng.consecutive_order_errors == 0 and eng.n_post_only_rejects >= 2
    assert eng.blocked_reason() is None
    assert eng.om.live() == []


def test_post_only_match_is_specific():
    import requests
    from kalshi_infra.kalshi.orders import is_post_only_reject

    def err(status, text):
        class R:
            pass
        r = R(); r.status_code = status; r.text = text
        e = requests.HTTPError(text); e.response = r
        return e
    assert is_post_only_reject(err(400, "Post only order would cross the book"))
    assert not is_post_only_reject(err(400, "insufficient balance"))
    assert not is_post_only_reject(err(429, "post only"))
    assert not is_post_only_reject(err(500, "post only"))


def _crossing_client(client, size):
    from kalshi_infra.kalshi.orders import RealFill

    def cross(order):
        client._n += 1
        client.creates.append((order.ticker, order.side, order.price, order.size))
        x = f"ex{client._n}"
        return RestingAck(exchange_order_id=x, fill=RealFill(price=order.price, size=min(size, order.size),
                                                             fee=0.0, exchange_order_id=x))
    client.place_resting = cross


def _net(eng):
    p = eng.portfolio.position("A")
    return p.yes_qty - p.no_qty


def test_quote_that_crossed_on_arrival_is_not_booked_again_from_the_fill_feed():
    """The create response books the cross with no trade id; the fill feed then
    reports the same execution with one."""
    eng, _, client = setup(real=True, quote_levels_behind=0, order_lots=1.0)
    _crossing_client(client, 1.0)
    fair(eng)
    before = _net(eng)
    yes = [x for x, (t, s, p, z) in zip((f"ex{i + 1}" for i in range(len(client.creates))), client.creates)
           if s == "yes"][0]
    eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.41, "size": 1.0, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "taker", "order_id": yes, "trade_id": "t1"})
    assert _net(eng) == before


def test_swept_cross_reported_per_level_is_booked_once():
    eng, _, client = setup(real=True, quote_levels_behind=0, order_lots=2.0)
    _crossing_client(client, 2.0)
    fair(eng)
    before = _net(eng)
    yes = [f"ex{i + 1}" for i, c in enumerate(client.creates) if c[1] == "yes"][0]
    for tid in ("t1", "t2"):
        eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.41, "size": 1.0, "fee": 0.0,
                          "time": pd.Timestamp.now(tz="UTC"), "liquidity": "taker", "order_id": yes,
                          "trade_id": tid})
    assert _net(eng) == before


def test_partial_cross_still_books_the_later_maker_fill():
    eng, _, client = setup(real=True, quote_levels_behind=0, order_lots=2.0)
    _crossing_client(client, 1.0)
    fair(eng)
    before = _net(eng)
    yes = [f"ex{i + 1}" for i, c in enumerate(client.creates) if c[1] == "yes"][0]
    eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.41, "size": 1.0, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "taker", "order_id": yes, "trade_id": "t1"})
    eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.41, "size": 1.0, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker", "order_id": yes, "trade_id": "t2"})
    assert _net(eng) == before + 1.0


def test_rearm_does_not_rebook_a_quote_fill_seeded_from_kalshi():
    """A quote fills, the session re-arms (reset + seed from Kalshi), and the
    REST poll reports that fill again: it must stay booked once."""
    eng, _, client = setup(real=True)
    fair(eng)
    x = xid(eng, "yes", 0.41)
    f = {"ticker": "A", "side": "yes", "price": 0.41, "size": 1.0, "fee": 0.0,
         "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker", "order_id": x, "trade_id": "t1"}
    eng.on_real_fill(dict(f))
    assert _net(eng) == 1.0
    eng.reset_session()
    eng.seed_fills_from_kalshi([dict(f)])
    eng.apply_real_fills([dict(f)])
    assert _net(eng) == 1.0


def test_quote_left_behind_within_quote_levels_behind():
    """Top steps 0.41 -> 0.42 (others' bid 0.41): with quote_levels_behind 1
    the old order is left where it is -- no amend, create or cancel."""
    eng, _, client = setup(real=True, quote_levels_behind=1)
    fair(eng)
    n = (len(client.creates), len(client.amends), len(client.cancels))
    eng.update_book(book(bid=0.41))
    eng.dispatch(eng.books["A"], pd.Timestamp.now(tz="UTC"))
    assert (len(client.creates), len(client.amends), len(client.cancels)) == n
    assert live_prices(eng, "yes") == [0.41]


def test_real_touch_improvement_is_one_amend_not_cancel_plus_create():
    """Others' bid steps up 0.40 -> 0.42: the top follows to 0.43, two ticks
    over our 0.41 -- past quote_levels_behind 1, so it's amended up; no yes
    create or cancel."""
    eng, _, client = setup(real=True, quote_levels_behind=1)
    fair(eng)
    yes_before = [c for c in client.creates if c[1] == "yes"]
    eng.update_book(book(bid=0.42))
    eng.dispatch(eng.books["A"], pd.Timestamp.now(tz="UTC"))
    yes_after = [c for c in client.creates if c[1] == "yes"]
    assert len(yes_after) == len(yes_before)
    assert client.amends and all(px >= 0.41 for _, px in client.amends)
    assert live_prices(eng, "yes")[0] == 0.43


def test_shadow_touch_improvement_moves_too():
    eng, _, _ = setup(quote_levels_behind=0)
    fair(eng)
    eng.om.sim_bucket._tokens = eng.om.sim_bucket.rate   # refill (the test clock is wall time)
    eng.update_book(book(bid=0.42))
    eng.dispatch(eng.books["A"], pd.Timestamp.now(tz="UTC"))
    assert live_prices(eng, "yes")[0] == 0.43 and eng.om.counts.get("move", 0) >= 1


def test_keep_inside_param_reaches_the_manager():
    eng, _, client = setup(real=True, quote_levels_behind=0, quote_keep_inside=0.02)
    fair(eng, 0.50)                      # yes top 0.41, edge 0.48
    fair(eng, 0.42)                      # edge 0.40: 0.41 is 1c inside -> kept
    assert not client.cancels or all(c != xid(eng, "yes", 0.41) for c in client.cancels)


def test_halt_blocks_new_creates():
    eng, _, client = setup(real=True)
    eng.halted, eng.halt_reason = True, "test"
    fair(eng)
    assert client.creates == []


def _losing(eng, loss=30.0):
    """A yes position bought at 0.90 with the bid at 0.40: touch PnL -0.50/lot."""
    eng.portfolio.position("A").yes_qty = loss / 0.50
    eng.portfolio.position("A").yes_cost = loss / 0.50 * 0.90


def test_loss_limit_is_per_event_not_run():
    eng, _, _ = setup(real=True)
    eng.risk.max_loss_usd = 25.0
    _losing(eng, 20.0)
    eng.loss_offset = -100.0           # an old run total must not count any more
    assert not eng.check_failsafe()
    _losing(eng, 26.0)
    assert eng.check_failsafe() and eng.halt_kind == "loss"
    assert "event PnL" in eng.halt_reason


def test_matched_yes_no_pairs_mark_at_one_dollar():
    """80 yes @0.81 and 80 no @0.16 (an exited LIP round trip) pay $80 whatever
    the result: worth $80 - $77.60 = +$2.40, not bid + (1 - ask) per pair.
    Touch-marking both legs with a 0.40/0.60 book read -$17.60."""
    eng, _, _ = setup(real=True)
    p = eng.portfolio.position("A")
    p.yes_qty, p.yes_cost, p.no_qty, p.no_cost = 80.0, 80 * 0.81, 80.0, 80 * 0.16
    assert eng.current_pnl(mark="touch") == pytest.approx(80.0 - 80 * 0.97)
    p.yes_qty, p.yes_cost = 86.0, 80 * 0.81 + 6 * 0.50       # 6 unpaired yes, marked at the 0.40 bid
    assert eng.current_pnl(mark="touch") == pytest.approx(80.0 - 80 * 0.97 + 6 * (0.40 - 0.50))


def test_emptied_book_side_marks_at_its_last_price_not_zero():
    """In the last seconds every yes bid can be pulled: 60 yes bought at 0.80
    marked at $0 read -$48 and could trip the loss limit (26OCT162015 read
    -$172.81, settled -$13.75). The last bid seen is used instead."""
    eng, _, _ = setup(real=True)
    eng.update_book(book(bid=0.78, ask=0.82))
    p = eng.portfolio.position("A")
    p.yes_qty, p.yes_cost = 60.0, 60 * 0.80
    lb = L2Book("A")
    lb.apply_snapshot("yes", [], time=pd.Timestamp.now(tz="UTC"))
    lb.apply_snapshot("no", [(0.18, 100.0)], time=pd.Timestamp.now(tz="UTC"))
    eng.update_book(BookView.from_quote(lb.to_quote(pd.Timestamp.now(tz="UTC")), ladder=lb))
    assert eng.books["A"].bid is None
    assert eng.current_pnl(mark="touch") == pytest.approx(60 * (0.78 - 0.80))


def test_real_lock_refuses_arm():
    eng, _, _ = setup(real=False)
    eng.order_client = FakeClient()
    eng.real_lock = "loss limit tripped in E0"
    assert eng.set_live_trading(True) is False and not eng.live_trading


def _audit_setup():
    eng, _, client = setup(real=True, order_lots=2.0)
    fair(eng)
    yes = [xid(eng, "yes", p) for p in live_prices(eng, "yes")]
    return eng, yes


def test_audit_fills_during_the_positions_read_do_not_halt():
    """26OCT130915 (2026-10-13): two quotes filled fully while the positions
    request was out. Kalshi's answer included them, our ledger didn't yet, and
    with the quotes gone from the book the resting tolerance no longer covered
    them -- a halt on a ledger that matched Kalshi's fills exactly."""
    eng, yes = _audit_setup()
    before = eng.ledger_positions()
    # Both yes quotes fill in full during the round trip and the no side is
    # pulled, so nothing is left resting to widen the tolerance -- as in the
    # real halt, where our websocket had the fills and Kalshi's read didn't.
    for q in list(eng.om.live("A", "no")):
        eng.om.on_cancel_done(q.order_id)
    for i, x in enumerate(yes):
        fill(eng, x, "yes", trade=f"y{i}a")
        fill(eng, x, "yes", trade=f"y{i}b")
    assert eng.om.live("A") == [] and eng.ledger_positions()["A"] == 2.0
    eng.audit_positions({"A": {"position": 0.0}}, before=before)
    eng.audit_positions({"A": {"position": 2.0}}, before=eng.ledger_positions())   # next audit: caught up
    assert not eng.halted and eng._audit_strikes == {}


def test_audit_one_off_mismatch_warns_then_clears():
    eng, _ = _audit_setup()
    eng.audit_positions({"A": {"position": 50.0}})     # far past tolerance (1 + resting)
    assert not eng.halted and eng.last_audit_diffs
    eng.audit_positions({"A": {"position": 0.0}})
    assert not eng.halted and eng._audit_strikes == {}


def test_audit_persistent_mismatch_still_halts():
    eng, _ = _audit_setup()
    eng.audit_positions({"A": {"position": 50.0}}, before=eng.ledger_positions())
    assert not eng.halted
    eng.audit_positions({"A": {"position": 50.0}}, before=eng.ledger_positions())
    assert eng.halted and eng.halt_kind == "audit"


def test_shadow_bucket_and_cancels_token_meter_creates():
    """Shadow models the 80/s bucket: a 60-token reserve leaves two creates."""
    eng, _, _ = setup(cancels_token=60.0)
    fair(eng)
    assert len(eng.om.live()) == 2
    assert {q.price for q in eng.om.live()} == {0.41}   # the touch level of each side first
