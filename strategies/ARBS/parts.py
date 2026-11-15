"""Which of Kalshi shard 0's processes (PROCS) owns each event, so they carry
about equal write load. One Python loop tops out at a core; a fixed hash
split left ARBS at its 150 tokens/s cap on ~250 hot baskets while ARBS0B
spent ~95 on ~1,190 (11-15 22:43-53 UTC).

The first process's scan is the only writer (update): a new event goes to
the process whose listed events cost less, priced by its series' measured
cost per event; once every REBALANCE_S, events move from the costlier
process to the other while the two differ by more than GAP of their total.
Costs are write tokens spent per event over the last two UTC days, from
each process's event_spend.json (EventSpend). A process holding a basket of
an event the other owns only finishes it.
"""
from __future__ import annotations

import json
import os
import time
import zlib

from infra.config import atomic_write_json
from infra.runtime.persist import LIVE_ROOT

PROCS = ("ARBS", "ARBS0B")
GAP = 0.10
REBALANCE_S = 86400.0
# An event not listed for this long is forgotten (its baskets have settled).
FORGET_S = 3 * 86400.0
OWNERS = "shard0_owners.json"
SPEND = "event_spend.json"
SPEND_DAYS = 3


def owners_path() -> str:
    return os.path.join(LIVE_ROOT, PROCS[0], OWNERS)


def event_of(basket_id: str) -> str:
    """A scanned basket's event (mecnet:EVENT:..., ladder:EVENT:..., complete_set:EVENT:...)."""
    parts = basket_id.split(":")
    return parts[1] if len(parts) > 2 else ""


def series_of(event: str) -> str:
    return event.split("-", 1)[0]


def load(path: str | None = None) -> dict:
    try:
        with open(path or owners_path(), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("owners", {})
    st.setdefault("seen", {})
    st.setdefault("rebalanced_at", 0.0)
    return st


def save(st: dict, path: str | None = None):
    atomic_write_json(path or owners_path(), st, indent=None)


def owner(st: dict, event: str) -> int | None:
    return st["owners"].get(event)


def spend(days: int = 2, root: str | None = None) -> dict[str, float]:
    """event -> write tokens every process spent on it over the last `days` UTC days."""
    out: dict[str, float] = {}
    for p in PROCS:
        try:
            with open(os.path.join(root or LIVE_ROOT, p, SPEND), encoding="utf-8") as fh:
                by_day = json.load(fh)
        except (OSError, ValueError):
            continue
        for day in sorted(by_day)[-days:]:
            for ev, t in by_day[day].items():
                out[ev] = out.get(ev, 0.0) + float(t)
    return out


def _costs(events: list[str], spent: dict[str, float]) -> dict[str, float]:
    """Each event's cost: its own measured spend, else its series' mean over
    measured events, else the median of the series means."""
    by_series: dict[str, list[float]] = {}
    for ev, t in spent.items():
        by_series.setdefault(series_of(ev), []).append(t)
    means = {s: sum(v) / len(v) for s, v in by_series.items()}
    vals = sorted(means.values())
    default = vals[len(vals) // 2] if vals else 1.0
    return {ev: spent[ev] if ev in spent else means.get(series_of(ev), default) for ev in events}


def update(st: dict, events: list[str], now: float, spent: dict[str, float]) -> list[tuple[str, int, int]]:
    """Assigns `events` (shard 0's listed now) not yet owned, forgets ones
    long unlisted, and rebalances when due. Returns the moves (event, from,
    to). A state with no owners starts from the old hash split (crc32), so
    the first rebalance moves as few events as it can."""
    fresh = not st["owners"]
    for ev in events:
        st["seen"][ev] = now
    for ev in [e for e, t in st["seen"].items() if now - t > FORGET_S]:
        st["seen"].pop(ev, None)
        st["owners"].pop(ev, None)
    cost = _costs(events, spent)
    load_ = [0.0] * len(PROCS)
    for ev in events:
        if ev in st["owners"]:
            load_[st["owners"][ev]] += cost[ev]
    for ev in sorted((e for e in events if e not in st["owners"]), key=lambda e: -cost[e]):
        i = zlib.crc32(ev.encode()) % len(PROCS) if fresh else min(range(len(PROCS)), key=lambda k: load_[k])
        st["owners"][ev] = i
        load_[i] += cost[ev]
    moves: list[tuple[str, int, int]] = []
    if fresh or now - st["rebalanced_at"] >= REBALANCE_S:
        st["rebalanced_at"] = now
        while True:
            hi = max(range(len(PROCS)), key=lambda k: load_[k])
            lo = min(range(len(PROCS)), key=lambda k: load_[k])
            gap = load_[hi] - load_[lo]
            if gap <= GAP * sum(load_) or gap <= 0:
                break
            # The heavy side's event that best closes the gap without overshooting it.
            pick = [e for e in events if st["owners"].get(e) == hi and 0 < cost[e] < gap]
            if not pick:
                break
            ev = min(pick, key=lambda e: abs(gap / 2 - cost[e]))
            st["owners"][ev] = lo
            load_[hi] -= cost[ev]
            load_[lo] += cost[ev]
            moves.append((ev, hi, lo))
    st["load"] = [round(x, 1) for x in load_]
    return moves


class EventSpend:
    """Write tokens a process spends per event per UTC day, kept SPEND_DAYS
    days in <root>/event_spend.json for update()'s costs."""

    def __init__(self, root: str):
        self.path = os.path.join(root, SPEND)
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.by_day: dict[str, dict[str, float]] = json.load(fh)
        except (OSError, ValueError):
            self.by_day = {}

    def add(self, event: str, tokens: float, now: float | None = None):
        if not event:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
        d = self.by_day.setdefault(day, {})
        d[event] = d.get(event, 0.0) + tokens

    def snapshot(self) -> dict[str, dict[str, float]]:
        keep = sorted(self.by_day)[-SPEND_DAYS:]
        self.by_day = {k: self.by_day[k] for k in keep}
        return {k: {e: round(t, 1) for e, t in v.items()} for k, v in self.by_day.items()}

    def write(self, snap: dict):
        atomic_write_json(self.path, snap, indent=None)
