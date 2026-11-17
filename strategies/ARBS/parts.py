"""Which of Kalshi shard 0's processes (PROCS) owns each event, so they carry
about equal write load. One Python loop tops out at a core; a fixed hash
split left ARBS at its 150 tokens/s cap on ~250 hot baskets while ARBS0B
spent ~95 on ~1,190 (11-15 22:43-53 UTC).

The first process's scan is the only writer (update): a new event goes to
the process whose listed events cost least, priced by its series' measured
cost per event; once every REBALANCE_S, or at once when PROCS changes,
events move from the costliest process to the cheapest while they differ
by more than GAP of the total, at most MAX_MOVE_FRAC of the events a pass.
An event's cost is half its share of the group's write tokens and half its
share of event-loop CPU seconds (evaluating its baskets and handling its
book updates), over the last two UTC days, from each process's
event_spend.json and event_cpu.json (EventSpend); book-update counts
(event_msgs.json) stand in where no CPU is measured yet. Writes alone
balanced ARBS0C onto the write-heavy index ladders, whose CPU held its loop
over target at 1,000 markets while ARBS0B idled at 51% (11-17); update
counts came out even across the three while their loops ran 70-93%.
An event an owner lists but its CPU-held scan leaves out (scan_dropped.json)
goes to the cheapest process with loop CPU to spare: nobody traded 1,101 of
them (11-17). An event any process holds a basket of is never moved. A
process holding a basket of an event another owns only finishes it.
"""
from __future__ import annotations

import json
import os
import time
import zlib

from infra.config import atomic_write_json
from infra.runtime.persist import LIVE_ROOT

# A third process since 11-16: two loops ran at ~70% of a core on quoting
# alone, and the CPU-held market count had cut shard 0 to ~4,700 markets.
PROCS = ("ARBS", "ARBS0B", "ARBS0C")
# Shard 3 split the same way since 11-17: one loop ran at ~89% of a core and its
# CPU-held market count was shedding markets. Its own owners map, under ARBS3.
SHARD3_PROCS = ("ARBS3", "ARBS3B")
GAP = 0.10
# Hourly, a twentieth of the events at most: a moved event's quotes are
# cancelled by its old owner and placed by its new one at its next scan.
REBALANCE_S = 3600.0
MAX_MOVE_FRAC = 0.05
# A process takes another's dropped events only below this loop CPU (its
# controller holds 0.70): one at its target would drop them again.
ROOM_LOOP_CPU = 0.65
# An event not listed for this long is forgotten (its baskets have settled).
FORGET_S = 3 * 86400.0
OWNERS = "shard0_owners.json"
SPEND = "event_spend.json"
MSGS = "event_msgs.json"
CPU = "event_cpu.json"
# Loop seconds the group must have timed before CPU replaces update counts:
# minutes of it after a deploy would rebalance on noise.
CPU_MIN_S = 3600.0
DROPPED = "scan_dropped.json"
DROPPED_STALE_S = 900.0
SPEND_DAYS = 3


def groups() -> tuple[tuple[str, ...], ...]:
    return (PROCS, SHARD3_PROCS)


def group_of(name: str) -> tuple[str, ...] | None:
    """The processes splitting `name`'s shard, or None for one that splits nothing."""
    return next((g for g in groups() if name in g), None)


def owners_path(procs: tuple[str, ...] | None = None) -> str:
    procs = procs or PROCS
    return os.path.join(LIVE_ROOT, procs[0], OWNERS if procs[0] == PROCS[0] else f"{procs[0].lower()}_owners.json")


def event_of(basket_id: str) -> str:
    """A scanned basket's event (mecnet:EVENT:..., ladder:EVENT:..., complete_set:EVENT:...)."""
    parts = basket_id.split(":")
    return parts[1] if len(parts) > 2 else ""


def series_of(event: str) -> str:
    return event.split("-", 1)[0]


