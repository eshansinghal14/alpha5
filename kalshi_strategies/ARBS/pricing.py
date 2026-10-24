"""Basket arithmetic, all per bundle (one contract of every leg), in dollars.

Prices are in each leg's OWN side terms: buying NO at 0.30 costs 0.30.
Fees use the per-contract Kalshi formula, which can only overstate an
order's fee (Kalshi ceils the whole order's fee, not each contract's).
"""
from __future__ import annotations

import dataclasses
import math

from kalshi_infra.kalshi.fees import fee

TICK = 0.01
MIN_PRICE = 0.01
MAX_PRICE = 0.99
_EPS = 1e-9


@dataclasses.dataclass
class LegView:
    """One leg's book from our side, with our own resting orders removed.
    `asks`: [(price, qty)] to BUY this side, cheapest first. `bid`: best
    resting bid on this side (where our quote would join), or None."""
    asks: list[tuple[float, float]]
    bid: float | None
    taker_rate: float
    maker_rate: float

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


def max_price_within(budget: float, rate: float) -> float | None:
    """Highest tick price p with p + fee(p) <= budget, or None below the
    minimum tick."""
    p = min(MAX_PRICE, math.floor((budget + _EPS) / TICK) * TICK)
    while p >= MIN_PRICE - _EPS:
        p = round(p, 2)
        if p + fee(p, 1.0, rate) <= budget + _EPS:
            return p
        p -= TICK
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
    """Largest whole number of bundles whose last (most expensive) bundle
    still clears min_edge at the levels the IOCs must reach. None if not even
    one does."""
    best = None
    for n in range(1, int(max_size) + 1):
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
    others = 0.0
    for i, lv in enumerate(legs):
        if i == j:
            continue
        p = lv.ladder_price(size)
        if p is None:
            return None
        others += taker_cost(p, lv.taker_rate)
    lv = legs[j]
    cap = max_price_within(payout - required_edge - others, lv.maker_rate)
    if cap is None:
        return None
    if lv.best_ask is not None:
        cap = min(cap, round(lv.best_ask - TICK, 2))
    if cap < MIN_PRICE - _EPS:
        return None
    price = cap if lv.bid is None else min(cap, round(lv.bid + TICK, 2))
    price = round(max(price, MIN_PRICE), 2)
    return QuotePlan(leg=j, price=price, max_price=cap,
                     edge=payout - others - maker_cost(price, lv.maker_rate), others_cost=others)


def best_quote(legs: list[LegView], payout: float, required_edge: float, size: float,
               current_leg: int | None = None, switch_margin: float = 0.02) -> QuotePlan | None:
    """The leg to quote: the one whose edge-limited price sits highest above
    its own best bid (most likely to be hit). Keeps `current_leg` unless
    another beats it by switch_margin, so the quote doesn't hop legs on
    every tick (each hop is a cancel plus a create)."""
    plans = [p for j in range(len(legs)) if (p := quote_leg(legs, j, payout, required_edge, size)) is not None]
    if not plans:
        return None

    def room(p: QuotePlan) -> float:
        b = legs[p.leg].bid
        return p.max_price - (b if b is not None else 0.0)
    top = max(plans, key=room)
    cur = next((p for p in plans if p.leg == current_leg), None)
    if cur is not None and room(top) < room(cur) + switch_margin:
        return cur
    return top


@dataclasses.dataclass
class Completion:
    need: list[float]                 # contracts each leg still needs
    caps: list[float | None]          # cap_j per contract incl. fee (None = leg done)
    limits: list[float | None]        # IOC limit price to send (None = don't send)
    short: list[int]                  # legs whose cap is below the price their need reaches


def completion(payout: float, max_loss: float, target: float, filled: list[float], spent: float,
               legs: list[LegView]) -> Completion:
    """How to finish a broken basket. With `target` bundles wanted, `filled[i]`
    held on leg i and `spent` dollars (fees included) paid toward them, leg j
    may cost at most

        cap_j = (target * (payout + max_loss) - spent - sum_{i != j} r_i * (ask_i + fee_i)) / r_j

    per contract (fee included), r_i being what leg i still needs: the basket
    then loses at most max_loss per bundle. ask_i is the level r_i reaches, or
    the deepest level when the book is shorter (that IOC fills partially and
    the next attempt recomputes from what was actually spent).

    Every missing leg's IOC goes out at once, so each can't be sent at its own
    cap_j: two legs both filling at their caps would overshoot. Each gets its
    current price plus an equal share of the slack; with one missing leg (any
    2-leg basket) that is exactly cap_j. When the slack is negative nothing is
    sent and the short legs are reported."""
    need = [max(0.0, target - f) for f in filled]
    unit = []
    for lv, r in zip(legs, need):
        if r <= _EPS:
            unit.append(0.0)
            continue
        p = lv.ladder_price(r)
        if p is None:
            p = lv.asks[-1][0] if lv.asks else MAX_PRICE
        unit.append(taker_cost(p, lv.taker_rate))
    budget = target * (payout + max_loss) - spent
    missing = [j for j, r in enumerate(need) if r > _EPS]
    caps: list[float | None] = [None] * len(legs)
    for j in missing:
        rest = sum(need[i] * unit[i] for i in missing if i != j)
        caps[j] = (budget - rest) / need[j]
    slack = budget - sum(need[i] * unit[i] for i in missing)
    limits: list[float | None] = [None] * len(legs)
    short = [j for j in missing if caps[j] < unit[j] - _EPS]
    if slack >= -_EPS and missing:
        for j in missing:
            lv = legs[j]
            if not lv.asks:
                continue
            limits[j] = max_price_within(unit[j] + max(0.0, slack) / (len(missing) * need[j]), lv.taker_rate)
    return Completion(need=need, caps=caps, limits=limits, short=short)
