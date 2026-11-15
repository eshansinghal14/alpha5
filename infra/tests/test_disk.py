import io

import pytest

from infra.runtime import disk


@pytest.fixture
def free(monkeypatch):
    """Sets the free space the guard sees; each call to recording_ok re-checks."""
    state = {"bytes": 10 * disk.MIN_FREE_BYTES}
    monkeypatch.setattr(disk, "free_bytes", lambda path=None: state["bytes"])
    monkeypatch.setattr(disk, "CHECK_S", 0.0)
    monkeypatch.setattr(disk, "_checked", 0.0)
    monkeypatch.setattr(disk, "_ok", True)
    return state


def test_recording_pauses_under_the_floor_and_resumes_above_it(free, capsys):
    assert disk.recording_ok()
    free["bytes"] = disk.MIN_FREE_BYTES - 1
    assert not disk.recording_ok()
    assert "recording paused, trading continues" in capsys.readouterr().out
    assert not disk.recording_ok()
    assert capsys.readouterr().out == ""                  # said once per change
    free["bytes"] = disk.MIN_FREE_BYTES
    assert disk.recording_ok()
    assert "recording resumes" in capsys.readouterr().out


def test_the_check_is_cached(free, monkeypatch):
    monkeypatch.setattr(disk, "CHECK_S", 3600.0)
    assert disk.recording_ok()
    free["bytes"] = 0
    assert disk.recording_ok()                            # not re-read within CHECK_S


def test_a_quiet_stream_drops_what_the_disk_refuses():
    class Full(io.StringIO):
        def write(self, s):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

    s = disk._Quiet(Full())
    assert s.write("line\n") == 5
    s.flush()
    ok = disk._Quiet(io.StringIO())
    ok.write("kept")
    assert ok.getvalue() == "kept"


def test_journal_parts_are_not_written_under_the_floor(free, tmp_path):
    from infra.runtime import journal as jr
    j = jr.DayJournal("TEST", "2026-11-15", out_root=str(tmp_path))
    j.record_decision({"time": 1.0, "x": 1})
    free["bytes"] = 0
    jr.write(j.capture())
    assert not (tmp_path / "TEST" / "2026-11-15" / "journal" / "decisions").exists()
    free["bytes"] = 10 * disk.MIN_FREE_BYTES
    j.record_decision({"time": 2.0, "x": 2})
    jr.write(j.capture())
    assert len(list((tmp_path / "TEST" / "2026-11-15" / "journal" / "decisions").glob("part-*.parquet"))) == 1
