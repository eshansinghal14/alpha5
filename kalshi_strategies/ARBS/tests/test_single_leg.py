import numpy as np
import pandas as pd
import pytest

from kalshi_infra.kalshi.fees import fee
from kalshi_strategies.ARBS.single_leg import backtest as B
from kalshi_strategies.ARBS.single_leg import candles as C
from kalshi_strategies.ARBS.spec import Basket, Leg

T = B.TAKER_RATE


def basket(payout=1.0):
    return Basket(basket_id="t", relationship_id="t", type="complete_set", cluster="t",
                  legs=(Leg("A", "yes"), Leg("B", "yes")), payout=payout, quote_until=None, resume_quoting=None)


def mkgrid(a_bid, a_ask, b_bid, b_ask, a_bh=None):
    n = len(a_bid)
    f = lambda v: np.asarray(v, dtype=np.float32)
    return C.Grid(ts=np.arange(n, dtype=np.int64) * 60,
                  bid={"A": f(a_bid), "B": f(b_bid)}, ask={"A": f(a_ask), "B": f(b_ask)},
                  bid_high={"A": f(a_bh if a_bh is not None else a_bid), "B": f(b_bid)},
                  ask_low={"A": f(a_ask), "B": f(b_ask)})


def P(**kw):
    base = dict(entry_edge=0.10, lag_rule="thinnest", lookback_min=2, stop_loss=0.05, max_hold_min=5,
                complete_on_exit=False, min_price=0.02, max_spread=0.10, cooldown_min=100)
    return B.Params(**{**base, **kw})


def test_buys_the_quiet_leg_and_exits_at_target():
    # B (deep) moves every minute around mid 0.60 -> A's fair = 0.40; A sits at ask 0.30, quiet.
    n = 8
    b_bid = [0.59, 0.60] * 4
    b_ask = [0.61, 0.62] * 4
    a_bid, a_ask = [0.28] * n, [0.30] * n
    bh = list(a_bid)
    bh[6] = 0.40                                   # someone pays our target in minute 6
    g = mkgrid(a_bid, a_ask, b_bid, b_ask, a_bh=bh)
    tr = B.simulate_basket(basket(), g, P())
    assert len(tr) == 1
    t = tr[0]
    assert t.ticker == "A" and t.reason == "target"
    assert t.entry == pytest.approx(0.30 + fee(0.30, 1, T))
    assert t.exit == t.target and t.pnl == pytest.approx(t.target - t.entry)


def test_stop_sells_at_the_bid_net_of_fee():
    n = 8
    b_bid, b_ask = [0.59, 0.60] * 4, [0.61, 0.62] * 4
    a_ask = [0.30] * n
    a_bid = [0.28, 0.28, 0.28, 0.28, 0.24, 0.22, 0.22, 0.22]
    tr = B.simulate_basket(basket(), mkgrid(a_bid, a_ask, b_bid, b_ask), P(stop_loss=0.05))
    t = tr[0]
    # Stop is 0.05 under the 0.28 bid at entry: 0.24 doesn't trip it, 0.22 does.
    assert t.reason == "stop" and t.hold_min == 3
    assert t.exit == pytest.approx(0.22 - fee(0.22, 1, T), abs=1e-4)


def test_one_position_per_leg_across_baskets():
    t = lambda b, a, z: B.Trade(basket_id=b, ticker="A", side="yes", entry_ts=a, entry=0.3, target=0.4, exit_ts=z,
                                exit=0.4, reason="target", pnl=0.1, hold_min=1, lag=0.1)
    kept = B.one_per_leg([t("x", 0, 600), t("y", 0, 600), t("z", 300, 900), t("w", 660, 900)])
    assert [k.basket_id for k in kept] == ["x", "w"]


def test_max_hold_completes_into_the_basket_when_it_locks_more_than_the_bid():
    n = 12
    # B's ask falls to 0.55 later: A at 0.31 incl fee + B 0.55 + fee < 1 -> completing locks a gain.
    b_bid = [0.59, 0.60] * 3 + [0.53, 0.54] * 3
    b_ask = [0.61, 0.62] * 3 + [0.55, 0.56] * 3
    a_bid, a_ask = [0.28] * n, [0.30] * n
    tr = B.simulate_basket(basket(), mkgrid(a_bid, a_ask, b_bid, b_ask), P(complete_on_exit=True, max_hold_min=4,
                                                                            stop_loss=0.5))
    t = tr[0]
    assert t.reason == "complete"
    assert t.exit > 0.28


def test_no_entry_when_lag_below_edge():
    n = 6
    b_bid, b_ask = [0.69, 0.70] * 3, [0.71, 0.72] * 3     # A fair ~0.30 = its ask: no lag
    g = mkgrid([0.28] * n, [0.30] * n, b_bid, b_ask)
    assert B.simulate_basket(basket(), g, P()) == []


def test_grid_forward_fills_and_marks_extremes_only_on_real_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CACHE_DIR", str(tmp_path))
    pd.DataFrame({"ts": [60, 240], "bid": [0.10, 0.20], "ask": [0.12, 0.22], "bid_high": [0.15, 0.25],
                  "ask_low": [0.11, 0.21], "volume": [1.0, 0.0]}).to_parquet(tmp_path / "X.parquet")
    g = C.grid(["X"], 0, 300)
    assert np.isnan(g.bid["X"][0])
    assert list(np.round(g.bid["X"][1:], 2)) == [0.10, 0.10, 0.10, 0.20, 0.20]
    assert g.bid_high["X"][1] == pytest.approx(0.15) and g.bid_high["X"][2] == pytest.approx(0.10)
