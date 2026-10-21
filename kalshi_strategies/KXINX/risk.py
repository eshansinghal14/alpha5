"""
Combined settlement risk across every S&P series priced off one expiry.

KXINXU and KXINXDUD settle on the same 16:00 close, so their
positions are one bet on where the index lands. Each series' own
max_loss_usd marks only its own book; this adds up what every position
would pay at each possible close and finds the worst one.
"""
from __future__ import annotations

import dataclasses

import numpy as np


@dataclasses.dataclass(frozen=True)
class Leg:
    """One market's holdings: yes and no contracts, dollars paid for both
    including fees, and Kalshi's yes condition (strike_type with
    floor/cap; None = open-ended)."""
    ticker: str
    yes_qty: float
    no_qty: float
    cost: float
    floor: float | None
    cap: float | None
    strike_type: str | None = None


def _pays_yes(leg: Leg, s: np.ndarray) -> np.ndarray:
    # Strictness matters at the boundary itself: KXINXDUD "greater 7808.69"
    # pays no at exactly 7808.69, KXINXU "greater_or_equal 7945" pays yes.
    st = leg.strike_type
    ok = np.ones_like(s, dtype=bool)
    if leg.floor is not None:
        ok &= (s > leg.floor) if st == "greater" else (s >= leg.floor)
    if leg.cap is not None:
        ok &= (s < leg.cap) if st == "less" else (s <= leg.cap)
    return ok


def scenario_grid(legs: list[Leg]) -> np.ndarray:
    """Closes that hit every payoff region: each boundary, a hair either
    side, and both tails. Payoffs are constant between boundaries."""
    b = sorted({x for leg in legs for x in (leg.floor, leg.cap) if x is not None})
    if not b:
        return np.array([0.0])
    eps = 1e-3
    pts = {b[0] - 100.0, b[-1] + 100.0}
    for x in b:
        pts |= {x - eps, x, x + eps}
    return np.array(sorted(pts))


def settlement_pnl(legs: list[Leg], s: np.ndarray) -> np.ndarray:
    out = np.zeros_like(s, dtype=float)
    for leg in legs:
        yes = _pays_yes(leg, s)
        out += leg.yes_qty * yes + leg.no_qty * ~yes - leg.cost
    return out


def worst_case(legs: list[Leg]) -> tuple[float, float | None]:
    """(worst settlement PnL, a close where it happens)."""
    legs = [lg for lg in legs if lg.yes_qty or lg.no_qty or lg.cost]
    if not legs:
        return 0.0, None
    s = scenario_grid(legs)
    pnl = settlement_pnl(legs, s)
    i = int(np.argmin(pnl))
    return float(pnl[i]), float(s[i])


def blocked_sides(legs: list[Leg], markets: dict[str, tuple[float | None, float | None, str | None]],
                  limit_usd: float | None, prices: dict[str, float]) -> dict[str, str]:
    """Nothing when limit_usd is None (the combined limit is off).

    When the combined worst case is at or past -limit_usd: every
    "TICKER:yes" / "TICKER:no" side in `markets` (ticker -> floor, cap,
    strike_type) whose next contract would make it worse, mapped to the
    reason. Sides that improve the worst case stay open so the book can be
    worked down.

    A contract is priced at `prices[ticker]` (the fair, 0.5 if missing): a
    yes costs p, a no costs 1 - p."""
    if limit_usd is None:
        return {}
    worst, at = worst_case(legs)
    if worst > -limit_usd:
        return {}
    reason = f"combined_risk (worst {worst:+.2f} if close {at:.2f}; limit -{limit_usd:g})"
    out = {}
    for ticker, (floor, cap, st) in markets.items():
        p = prices.get(ticker, 0.5)
        for side, leg in (("yes", Leg(ticker, 1.0, 0.0, p, floor, cap, st)),
                          ("no", Leg(ticker, 0.0, 1.0, 1.0 - p, floor, cap, st))):
            if worst_case(legs + [leg])[0] < worst - 1e-9:
                out[f"{ticker}:{side}"] = reason
    return out
