import json

from strategies.ARBS import dormant as D


def _journal(root, day, rows):
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "journal" / f"{day}.jsonl").write_text("".join(rows))


def test_day_stats_counts_write_tokens_and_finished_arbs(tmp_path):
    _journal(tmp_path, "2026-11-20", [
        json.dumps({"t": 1.0, "kind": "quote", "ticker": "KXA-1-X", "price": 0.4}) + "\n",
        json.dumps({"t": 2.0, "kind": "move", "ticker": "KXA-1-X", "from": 0.4, "to": 0.41}) + "\n",
        json.dumps({"t": 3.0, "kind": "cancel", "ticker": "KXA-1-X"}) + "\n",
        json.dumps({"t": 4.0, "kind": "quote", "ticker": "KXB-1-Y"}, separators=(",", ":")) + "\n",
        json.dumps({"t": 5.0, "kind": "size_shadow", "series": "KXB", "pnl": 1.5}) + "\n",
        json.dumps({"t": 6.0, "kind": "fill", "ticker": "KXB-1-Y"}) + "\n",
    ])
    assert D.day_stats(str(tmp_path), "2026-11-20") == {"KXA": [22, 0, 0.0], "KXB": [10, 1, 1.5]}
    assert D.day_stats(str(tmp_path), "2026-11-21") is None


def test_list_takes_costly_series_without_arbs_and_keeps_them_until_an_arb_or_max_days():
    days = {"2026-11-19": [{"A": [60_000, 0, 0.0], "B": [200_000, 1, 0.5], "C": [50_000, 0, 0.0]}],
            "2026-11-20": [{"A": [50_000, 0, 0.0]}, {"C": [10_000, 0, 0.0]}]}
    doc = D.build(days, "2026-11-21", None)
    assert set(doc["series"]) == {"A"}                     # B finished an arb, C spent too little
    # Asleep, A spends little: still listed, from when it was first listed.
    quiet = {"2026-11-21": [{"A": [1_000, 0, 0.0]}]}
    doc2 = D.build(quiet, "2026-11-22", doc)
    assert doc2["series"]["A"]["since"] == "2026-11-21"
    assert "A" not in D.build({"2026-11-22": [{"A": [500, 1, 0.2]}]}, "2026-11-23", doc2)["series"]
    assert "A" not in D.build(quiet, "2026-11-28", doc2)["series"]   # MAX_DAYS: has to qualify again


def test_write_list_builds_missing_days_from_journals(tmp_path):
    for s in D.ROOTS:
        _journal(tmp_path / s, "2026-11-20",
                 [json.dumps({"t": 1.0, "kind": "move", "ticker": "KXA-1-X"}) + "\n"] * 4000)
    doc = D.write_list(str(tmp_path), "2026-11-21")
    assert doc["days"] == ["2026-11-20"] and doc["series"]["KXA"]["tokens"] == 120_000
    assert json.loads((tmp_path / "ARBS" / D.LIST_FILE).read_text())["series"] == doc["series"]
    assert (tmp_path / "ARBS" / "series_tokens" / "2026-11-20.json").exists()
