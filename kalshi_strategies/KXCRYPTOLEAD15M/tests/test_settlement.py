"""SettlementTracker and CfIndex tests."""
import math
import time

from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import STALE_S, CfIndex, load_cf
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.settlement_tracker import EventWindow, SettlementTracker

S, T = 1_000_200, 1_001_100


def _cf(start=100.0, end=None, end_secs=60):
    """CF holding [S-60, S) at `start` and the first `end_secs` end seconds at `end`."""
    cf = CfIndex(persist=False)
    end = end or {}
    for c in COINS:
        for t in range(S - 60, S):
            cf.add(c, t, start, live=False)
        for t in range(T - 60, T - 60 + end_secs):
            cf.add(c, t, end.get(c, start), live=False)
    return cf


def test_start_is_mean_of_the_60_seconds_before_S():
    cf = CfIndex(persist=False)
    for c in COINS:
        for i, t in enumerate(range(S - 60, S)):
            cf.add(c, t, 100.0 + i, live=False)
        cf.add(c, S, 1e6, live=False)            # S itself is not in the window
        cf.add(c, S - 61, 1e6, live=False)
    tr = SettlementTracker(EventWindow("E", S, T), cf)
    assert math.isclose(tr.start("BTC"), 100.0 + 29.5)


def test_start_waits_for_all_60_seconds():
    cf = CfIndex(persist=False)
    for t in range(S - 60, S - 1):
        cf.add("BTC", t, 100.0, live=False)
    tr = SettlementTracker(EventWindow("E", S, T), cf)
    assert tr.start("BTC") is None
    cf.add("BTC", S - 1, 100.0, live=False)
    assert tr.start("BTC") == 100.0


def test_basic_winner():
    tr = SettlementTracker(EventWindow("E", S, T), _cf(end={"BTC": 100.1}))
    out = tr.finalize()
    assert out.winners == ["BTC"] and math.isclose(out.returns_pct["BTC"], 0.1, abs_tol=1e-9)


def test_tie_payout_split():
    tr = SettlementTracker(EventWindow("E", S, T), _cf(end={"BTC": 100.1, "ETH": 100.1}))
    out = tr.finalize()
    assert set(out.winners) == {"BTC", "ETH"} and out.payout_cents_each == 50


def test_rounding_at_boundary():
    """+0.0004% rounds to 0.000%: a five-way tie at 20c each."""
    out = SettlementTracker(EventWindow("E", S, T), _cf(end={"BTC": 100.0004})).finalize()
    assert len(out.winners) == 5 and out.payout_cents_each == 20


def test_finalize_needs_the_whole_end_window():
    assert SettlementTracker(EventWindow("E", S, T), _cf(end_secs=59)).finalize() is None


def test_snapshot_end_sum_counts_only_seconds_in():
    cf = _cf(end={"SOL": 101.0}, end_secs=20)
    snap = SettlementTracker(EventWindow("E", S, T), cf).snapshot(T - 40 + 0.5)
    assert snap.end_n["SOL"] == 20
    assert math.isclose(snap.end_log_sum["SOL"], 20 * math.log(1.01))
    assert snap.end_n["BTC"] == 20 and snap.end_log_sum["BTC"] == 0.0
    assert SettlementTracker(EventWindow("E", S, T), cf).snapshot(T - 100).end_n["SOL"] == 0


def test_snapshot_sees_a_late_tick():
    cf = _cf(end_secs=10)
    tr = SettlementTracker(EventWindow("E", S, T), cf)
    assert tr.snapshot(T - 45.5).end_n["BTC"] == 10
    for c in COINS:
        cf.add(c, T - 50, 100.0, live=False)
    assert tr.snapshot(T - 45.5).end_n["BTC"] == 11


def test_cf_age_and_stale():
    cf = CfIndex(persist=False)
    now = time.monotonic()
    for c in COINS[:-1]:
        cf.add(c, S, 1.0, mono=now)
    assert cf.age_s(now) is None and cf.stale(now)          # one coin never ticked
    cf.add("HYPE", S, 1.0, mono=now - 1.0)
    assert math.isclose(cf.age_s(now), 1.0) and not cf.stale(now)
    assert cf.stale(now + STALE_S)
    cf.add("HYPE", S - 5, 1.0, live=False)                  # a REST fill isn't a live tick
    assert cf.stale(now + STALE_S)


def test_cf_history_trims_and_ignores_old_fills():
    from kalshi_strategies.KXCRYPTOLEAD15M import cf_index
    cf = CfIndex(persist=False)
    cf.add("BTC", 10_000, 1.0, live=False)
    cf.add("BTC", 10_000 + cf_index.HISTORY_S + 1, 2.0, live=False)
    assert cf.value("BTC", 10_000) is None
    assert not cf.add("BTC", 10_000, 1.0, live=False)
    assert cf.latest("BTC") == (10_000 + cf_index.HISTORY_S + 1, 2.0)


def test_cf_ws_message_parsing():
    import asyncio
    import json
    cf = CfIndex(persist=False)
    msg = {"type": "cfbenchmarks_value", "msg": {"index_id": "XRPUSD_RTI", "data": json.dumps(
        {"type": "value", "time": S * 1000, "id": "XRPUSD_RTI", "value": "1.2913"})}}

    async def handle(m):
        return cf._handle(m)
    assert asyncio.run(handle(msg)) == ("XRP", S)
    assert cf.value("XRP", S) == 1.2913
    assert asyncio.run(handle(msg)) is None                 # duplicate second
    off_second = {"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "data": json.dumps(
        {"time": S * 1000 + 200, "value": "1"})}}
    assert asyncio.run(handle(off_second)) is None


def test_cf_write_load_roundtrip(tmp_path):
    cf = CfIndex(persist=True, root=tmp_path)
    for t in range(S - 5, S + 5):
        cf.add("ETH", t, float(t), live=False)
    assert cf.write(cf.drain()) == 10
    cf2 = CfIndex(persist=True, root=tmp_path)
    cf2.add("ETH", S, -1.0, live=False)        # rejected: not > 0
    cf2.add("ETH", S + 100, float(S + 100), live=False)
    cf2.write(cf2.drain())
    got = load_cf("ETH", S - 10, S + 200, tmp_path)
    assert got[S - 5] == float(S - 5) and got[S + 100] == float(S + 100) and len(got) == 11
