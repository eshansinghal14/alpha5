"""
Write groups: series that run in one process and draw on ONE write budget
(Series.write_group). Members share the token bucket (real: WriteBudget;
shadow: the group's modelled bucket), one cancels_token reserve, and one
quote priority queue (order_manager.WriteGroup).

Keyed by (group name, exchange shard): Kalshi bills each shard's writes to
its own bucket, so series on different shards can't share one.
"""
from __future__ import annotations

import dataclasses
import threading

from kalshi_infra.kalshi.orders import WRITE_HEADROOM, WriteBudget, process_write_tokens_per_s
from kalshi_infra.trading.order_manager import WriteGroup

# Basic tier refill rate (2026-10-09), used when the account limit can't be
# read: shadow still models a realistic bucket.
FALLBACK_TOKENS_PER_S = 100.0


@dataclasses.dataclass
class Shared:
    budget: WriteBudget
    group: WriteGroup


_LOCK = threading.Lock()
_GROUPS: dict[tuple[str, int | None], Shared] = {}


def get(name: str, shard: int | None) -> Shared:
    """The process-wide shared budget for (name, shard), created on first
    use from this process's share of the account budget. Read once: a tier
    change applies on restart."""
    key = (name, shard)
    with _LOCK:
        s = _GROUPS.get(key)
        if s is not None:
            return s
    try:
        rate = process_write_tokens_per_s() if shard is not None else FALLBACK_TOKENS_PER_S
    except Exception as e:
        print(f"[write_group {name}] WARN: account write limit unreadable ({e!r}); "
              f"modelling {FALLBACK_TOKENS_PER_S:g} tokens/s", flush=True)
        rate = FALLBACK_TOKENS_PER_S
    with _LOCK:
        s = _GROUPS.get(key)
        if s is None:
            budget = WriteBudget(rate)
            s = _GROUPS[key] = Shared(budget, WriteGroup(sim_tokens_per_s=rate * WRITE_HEADROOM))
            print(f"[write_group {name}] shard {shard}: one bucket of {budget.rate:g} tokens/s "
                  f"shared by its series", flush=True)
        return s


def reset():
    """Tests only."""
    with _LOCK:
        _GROUPS.clear()
