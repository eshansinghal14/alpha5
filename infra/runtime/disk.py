"""Recording stops before the disk fills, so trading never does.

Journals and recorders call `recording_ok()` before writing and skip the
write below `MIN_FREE_BYTES`; state files (ledgers, configs, snapshots) and
logs don't, so they keep the space left. `quiet_streams()` makes the
process's stdout/stderr drop a write the disk refuses instead of raising it
into whatever was printing.
"""
from __future__ import annotations

import os
import sys
import time

from infra import DATA_ROOT

# The box wrote ~5 GB/day of recordings on 11-14 and ran out at ~1 GB/day net
# of the nightly S3 archive (deploy/archive_s3.sh); 3 GB is days of headroom
# for state files and logs, which grow by tens of MB a day.
MIN_FREE_BYTES = 3 * 1024 ** 3
CHECK_S = 5.0

_checked = 0.0
_ok = True


def free_bytes(path: str = DATA_ROOT) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def recording_ok(path: str = DATA_ROOT) -> bool:
    """False while the disk holding `path` has under MIN_FREE_BYTES free.
    Re-checked at most every CHECK_S; says so once on each change."""
    global _checked, _ok
    now = time.monotonic()
    if now - _checked < CHECK_S:
        return _ok
    _checked = now
    try:
        free = free_bytes(path)
    except (OSError, AttributeError):        # no statvfs (Windows): record
        free = MIN_FREE_BYTES
    ok = free >= MIN_FREE_BYTES
    if ok != _ok:
        _ok = ok
        _say(("WARN: disk has room again; recording resumes" if ok else
              "WARN: disk nearly full; recording paused, trading continues")
             + f" ({free / 1e9:.1f} GB free, floor {MIN_FREE_BYTES / 1e9:.1f} GB)")
    return _ok


def _say(msg: str):
    try:
        print(f"[disk] {msg}", flush=True)
    except (OSError, ValueError):
        pass


class _Quiet:
    """A stream whose writes never raise OSError (ENOSPC on a log file)."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        try:
            return self._inner.write(s)
        except OSError:
            return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


def quiet_streams():
    if not isinstance(sys.stdout, _Quiet):
        sys.stdout = _Quiet(sys.stdout)
    if not isinstance(sys.stderr, _Quiet):
        sys.stderr = _Quiet(sys.stderr)
