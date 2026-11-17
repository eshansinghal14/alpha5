"""Basket arithmetic, all per bundle (one contract of every leg), in dollars.

Prices are in each leg's OWN side terms: buying NO at 0.30 costs 0.30.
Fees use the per-contract Kalshi formula, which can only overstate an
order's fee (Kalshi ceils the whole order's fee, not each contract's).
"""
from __future__ import annotations

import dataclasses
import functools
import math

from infra.kalshi.fees import fee

_EPS = 1e-9


_TICKS: dict[tuple, "Ticks"] = {}    # price_ranges -> its Ticks (from_market)


@dataclasses.dataclass(frozen=True)
class Ticks:
    """A market's price grid, from its `price_ranges`: (start, end, step)
    covering 0..1. tapered_deci_cent markets step 0.1c below 10c and above
    90c, 1c between. Prices are in one side's terms; the grid is symmetric,
    so it holds for YES and NO alike."""
    ranges: tuple[tuple[float, float, float], ...] = ((0.0, 1.0, 0.01),)

    @classmethod
    def from_market(cls, price_ranges: list[dict] | None) -> "Ticks":
        """One shared Ticks per distinct grid: rebuilt for every market each
        meta refresh, it was ~10% of that thread's GIL time (py-spy 11-16)."""
        if not price_ranges:
            return cls()
        key = tuple((r["start"], r["end"], r["step"]) for r in price_ranges)
        got = _TICKS.get(key)
        if got is None:
            got = _TICKS[key] = cls(tuple(sorted((float(a), float(b), float(c)) for a, b, c in key)))
        return got

    # Cached: read on every quote_leg, the round() was 2-3% of a busy ARBS
    # loop (py-spy 11-17 18:48).
    @functools.cached_property
    def lo(self) -> float:
        s, _, st = self.ranges[0]
        return round(s + st, 4)

    @functools.cached_property
    def hi(self) -> float:
        _, e, st = self.ranges[-1]
        return round(e - st, 4)

    def floor(self, p: float) -> float:
        """Highest grid price <= p."""
        for s, e, st in self.ranges:
            if p < e - _EPS or (s, e, st) == self.ranges[-1]:
                return round(s + math.floor((p - s) / st + 1e-6) * st, 4)
        raise AssertionError

    def down(self, p: float) -> float:
        return self.floor(p - 1e-6)

    def up(self, p: float) -> float:
        for s, e, st in self.ranges:
            if p < e - _EPS:
                return round(min(e, s + (math.floor((p - s) / st + 1e-6) + 1) * st), 4)
        return round(p, 4)

    def steps(self, a: float, b: float) -> int:
        """Grid steps between two grid prices."""
        lo, hi, n = min(a, b), max(a, b), 0
        while lo < hi - _EPS:
            lo, n = self.up(lo), n + 1
        return n


CENT = Ticks()


@dataclasses.dataclass
class LegView:
    """One leg's book from our side, with our own resting orders removed.
    `asks`: [(price, qty)] to BUY this side, cheapest first. `bid`: best
    resting bid on this side (where our quote would join), or None."""
    asks: list[tuple[float, float]]
    bid: float | None
    taker_rate: float
    maker_rate: float
    ticks: Ticks = CENT

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def ladder_price(self, n: float) -> float | None:
        """Worst price an IOC must reach to buy `n`, or None if the book
        holds less than n."""
        got = 0.0
        for p, q in self.asks:
            got += q
            if got >= n - _EPS:
                return p
        return None

    def depth(self) -> float:
        return sum(q for _, q in self.asks)


def taker_cost(p: float, rate: float) -> float:
    return p + fee(p, 1.0, rate)


def maker_cost(p: float, rate: float) -> float:
    return p + fee(p, 1.0, rate)


def max_price_within(budget: float, rate: float, ticks: Ticks = CENT) -> float | None:
    """Highest grid price p with p + fee(p) <= budget, or None below the
    lowest one."""
    p = ticks.floor(min(ticks.hi, budget + _EPS))
    while p >= ticks.lo - _EPS:
        if p + fee(p, 1.0, rate) <= budget + _EPS:
            return p
        p = ticks.down(p)
    return None


def gap(legs: list[LegView], payout: float) -> float | None:
    """payout - sum(best ask + taker fee): the edge of taking one bundle now.
    None if any leg has no ask. The ranking key."""
    total = 0.0
    for lv in legs:
        if lv.best_ask is None:
            return None
        total += taker_cost(lv.best_ask, lv.taker_rate)
    return payout - total


@dataclasses.dataclass
class TakePlan:
    size: float
    limits: list[float]          # per-leg IOC limit (worst level reached)
    edge: float                  # edge of the WORST bundle in the plan
    cost: float                  # dollars, all bundles, limits + fees (upper bound)


