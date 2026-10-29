"""Per-market liquidity log, for rules on completion risk: a basket is only
as completable as its thinnest leg. One row per market per window, appended
to data/live/ARBS/liquidity/YYYY-MM-DD.jsonl.gz.

Per side we can buy (yes ask, no ask = 1 - yes bid): the touch at the
window's end, its thinnest depth during the window, how often it worsened
(a lift or a pull: the price we'd have completed at went away), and how long
it took to come back to that price. Plus book updates, prints and contracts
traded."""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
import statistics

_EPS = 1e-9
# A worsened ask not back within this long counts as unrefilled: past it a
# completion has had to pay up anyway.
REFILL_CAP_S = 300.0


@dataclasses.dataclass
class _Side:
    ask: float | None = None
    depth: float = 0.0
    min_depth: float | None = None
    worse_at: float | None = None       # when the ask last worsened, until it comes back
    worse_from: float | None = None
    worsened: int = 0
    refills: list = dataclasses.field(default_factory=list)
    unrefilled: int = 0

    def update(self, ask: float | None, depth: float | None, now: float):
        depth = (depth or 0.0) if ask is not None else 0.0
        self.expire(now)
        if self.worse_at is not None and ask is not None and ask <= self.worse_from + _EPS:
            self.refills.append(now - self.worse_at)
            self.worse_at = None
        if self.worse_at is None and self.ask is not None and (ask is None or ask > self.ask + _EPS):
            self.worsened += 1
            self.worse_at, self.worse_from = now, self.ask
        self.ask, self.depth = ask, depth
        self.min_depth = depth if self.min_depth is None else min(self.min_depth, depth)

    def expire(self, now: float):
        if self.worse_at is not None and now - self.worse_at > REFILL_CAP_S:
            self.unrefilled += 1
            self.worse_at = None

    def row(self, p: str) -> dict:
        r = self.refills
        return {f"{p}_ask": self.ask, f"{p}_depth": round(self.depth, 2),
                f"{p}_min_depth": None if self.min_depth is None else round(self.min_depth, 2),
                f"{p}_worsened": self.worsened, f"{p}_refills": len(r),
                f"{p}_refill_p50_s": round(statistics.median(r), 3) if r else None,
                f"{p}_refill_max_s": round(max(r), 3) if r else None, f"{p}_unrefilled": self.unrefilled}

    def reset(self):
        self.min_depth = self.depth if self.ask is not None else None
        self.worsened, self.refills, self.unrefilled = 0, [], 0


@dataclasses.dataclass
class _Market:
    yes: _Side = dataclasses.field(default_factory=_Side)
    no: _Side = dataclasses.field(default_factory=_Side)
    updates: int = 0
    prints: int = 0
    contracts: float = 0.0


class Liquidity:
    def __init__(self, root: str):
        self.dir = os.path.join(root, "liquidity")
        self.m: dict[str, _Market] = {}

    def on_book(self, ticker: str, now: float, yes_ask, yes_depth, yes_bid, yes_bid_depth):
        """`now` monotonic seconds. The no ask is 1 - yes bid, its depth the yes bid's."""
        m = self.m.setdefault(ticker, _Market())
        m.updates += 1
        m.yes.update(yes_ask, yes_depth, now)
        m.no.update(None if yes_bid is None else round(1.0 - yes_bid, 4), yes_bid_depth, now)

    def on_trade(self, ticker: str, size: float):
        m = self.m.setdefault(ticker, _Market())
        m.prints += 1
        m.contracts += size

    def flush(self, now: float, wall: float) -> list[dict]:
        """One row per market seen so far; counters restart."""
        rows = []
        for t, m in self.m.items():
            m.yes.expire(now)
            m.no.expire(now)
            spread = None if m.yes.ask is None or m.no.ask is None else round(m.yes.ask + m.no.ask - 1.0, 4)
            rows.append({"t": round(wall, 1), "ticker": t, "updates": m.updates, "prints": m.prints,
                         "contracts": round(m.contracts, 2), "spread": spread, **m.yes.row("yes"),
                         **m.no.row("no")})
            m.updates, m.prints, m.contracts = 0, 0, 0.0
            m.yes.reset()
            m.no.reset()
        return rows

    def write(self, rows: list[dict], day: str):
        """Blocking."""
        os.makedirs(self.dir, exist_ok=True)
        with gzip.open(os.path.join(self.dir, f"{day}.jsonl.gz"), "at", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
