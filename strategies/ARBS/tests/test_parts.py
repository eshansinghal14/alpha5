import json
import zlib

import pytest

from strategies.ARBS import parts

DAY = parts.REBALANCE_S


@pytest.fixture
def two(monkeypatch):
    monkeypatch.setattr(parts, "PROCS", ("ARBS", "ARBS0B"))


def fresh():
    return parts.load("/nonexistent/owners.json")


def test_a_new_event_goes_to_the_process_with_less_cost(two):
    st = fresh()
    st["owners"] = {"SER-1": 0, "SER-2": 1}
    st["rebalanced_at"] = 1000.0                       # not due
    spent = {"SER-1": 100.0, "SER-2": 10.0}            # SER-3 is priced at its series' mean, 110 / 3
    assert parts.update(st, ["SER-1", "SER-2", "SER-3"], 1000.0, spent) == []
    assert st["owners"]["SER-3"] == 1 and st["load"] == [100.0, 46.7]


def test_the_first_update_starts_from_the_hash_split_then_balances(two):
    evs = [f"S{i}-E" for i in range(40)]
    spent = {e: float(1 + i % 7) * (10 if i % 3 == 0 else 1) for i, e in enumerate(evs)}
    st = fresh()
    moves = parts.update(st, evs, 5000.0, spent)
    hashed = {e: zlib.crc32(e.encode()) % len(parts.PROCS) for e in evs}
    assert all(hashed[e] == f for e, f, _ in moves)    # moves start from the old split
    lo, hi = sorted(st["load"])
    assert hi - lo <= parts.GAP * (lo + hi) and len(moves) < len(evs) // 2


def test_rebalance_runs_once_a_day_and_moves_until_within_the_gap(two):
    evs = ["A-1", "A-2", "A-3", "B-1"]
    st = fresh()
    st["owners"] = {"A-1": 0, "A-2": 0, "A-3": 0, "B-1": 1}
    spent = {"A-1": 50.0, "A-2": 30.0, "A-3": 20.0, "B-1": 10.0}
    st["rebalanced_at"] = 0.0
    moves = parts.update(st, evs, DAY, spent)
    assert moves and sorted(st["load"]) == [50.0, 60.0]
    st["owners"]["A-1"] = 0                             # a fresh imbalance the same day: left alone
    assert parts.update(st, evs, DAY + 3600, spent) == []


def test_a_new_process_gets_its_share_at_once():
    evs = [f"S{i}-E" for i in range(30)]
    spent = {e: float(1 + i % 5) for i, e in enumerate(evs)}
    st = fresh()
    st["owners"] = {e: i % 2 for i, e in enumerate(evs)}   # a map built for two processes
    st["rebalanced_at"] = 1000.0                             # not due
    moves = parts.update(st, evs, 1000.0, spent)
    assert moves and all(to == 2 for _, _, to in moves) and st["procs"] == len(parts.PROCS)
    lo, hi = min(st["load"]), max(st["load"])
    assert hi - lo <= parts.GAP * sum(st["load"])
    assert parts.update(st, evs, 1001.0, spent) == []       # sized now: back to once a day


def test_an_event_long_unlisted_is_forgotten():
    st = fresh()
    st["rebalanced_at"] = 1e9
    parts.update(st, ["E-1", "E-2"], 0.0, {})
    parts.update(st, ["E-2"], parts.FORGET_S + 1, {})
    assert "E-1" not in st["owners"] and "E-2" in st["owners"]


def test_spend_is_kept_per_event_and_day_and_summed_over_both_processes(tmp_path):
    for p, t in zip(parts.PROCS, (3.0, 4.0)):
        (tmp_path / p).mkdir()
        es = parts.EventSpend(str(tmp_path / p))
        es.add("E-1", t, now=86400 * 2)
        es.add("E-1", 1.0, now=86400 * 3)
        es.add("", 5.0)                                 # no event: not counted
        es.write(es.snapshot())
    assert parts.spend(days=1, root=str(tmp_path)) == {"E-1": 2.0}
    assert parts.spend(days=2, root=str(tmp_path)) == {"E-1": 9.0}
    assert json.load(open(tmp_path / "ARBS" / parts.SPEND)) == {"1970-01-03": {"E-1": 3.0},
                                                                 "1970-01-04": {"E-1": 1.0}}


def test_event_of_reads_scanned_basket_ids():
    assert parts.event_of("mecnet:KXNFLGAME-26NOV15CARNO:no:CAR+NO") == "KXNFLGAME-26NOV15CARNO"
    assert parts.event_of("ladder:KXHIGHLAX-26NOV14:B71.5=>B69.5") == "KXHIGHLAX-26NOV14"
    assert parts.event_of("t:b") == ""