def take_plan(legs: list[LegView], payout: float, min_edge: float, max_size: int) -> TakePlan | None:
    """Bundles to take now, every leg at its touch: as many as the thinnest
    touch holds (capped at max_size), if the touch clears min_edge. None if it
    doesn't.

    Never walks a leg past its touch even when deeper levels would still clear
    min_edge: a thin touch often refills at the same price, so the runner takes
    it, waits take_cooldown_s and takes again. The one exception is a touch
    under one contract, where the IOC may reach the level that completes one
    bundle (whole bundles only)."""
    if any(not lv.asks for lv in legs):
        return None
    touch = int(min(min(lv.asks[0][1] for lv in legs), max_size) + _EPS)
    if touch >= 1:
        lim = [lv.asks[0][0] for lv in legs]
        e = payout - sum(taker_cost(p, lv.taker_rate) for p, lv in zip(lim, legs))
        if e < min_edge - _EPS:
            return None
        return TakePlan(size=float(touch), limits=lim, edge=e,
                        cost=touch * sum(taker_cost(p, lv.taker_rate) for p, lv in zip(lim, legs)))
    best = None
    for n in range(1, min(1, int(max_size)) + 1):
        lim = [lv.ladder_price(n) for lv in legs]
        if any(p is None for p in lim):
            break
        e = payout - sum(taker_cost(p, lv.taker_rate) for p, lv in zip(lim, legs))
        if e < min_edge - _EPS:
            break
        best = TakePlan(size=float(n), limits=lim, edge=e,
                        cost=n * sum(taker_cost(p, lv.taker_rate) for p, lv in zip(lim, legs)))
    return best


@dataclasses.dataclass
class QuotePlan:
    leg: int
    price: float
    max_price: float             # highest price that still clears the quote edge
    edge: float                  # edge if filled at `price` and completed at today's asks
    others_cost: float           # per bundle, other legs at the levels `size` needs


def quote_leg(legs: list[LegView], j: int, payout: float, required_edge: float, size: float) -> QuotePlan | None:
    """Price to rest on leg j so that a fill, completed by taking every other
    leg at the levels `size` bundles needs, still makes `required_edge`.

    Joins one tick above the best bid (never above the edge-limited price,
    never at or through the ask, which post_only would reject). None if no
    price works or another leg can't fill `size` now."""
    others = others_cost(legs, j, size)
    return None if others is None else plan_at(legs, j, payout, required_edge, others)


def others_cost(legs: list[LegView], j: int, size: float) -> float | None:
    """Per bundle, every leg but j taken at the level `size` bundles reach,
    fees in; None if one can't fill `size`."""
    others = 0.0
    for i, lv in enumerate(legs):
        if i == j:
            continue
        p = lv.ladder_price(size)
        if p is None:
            return None
        others += taker_cost(p, lv.taker_rate)
    return others


def plan_at(legs: list[LegView], j: int, payout: float, required_edge: float, others: float) -> QuotePlan | None:
    """quote_leg given others_cost: one ladder walk can price every edge."""
    lv = legs[j]
    cap = max_price_within(payout - required_edge - others, lv.maker_rate, lv.ticks)
    if cap is None:
        return None
    if lv.best_ask is not None:
        cap = min(cap, lv.ticks.down(lv.best_ask))
    if cap < lv.ticks.lo - _EPS:
        return None
    price = cap if lv.bid is None else min(cap, lv.ticks.up(lv.bid))
    price = max(price, lv.ticks.lo)
    return QuotePlan(leg=j, price=price, max_price=cap,
                     edge=payout - others - maker_cost(price, lv.maker_rate), others_cost=others)


def depth_size(legs: list[LegView], j: int, payout: float, required_edge: float, cap: float,
               at: float | None = None) -> float:
    """Largest whole size, up to `cap`, that leg j can rest at price `at`
    (default: its smallest quote's price) and still have a fill completed
    through the other legs' books (each taken at the worst level that size
    reaches) for `required_edge`. 0 if not even 1 works."""
    return depth_sizes(legs, j, payout, (required_edge,), cap, at)[0]


