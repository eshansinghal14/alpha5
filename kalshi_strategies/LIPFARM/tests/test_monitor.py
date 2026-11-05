from kalshi_strategies.LIPFARM import monitor


def _cur(**kw):
    c = {"mode": "LIVE", "pnl": 0.0, "exposure": 0.0, "arbs_bad": "", "new_tb": 0, "write_tps": 3.0,
         "write_cap": 30.0, "worst_market": 0.0, "worst_name": "", "journal_gap": ""}
    c.update(kw)
    return c


def test_one_bad_market_only_alerts():
    stop, alerts, pending = monitor.decide(_cur(worst_market=-138.0, worst_name="TJAC"), {})
    assert not stop and not pending and "TJAC" in alerts[0]


def test_a_rule_disarms_only_when_it_holds_two_runs():
    stop, _, pending = monitor.decide(_cur(pnl=-80.0), {})
    assert not stop and "loss" in pending
    stop, _, _ = monitor.decide(_cur(pnl=-80.0), pending)
    assert stop and "P&L" in stop[0]
    stop, _, pending = monitor.decide(_cur(pnl=-10.0), pending)      # recovered: cleared
    assert not stop and not pending


def test_hard_loss_disarms_at_once():
    stop, _, _ = monitor.decide(_cur(pnl=-250.0), {})
    assert len(stop) == 1 and "at once" in stop[0]


def test_write_runaway_and_shadow():
    _, _, pending = monitor.decide(_cur(write_tps=72.8), {})
    assert "writes" in pending
    assert monitor.decide(_cur(mode="shadow", pnl=-500.0), {"loss": 1.0}) == ([], [], {})


def test_marks_and_pairs():
    m = {"yes_bid_dollars": "0.30", "no_bid_dollars": "0.60"}
    assert monitor.mark(m, "yes", "bid") == 0.30 and abs(monitor.mark(m, "yes", "mid") - 0.35) < 1e-9
    assert monitor.mark({"result": "no", "yes_bid_dollars": "0"}, "no", "bid") == 1.0
    fills = [{"ticker": "T", "side": "yes", "price": 0.33, "size": 295.0, "fee": 0.0},
             {"ticker": "T", "side": "no", "price": 0.7034, "size": 295.0, "fee": 4.307}]
    assert abs(monitor.marked_pnl(fills, lambda t, s: 0.0) - (-14.16)) < 0.01      # pair: marks don't matter
