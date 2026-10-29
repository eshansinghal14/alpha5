import json

from kalshi_strategies.ARBS.config import SPEC_PATH
from kalshi_strategies.ARBS.ledger import Ledger
from kalshi_strategies.ARBS.spec import load_spec


def test_spec_loads_and_inexact_baskets_use_worst_payout():
    bs = {b.basket_id: b for b in load_spec(SPEC_PATH)}
    assert len(bs) > 100
    assert bs["complete_set:GOVPARTYFL-26:buy_all_yes"].payout == 0.0
    assert bs["complete_set:KXGENERALMOV-SENATETX-26:buy_all_no"].payout == 8.0
    assert bs["complete_set:SENATETX-26:buy_all_yes"].payout == 1.0


def test_exclude_by_cluster(tmp_path):
    n_all = len(load_spec(SPEC_PATH))
    n = len(load_spec(SPEC_PATH, exclude=["tx_county"]))
    assert 0 < n < n_all


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
