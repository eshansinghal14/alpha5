"""PnL replay from journal rows."""
import pytest

from kalshi_viz.arbs_pnl import replay


def fill(t, bid, ticker, side, size, cost):
    return {"t": t, "live": True, "kind": "fill", "basket_id": bid, "ticker": ticker, "side": side, "size": size,
            "cost": cost}


def test_locked_then_settled_then_unwound():
    info = {"b": (1.0, 2), "m": (1.0, 2)}
    rows = [fill(1, "b", "A", "yes", 5, 2.0), fill(2, "b", "B", "yes", 5, 2.5),          # locks 5 - 4.5
            {"t": 3, "live": True, "kind": "settled", "basket_id": "b", "pnl": 0.5, "results": {"A": "yes"}},
            fill(4, "m", "A", "no", 2, 0.6),                                              # unfinished: counts 0
            fill(5, "m", "A", "yes", 2, 1.5),                                             # nets out
            {"t": 5, "live": True, "kind": "settled", "basket_id": "m", "pnl": -0.1, "results": {"A": "unwound 2"}},
            fill(6, "x", "A", "yes", 1, 0.5) | {"live": False}]
    df = replay(rows, info)
    assert list(df.total.round(4)) == [0.0, 0.5, 0.5, 0.5, 0.5, 0.4]
    assert df.settled.iloc[-1] == pytest.approx(0.4)
