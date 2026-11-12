import math
import types

from infra.trading.fast_take import FastTaker, Ticket, whole_touch_shift_bp


def test_whole_touch_shift_needs_both_sides_and_is_capped():
    lb, la = math.log(100000.0), math.log(100005.0)
    assert whole_touch_shift_bp(lb + 1e-4, la, lb, la, 2.0) == 0.0          # one side only
    assert abs(whole_touch_shift_bp(lb + 1.5e-4, la + 1.2e-4, lb, la, 2.0) - 1.2) < 1e-9
    assert whole_touch_shift_bp(lb + 5e-4, la + 5e-4, lb, la, 2.0) == 2.0


def _taker():
    sent = []
    eng = types.SimpleNamespace(blocked_reason=lambda: None, market_paused=lambda t: False, live_trading=True,
                                _oev=lambda ev, **kw: sent.append((ev, kw)))
    q = types.SimpleNamespace(cfg=types.SimpleNamespace(quoting=types.SimpleNamespace(fast_take_live=False)))
    return FastTaker(eng, q), sent


def test_ticket_fires_once_on_a_crossing_shift_and_not_below():
    ft, sent = _taker()
    b, a = 100000.0, 100005.0
    tk = Ticket("KX", "yes", "BTC", math.log(b), math.log(a), thr_bp=1.2, cap_bp=2.0, price=0.56, size=1.0,
                not_before=0.0, expires=1e18, req_prob=0.57)
    ft.publish("KX", "yes", tk)
    assert ft.on_cme_touch("BTC", b * (1 + 1.0e-4), a * (1 + 1.0e-4), 0.0) is None
    assert ft.on_cme_touch("ETH", b * (1 + 1.5e-4), a * (1 + 1.5e-4), 0.0) is None
    fire = ft.on_cme_touch("BTC", b * (1 + 1.5e-4), a * (1 + 1.5e-4), 0.0)
    assert fire is not None
    fire()
    assert sent[0][0] == "fast_take_shadow" and sent[0][1]["side"] == "yes"
    assert ft.on_cme_touch("BTC", b * (1 + 1.6e-4), a * (1 + 1.6e-4), 0.0) is None    # single shot
    assert ("KX", "yes") in ft.fired


def test_no_side_fires_on_a_down_shift():
    ft, sent = _taker()
    b, a = 100000.0, 100005.0
    ft.publish("KX", "no", Ticket("KX", "no", "BTC", math.log(b), math.log(a), thr_bp=-1.0, cap_bp=2.0,
                                  price=0.45, size=1.0, not_before=0.0, expires=1e18, req_prob=0.4))
    assert ft.on_cme_touch("BTC", b * (1 + 1.5e-4), a * (1 + 1.5e-4), 0.0) is None
    assert ft.on_cme_touch("BTC", b * (1 - 1.1e-4), a * (1 - 1.1e-4), 0.0) is not None


def test_a_bounce_back_to_a_recent_level_does_not_fire():
    ft, sent = _taker()
    b, a = 100000.0, 100005.0
    up = (b * (1 + 1.5e-4), a * (1 + 1.5e-4))
    assert ft.on_cme_touch("BTC", *up, 0.0) is None                            # held up here first
    assert ft.on_cme_touch("BTC", b, a, 0.0) is None                           # dips back
    ft.publish("KX", "yes", Ticket("KX", "yes", "BTC", math.log(b), math.log(a), thr_bp=1.2, cap_bp=2.0,
                                   price=0.56, size=1.0, not_before=0.0, expires=1e18, req_prob=0.57))
    assert ft.on_cme_touch("BTC", *up, 0.0) is None                            # back up: not a new high
    assert ft.on_cme_touch("BTC", b * (1 + 1.8e-4), a * (1 + 1.8e-4), 0.0) is not None


def test_a_ticket_on_an_old_reference_does_not_fire():
    ft, sent = _taker()
    b, a = 100000.0, 100005.0
    ref = [(math.log(b), math.log(a))]
    ft.ref_fn = lambda coin: ref[0]
    ft.publish("KX", "yes", Ticket("KX", "yes", "BTC", math.log(b), math.log(a), thr_bp=1.2, cap_bp=2.0,
                                   price=0.56, size=1.0, not_before=0.0, expires=1e18, req_prob=0.57))
    assert ft.on_cme_touch("BTC", b, a, 0.0) is None
    ref[0] = (math.log(b * 1.0001), math.log(a * 1.0001))                      # spot moved: reference reset
    assert ft.on_cme_touch("BTC", b * (1 + 1.5e-4), a * (1 + 1.5e-4), 0.0) is None
    assert ("KX", "yes") not in ft._tickets
