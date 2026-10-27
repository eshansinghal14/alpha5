import json

import pandas as pd
import pytest

from kalshi_infra.runtime import control


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "trading_mode_path", lambda s: str(tmp_path / s / "trading_mode.json"))
    monkeypatch.setattr(control, "STOP_PATH", str(tmp_path / "STOP.json"))
    return tmp_path


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _ago(s):
    return (pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=s)).isoformat()


def test_recent_arm_resumes(live_root):
    _write(live_root / "X" / "trading_mode.json", {"live_trading": True, "requested_at": _ago(60)})
    assert control.resumable_arm("X")


def test_disarm_or_missing_file_does_not(live_root):
    assert control.resumable_arm("X") is None
    _write(live_root / "X" / "trading_mode.json", {"live_trading": False, "requested_at": _ago(60)})
    assert control.resumable_arm("X") is None


def test_arm_from_days_ago_still_resumes(live_root):
    _write(live_root / "X" / "trading_mode.json", {"live_trading": True, "requested_at": _ago(3 * 86400)})
    assert control.resumable_arm("X")


def test_stop_after_the_arm_blocks_resume(live_root):
    _write(live_root / "X" / "trading_mode.json", {"live_trading": True, "requested_at": _ago(600)})
    _write(live_root / "STOP.json", {"reason": "loss limit", "requested_at": _ago(300)})
    assert control.resumable_arm("X") is None


def test_stop_before_the_arm_does_not(live_root):
    _write(live_root / "STOP.json", {"reason": "old", "requested_at": _ago(900)})
    _write(live_root / "X" / "trading_mode.json", {"live_trading": True, "requested_at": _ago(600)})
    assert control.resumable_arm("X")
