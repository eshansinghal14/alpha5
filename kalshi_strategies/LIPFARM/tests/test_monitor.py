import pytest

from kalshi_strategies.LIPFARM import monitor


def _cur(**kw):
    c = {"mode": "LIVE", "pnl": 0.0, "lip": 0.0, "exposure": 0.0, "arbs_bad": "", "new_tb": 0, "write_tps": 3.0,
         "write_cap": 30.0, "worst_market": 0.0, "worst_name": "", "journal_gap": ""}
    c.update(kw)
    return c


def test_one_bad_market_alerts():
    alerts, active = monitor.decide(_cur(worst_market=-138.0, worst_name="TJAC"), {})
    assert not active and "TJAC" in alerts[0]


def test_loss_is_net_of_lip_and_alerts_once():
    alerts, active = monitor.decide(_cur(pnl=-80.0, lip=50.0), {})
    assert not alerts and not active                                  # net -30
    alerts, active = monitor.decide(_cur(pnl=-80.0), {})
    assert "net P&L" in alerts[0] and "loss" in active
    alerts, active = monitor.decide(_cur(pnl=-500.0), active)         # still holding: no repeat
    assert not alerts and "loss" in active


def test_write_runaway_and_shadow():
    alerts, active = monitor.decide(_cur(write_tps=72.8), {})
    assert "writes" in active and alerts
    assert monitor.decide(_cur(mode="shadow", pnl=-500.0), {"loss": 1.0}) == ([], {})


def test_monitor_never_disarms():
    import inspect
    assert "request_trading_mode" not in inspect.getsource(monitor)


def test_marks_and_pairs():
    m = {"yes_bid_dollars": "0.30", "no_bid_dollars": "0.60"}
    assert monitor.mark(m, "yes", "bid") == 0.30 and abs(monitor.mark(m, "yes", "mid") - 0.35) < 1e-9
    assert monitor.mark({"result": "no", "yes_bid_dollars": "0"}, "no", "bid") == 1.0
    fills = [{"ticker": "T", "side": "yes", "price": 0.33, "size": 295.0, "fee": 0.0},
             {"ticker": "T", "side": "no", "price": 0.7034, "size": 295.0, "fee": 4.307}]
    assert abs(monitor.marked_pnl(fills, lambda t, s: 0.0) - (-14.16)) < 0.01      # pair: marks don't matter


def test_kalshi_fills_are_cached_and_read_from_the_newest_held(monkeypatch):
    calls = []

    def get(path, params=None, auth=False):
        calls.append(params["min_ts"])
        if params.get("cursor"):
            return {"fills": [], "cursor": None}
        fs = [{"order_id": "o1", "outcome_side": "yes", "yes_price_dollars": "0.40", "count_fp": "10",
               "trade_id": "t1", "ticker": "A-1", "created_time": "2026-11-08T00:00:00Z"}]
        if params["min_ts"] > 1794050000:
            fs.append({"order_id": "o1", "outcome_side": "yes", "yes_price_dollars": "0.40", "count_fp": "5",
                       "trade_id": "t2", "ticker": "A-1", "created_time": "2026-11-08T01:00:00Z"})
        return {"fills": fs, "cursor": "c"}
    monkeypatch.setattr(monitor, "kalshi_get", get)
    mon = {"started": 1794000000.0}
    assert [f["size"] for f in monitor.kalshi_fills_cached(mon, {"o1"})] == [10.0]
    out = monitor.kalshi_fills_cached(mon, {"o1"})
    assert sorted(f["size"] for f in out) == [5.0, 10.0]
    assert calls[-2] == 1794096000 - 600                     # 2026-11-08T00:00Z less 10 min


def test_kalshi_fills_raise_instead_of_returning_part(monkeypatch):
    monkeypatch.setattr(monitor, "kalshi_get", lambda path, params=None, auth=False: {
        "fills": [{"order_id": "x"}], "cursor": "more"})
    with pytest.raises(RuntimeError):
        monitor.kalshi_fills(0.0, {"o1"}, max_pages=3)
