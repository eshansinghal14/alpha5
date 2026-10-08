"""
CompositeFeed.flush crash-safety tests.

The 2026-10-07 bug: a process killed mid-`to_parquet` left a target file
whose parquet footer pointed past the real bytes (a 25KB file claiming
827MB). Every subsequent flush then raised ArrowInvalid on read and
aborted, wedging the whole flush loop. The hardened flush writes
atomically (temp + os.replace), quarantines an unreadable existing file
and starts fresh, and keeps pending bars until the write succeeds.
"""
import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kalshi_crypto.composite import CompositeFeed, CompositeBar


def _bar(coin: str, ts_s: float, price: float = 100.0) -> CompositeBar:
    return CompositeBar(coin=coin, ts_s=ts_s, price=price, utilized_depth=1.0,
                        n_venues_in=1, n_venues_dropped=0, is_fallback=False,
                        fallback_mid=None, nowcast_count=1)


def test_flush_writes_and_reloads(tmp_path):
    f = CompositeFeed("BTC", persist=True, persist_dir=tmp_path)
    f._pending_disk = [_bar("BTC", 1_700_000_000.0, 100.0),
                       _bar("BTC", 1_700_000_001.0, 101.0)]
    n = f.flush()
    assert n == 2
    assert not f._pending_disk            # cleared only after success
    files = list(tmp_path.glob("BTC_*.parquet"))
    assert len(files) == 1
    df = pd.read_parquet(files[0])
    assert len(df) == 2


def test_flush_appends_dedup(tmp_path):
    f = CompositeFeed("BTC", persist=True, persist_dir=tmp_path)
    f._pending_disk = [_bar("BTC", 1_700_000_000.0, 100.0)]
    f.flush()
    # Re-flush an overlapping ts (keep="last") plus a new one.
    f._pending_disk = [_bar("BTC", 1_700_000_000.0, 999.0),
                       _bar("BTC", 1_700_000_002.0, 102.0)]
    f.flush()
    df = pd.read_parquet(next(tmp_path.glob("BTC_*.parquet")))
    assert len(df) == 2                   # deduped on ts_s
    # The duplicate ts_s kept the LAST value.
    assert df.loc[df["ts_s"] == 1_700_000_000.0, "price"].iloc[0] == 999.0


def test_flush_recovers_from_corrupt_file(tmp_path, capsys):
    f = CompositeFeed("BTC", persist=True, persist_dir=tmp_path)
    f._pending_disk = [_bar("BTC", 1_700_000_000.0, 100.0)]
    f.flush()
    target = next(tmp_path.glob("BTC_*.parquet"))
    # Corrupt the file the way an interrupted write does: truncate it.
    target.write_bytes(b"PAR1\x00\x00garbage not a real parquet footer")
    # Next flush must NOT raise; it quarantines the bad file and writes fresh.
    f._pending_disk = [_bar("BTC", 1_700_000_003.0, 103.0)]
    n = f.flush()
    assert n == 1
    # A quarantine file now exists (suffix intentionally does NOT end in
    # .parquet so loaders' {coin}_*.parquet globs skip it), and the target
    # is readable again.
    assert list(tmp_path.glob("BTC_*.parquet.corrupt-*"))
    df = pd.read_parquet(target)
    assert len(df) == 1 and df["price"].iloc[0] == 103.0
    assert "unreadable" in capsys.readouterr().out


def test_flush_keeps_pending_on_write_failure(tmp_path, monkeypatch):
    """If the write itself fails, the pending buffer is NOT cleared, so the
    next cadence retries rather than silently dropping bars."""
    f = CompositeFeed("BTC", persist=True, persist_dir=tmp_path)
    bars = [_bar("BTC", 1_700_000_000.0, 100.0)]
    f._pending_disk = list(bars)

    import kalshi_crypto.composite as comp

    def boom(*a, **k):
        raise OSError("disk full")
    # Make the atomic rename fail (temp written, replace blows up).
    monkeypatch.setattr(comp.os, "replace", boom)
    with pytest.raises(OSError):
        f.flush()
    assert f._pending_disk == bars        # retained for the next cadence


def test_no_tmp_files_left_after_success(tmp_path):
    f = CompositeFeed("BTC", persist=True, persist_dir=tmp_path)
    f._pending_disk = [_bar("BTC", 1_700_000_000.0, 100.0)]
    f.flush()
    assert not list(tmp_path.glob("*.tmp-*.parquet"))
