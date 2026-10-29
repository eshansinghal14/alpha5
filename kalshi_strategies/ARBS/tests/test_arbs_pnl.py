"""PnL replay from journal rows."""
import pandas as pd
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


def test_bundle_log_groups_bursts_newest_first(tmp_path, monkeypatch):
    import json
    from kalshi_viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    rows = [fill(100, "b", "S-A", "yes", 5, 2.0) | {"fill_kind": "quote"},
            fill(101, "b", "S-B", "yes", 5, 2.5) | {"fill_kind": "complete"},
            fill(500, "b", "S-A", "yes", 3, 1.2) | {"fill_kind": "take"}]       # a later burst, one leg so far
    (j / "2026-10-29.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = arbs_pnl.bundle_log("ARBS")
    assert list(log.how) == ["take", "complete+quote"]
    assert log.bundles.iloc[1] == 5 and log.locked.iloc[1] == pytest.approx(0.5)
    assert pd.isna(log.bundles.iloc[0])
