"""
Dashboard -> process control: the arm/disarm request file.

Kept separate from config.json on purpose: the switch that moves real money
must not ride along with an edge tweak, and it has different startup
semantics -- a leftover `live_trading: true` from a previous session must
NOT re-arm on start, so only a file written after the process started counts.
The one exception is opt-in (`run --resume-arm`, used by the EC2 units):
see resumable_arm.
"""
from __future__ import annotations

import json
import os

import pandas as pd

from kalshi_infra.config import atomic_write_json
from kalshi_infra.runtime.persist import LIVE_ROOT


def trading_mode_path(series: str) -> str:
    return os.path.join(LIVE_ROOT, series, "trading_mode.json")


def request_trading_mode(series: str, live_trading: bool):
    """Dashboard side."""
    atomic_write_json(trading_mode_path(series),
                      {"live_trading": bool(live_trading),
                       "requested_at": pd.Timestamp.now(tz="UTC").isoformat()})


STOP_PATH = os.path.join(LIVE_ROOT, "STOP.json")


def request_stop_all(reason: str):
    """Tell every running series process to disarm and exit."""
    atomic_write_json(STOP_PATH, {"reason": reason, "requested_at": pd.Timestamp.now(tz="UTC").isoformat()})


class StopWatcher:
    """`poll()` returns the stop reason once a STOP written after startup
    appears, else None. Mtime-seeded like TradingModeWatcher, so a STOP left
    by a previous run doesn't kill the next one."""

    def __init__(self):
        self._mtime = os.path.getmtime(STOP_PATH) if os.path.exists(STOP_PATH) else 0.0

    def poll(self) -> str | None:
        try:
            if os.path.getmtime(STOP_PATH) <= self._mtime:
                return None
            with open(STOP_PATH) as fh:
                return str(json.load(fh).get("reason") or "stop requested")
        except (OSError, json.JSONDecodeError):
            return None


def resumable_arm(series: str, max_age_s: float) -> str | None:
    """Why a restarted process may re-arm `series`, or None if it may not.

    Only an arm request made in the last `max_age_s`, and only if no
    STOP.json (stop-all, loss halt, balance watchdog) was written after it:
    a deliberate stop must never be undone by a restart. Restarts that
    dropped armed S&P to shadow for 7+ min each happened twice on
    2026-10-15."""
    try:
        with open(trading_mode_path(series)) as fh:
            tm = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not tm.get("live_trading"):
        return None
    try:
        at = pd.Timestamp(tm["requested_at"])
    except (KeyError, ValueError):
        return None
    age = (pd.Timestamp.now(tz="UTC") - at).total_seconds()
    if age > max_age_s:
        return None
    try:
        with open(STOP_PATH) as fh:
            stop_at = pd.Timestamp(json.load(fh)["requested_at"])
        if stop_at >= at:
            return None
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return f"armed {age / 3600:.1f}h ago ({at.isoformat()}), no stop since"


class TradingModeWatcher:
    """Process side. `poll()` returns the newly requested state, or None."""

    def __init__(self, series: str):
        self.path = trading_mode_path(series)
        # Seed to the existing mtime: only a click after startup arms.
        self._mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else 0.0
        self.requested: bool | None = None

    def poll(self) -> bool | None:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return None
        if m <= self._mtime:
            return None
        try:
            with open(self.path) as fh:
                want = bool(json.load(fh).get("live_trading", False))
        except (OSError, json.JSONDecodeError):
            return None   # mid-write; retry next poll without advancing mtime
        self._mtime = m
        self.requested = want
        return want
