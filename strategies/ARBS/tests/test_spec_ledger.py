import json

from strategies.ARBS.ledger import Ledger
from strategies.ARBS.spec import load_spec

SPEC = {"relationships": [
    {"relationship_id": "complete_set:E", "type": "complete_set", "cluster": "c1", "baskets": [
        {"basket_id": "complete_set:E:buy_all_yes", "payout": 1, "exact": False, "payout_in_tie_or_rounding_case": 0,
         "legs": [{"ticker": "E-A", "side": "yes"}, {"ticker": "E-B", "side": "yes"}]},
        {"basket_id": "complete_set:E:buy_all_no", "payout": 1,
         "legs": [{"ticker": "E-A", "side": "no"}, {"ticker": "E-B", "side": "no"}]}]},
    {"relationship_id": "mecnet:F", "type": "mutually_exclusive_no", "cluster": "c2", "baskets": [
        {"basket_id": "mecnet:F:no:A+B", "payout": 1,
         "legs": [{"ticker": "F-A", "side": "no"}, {"ticker": "F-B", "side": "no"}]}]}]}


def test_spec_loads_and_inexact_baskets_use_worst_payout(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(SPEC))
    bs = {b.basket_id: b for b in load_spec(str(p))}
    assert bs["complete_set:E:buy_all_yes"].payout == 0.0 and not bs["complete_set:E:buy_all_yes"].exact
    assert bs["complete_set:E:buy_all_no"].payout == 1.0 and bs["complete_set:E:buy_all_no"].exact
    assert [b.basket_id for b in load_spec(str(p), exclude=["c1"])] == ["mecnet:F:no:A+B"]


def test_ledger_roundtrip_bundles_and_broken(tmp_path):
    p = str(tmp_path / "l.json")
    led = Ledger(p)
    tk = ["A", "B"]
    led.record("b1", tk, "A", "yes", 5, 2.0, now=100.0)
    pos = led.baskets["b1"]
    assert pos.bundles(tk) == 0 and pos.unhedged(tk) == {"A": 5} and pos.broken_since == 100.0
    led.record("b1", tk, "B", "no", 5, 2.5, now=101.0)
    led2 = Ledger(p)
    pos = led2.baskets["b1"]
    assert pos.bundles(tk) == 5 and pos.broken_since is None and pos.cost() == 4.5
    assert led2.net_by_ticker() == {"A": 5, "B": -5}


def test_quote_ids_saved_apart_and_pruned_after_an_hour(tmp_path):
    led = Ledger(str(tmp_path / "l.json"))
    led.quote_ids = {"x": {"retired_at": 0.0}, "y": {"retired_at": None}, "z": {"retired_at": 4000.0}}
    led.save_quote_ids(now=5000.0)
    led.save()
    assert set(json.load(open(tmp_path / "l_quote_ids.json"))) == {"y", "z"}
    assert "quote_ids" not in json.load(open(tmp_path / "l.json"))
    assert set(Ledger(str(tmp_path / "l.json")).quote_ids) == {"y", "z"}


def test_quote_ids_read_from_an_old_ledger(tmp_path):
    (tmp_path / "l.json").write_text(json.dumps({"baskets": {}, "quote_ids": {"o": {"retired_at": None}}}))
    assert set(Ledger(str(tmp_path / "l.json")).quote_ids) == {"o"}


def test_quote_id_changes_logged_and_replayed_until_the_next_full_write(tmp_path):
    led = Ledger(str(tmp_path / "l.json"))
    led.quote_ids = {"a": {"basket_id": "b", "retired_at": None}}
    led.save()
    led.save_quote_ids(now=100.0)
    led.quote_ids["n"] = {"basket_id": "b", "retired_at": None}
    led.note_quote_id("n")
    led.quote_ids["a"]["retired_at"] = 150.0
    led.note_quote_id("a")
    led.flush_quote_ids()
    with open(tmp_path / "l_quote_ids.log.jsonl", "a") as fh:
        fh.write('["cut", {"bas')                              # a crash mid-line
    again = Ledger(str(tmp_path / "l.json"))
    assert set(again.quote_ids) == {"a", "n"} and again.quote_ids["a"]["retired_at"] == 150.0
    again.save_quote_ids(now=200.0)
    assert (tmp_path / "l_quote_ids.log.jsonl").read_text() == ""
    assert set(Ledger(str(tmp_path / "l.json")).quote_ids) == {"a", "n"}
