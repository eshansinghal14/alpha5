"""Connect to Kalshi's API through its nearest load-balancer nodes.

DNS for external-api.kalshi.com hands out 8 rotating LB IPs across us-east-2's
zones, and a connection lands on whichever it gets. From the box (us-east-2c,
11-05) half answered a warm GET in a 2.2-2.8 ms median and half in 3.7-4.0 ms:
the far ones cost every order, cancel and take ~1.4 ms more round trip.

With ALPHA5_KALSHI_ROUTE=1 a background thread re-resolves the host every
REFRESH_S, times a TCP connect to each IP, and keeps the near ones; new
connections to the host go to one of them (round robin) and fall back to
plain DNS if none answers. Only the TCP target changes: TLS still verifies
the certificate by hostname (urllib3 keeps server_hostname = the host).
Requests sessions only (orders, REST); the websocket feeds resolve on their own.
"""
from __future__ import annotations

import itertools
import os
import socket
import statistics
import sys
import threading
import time

HOST = "external-api.kalshi.com"
PORT = 443
REFRESH_S = 60.0
PROBES = 3
PROBE_TIMEOUT_S = 1.0
# Kept: IPs whose connect time is within this of the fastest (ms). The near
# nodes measured 0.42-0.58 ms, the far ones 1.07-1.91 ms (11-05).
NEAR_MARGIN_MS = 0.4
# A preferred IP that refuses the connect is skipped this fast before the next.
CONNECT_TRY_S = 1.0

_preferred: list[str] = []
_rr = itertools.count()
_installed = False
_lock = threading.Lock()


def pick_near(times_ms: dict[str, float], margin_ms: float = NEAR_MARGIN_MS) -> list[str]:
    """The IPs within `margin_ms` of the fastest connect, fastest first."""
    ok = {ip: t for ip, t in times_ms.items() if t == t}          # drop NaN
    if not ok:
        return []
    best = min(ok.values())
    return sorted((ip for ip, t in ok.items() if t <= best + margin_ms), key=ok.get)


def _probe(ip: str) -> float:
    ts = []
    for _ in range(PROBES):
        t0 = time.perf_counter()
        try:
            s = socket.create_connection((ip, PORT), timeout=PROBE_TIMEOUT_S)
        except OSError:
            return float("nan")
        ts.append((time.perf_counter() - t0) * 1000)
        s.close()
    return statistics.median(ts)


def refresh() -> list[str]:
    """Re-resolve, probe and replace the preferred list. Returns it."""
    global _preferred
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)})
    except OSError:
        return _preferred
    times = {ip: _probe(ip) for ip in ips}
    near = pick_near(times)
    if near:
        with _lock:
            _preferred = near
    return near


def preferred() -> list[str]:
    return list(_preferred)


def _loop():
    while True:
        time.sleep(REFRESH_S)
        try:
            refresh()
        except Exception:
            pass


def _wrap(orig):
    def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
        host, port = address
        pref = _preferred
        if host == HOST and pref:
            start = next(_rr)
            for k in range(len(pref)):
                ip = pref[(start + k) % len(pref)]
                try:
                    t = CONNECT_TRY_S if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None \
                        else min(timeout, CONNECT_TRY_S)
                    return orig((ip, port), t, source_address=source_address, socket_options=socket_options)
                except OSError:
                    continue
        return orig(address, timeout, source_address=source_address, socket_options=socket_options)
    create_connection._kalshi_route = True
    return create_connection


def install() -> list[str]:
    """Probe once now, patch urllib3's connect, and keep refreshing. Idempotent."""
    global _installed
    if _installed:
        return preferred()
    import urllib3.util.connection as uc
    near = refresh()
    if not getattr(uc.create_connection, "_kalshi_route", False):
        uc.create_connection = _wrap(uc.create_connection)
    threading.Thread(target=_loop, daemon=True, name="kalshi-route").start()
    _installed = True
    # stderr: a venue child process imports this too, and its stdout is the
    # binary frame pipe -- a line there cut every venue feed (11-05 20:19).
    print(f"[route] {HOST}: connecting to {len(near)} near node(s) {near}", file=sys.stderr, flush=True)
    return near


def install_from_env() -> None:
    if os.environ.get("ALPHA5_KALSHI_ROUTE") == "1":
        try:
            install()
        except Exception as e:
            print(f"[route] not installed ({e!r}); plain DNS", file=sys.stderr, flush=True)


__all__ = ["install", "install_from_env", "pick_near", "refresh", "preferred"]
