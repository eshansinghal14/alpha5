"""Arm/disarm buttons shared by every series page.

Buttons, not a checkbox: Streamlit 1.39 calls on_change whenever the browser
reports a widget value the server has dropped, and a 2s fragment run that
skips the checkbox (app._stale) drops it. The browser's stale value was then
written without a click (KXCRYPTOLEAD15M disarmed, 2026-10-26 18:27 UTC). A
button press is a trigger, sent only on the click itself."""
from __future__ import annotations

import time

import streamlit as st

from kalshi_infra.runtime.control import request_trading_mode

# Long enough for the process to poll the request and write a summary.
PENDING_S = 30.0


def _source() -> str:
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    ctx = get_script_run_ctx()
    return f"dashboard session {ctx.session_id[:8]}" if ctx else "dashboard"


def _request(series: str, want: bool):
    request_trading_mode(series, want, source=_source())
    st.session_state[f"_arm_pending_{series}"] = (want, time.monotonic())


def arm_control(series: str, requested: bool, help: str) -> bool:
    """One button for the opposite of the arm state, which is `requested`
    (what the process reports) unless this tab's click is still waiting to
    be picked up. Returns that state."""
    p = st.session_state.get(f"_arm_pending_{series}")
    waiting = p is not None and p[0] != requested
    pending = waiting and time.monotonic() - p[1] < PENDING_S
    shown = p[0] if pending else requested
    if shown:
        st.button("Disarm -- stop sending real orders", key=f"btn_disarm_{series}",
                  on_click=_request, args=(series, False), help=help)
    else:
        st.button("Arm -- send REAL orders to Kalshi (real money)", key=f"btn_arm_{series}", type="primary",
                  on_click=_request, args=(series, True), help=help)
    if pending:
        st.warning("Waiting for the process to pick up the request.")
    elif waiting:
        st.warning(f"The process hasn't picked up the {'arm' if p[0] else 'disarm'} request after "
                   f"{PENDING_S:.0f}s -- is it running? Showing what it last reported.")
    return shown
