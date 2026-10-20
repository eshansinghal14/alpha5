"""Edge market share: our edge vs theo against all the edge vs theo that
printed, end to end from journal parts."""
import math

import pandas as pd

from kalshi_infra.kalshi.ws_feed import parse_ws_print
from kalshi_infra.runtime import journal as jr
from kalshi_viz import perf

T0 = pd.Timestamp("2026-10-15 13:00:00", tz="UTC")
DAY = "2026-10-15"


def _t(s):
    return T0 + pd.Timedelta(seconds=s)


def test_parse_ws_print_uses_exchange_time():
    now = _t(5)
    r = parse_ws_print({"market_ticker": "E-A", "trade_id": "T1", "yes_price_dollars": "0.4200",
                        "no_price_dollars": "0.5800", "count_fp": "3.00", "taker_side": "no",
                        "ts_ms": int(_t(1).value // 10**6)}, now)
    assert r["time"] == _t(1) and r["received"] == now
    assert r["yes_price"] == 0.42 and r["size"] == 3.0 and r["taker_side"] == "no"
    assert parse_ws_print({"market_ticker": "E-A"}, now) is None


def test_journal_dedupes_prints_on_trade_id(tmp_path):
    j = jr.DayJournal("S", DAY, out_root=str(tmp_path))
    row = {"time": _t(0), "ticker": "E-A", "trade_id": "T1", "yes_price": 0.4, "size": 1.0}
    j.record_trades([row, dict(row)])
    j.record_trades([dict(row, trade_id=None), dict(row, trade_id=None)])
    assert len(j._buf["trades"]) == 3


def _journal(root, decisions, fills, trades, bbo):
    j = jr.DayJournal("S", DAY, out_root=str(root))
    for r in decisions:
        j.record_decision(r)
    j._buf["bbo"].extend(bbo)
    j._buf["fills"].extend(fills)
    j.record_trades(trades)
    jr.write(j.capture())


def test_edge_share_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    dec = [
        {"time": _t(0), "ticker": "E-A", "prob": 0.50, "live": True},
        {"time": _t(100), "ticker": "E-A", "prob": 0.60, "live": True},
        {"time": _t(200), "ticker": "E-A", "prob": 0.70, "live": False},   # shadow: no theo
        {"time": _t(400), "ticker": "E-A", "prob": 0.70, "live": True},
    ]
    trades = [
        {"time": _t(10), "ticker": "E-A", "trade_id": "P1", "yes_price": 0.45, "size": 10.0},   # 5c x 10
        {"time": _t(110), "ticker": "E-A", "trade_id": "P2", "yes_price": 0.64, "size": 5.0},   # 4c x 5
        {"time": _t(210), "ticker": "E-A", "trade_id": "P3", "yes_price": 0.10, "size": 99.0},  # shadow
    ]
    fills = [  # our buy YES 4 @ .45 (P1): +5c x 4; our buy NO 5 @ .36 (P2, yes .64): +4c x 5
        {"time": _t(10), "ticker": "E-A", "side": "yes", "price": 0.45, "size": 4.0, "fee": 0.0,
         "liquidity": "maker", "role": "edge", "mode": "real", "seeded": False,
         "exchange_order_id": "X1", "trade_id": "P1", "order_id": 1},
        {"time": _t(110), "ticker": "E-A", "side": "no", "price": 0.36, "size": 5.0, "fee": 0.0,
         "liquidity": "taker", "role": "edge", "mode": "real", "seeded": False,
         "exchange_order_id": "X2", "trade_id": "P2", "order_id": 2},
    ]
    bbo = [{"time": _t(s), "ticker": "E-A", "bid": 0.44, "ask": 0.46, "bid_depth": 1.0, "ask_depth": 1.0}
           for s in (0, 500)]
    _journal(tmp_path, dec, fills, trades, bbo)
    p = perf.Perf("S")
    df = p.refresh()
    assert list(df.theo.round(2)) == [0.50, 0.60]
    pr = p.prints_frame()
    assert list(pr.trade_id) == ["P1", "P2"]   # P3 (shadow, no theo) isn't kept

    E = perf.summarize_edge(df, pr)
    assert E["prints"] == 2
    assert math.isclose(E["market_usd"], 0.50 + 0.20)
    assert math.isclose(E["ours_usd"], 0.20 + 0.20)
    assert math.isclose(E["share"], 0.40 / 0.70)
    assert math.isclose(E["vol_share"], 9 / 15)
    assert math.isclose(E["maker_usd"], 0.20) and math.isclose(E["taker_usd"], 0.20)


def test_edge_share_negative_when_we_buy_above_theo():
    df = pd.DataFrame({"time": [T0], "ticker": ["E-A"], "dir": [1.0], "yes_px": [0.55], "theo": [0.50],
                       "size": [2.0], "liquidity": ["taker"]})
    pr = pd.DataFrame({"time": [T0], "ticker": ["E-A"], "trade_id": ["P"], "yes_price": [0.55],
                       "size": [2.0], "theo": [0.50]})
    E = perf.summarize_edge(df, pr)
    assert math.isclose(E["share"], -1.0)


def test_edge_share_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    E = perf.summarize_edge(pd.DataFrame(), perf.Perf("none").prints_frame())
    assert E["prints"] == 0 and math.isnan(E["share"])
