"""PnL replay from journal rows."""
import pandas as pd
import pytest

from viz.arbs_pnl import replay


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
    from viz import arbs_pnl
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
    from viz import arbs_pnl
    monkeypatch.setattr(arbs_pnl, "LIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(arbs_pnl, "basket_info", lambda root: {"b": (1.0, 2)})
    j = tmp_path / "ARBS" / "journal"
    j.mkdir(parents=True)
    (j / "2026-10-29.jsonl").write_text(json.dumps(fill(1, "b", "S-A", "yes", 3, 1.2) | {"fill_kind": "take"}) + "\n")
    log = arbs_pnl.bundle_log("ARBS")
    assert len(log) == 1 and pd.isna(log.bundles.iloc[0]) and pd.isna(log.pnl.iloc[0])


def test_bundle_log_counts_bundles_and_unwound_pairs_in_one_arb(tmp_path, monkeypatch):
    import json
    from viz import arbs_pnl
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
    from viz import arbs_pnl
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


def test_dashboard_merges_the_arbs_processes():
    from viz import arbs_page as ap
    a = {"live_trading": True, "ledger": "real",
         "pnl": {"settled": 10.0, "locked": 2.0, "cash": 900.0, "cash_room": 800.0, "positions_value": 500.0,
                 "cash_by_shard": {"0": 900.0}, "cash_room_by_shard": {"0": 800.0}},
         "counts": {"quote_fills": 3}, "quotes": [{"basket_id": "x"}, {"basket_id": "x"}], "feed": {"up": True},
         "n_baskets": 10, "blocked_reason": None}
    b = {"live_trading": True, "ledger": "real",
         "pnl": {"settled": -1.0, "locked": 0.5, "cash": 300.0, "cash_room": 200.0, "positions_value": 500.0,
                 "cash_by_shard": {"3": 300.0}, "cash_room_by_shard": {"3": 200.0}},
         "counts": {"quote_fills": 1}, "quotes": [{"basket_id": "y"}], "feed": {"up": False}, "n_baskets": 4,
         "blocked_reason": "feed_down"}
    # Polymarket in shadow: its PnL is simulated, and it reads shard 0's balance too.
    c = {"live_trading": False, "ledger": "shadow",
         "pnl": {"settled": 5.0, "cash_by_shard": {"0": 905.0, "100": 50.0},
                 "cash_room_by_shard": {"0": 805.0, "100": 40.0}},
         "quotes": [{"basket_id": "z"}], "feed": {"up": True}, "n_baskets": 3}
    m = ap.merge({"ARBS": a, "ARBS3": b, "ARBSPM": c})
    assert m["pnl"]["settled"] == 9.0 and m["pnl"]["cash"] == 1200.0  # the shadow process left out
    assert m["pnl"]["positions_value"] == 500.0                    # the account's, not summed
    assert m["counts"]["quote_fills"] == 4 and m["n_baskets"] == 17
    assert [q["shard"] for q in m["quotes"]] == ["shard 0 (a)", "shard 0 (a)", "shard 3", "Polymarket"]
    assert m["blocked_reason"] == "shard 3: feed_down" and not m["feed"]["up"]
    sh = m["by_shard"]
    assert list(sh) == ["0", "3", "100"] and sh["0"]["room"] == 800.0 and sh["0"]["real"]   # shard 0 once
    assert not sh["100"]["real"] and sh["100"]["label"] == "PM"
    pr = m["by_process"]
    assert pr["ARBS"]["quoting"] == 1 and pr["ARBS"]["pnl"] == 12.0 and not pr["ARBSPM"]["real"]
    # The listed parts add up to the headline; a shadow process follows apart.
    assert ap._split(list(pr.values()), lambda r: ap._usd0(r["pnl"])) ==         "$+12 (K0a), $0 (K3) · not in total: $+5 (PM shadow)"
    assert ap._split([pr["ARBSPM"]], lambda r: ap._usd0(r["pnl"])) == "$+5 (PM shadow)"
    # Baskets tile: one quoting / watching pair per process, by shard.
    g = ap._grouped([{"name": "ARBS", "label": "K0", "real": True, "quoting": 900, "watching": 9500},
                     {"name": "ARBS0B", "label": "K0b", "real": True, "quoting": 1000, "watching": 5600},
                     {"name": "ARBS3", "label": "K3", "real": True, "quoting": 50, "watching": 800}])
    assert [ap._basket_line(x) for x in g] == ["900 / 9.5k, 1,000 / 5.6k (K0)", "50 / 800 (K3)"]
    # Total PnL's subtext: one entry per venue, the Kalshi shards summed.
    assert ap.by_venue(list(pr.values())) == [{"label": "K", "real": True, "pnl": 11.5},
                                              {"label": "PM", "real": False, "pnl": 5.0}]


def test_phone_groups_arbs_by_market_biggest_pnl_first():
    from viz import arbs_page as ap
    t = pd.Timestamp("2026-11-22T01:00Z")
    rows = ([{"basket_id": "complete_set:EV-A:buy_all_yes", "time": t, "cost": 90.0, "pnl": 5.0}]
            + [{"basket_id": f"mecnet:EV-B:no:X+Y{i}", "time": t + pd.Timedelta(minutes=i), "cost": 2.0, "pnl": 0.6}
               for i in range(10)]
            + [{"basket_id": "ladder:EV-C:T2=>T1", "time": t, "cost": 4.0, "pnl": -2.0},
               {"basket_id": "ladder:EV-D:T2=>T1", "time": t, "cost": 3.0, "pnl": None}])
    m = ap.by_market(pd.DataFrame(rows))
    assert list(m.market) == ["EV-B", "EV-A", "EV-C", "EV-D"]      # ten small arbs adding to $6 lead
    assert m.arbs.tolist() == [10, 1, 1, 1] and m.pnl.tolist() == [6.0, 5.0, -2.0, 0.0]
    assert m.time.iloc[0] == t + pd.Timedelta(minutes=9)


def test_dashboard_pnl_history_adds_the_processes():
    from viz import arbs_page as ap
    t = lambda s: pd.Timestamp(s, tz="UTC")
    a = pd.DataFrame({"time": [t("2026-11-11 10:00"), t("2026-11-11 12:00")], "settled": [1.0, 2.0],
                      "total": [1.5, 2.5], "capital": [100.0, 110.0]})
    b = pd.DataFrame({"time": [t("2026-11-11 11:00")], "settled": [5.0], "total": [6.0], "capital": [float("nan")]})
    c = ap.combine([a, b])
    assert list(c.settled) == [1.0, 6.0, 7.0] and list(c.total) == [1.5, 7.5, 8.5]
    assert list(c.capital) == [100.0, 100.0, 110.0]


def test_journal_rows_reads_only_new_lines_and_waits_for_a_partial_one(tmp_path):
    import os
    from viz.arbs_pnl import journal_rows
    p = tmp_path / "2026-11-20.jsonl"
    p.write_text('{"kind": "fill", "t": 1}\n{"kind": "quote", "t": 2}\n{"kind": "settled", "t": 3')
    assert [r["t"] for r in journal_rows(str(p))] == [1]
    with open(p, "a") as fh:
        fh.write(', "pnl": 1}\n{"kind": "balance", "t": 4}\n')
    assert [r["t"] for r in journal_rows(str(p))] == [1, 3, 4]
    # Replaced in place (the archive's slimming): read again from the start.
    q = tmp_path / "new.jsonl"
    q.write_text('{"kind": "fill", "t": 9}\n')
    os.replace(q, p)
    assert [r["t"] for r in journal_rows(str(p))] == [9]
