"""One Polymarket request budget for every process on this machine.

Polymarket US limits the retail API per source IP (25 requests/s across
endpoints, docs rate-limits page read 11-15), not per key or process, so
ARBSPM's orders and reads, its scan and the recorder all draw on one bucket
kept in shared memory, like Kalshi's read and write buckets. Counted in
ARBS's token units: 10 per request.

The public gateway (market listings, books, settlements) is counted too: the
docs don't say whether it shares the API's counter, and treating it as shared
only costs background reads a short wait. On 11-15 ARBSPM's uncounted
reads drew an IP-wide 429 that blocked arming for 10 minutes.
"""
from __future__ import annotations

import collections
import threading
import time

from infra.kalshi.orders import SharedWriteBudget, _shm_path

REQUESTS_PER_S = 20.0     # under the documented 25/s per IP
COST = 10.0
TOKENS_PER_S = REQUESTS_PER_S * COST
# A trading read (reconcile, positions, balance) is refused rather than leave
# less than this, so cancels always find tokens.
READ_KEEP = 20.0
# Background reads (scan, meta refresh, recorder) wait until this much would
# be left: they never take the last ten requests' worth from trading.
BACKGROUND_KEEP = 100.0

_BUDGET: SharedWriteBudget | None = None
_LOCK = threading.Lock()
_SENT: collections.Counter = collections.Counter()   # this process's requests per wall-clock second
_429: collections.Counter = collections.Counter()    # this process's 429s by kind


class PMBudgetLow(RuntimeError):
    """A Polymarket read held back because the shared bucket couldn't pay for it."""


def budget() -> SharedWriteBudget:
    global _BUDGET
    with _LOCK:
        if _BUDGET is None:
            _BUDGET = SharedWriteBudget(TOKENS_PER_S, 0, path=_shm_path("alpha5-polymarket.bucket"))
        return _BUDGET


def _note():
    now = int(time.time())
    with _LOCK:
        _SENT[now] += 1
        if len(_SENT) > 180:
            for k in [k for k in _SENT if k < now - 120]:
                del _SENT[k]


def pay(keep: float = READ_KEEP, force: bool = False):
    """Spend one request's tokens now, or raise PMBudgetLow if that would
    leave less than `keep`. force: always (a cancel, a create already paid
    for by the write path's allow_now is not paid again)."""
    if force:
        budget().take(COST, force=True)
    elif not budget().take_keeping(COST, keep):
        raise PMBudgetLow(f"polymarket bucket below {COST + keep:g} tokens")
    _note()


def wait(keep: float = BACKGROUND_KEEP, max_wait_s: float = 120.0):
    """Block until one request's tokens can be spent leaving `keep`; for
    background reads, which can afford to wait. Raises PMBudgetLow after
    max_wait_s."""
    end = time.monotonic() + max_wait_s
    while not budget().take_keeping(COST, keep):
        if time.monotonic() > end:
            raise PMBudgetLow(f"polymarket bucket stayed below {COST + keep:g} tokens for {max_wait_s:g}s")
        time.sleep(0.1)
    _note()


def note_status(status: int | None, kind: str):
    """After a response: a 429 empties the shared bucket (Polymarket's
    counter is fuller than ours thinks), so every process backs off."""
    if status == 429:
        budget().empty()
        with _LOCK:
            _429[kind] += 1


def stats() -> dict:
    """Tokens left machine-wide; this process's requests in the last whole
    second and the busiest second of the last minute; its 429s by kind."""
    now = int(time.time())
    tokens = round(budget().tokens_now(), 1)     # budget() takes _LOCK itself
    with _LOCK:
        return {"tokens": tokens, "last_s": _SENT.get(now - 1, 0),
                "max_s_60s": max((_SENT.get(t, 0) for t in range(now - 60, now)), default=0),
                "http_429": dict(_429)}


def gateway_get(path: str, query: dict | None = None, agent: str = "alpha5", timeout_s: float = 30.0) -> dict:
    """A public gateway GET (listings, books, settlements) paid as a
    background read: waits for the bucket rather than take trading's share."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    from infra.polymarket import GATEWAY

    wait()
    url = GATEWAY + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        note_status(e.code, "gateway")
        raise
