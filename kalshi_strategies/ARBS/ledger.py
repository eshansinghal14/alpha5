"""Per-basket holdings, persisted to disk.

Kalshi only knows a net position per ticker, so which basket a contract
belongs to exists nowhere but here. These positions live for weeks, so the
ledger is saved after every change and reloaded at start; the audit checks
its per-ticker sum against Kalshi.

Kalshi nets YES against NO on one ticker (and credits $1 per pair). Two
baskets holding opposite sides of the same ticker are still each correct
here: the pair's $1 is what the two legs would have paid at settlement.
"""
from __future__ import annotations

import dataclasses
import json
import os

from kalshi_infra.config import atomic_write_json

_EPS = 1e-9


@dataclasses.dataclass
class LegPos:
    side: str = "yes"
    qty: float = 0.0
    cost: float = 0.0        # dollars paid incl. fees

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > _EPS else 0.0


@dataclasses.dataclass
class BasketPos:
    legs: dict[str, LegPos] = dataclasses.field(default_factory=dict)   # ticker -> pos
    broken_since: float | None = None       # unix time the basket first went uneven

    def held(self, tickers: list[str]) -> list[float]:
        return [self.legs.get(t, LegPos()).qty for t in tickers]

    def bundles(self, tickers: list[str]) -> float:
        return min(self.held(tickers)) if tickers else 0.0

    def cost(self) -> float:
        return sum(l.cost for l in self.legs.values())

    def unhedged(self, tickers: list[str]) -> dict[str, float]:
        b = self.bundles(tickers)
        return {t: q - b for t, q in zip(tickers, self.held(tickers)) if q - b > _EPS}

    def broken_spent(self, tickers: list[str]) -> float:
        """Cost (at each leg's average) of the contracts beyond complete bundles."""
        b = self.bundles(tickers)
        return sum(self.legs[t].avg * (self.legs[t].qty - b) for t in tickers
                   if t in self.legs and self.legs[t].qty - b > _EPS)


class Ledger:
    def __init__(self, path: str | None):
        self.path = path
        self.baskets: dict[str, BasketPos] = {}
        # exchange order id -> {basket_id, ticker, side, retired_at}: every
        # quote we placed, so a maker fill (websocket, REST backstop, or one
        # taken while the process was down) finds its basket.
        self.quote_ids: dict[str, dict] = {}
        self.last_fill_ts: float | None = None
        self.seen_trades: set[str] = set()
        self.settled: list[dict] = []
        if path and os.path.exists(path):
            self._load()

    def _load(self):
        with open(self.path, encoding="utf-8") as fh:
            d = json.load(fh)
        for bid, b in d.get("baskets", {}).items():
            self.baskets[bid] = BasketPos(legs={t: LegPos(**l) for t, l in b["legs"].items()},
                                          broken_since=b.get("broken_since"))
        self.quote_ids = d.get("quote_ids", {})
        self.last_fill_ts = d.get("last_fill_ts")
        self.seen_trades = set(d.get("seen_trades", []))
        self.settled = d.get("settled", [])

    def save(self, now: float | None = None):
        if not self.path:
            return
        if now is not None:
            # A retired quote's late fills arrive within seconds; a day is ample.
            self.quote_ids = {k: v for k, v in self.quote_ids.items()
                              if v.get("retired_at") is None or now - v["retired_at"] < 86400}
        atomic_write_json(self.path, {
            "baskets": {bid: {"legs": {t: dataclasses.asdict(l) for t, l in b.legs.items()},
                              "broken_since": b.broken_since}
                        for bid, b in self.baskets.items() if b.legs},
            "quote_ids": self.quote_ids,
            "last_fill_ts": self.last_fill_ts,
            # Bounded: only dedupes quote fills replayed from the REST backstop.
            "seen_trades": sorted(self.seen_trades)[-20000:],
            "settled": self.settled,
        }, indent=None)

    def close(self, basket_id: str, row: dict):
        """Move a fully settled basket to the settled history."""
        self.baskets.pop(basket_id, None)
        self.settled.append(row)
        self.save()

    def pos(self, basket_id: str) -> BasketPos:
        return self.baskets.setdefault(basket_id, BasketPos())

    def record(self, basket_id: str, tickers: list[str], ticker: str, side: str, qty: float, cost: float,
               now: float):
        b = self.pos(basket_id)
        lp = b.legs.setdefault(ticker, LegPos(side=side))
        lp.qty += qty
        lp.cost += cost
        if b.unhedged(tickers):
            b.broken_since = b.broken_since or now
        else:
            b.broken_since = None
        self.save(now)

    def unwind(self, basket_id: str, tickers: list[str], ticker: str, qty: float, now: float) -> float:
        """Net `qty` contracts out of a leg (we bought the other side; Kalshi
        pays $1 a pair). The leg shrinks at its average cost, which is returned."""
        b = self.baskets[basket_id]
        lp = b.legs[ticker]
        qty = min(qty, lp.qty)
        basis = lp.avg * qty
        lp.qty -= qty
        lp.cost -= basis
        if lp.qty <= _EPS:
            del b.legs[ticker]
        if not b.legs:
            del self.baskets[basket_id]
        else:
            b.broken_since = (b.broken_since or now) if b.unhedged(tickers) else None
        self.save(now)
        return basis

    def net_by_ticker(self) -> dict[str, float]:
        """Signed YES position per ticker summed over baskets (NO negative),
        the way Kalshi's positions endpoint reports it."""
        out: dict[str, float] = {}
        for b in self.baskets.values():
            for t, lp in b.legs.items():
                out[t] = out.get(t, 0.0) + (lp.qty if lp.side == "yes" else -lp.qty)
        return out