def load(path: str | None = None, procs: tuple[str, ...] | None = None) -> dict:
    try:
        with open(path or owners_path(procs), encoding="utf-8") as fh:
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


def spend(days: int = 2, root: str | None = None, procs: tuple[str, ...] | None = None,
          name: str = SPEND) -> dict[str, float]:
    """event -> write tokens (name=MSGS: book updates) every process of
    `procs` (shard 0's) counted on it over the last `days` UTC days."""
    out: dict[str, float] = {}
    for p in procs or PROCS:
        try:
            with open(os.path.join(root or LIVE_ROOT, p, name), encoding="utf-8") as fh:
                by_day = json.load(fh)
        except (OSError, ValueError):
            continue
        for day in sorted(by_day)[-days:]:
            for ev, t in by_day[day].items():
                out[ev] = out.get(ev, 0.0) + float(t)
    return out


def _costs(events: list[str], spent: dict[str, float]) -> dict[str, float]:
    """Each event's cost: its own measured spend, else its series' mean over
    all its listed events, unspent ones counting 0. Over measured events
    alone, the ~13k shard-0 events never quoted took the quoted ones' mean
    and swamped the real loads (11-15 seed)."""
    tot: dict[str, float] = {}
    n: dict[str, int] = {}
    for ev in events:
        s = series_of(ev)
        tot[s] = tot.get(s, 0.0) + spent.get(ev, 0.0)
        n[s] = n.get(s, 0) + 1
    return {ev: spent[ev] if ev in spent else tot[series_of(ev)] / n[series_of(ev)] for ev in events}


def _mixed(events: list[str], spent: dict[str, float], msgs: dict[str, float] | None) -> dict[str, float]:
    """Each event's cost in write tokens: its writes, or with book-update
    counts, half its share of writes and half its share of updates, scaled
    to the writes' total."""
    w = _costs(events, spent)
    if not msgs:
        return w
    m = _costs(events, msgs)
    tw, tm = sum(w.values()), sum(m.values())
    if tw <= 0 or tm <= 0:
        return w if tw > 0 else {e: m[e] for e in events}
    return {e: 0.5 * w[e] + 0.5 * m[e] * tw / tm for e in events}


def held_events(procs: tuple[str, ...], root: str | None = None) -> set[str]:
    """Events any of `procs` holds a basket of (real or shadow ledger)."""
    out: set[str] = set()
    for p in procs:
        for f in ("ledger.json", "ledger_shadow.json"):
            try:
                with open(os.path.join(root or LIVE_ROOT, p, f), encoding="utf-8") as fh:
                    out |= {event_of(b) for b in (json.load(fh).get("baskets") or {})}
            except (OSError, ValueError):
                pass
    out.discard("")
    return out


