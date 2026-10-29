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
            fill(500, "b", "S-A", "yes", 3, 1.2) | {"fill_kind": "take"},       # a later burst, one leg so far
            fill(900, "b", "S-B", "yes", 2, 0.9) | {"fill_kind": "take"},
            fill(900.1, "b", "S-B", "no", 2, 1.2) | {"fill_kind": "unwind"}]
    (j / "2026-10-29.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = arbs_pnl.bundle_log("ARBS")
    assert list(log.how) == ["take+unwind", "take", "complete+quote"]
    assert pd.isna(log.bundles.iloc[0]) and log.pnl.iloc[0] == pytest.approx(2 - 2.1)
    assert pd.isna(log.bundles.iloc[1]) and pd.isna(log.pnl.iloc[1])
    assert log.bundles.iloc[2] == 5 and log.pnl.iloc[2] == pytest.approx(0.5)


def test_bundle_log_counts_bundles_and_unwound_pairs_in_one_burst(tmp_path, monkeypatch):
    import json
    from kalshi_viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    rows = [fill(1, "b", "S-K", "no", 1, 0.70) | {"fill_kind": "quote"},       # a bundle: K no + C no
            fill(2, "b", "S-C", "no", 2, 0.53) | {"fill_kind": "complete"},
            fill(3, "b", "S-C", "yes", 1, 0.71) | {"fill_kind": "unwind"}]     # one C no netted out
    (j / "2026-10-29.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = arbs_pnl.bundle_log("ARBS")
    assert log.bundles.iloc[0] == 1 and log.pnl.iloc[0] == pytest.approx(2 - 1.94)
