"""
Account floor: stop ALL live trading if Kalshi portfolio (cash + position
value) drops below a floor.

    python -m kalshi_infra.runtime.balance_watchdog --floor 1000 --interval 30

On trip: STOP.json (every series disarms, cancels, exits), then a direct
cancel sweep of every resting order on the account, and if a session process
is still alive after the grace period it is terminated and swept again.
Exits after tripping, so it never fights a session restarted by hand later.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import pandas as pd

from kalshi_infra.api_functions import BASE_URL
from kalshi_infra.config import atomic_write_json
from kalshi_infra.kalshi.orders import BALANCE_PATH, KalshiOrderClient
from kalshi_infra.runtime.control import STOP_PATH, request_stop_all, request_trading_mode
from kalshi_infra.runtime.persist import LIVE_ROOT

HEARTBEAT_PATH = os.path.join(LIVE_ROOT, "balance_watchdog.json")
LOG_PATH = os.path.join(LIVE_ROOT, "balance_watchdog.log")
STOP_GRACE_S = 90


def log(msg: str):
    line = f"{pd.Timestamp.now(tz='UTC').isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_balance(client: KalshiOrderClient) -> tuple[float, float]:
    """(cash, portfolio_value) in dollars. Raises on a missing field rather
    than returning 0 -- KalshiOrderClient.balance() maps a missing field to
    0.0, which here would be a false trip."""
    resp = client._session.get(BASE_URL + BALANCE_PATH, headers=client._headers("GET", BALANCE_PATH),
                               timeout=10)
    resp.raise_for_status()
    data = resp.json()
    cash = float(data["balance_dollars"])
    # portfolio_value has no _dollars form; it is cents, consistent with the
    # bare `balance` cents field (110845 vs balance_dollars 1108.45, 2026-10-11).
    pv = float(data.get("portfolio_value") or 0) / 100.0
    return cash, pv


def session_pids() -> list[int]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(p.info.get("cmdline") or [])
        if "kalshi_strategies.run" in cmd:
            out.append(p.info["pid"])
    return out


def cancel_all(client: KalshiOrderClient) -> int:
    n = 0
    for _ in range(5):
        resting = client._resting("")
        if not resting:
            break
        for oid, ticker, _cid in resting:
            try:
                client.cancel(oid, ticker)
                n += 1
            except Exception as e:
                log(f"cancel {oid} {ticker} failed: {e!r}")
        time.sleep(1)
    return n


def trip(client: KalshiOrderClient, total: float, floor: float):
    reason = f"balance watchdog: portfolio ${total:.2f} < floor ${floor:.2f}"
    log(f"TRIP: {reason}")
    request_stop_all(reason)
    for series in os.listdir(LIVE_ROOT):
        if os.path.exists(os.path.join(LIVE_ROOT, series, "trading_mode.json")):
            request_trading_mode(series, False, source="balance watchdog")

    deadline = time.time() + STOP_GRACE_S
    while time.time() < deadline and session_pids():
        time.sleep(3)
    log(f"swept {cancel_all(client)} resting orders after STOP")

    alive = session_pids()
    if alive:
        import psutil
        log(f"session processes still alive after {STOP_GRACE_S}s: {alive}; terminating")
        for pid in alive:
            try:
                psutil.Process(pid).terminate()
            except Exception as e:
                log(f"terminate {pid} failed: {e!r}")
        time.sleep(5)
        log(f"swept {cancel_all(client)} resting orders after terminate")

    left = client._resting("")
    log(f"done. resting orders left: {len(left)}; session processes: {session_pids()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, default=1000.0)
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()

    client = KalshiOrderClient(tokens_per_s=1)
    log(f"started: floor ${args.floor:.2f}, every {args.interval:.0f}s")
    errors = 0
    while True:
        try:
            cash, pv = read_balance(client)
            errors = 0
        except Exception as e:
            errors += 1
            log(f"balance read failed ({errors}): {e!r}")
            time.sleep(min(args.interval, 10))
            continue

        # Cash + position value: cash alone dips whenever money is tied up
        # in open positions, which isn't a loss.
        total = cash + pv
        atomic_write_json(HEARTBEAT_PATH, {
            "time": pd.Timestamp.now(tz="UTC").isoformat(), "cash": cash,
            "portfolio_value": pv, "total": total, "floor": args.floor, "pid": os.getpid()})
        if total < args.floor:
            # Confirm with a second read so one odd response can't kill the session.
            time.sleep(2)
            try:
                total2 = sum(read_balance(client))
            except Exception as e:
                log(f"confirm read failed: {e!r}; tripping on first read")
                total2 = total
            if total2 < args.floor:
                try:
                    trip(client, total2, args.floor)
                except Exception as e:
                    # STOP.json is written first, so the sessions still stop.
                    log(f"trip cleanup error: {e!r}")
                return
            log(f"sub-floor read ${total:.2f} not confirmed (${total2:.2f})")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
