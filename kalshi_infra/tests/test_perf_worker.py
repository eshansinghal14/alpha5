"""The perf worker's saved state and output files: a restart resumes instead
of rescanning, and the dashboard reads what the worker wrote."""
import os
import threading

import pandas as pd

from kalshi_infra.runtime import journal as jr
from kalshi_viz import perf, perf_worker

T0 = pd.Timestamp("2026-10-15 13:00:00", tz="UTC")
DAY = "2026-10-15"


def _t(s):
    return T0 + pd.Timedelta(seconds=s)


def _fill(s, oid):
    return {"time": _t(s), "ticker": "E-A", "side": "yes", "price": 0.45, "size": 4.0, "fee": 0.0,
            "liquidity": "maker", "role": "edge", "mode": "real", "seeded": False,
            "exchange_order_id": oid, "trade_id": oid, "order_id": 1}


def _write(root, fills, bbo_s):
    j = jr.DayJournal("S", DAY, out_root=str(root))
    j._buf["bbo"].extend({"time": _t(s), "ticker": "E-A", "bid": 0.44, "ask": 0.46,
                          "bid_depth": 1.0, "ask_depth": 1.0} for s in bbo_s)
    j._buf["fills"].extend(fills)
    jr.write(j.capture())


def _no_settle(monkeypatch):
    monkeypatch.setattr(perf.Perf, "_update_settlements", lambda self, *a, **k: None)


def test_saved_state_resumes_without_rereading_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    _no_settle(monkeypatch)
    _write(tmp_path, [_fill(10, "X1")], (0, 100))
    p = perf.Perf("S")
    assert len(p.refresh()) == 1
    path = perf_worker.state_path("S")
    p.save(path)

    q = perf.Perf.load("S", path)
    assert q._fill_parts == p._fill_parts and set(q._bbo) == set(p._bbo)
    reads = []
    real = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda path, *a, **k: reads.append(path) or real(path, *a, **k))
    assert len(q.refresh()) == 1
    assert not any("fills" in r for r in reads)

    _write(tmp_path, [_fill(150, "X2")], (140, 300))
    assert len(q.refresh()) == 2


def test_markout_skips_a_swept_book(tmp_path, monkeypatch):
    """10-22 11:40 KXINXU shape: bought YES at 0.45 on a 0.44/0.46 book, then
    the book empties to 0.17/0.88 (mid 0.525). The 10s markout must use the
    last normal mid (0.45 -> 0c), not the empty book's (+7.5c)."""
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    _no_settle(monkeypatch)
    j = jr.DayJournal("S", DAY, out_root=str(tmp_path))
    j._buf["bbo"].extend([
        {"time": _t(0), "ticker": "E-A", "bid": 0.44, "ask": 0.46, "bid_depth": 1.0, "ask_depth": 1.0},
        {"time": _t(10.5), "ticker": "E-A", "bid": 0.17, "ask": 0.88, "bid_depth": 1.0, "ask_depth": 1.0},
        {"time": _t(100), "ticker": "E-A", "bid": 0.17, "ask": 0.88, "bid_depth": 1.0, "ask_depth": 1.0}])
    j._buf["fills"].append(_fill(10, "X1"))
    jr.write(j.capture())
    f = perf.Perf("S").refresh()
    assert len(f) == 1
    assert abs(f.mo10.iloc[0]) < 1e-9 and abs(f.mo60.iloc[0]) < 1e-9


def test_load_falls_back_to_fresh_on_version_or_series_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    path = str(tmp_path / "state.pkl")
    p = perf.Perf("S")
    p._fill_parts.add("x")
    p.save(path)
    assert perf.Perf.load("OTHER", path)._fill_parts == set()
    monkeypatch.setattr(perf, "STATE_VERSION", perf.STATE_VERSION + 1)
    assert perf.Perf.load("S", path)._fill_parts == set()
    open(path, "wb").write(b"garbage")
    assert perf.Perf.load("S", path).series == "S"


def test_worker_output_is_what_the_dashboard_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "LIVE_ROOT", str(tmp_path))
    _no_settle(monkeypatch)
    _write(tmp_path, [_fill(10, "X1")], (0, 100))
    assert perf.read_result("S") is None

    stop = threading.Event()
    real_write = perf.write_result
    monkeypatch.setattr(perf, "write_result", lambda *a, **k: (real_write(*a, **k), stop.set()))
    perf_worker.run_series("S", every_s=0.01, stop=stop)

    r = perf.read_result("S")
    assert r is not None and r.error is None and r.updated is not None
    assert len(r.fills) == 1 and r.fills.mo10.notna().all()
    assert os.path.exists(perf_worker.state_path("S"))

    perf.write_error("S", "boom")
    r2 = perf.read_result("S")
    assert r2.error == "boom" and len(r2.fills) == 1
