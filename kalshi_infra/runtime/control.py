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


def request_trading_mode(series: str, live_trading: bool, *, source: str):
    """Dashboard side. `source` names the writer in the process log."""
    atomic_write_json(trading_mode_path(series),
                      {"live_trading": bool(live_trading),
                       "requested_at": pd.Timestamp.now(tz="UTC").isoformat(), "source": source})


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


def resumable_arm(series: str) -> str | None:
    """Why a restarted process may re-arm `series`, or None if it may not.

    Only if the last request was an arm and no STOP.json (stop-all, balance
    watchdog) was written after it: a deliberate stop must never be undone by
    a restart. No age limit: an 18h one left an arm held since the previous
    day in shadow after any restart, with nothing logged."""
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
        self.last: dict = {}

    def poll(self) -> bool | None:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return None
        if m <= self._mtime:
            return None
        try:
            with open(self.path) as fh:
                self.last = json.load(fh)
            want = bool(self.last.get("live_trading", False))
        except (OSError, json.JSONDecodeError, AttributeError):
            return None   # mid-write; retry next poll without advancing mtime
        self._mtime = m
        self.requested = want
        return want

    def origin(self) -> str:
        """When and by whom the last polled request was written."""
        return (f"(written {self.last.get('requested_at', '?')} by "
                f"{self.last.get('source', 'unknown writer')})")
