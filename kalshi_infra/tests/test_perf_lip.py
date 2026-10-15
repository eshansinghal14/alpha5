import math

import pandas as pd

from kalshi_backtest.core import Order, Portfolio
from kalshi_viz import perf
from kalshi_viz.data import has_lip


def _fills():
    t0 = pd.Timestamp("2026-10-15 13:00", tz="UTC")
    rows = [  # LIP fill picked off by 3c at 10s; edge maker fill +1c; taker -2c
        dict(time=t0, ticker="E-BTC", size=10.0, liquidity="maker", role="lip", mo0=1.0, mo1=-1.0, mo10=-3.0, mo60=-4.0, pnl_c=-5.0),
        dict(time=t0, ticker="E-ETH", size=4.0, liquidity="maker", role="edge", mo0=1.0, mo1=1.0, mo10=1.0, mo60=1.0, pnl_c=2.0),
        dict(time=t0, ticker="E-ETH", size=2.0, liquidity="taker", role="edge", mo0=-1.0, mo1=-2.0, mo10=-2.0, mo60=-2.0, pnl_c=-2.0),
    ]
    d = pd.DataFrame(rows)
    d["event"] = "E"
    d["fee"], d["settled"], d["open_c"] = 0.0, d.pnl_c, d.pnl_c
    return d


def test_lip_summary_sets_reward_against_lip_fill_markout():
    lip = pd.DataFrame({"time": [pd.Timestamp("2026-10-15 13:10", tz="UTC")] * 2, "ticker": ["E-BTC", "E-ETH"],
                        "event": ["E", "E"], "usd": [0.40, 0.10]})
    s = perf.summarize_lip(_fills(), lip)
    assert math.isclose(s["reward"], 0.50)
    assert s["lip_fills"] == 1 and s["lip_contracts"] == 10
    assert math.isclose(s["adverse_10s"], -0.30) and math.isclose(s["adverse_60s"], -0.40)
    assert math.isclose(s["lip_fill_pnl"], -0.50) and math.isclose(s["net"], 0.0)
    assert math.isclose(s["reward_per_contract_c"], 5.0)


def test_markouts_split_lip_from_edge_quotes():
    rows = perf.summarize(_fills())["markouts"]
    assert rows["LIP quote"]["fills"] == 1 and rows["edge quote"]["fills"] == 1
    assert math.isclose(rows["LIP quote"]["mo10"], -3.0)


def test_no_lip_rows_without_lip_fills():
    d = _fills()
    d["role"] = "edge"
    assert "LIP quote" not in perf.summarize(d)["markouts"]


def test_curves_add_lip_lines_only_with_lip():
    d = _fills()
    assert set(perf.curves(d).line) == {"PnL (settled; open at mid)", "1s markout", "10s markout"}
    lip = pd.DataFrame({"time": [pd.Timestamp("2026-10-15 13:10", tz="UTC")], "ticker": ["E-BTC"], "event": ["E"], "usd": [0.4]})
    c = perf.curves(d, lip=lip)
    assert {"LIP reward", "PnL incl. LIP"} <= set(c.line)
    assert math.isclose(c[c.line == "PnL incl. LIP"].usd.iloc[-1], (-0.5 + 0.08 - 0.04) + 0.4)


def test_fill_carries_order_role():
    p = Portfolio()
    o = Order(order_id=1, ticker="E-BTC", side="yes", order_type="limit", price=0.4, size=1, decision_time=None)
    o.role = "lip"
    assert p.record_fill(o, 0.4, 1, pd.Timestamp.now(tz="UTC"), "maker").role == "lip"
    o2 = Order(order_id=2, ticker="E-BTC", side="yes", order_type="limit", price=0.4, size=1, decision_time=None)
    assert p.record_fill(o2, 0.4, 1, pd.Timestamp.now(tz="UTC"), "taker").role == "edge"
    assert p.record_fill(o, 0.4, 1, pd.Timestamp.now(tz="UTC"), "maker", seeded=True).role is None


def test_has_lip():
    assert not has_lip({"lip": {"total_usd": 0.0, "accrued_usd": {}, "period_reward_usd": {}}})
    assert has_lip({"lip": {"period_reward_usd": {"E-BTC": 20.0}}})
    assert not has_lip(None) and not has_lip({})
