"""Fill journal dedupe: a re-arm replays the event's real fills under fresh
local order ids, and must not journal them twice."""
import pandas as pd

from kalshi_backtest.core import Fill
from kalshi_infra.runtime import journal as jr

T0 = pd.Timestamp("2026-10-10 01:10:00", tz="UTC")


def _fill(oid, t, size=3.0, xo=None, tid=None, seeded=False, price=0.92):
    return Fill(order_id=oid, ticker="EV-XRP", side="no", price=price, size=size,
                time=T0 + pd.Timedelta(seconds=t), fee=0.01, liquidity="taker",
                exchange_order_id=xo, trade_id=tid, seeded=seeded)


def _rows(j):
    return j._buf["fills"]


def test_rearm_replay_of_ioc_fill_is_not_journaled_again(tmp_path):
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    # IOC booked off its create response: our clock, no trade id.
    j.record_fills([_fill(3388, 0.8, xo="X1")], live_trading=True)
    # Two re-arms replay it from the fills endpoint: new local ids, Kalshi's time.
    j.record_fills([_fill(10, 0.0, xo="X1", tid="T1", seeded=True)], live_trading=True)
    j.record_fills([_fill(20, 0.0, xo="X1", tid="T1", seeded=True)], live_trading=True)
    assert len(_rows(j)) == 1


def test_sweep_replayed_as_several_trades_is_one_fill(tmp_path):
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    j.record_fills([_fill(1, 0.0, size=5.0, xo="X1")], live_trading=True)
    j.record_fills([_fill(7, 0.0, size=2.0, xo="X1", tid="T1", seeded=True),
                    _fill(8, 0.0, size=3.0, xo="X1", tid="T2", seeded=True)], live_trading=True)
    assert [r["size"] for r in _rows(j)] == [5.0]


def test_partial_fills_of_one_resting_quote_all_journal(tmp_path):
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    j.record_fills([_fill(1, 0.0, size=1.0, xo="Q1", tid="T1"),
                    _fill(1, 1.0, size=2.0, xo="Q1", tid="T2")], live_trading=True)
    assert len(_rows(j)) == 2


def test_seeded_fill_first_seen_on_replay_is_kept(tmp_path):
    """A fill this process never booked (made before a restart that lost its
    journal buffer) still belongs in the record."""
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    j.record_fills([_fill(5, 0.0, xo="X9", tid="T9", seeded=True)], live_trading=True)
    assert len(_rows(j)) == 1 and _rows(j)[0]["seeded"] is True


def test_restart_reads_journaled_ids_back(tmp_path):
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    j.record_fills([_fill(1, 0.0, xo="X1"), _fill(2, 1.0, xo="Q1", tid="T5")], live_trading=True)
    jr.write(j.capture())
    j2 = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    j2.record_fills([_fill(9, 0.0, xo="X1", tid="T1", seeded=True),
                     _fill(10, 1.0, xo="Q1", tid="T5", seeded=True)], live_trading=True)
    assert _rows(j2) == []


def test_shadow_fills_still_dedupe_on_local_key(tmp_path):
    j = jr.DayJournal("S", "2026-10-10", out_root=str(tmp_path))
    f = _fill(1, 0.0)
    j.record_fills([f, f], live_trading=False)
    assert len(_rows(j)) == 1 and _rows(j)[0]["mode"] == "shadow"