def dropped(procs: tuple[str, ...], now: float, root: str | None = None) -> dict[int, set[str]]:
    """Process index -> events its last scan listed as its own but left out."""
    out: dict[int, set[str]] = {}
    for i, p in enumerate(procs):
        try:
            with open(os.path.join(root or LIVE_ROOT, p, DROPPED), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if now - float(d.get("t", 0.0)) <= DROPPED_STALE_S:
            out[i] = set(d.get("events") or [])
    return out


def room(procs: tuple[str, ...], now: float, root: str | None = None) -> set[int]:
    """Indices of processes whose fresh summary shows loop CPU under ROOM_LOOP_CPU."""
    out: set[int] = set()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    for i, p in enumerate(procs):
        path = os.path.join(root or LIVE_ROOT, p, day, "summary.json")
        try:
            if now - os.path.getmtime(path) > 300.0:       # written every 2 s while it runs
                continue
            with open(path, encoding="utf-8") as fh:
                cpu = json.load(fh).get("loop_cpu")
        except (OSError, ValueError):
            continue
        if cpu is not None and float(cpu) < ROOM_LOOP_CPU:
            out.add(i)
    return out


def update(st: dict, events: list[str], now: float, spent: dict[str, float],
           procs: tuple[str, ...] | None = None, msgs: dict[str, float] | None = None,
           held: set[str] = frozenset(), drops: dict[int, set[str]] | None = None,
           spare: set[int] = frozenset()) -> list[tuple[str, int, int]]:
    """Assigns `events` (shard 0's listed now) not yet owned, forgets ones
    long unlisted, and rebalances when due. Returns the moves (event, from,
    to). A state with no owners starts from the old hash split (crc32), so
    the first rebalance moves as few events as it can."""
    PROCS = procs or globals()["PROCS"]     # noqa: N806 (shard 0's unless given)
    fresh = not st["owners"]
    resized = st.get("procs", 2) != len(PROCS)
    # An owner past the end of PROCS (a process removed) owns nothing.
    st["owners"] = {e: i for e, i in st["owners"].items() if i < len(PROCS)}
    for ev in events:
        st["seen"][ev] = now
    for ev in [e for e, t in st["seen"].items() if now - t > FORGET_S]:
        st["seen"].pop(ev, None)
        st["owners"].pop(ev, None)
    cost = _mixed(events, spent, msgs)
    load_ = [0.0] * len(PROCS)
    for ev in events:
        if ev in st["owners"]:
            load_[st["owners"][ev]] += cost[ev]
    for ev in sorted((e for e in events if e not in st["owners"]), key=lambda e: -cost[e]):
        i = zlib.crc32(ev.encode()) % len(PROCS) if fresh else min(range(len(PROCS)), key=lambda k: load_[k])
        st["owners"][ev] = i
        load_[i] += cost[ev]
    moves: list[tuple[str, int, int]] = []
    cap = len(events) if (fresh or resized) else max(1, int(MAX_MOVE_FRAC * len(events)))
    if fresh or resized or now - st["rebalanced_at"] >= REBALANCE_S:
        st["rebalanced_at"] = now
        while len(moves) < cap:
            hi = max(range(len(PROCS)), key=lambda k: load_[k])
            lo = min(range(len(PROCS)), key=lambda k: load_[k])
            gap = load_[hi] - load_[lo]
            if gap <= GAP * sum(load_) or gap <= 0:
                break
            # The heavy side's event that best closes the gap without overshooting it.
            pick = [e for e in events if st["owners"].get(e) == hi and 0 < cost[e] < gap and e not in held]
            if not pick:
                break
            ev = min(pick, key=lambda e: abs(gap / 2 - cost[e]))
            st["owners"][ev] = lo
            load_[hi] -= cost[ev]
            load_[lo] += cost[ev]
            moves.append((ev, hi, lo))
    # Listed events an owner's CPU-held scan left out, to the cheapest process
    # with loop CPU to spare. Untraded where they are, so moving them stops no
    # quotes; capped like the rebalance.
    listed = set(events)
    for i, evs in sorted((drops or {}).items()):
        for ev in sorted(evs):
            if len(moves) >= 2 * cap:
                break
            if ev not in listed or ev in held or st["owners"].get(ev) != i:
                continue
            to = [k for k in spare if k != i and k < len(PROCS)]
            if not to:
                break
            k = min(to, key=lambda x: load_[x])
            st["owners"][ev] = k
            load_[i] -= cost[ev]
            load_[k] += cost[ev]
            moves.append((ev, i, k))
    st["load"] = [round(x, 1) for x in load_]
    st["procs"] = len(PROCS)
    return moves


class EventSpend:
    """Write tokens a process spends per event per UTC day, kept SPEND_DAYS
    days in <root>/event_spend.json for update()'s costs."""

    def __init__(self, root: str, name: str = SPEND, dp: int = 1):
        self.path = os.path.join(root, name)
        self.dp = dp
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
        return {k: {e: round(t, self.dp) for e, t in v.items()} for k, v in self.by_day.items()}

    def write(self, snap: dict):
        atomic_write_json(self.path, snap, indent=None)
