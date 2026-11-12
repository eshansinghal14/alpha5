"""
CPU priority for the live processes: trading first, everything else yields.

The OS scheduler is what enforces it. Under contention a lower-priority
process gets CPU only when the higher ones don't want it, so the writer's
dashboard/journal writes slip instead of the trading loop reading its feeds
late (stale_signal went 0% -> 10-13% when the box was saturated, 2026-10-11
15:30 UTC). Lowering is always permitted; raising may need privileges, and a
refusal is ignored -- on Linux, run the service with systemd Nice= instead.
"""
from __future__ import annotations

import os
import sys

# Windows priority classes (SetPriorityClass).
_WIN = {"below_normal": 0x4000, "normal": 0x20, "above_normal": 0x8000}
# POSIX nice values: 19 is the lowest priority, negative needs CAP_SYS_NICE.
_NICE = {"below_normal": 19, "normal": 0, "above_normal": -5}


def set_priority(level: str) -> bool:
    """Set this process's CPU priority. Returns whether it took effect."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            # Declared, or ctypes passes the pseudo-handle as a 32-bit int and
            # the call fails on 64-bit Python.
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k.SetPriorityClass.restype = wintypes.BOOL
            return bool(k.SetPriorityClass(k.GetCurrentProcess(), _WIN[level]))
        target = _NICE[level]
        os.nice(target - os.nice(0))
        return True
    except Exception:
        return False


__all__ = ["set_priority"]
