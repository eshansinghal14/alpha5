"""Chart streams exist for every market from the session start, decisions or not."""
import json
import math
import os

import pandas as pd

from kalshi_infra.runtime import persist

T0 = pd.Timestamp("2026-10-09 20:00:00", tz="UTC")


def _t(s):
    return T0 + pd.Timedelta(seconds=s)


def test_timeline_holds_touch_and_fair_before_any_decision():
    bbo = pd.DataFrame({"time": [_t(0), _t(2)], "ticker": "A", "bid": [0.40, 0.41], "ask": [0.45, 0.46]})
    fair = pd.DataFrame({"time": [_t(1), _t(3)], "ticker": "A", "prob": [0.43, float("nan")],
                         "prob_lo": [0.42, float("nan")], "prob_hi": [0.44, float("nan")]})
    tl = persist.market_timeline(pd.DataFrame(), bbo, fair)
    assert list(tl["market_bid"]) == [0.40, 0.40, 0.41, 0.41]
    p = list(tl["prob"])
    assert math.isnan(p[0]) and p[1] == 0.43 and p[2] == 0.43
    assert math.isnan(p[3])        # a withheld fair is a gap, not the last value
    assert tl["bid_reservation"].isna().all()


def test_decision_values_hold_and_takes_stay_point_events():
    q = pd.DataFrame({"time": [_t(1)], "ticker": "A", "market_bid": 0.40, "market_ask": 0.45,
                      "prob": 0.43, "prob_raw": 0.43, "prob_lo": 0.42, "prob_hi": 0.44,
                      "bid_reservation": 0.41, "ask_reservation": 0.46, "quote_bid": 0.41,
                      "bid_final": 0.45, "ask_final": None})
    bbo = pd.DataFrame({"time": [_t(2)], "ticker": "A", "bid": 0.39, "ask": 0.44})
    tl = persist.market_timeline(q, bbo, pd.DataFrame())
    assert list(tl["bid_reservation"]) == [0.41, 0.41]
    assert tl["bid_final"].iloc[0] == 0.45 and math.isnan(tl["bid_final"].iloc[1])
    assert list(tl["market_bid"]) == [0.40, 0.39]


def test_every_event_market_gets_a_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(persist, "stream_dir", lambda s: str(tmp_path))
    bbo = pd.DataFrame({"time": [_t(0)], "ticker": "B", "bid": 0.2, "ask": 0.3})
    persist.write_streams("S", pd.DataFrame(), persist.fills_frame([]), {}, bbo_df=bbo,
                          tickers=["A", "B"])
    assert sorted(os.listdir(tmp_path)) == ["A.json", "B.json"]
    b = json.load(open(tmp_path / "B.json"))["market"]
    assert b["bid"] == [0.2] and b["prob"] == [None]
