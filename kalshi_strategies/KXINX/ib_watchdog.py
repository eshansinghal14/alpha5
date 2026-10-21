"""
Backstop for the IB Gateway container: every CHECK_S, open an API session
and ask IB for the time. If that has failed for DOWN_RESTART_S during market
hours, restart the container, which makes IBC log in again and send a new
IBKR Mobile push. Approving the push is the only manual step.

IBC already retries a timed-out 2FA by itself (RELOGIN_AFTER_TWOFA_TIMEOUT)
and retakes the session when another login kicks it off
(EXISTING_SESSION_DETECTED_ACTION=primary). This covers what IBC can't see:
a hung gateway, or a dead session with the API port still open.

    python -m kalshi_strategies.KXINX.ib_watchdog
"""
from __future__ import annotations

import asyncio
import datetime
import subprocess
import time
from zoneinfo import ZoneInfo

from kalshi_infra import ib_functions as ibf
from kalshi_infra import keys

CHECK_S = 60
# Long enough to cover the nightly IBC restart and a push you're about to
# approve; short enough that a dead gateway is back before the open.
DOWN_RESTART_S = 600
MIN_RESTART_GAP_S = 900
COMPOSE_DIR = "/opt/alpha5/deploy/ibgateway"
ET = ZoneInfo("America/New_York")
# Restart only while SPXW quotes (Cboe overnight 20:15 ET through the 16:15
# close, Sunday evening to Friday), so a gateway down in the evening break or
# at the weekend doesn't send a push every 15 minutes. These are the break's
# bounds: inside them is NOT active.
ACTIVE = (datetime.time(16, 15), datetime.time(20, 15))


def log(msg: str):
    print(f"{datetime.datetime.now(ET):%Y-%m-%d %H:%M:%S} [ib_watchdog] {msg}", flush=True)


async def api_ok() -> bool:
    ib_mod = ibf._import_ib()
    ib = ib_mod.IB()
    try:
        await asyncio.wait_for(ib.connectAsync(keys.IB_HOST, keys.IB_PORT, clientId=keys.IB_CLIENT_ID + 50,
                                               readonly=True, fetchFields=ib_mod.StartupFetchNONE), 15)
        await asyncio.wait_for(ib.reqCurrentTimeAsync(), 10)
        return True
    except Exception:
        return False
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def active_now(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now(ET)
    wd, t = now.weekday(), now.time()
    if ACTIVE[0] <= t < ACTIVE[1]:
        return False
    if t >= ACTIVE[1]:                       # evening: leads into the next day's session
        return wd in (6, 0, 1, 2, 3)         # Sun-Thu
    return wd < 5                            # overnight/day: Mon-Fri


def main():
    ibf.quiet_ib_logging()
    down_since, last_restart, was_ok = None, 0.0, None
    log(f"watching {keys.IB_HOST}:{keys.IB_PORT}")
    while True:
        ok = asyncio.run(api_ok())
        now = time.monotonic()
        if ok != was_ok:
            log("IB API up" if ok else "IB API DOWN (approve the IBKR Mobile push if you get one)")
            was_ok = ok
        if ok:
            down_since = None
        else:
            down_since = down_since or now
            if (now - down_since >= DOWN_RESTART_S and now - last_restart >= MIN_RESTART_GAP_S
                    and active_now()):
                log(f"down {now - down_since:.0f}s: restarting the gateway container (new login + push)")
                r = subprocess.run(["docker", "compose", "restart"], cwd=COMPOSE_DIR,
                                   capture_output=True, text=True, timeout=180)
                log(f"restart exit {r.returncode} {r.stderr.strip()[-300:]}")
                last_restart = now
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
