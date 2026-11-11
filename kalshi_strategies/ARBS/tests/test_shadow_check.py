import gzip
import json

import pytest

from kalshi_strategies.ARBS import shadow_check as sc


def _print_row(t, b_no_after):
    """X-A YES printed at 0.40 against our 0.45 quote; basket buys YES A + YES B for $1.
    B's YES ask is 0.50 just before the print, then 1 - b_no_after."""
    books = {"X-A": {"yes": [[0.40, 100]], "no": [[0.55, 100]]},
             "X-B": {"yes": [[0.45, 100]], "no": [[0.50, 100]]}}
    after = {"X-A": books["X-A"], "X-B": {"yes": [[0.45, 100]], "no": [[b_no_after, 100]]}}
    return {"t": t, "ticker": "X-A", "side": "yes", "prints": [[0.0, 0.40, 50]],
            "baskets": [["complete_set:X:buy_all_yes", 1.0, True]], "books": books,
            "after": {"0.03": after, "0.1": after, "1.0": after},
            "pre": {"X-A": [[0.02, 0.45, 100, 0.55, 100]], "X-B": [[0.02, 0.45, 100, 0.50, 100]]}, "tte_h": {"complete_set:X:buy_all_yes": 3.0},
            "quote": {"basket_id": "complete_set:X:buy_all_yes", "price": 0.45, "left": 10, "edge": 0.05,
                      "tripped": False}, "pull": {"ratio": 1.0}}


def test_daily_check_compares_the_replay_with_a_real_fill(tmp_path):
    day = "2026-11-12"
    (tmp_path / "journal").mkdir()
    (tmp_path / "prints").mkdir()
    rows = [{"t": 999.0, "kind": "live_params", "events": {"X": 0.1}},
            {"t": 1001.0, "kind": "size_shadow", "basket_id": "complete_set:X:buy_all_yes", "group": "S:Test",
             "lots": 10.0, "pnl": -0.3, "t0": 1000.05, "entry": ["quote", "X-A", "yes"]}]
    (tmp_path / "journal" / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with gzip.open(tmp_path / "prints" / f"{day}.jsonl.gz", "wt") as fh:
        fh.write(json.dumps(_print_row(1000.0, 0.47)) + "\n")       # B's ask 0.50 -> 0.53 after the print
    doc = sc.run(day, str(tmp_path), groups={"X": "S:Test"})
    assert doc["coverage"] == {"arbs": 1, "matched": 1}
    c = doc["calibration"][0]
    # Replayed at its 5c edge on B's 0.50 (+ taker fee): quote 0.43; B then bought at 0.53 + fee.
    assert c["group"] == "S:Test" and c["real_c"] == pytest.approx(-3.0)
    assert c["shadow_c"] == pytest.approx(100 * (1 - 0.43 - 0.53 - 0.07 * 0.53 * 0.47), abs=0.01)
    p = doc["policy"][0]
    assert p["prints"] == 1 and p["shadow_prints"] == 1
    assert p["shadow_usd"] == pytest.approx(p["edges"][sc.EDGES.index(0.1)])
    m = doc["markout"][0]
    assert (m["tte"], m["live"], m["shape"]) == ("<6h", True, "complete_set")
    assert m["edge_c"] == pytest.approx(10.0) and m["move_01_c"] == pytest.approx(3.0)


def test_rules_train_on_earlier_days_only(tmp_path):
    import os
    (tmp_path / "journal").mkdir()
    (tmp_path / "prints").mkdir()
    (tmp_path / sc.OUT_DIR).mkdir()
    for day in ("2026-11-12", "2026-11-13"):
        (tmp_path / "journal" / f"{day}.jsonl").write_text("")
        with gzip.open(tmp_path / "prints" / f"{day}.jsonl.gz", "wt") as fh:
            fh.write(json.dumps(_print_row(1000.0, 0.47)) + chr(10))
        doc = sc.run(day, str(tmp_path), groups={"X": "S:Test"})
        json.dump(doc, open(os.path.join(tmp_path, sc.OUT_DIR, f"{day}.json"), "w"))
        assert doc["exit_stats"]["n"] == 1
    ru = doc["rules"]
    assert [t.split(":")[-1] for t in ru["trained_on"]] == ["2026-11-12"] and ru["prints"] == 1
    assert ru["best_ev"] is not None and ru["narrowest"] is not None and ru["best_ev_size"] is not None
    assert set(ru["fixed_by_size"]) == {"5", "20", "50", "100"}
