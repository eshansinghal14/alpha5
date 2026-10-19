"""SnapshotWriter.depth: one row per level near the touch whose size changed."""
import types

import pandas as pd

from kalshi_infra.runtime.writer import SnapshotWriter


def _w():
    w = SnapshotWriter.__new__(SnapshotWriter)
    w._clear_buffers()
    w._last_depth = {}
    return w


def _book(yes, no):
    return types.SimpleNamespace(ticker="A", time=pd.Timestamp("2026-10-19", tz="UTC"),
                                 ladder=types.SimpleNamespace(yes_bids=dict(yes), no_bids=dict(no)))


def test_only_changed_levels_near_the_touch():
    w = _w()
    w.depth(_book({0.50: 10.0, 0.49: 5.0, 0.30: 99.0}, {0.48: 7.0}), levels=3)
    assert sorted((r["side"], r["price"], r["size"]) for r in w._depth) == [
        ("no", 0.48, 7.0), ("yes", 0.49, 5.0), ("yes", 0.50, 10.0)]
    w._depth.clear()
    w.depth(_book({0.50: 10.0, 0.49: 2.0}, {0.48: 7.0}), levels=3)
    assert [(r["side"], r["price"], r["size"]) for r in w._depth] == [("yes", 0.49, 2.0)]


def test_emptied_level_is_a_zero_row():
    w = _w()
    w.depth(_book({0.50: 10.0, 0.49: 5.0}, {}), levels=3)
    w._depth.clear()
    w.depth(_book({0.50: 10.0}, {}), levels=3)
    assert [(r["price"], r["size"]) for r in w._depth] == [(0.49, 0.0)]


def test_no_ladder_is_ignored():
    w = _w()
    w.depth(types.SimpleNamespace(ticker="A", time=None, ladder=None))
    assert w._depth == []
