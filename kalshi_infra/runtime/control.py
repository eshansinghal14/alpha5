"""
Dashboard -> process control: the arm/disarm request file.

Kept separate from config.json on purpose: the switch that moves real money
must not ride along with an edge tweak, and it has different startup
semantics -- a leftover `live_trading: true` from a previous session must
NOT re-arm on start, so only a file written after the process started counts.
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
