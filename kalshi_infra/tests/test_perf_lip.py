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


def _lip():
    return pd.DataFrame({"time": [pd.Timestamp("2026-10-15 13:10", tz="UTC")] * 2, "ticker": ["E-BTC", "E-ETH"],
                         "event": ["E", "E"], "usd": [0.40, 0.10]})


def test_split_lip_reward_against_lip_fills():
    s = perf.summarize_split(_fills(), _lip())["lip"]
    assert s["fills"] == 1 and s["contracts"] == 10
    assert math.isclose(s["mo1"], -1.0) and math.isclose(s["mo10"], -3.0)
    assert math.isclose(s["reward"], 0.50) and math.isclose(s["pnl"], -0.50) and math.isclose(s["net"], 0.0)
    assert math.isclose(s["reward_c"], 5.0)


def test_split_regular_is_everything_off_the_lip_quote():
    s = perf.summarize_split(_fills(), _lip())
    r, a = s["regular"], s["all"]
    assert r["fills"] == 2 and r["taker"] == 1 and r["contracts"] == 6
    assert math.isclose(r["mo1"], (4 * 1.0 + 2 * -2.0) / 6)
    assert a["fills"] == r["fills"] + s["lip"]["fills"]
    assert math.isclose(a["pnl"], r["pnl"] + s["lip"]["pnl"])
    assert math.isclose(a["pnl_incl_lip"], a["pnl"] + 0.50)


def test_split_without_lip_frame_or_fills():
    s = perf.summarize_split(_fills())
    assert "reward" not in s["lip"] and "pnl_incl_lip" not in s["all"]
    e = perf.summarize_split(_fills().iloc[0:0], _lip())
    assert e["all"]["fills"] == 0 and math.isnan(e["lip"]["reward_c"]) and math.isclose(e["lip"]["reward"], 0.5)


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


def test_lip_frame_drops_markets_under_one_dollar():
    p = perf.Perf.__new__(perf.Perf)
    p._lock = __import__("threading").Lock()
    t = pd.Timestamp("2026-10-15 20:30", tz="UTC")
    p._lip = {"E1-SOL": (t, 1.512), "E1-BTC": (t, 0.94), "E1-ETH": (t, 0.999999), "E2-XRP": (t, 1.0)}
    f = p.lip_frame()
    # 0.999999 rounds down to 0.99 before the $1 test, so it pays nothing.
    assert dict(zip(f.ticker, f.usd)) == {"E1-SOL": 1.51, "E2-XRP": 1.00}
    assert set(f.event) == {"E1", "E2"}
    p._lip = {"E1-BTC": (t, 0.5)}
    assert p.lip_frame().empty


def test_has_lip():
    assert not has_lip({"lip": {"total_usd": 0.0, "accrued_usd": {}, "period_reward_usd": {}}})
    assert has_lip({"lip": {"period_reward_usd": {"E-BTC": 20.0}}})
    assert not has_lip(None) and not has_lip({})
