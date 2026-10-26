import json
import time

from streamlit.testing.v1 import AppTest

from kalshi_infra.runtime import control

SCRIPT = """
import streamlit as st
from kalshi_infra.runtime import control
from kalshi_viz.arm import arm_control
control.trading_mode_path = lambda s: {path!r}
arm_control("X", st.session_state.get("reported", False), help="")
"""


def _app(tmp_path):
    path = str(tmp_path / "trading_mode.json")
    return AppTest.from_string(SCRIPT.format(path=path)).run(), tmp_path / "trading_mode.json"


def test_reruns_without_a_click_write_nothing(tmp_path):
    at, f = _app(tmp_path)
    for reported in (False, True, False):
        at.session_state["reported"] = reported
        at.run()
    assert not f.exists()


def test_click_writes_what_the_button_says_and_who(tmp_path):
    at, f = _app(tmp_path)
    at.button(key="btn_arm_X").click().run()
    got = json.loads(f.read_text())
    assert got["live_trading"] is True and got["source"].startswith("dashboard")
    # Pending: shows the requested state until the process reports it.
    assert at.button(key="btn_disarm_X") is not None
    at.session_state["reported"] = True
    at.run()
    at.button(key="btn_disarm_X").click().run()
    assert json.loads(f.read_text())["live_trading"] is False


def test_watcher_reports_writer(tmp_path, monkeypatch):
    path = tmp_path / "X" / "trading_mode.json"
    path.parent.mkdir()
    monkeypatch.setattr(control, "trading_mode_path", lambda s: str(path))
    w = control.TradingModeWatcher("X")
    time.sleep(0.01)
    control.request_trading_mode("X", False, source="balance watchdog")
    assert w.poll() is False
    assert "by balance watchdog" in w.origin()