def depth_sizes(legs: list[LegView], j: int, payout: float, edges, cap: float,
                at: float | None = None) -> list[float]:
    """depth_size at each of `edges`. Each size's ladder walk is shared by
    every edge, and a probe computes only the limit price, not a whole plan:
    model_pick values 7 edges, and its depth_size calls were 7-9% of a busy
    ARBS loop (py-spy 11-17)."""
    if cap < 1:
        return [0.0] * len(edges)
    lv = legs[j]
    ticks, rate, lo = lv.ticks, lv.maker_rate, lv.ticks.lo
    ask_cap = None if lv.best_ask is None else ticks.down(lv.best_ask)
    walked: dict[float, float | None] = {}

    def limit(e: float, n: float) -> float | None:
        """plan_at's max_price at size n, None where quote_leg is None."""
        if n in walked:
            o = walked[n]
        else:
            o = walked[n] = others_cost(legs, j, n)
        if o is None:
            return None
        c = max_price_within(payout - e - o, rate, ticks)
        if c is None:
            return None
        if ask_cap is not None and ask_cap < c:
            c = ask_cap
        return None if c < lo - _EPS else c

    # Sizes where some other leg's worst level changes, then the cap.
    marks = {float(cap)}
    for i, other in enumerate(legs):
        if i == j:
            continue
        got = 0.0
        for _, q in other.asks:
            got += q
            if got >= cap:
                break
            marks.add(float(int(got + _EPS)))
    # A bigger size only reaches worse levels on the other legs, so whether
    # it still works is monotone: binary search, not one quote_leg per mark
    # (depth_size was ~2% of ARBS's busy loop, py-spy 11-17).
    ns = sorted(m for m in marks if m >= 1)
    out = []
    for e in edges:
        c1 = limit(e, 1.0)
        if c1 is None:
            out.append(0.0)
            continue
        if at is not None:
            price = at
        else:
            price = c1 if lv.bid is None else min(c1, ticks.up(lv.bid))
            price = max(price, lo)
        if c1 < price - _EPS:
            out.append(0.0)
            continue
        i_lo, i_hi, best = 0, len(ns) - 1, 1.0
        while i_lo <= i_hi:
            mid = (i_lo + i_hi) // 2
            c = limit(e, ns[mid])
            if c is None or c < price - _EPS:
                i_hi = mid - 1
            else:
                best, i_lo = ns[mid], mid + 1
        out.append(best)
    return out


def second_level_size(legs: list[LegView], j: int, payout: float, cap: float, at: float) -> float:
    """depth_size for leg j resting at `at` with every other leg's best ask
    level gone and the completion asked only not to lose: what a fill there
    still completes when the level it was sized on was taken with it."""
    rest = [lv if i == j else dataclasses.replace(lv, asks=lv.asks[1:]) for i, lv in enumerate(legs)]
    return depth_size(rest, j, payout, 0.0, cap, at=at)


def touch_size(legs: list[LegView], j: int) -> float:
    """Contracts every leg but j offers at its best ask: what a fill on leg j
    can be completed with without walking past the touch."""
    return min((lv.asks[0][1] if lv.asks else 0.0 for i, lv in enumerate(legs) if i != j), default=0.0)


def buy_cost(lv: LegView | None, n: float) -> tuple[float, float] | None:
    """(dollars incl. the taker fee, worst price reached) to buy `n` now, or
    None if the book holds less (or the leg is closed: lv None)."""
    if lv is None:
        return None
    got, cost = 0.0, 0.0
    for p, q in lv.asks:
        k = min(q, n - got)
        got += k
        cost += k * p
        if got >= n - _EPS:
            return cost + fee(cost / n, n, lv.taker_rate), p
    return None


def rest_price(lv: LegView) -> float | None:
    """A post-only bid one tick above the best bid, never at or through the ask."""
    p = lv.ticks.lo if lv.bid is None else lv.ticks.up(lv.bid)
    if lv.best_ask is not None:
        p = min(p, lv.ticks.down(lv.best_ask))
    return p if p >= lv.ticks.lo - _EPS else None


@dataclasses.dataclass
class Exits:
    need: list[float]                   # contracts each leg buys to complete
    excess: list[float]                 # contracts each leg holds beyond complete bundles
    complete: float | None              # $ lost completing at the asks now; None if a missing leg can't fill
    unwind: float | None                # $ lost netting the excess out at the other sides' asks now
    complete_limits: list[float | None]
    unwind_limits: list[float | None]

    @property
    def target(self) -> float:
        return max(self.excess)

    def cheaper(self) -> tuple[str, float] | None:
        got = [(k, v) for k, v in (("complete", self.complete), ("unwind", self.unwind)) if v is not None]
        return min(got, key=lambda kv: kv[1]) if got else None


def exits(payout: float, held: list[float], spent: float, legs: list[LegView | None],
          others: list[LegView | None]) -> Exits:
    """The two ways out of a broken basket, as losses on `spent` (what the
    contracts beyond complete bundles cost, fees included):

      complete  buy each leg's `need` at its asks; the target bundles then
                pay target * payout.
      unwind    buy the other side of each leg's `excess`: Kalshi pays $1 a
                YES/NO pair back at once.

    legs[j] / others[j]: leg j's own side and its other side, None when the
    market is closed. A path any leg can't fill in full is None."""
    done = min(held)
    excess = [h - done for h in held]
    target = max(excess)
    need = [target - e for e in excess]

    def walk(views, sizes):
        total, limits = 0.0, [None] * len(sizes)
        for j, n in enumerate(sizes):
            if n <= _EPS:
                continue
            got = buy_cost(views[j], n)
            if got is None:
                return None, limits
            total += got[0]
            limits[j] = got[1]
        return total, limits

    c_cost, c_lim = walk(legs, need)
    u_cost, u_lim = walk(others, excess)
    return Exits(need=need, excess=excess,
                 complete=None if c_cost is None else spent + c_cost - target * payout,
                 unwind=None if u_cost is None else spent - (sum(excess) - u_cost),
                 complete_limits=c_lim, unwind_limits=u_lim)
