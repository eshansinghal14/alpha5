"""Alternating-event A/B of the race's per-coin constant edge (alpha5-race-ab.timer).

Since 11-10 the race books tightened from 9-10c to 7-8c while our distance from
fair stayed put, so we sit 1-2c further behind the touch and fill less. B
quotes BTC/ETH/SOL 1c closer; XRP is left alone (it lost below ~11c of edge,
10-30..11-04). Run 30 s before each event opens (the previous event is past
its 60 s cutoff by then), so each event trades in one arm. Analyse by the
constant_edge the decisions journal logs, not by this schedule.

    python ab_edge.py          set the arm for the next event
    python ab_edge.py --stop   restore A (then disable the timer)
"""
import json
import math
import sys
import time

from infra.config import read_json, update_file
from strategies.KXCRYPTOLEAD15M import SERIES

ARMS = {
    "A": {"pricing.btc_constant_edge": 0.035, "pricing.eth_constant_edge": 0.055, "pricing.sol_constant_edge": 0.04},
    "B": {"pricing.btc_constant_edge": 0.025, "pricing.eth_constant_edge": 0.045, "pricing.sol_constant_edge": 0.03},
}
LOG = "/opt/alpha5/data/live/KXCRYPTOLEAD15M/ab_edge.jsonl"
EVENT_S = 900


def current(path: str) -> dict:
    p = read_json(path)["pricing"]
    return {k: p[k.split(".", 1)[1]] for k in ARMS["A"]}


def main():
    s = SERIES["KXCRYPTOLEAD15M"]
    now = current(s.config_path)
    # Someone changed these by hand: their value wins and the experiment stops switching.
    if now not in ARMS.values():
        print(f"constant edges {now} are neither arm; not switching", file=sys.stderr)
        sys.exit(1)
    start = math.ceil(time.time() / EVENT_S) * EVENT_S
    arm = "A" if "--stop" in sys.argv else ("B" if (start // EVENT_S) % 2 else "A")
    if now != ARMS[arm]:
        update_file(s.config_cls, s.config_path, s.config_key, ARMS[arm])
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"event_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)), "arm": arm,
                             "stop": "--stop" in sys.argv}) + "\n")
    print(arm, time.strftime("%H:%M", time.gmtime(start)))


if __name__ == "__main__":
    main()
