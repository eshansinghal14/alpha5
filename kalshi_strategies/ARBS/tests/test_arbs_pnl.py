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
    assert list(df.capital.round(4)) == [2.0, 4.5, 0.0, 0.6, 0.0, 0.0]


def test_capital_nets_a_markets_yes_and_no_held_in_different_baskets():
    info = {"x": (1.0, 2), "y": (1.0, 2)}
    rows = [fill(1, "x", "A", "yes", 3, 1.8), fill(2, "y", "A", "no", 2, 1.0)]
    # Kalshi pays the 2 YES/NO pairs out at once: $2.80 spent, $2 back.
    assert replay(rows, info).capital.iloc[-1] == pytest.approx(0.8)


def test_bundle_log_one_row_per_arb_newest_first(tmp_path, monkeypatch):
    import json
    from kalshi_viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    rows = [fill(100, "b", "S-A", "yes", 5, 2.0) | {"fill_kind": "quote"},
            fill(100.1, "b", "S-B", "yes", 5, 2.5) | {"fill_kind": "complete"},
            fill(105, "b", "S-A", "yes", 3, 1.2) | {"fill_kind": "quote"},      # seconds later: its own arb
            fill(105.1, "b", "S-B", "yes", 3, 1.5) | {"fill_kind": "complete"},
            fill(500, "b", "S-A", "yes", 2, 0.8) | {"fill_kind": "take"},       # one leg, then unwound much later
            fill(900, "b", "S-A", "no", 2, 1.1) | {"fill_kind": "unwind"}]
    (j / "2026-10-29.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = arbs_pnl.bundle_log("ARBS")
    assert list(log.how) == ["take+unwind", "complete+quote", "complete+quote"]
    assert pd.isna(log.bundles.iloc[0]) and log.pnl.iloc[0] == pytest.approx(2 - 1.9)
    assert log.bundles.iloc[1] == 3 and log.pnl.iloc[1] == pytest.approx(0.3)
    assert log.bundles.iloc[2] == 5 and log.pnl.iloc[2] == pytest.approx(0.5)


def test_bundle_log_leaves_an_unfinished_arb_open(tmp_path, monkeypatch):
    import json
    from kalshi_viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    (j / "2026-10-29.jsonl").write_text(json.dumps(fill(1, "b", "S-A", "yes", 3, 1.2) | {"fill_kind": "take"}) + "\n")
    log = arbs_pnl.bundle_log("ARBS")
    assert len(log) == 1 and pd.isna(log.bundles.iloc[0]) and pd.isna(log.pnl.iloc[0])


def test_bundle_log_counts_bundles_and_unwound_pairs_in_one_arb(tmp_path, monkeypatch):
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


def test_capital_is_the_shards_positions_from_balance_rows(tmp_path, monkeypatch):
    import json
    from kalshi_viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    rows = [fill(100, "b", "S-A", "yes", 5, 2.0),
            {"t": 150, "kind": "balance", "cash": {"0": 900.0}, "positions": {"0": 40.0, "2": 2.0}},
            fill(200, "b", "S-B", "yes", 5, 2.5),
            {"t": 250, "kind": "balance", "cash": {"0": 895.0}, "positions": {"0": 45.0}}]
    (j / "2026-11-11.jsonl").write_text("".join(json.dumps(r) + chr(10) for r in rows))
    df = arbs_pnl.load("ARBS")
    assert list(df.time.astype("int64") // 10**9) == [100, 150, 200, 250]
    assert pd.isna(df.capital.iloc[0]) and list(df.capital.iloc[1:]) == [42.0, 42.0, 45.0]
    assert df.total.iloc[-1] == pytest.approx(df.total.iloc[2])


def test_dashboard_merges_both_arbs_processes():
    from kalshi_viz import arbs_page as ap
    a = {"live_trading": True, "pnl": {"settled": 10.0, "locked": 2.0, "cash": 900.0, "cash_room": 800.0,
                                       "positions_value": 500.0}, "counts": {"quote_fills": 3},
         "quotes": [{"basket_id": "x"}], "feed": {"up": True}, "n_baskets": 10, "blocked_reason": None}
    b = {"live_trading": False, "pnl": {"settled": -1.0, "locked": 0.5, "cash": 300.0, "cash_room": 200.0,
                                        "positions_value": 500.0}, "counts": {"quote_fills": 1},
         "quotes": [{"basket_id": "y"}], "feed": {"up": False}, "n_baskets": 4, "blocked_reason": "feed_down"}
    m = ap.merge({"ARBS": a, "ARBS3": b})
    assert m["pnl"]["settled"] == 9.0 and m["pnl"]["cash"] == 1200.0
    assert m["pnl"]["positions_value"] == 500.0                    # the account's, not summed
    assert m["counts"]["quote_fills"] == 4 and m["n_baskets"] == 14
    assert [q["shard"] for q in m["quotes"]] == ["shard 0", "shard 3"]
    assert m["blocked_reason"] == "shard 3: feed_down" and not m["feed"]["up"]


def test_dashboard_pnl_history_adds_the_processes():
    from kalshi_viz import arbs_page as ap
    t = lambda s: pd.Timestamp(s, tz="UTC")
    a = pd.DataFrame({"time": [t("2026-11-11 10:00"), t("2026-11-11 12:00")], "settled": [1.0, 2.0],
                      "total": [1.5, 2.5], "capital": [100.0, 110.0]})
    b = pd.DataFrame({"time": [t("2026-11-11 11:00")], "settled": [5.0], "total": [6.0], "capital": [float("nan")]})
    c = ap.combine([a, b])
    assert list(c.settled) == [1.0, 6.0, 7.0] and list(c.total) == [1.5, 7.5, 8.5]
    assert list(c.capital) == [100.0, 100.0, 110.0]
