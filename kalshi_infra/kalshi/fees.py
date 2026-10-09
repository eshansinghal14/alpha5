"""
Kalshi trading fees, resolved per event from the API -- never configured.

Kalshi publishes a fee TYPE and MULTIPLIER per series (GET /series/{ticker}),
optionally overridden per event (GET /events/fee_changes). The fee itself is

    fee = ceil_to_0.0001( rate * multiplier * contracts * p * (1 - p) )

with the base rates below. Verified against real fills on this account
(2026-10-08): 216 KXINX/KXINXU taker fills on "quadratic" x 1 all imply
0.0700-0.0722 (the spread is the 1/100-cent ceiling), and 10
KXCRYPTOLEAD15M maker fills on "quadratic" x 1 paid exactly 0.
"""
from __future__ import annotations

import dataclasses
import math

import pandas as pd

from kalshi_infra.api_functions import kalshi_get

# Base rates per fee_type, applied to p(1-p). Taker 0.07 measured (above);
# maker 0.0175 on maker-fee series is the published schedule rate (a quarter
# of taker) and has NOT been checked against a fill on this account.
_BASE = {
    "quadratic": (0.07, 0.0),
    "quadratic_with_maker_fees": (0.07, 0.0175),
    "quadratic_with_combo_maker_fees": (0.07, 0.0175),
}


@dataclasses.dataclass(frozen=True)
class FeeSchedule:
    fee_type: str
    multiplier: float
    taker_rate: float     # multiplier already applied
    maker_rate: float

    def taker(self, price: float, contracts: float = 1.0) -> float:
        return fee(price, contracts, self.taker_rate)

    def maker(self, price: float, contracts: float = 1.0) -> float:
        return fee(price, contracts, self.maker_rate)


def fee(price: float, contracts: float, rate: float) -> float:
    if contracts <= 0 or rate <= 0:
        return 0.0
    return math.ceil(rate * contracts * price * (1.0 - price) * 10000.0) / 10000.0


def schedule(fee_type: str, multiplier: float) -> FeeSchedule:
    if fee_type not in _BASE:
        # "flat" or anything new: refuse rather than trade on a guessed fee.
        raise ValueError(f"unsupported Kalshi fee_type {fee_type!r}")
    t, m = _BASE[fee_type]
    return FeeSchedule(fee_type, float(multiplier), t * multiplier, m * multiplier)


def for_event(series_ticker: str, event_ticker: str, at: pd.Timestamp | None = None) -> FeeSchedule:
    """The schedule in force for `event_ticker` now: the series' own fee,
    replaced by the latest event override already in effect."""
    at = at or pd.Timestamp.now(tz="UTC")
    s = kalshi_get(f"/trade-api/v2/series/{series_ticker}")["series"]
    fee_type, mult = s["fee_type"], s["fee_multiplier"]
    try:
        ch = kalshi_get("/trade-api/v2/events/fee_changes",
                        params={"event_ticker": event_ticker}).get("event_fee_changes") or []
    except Exception:
        ch = []
    live = sorted((c for c in ch if c.get("event_ticker") == event_ticker
                   and pd.Timestamp(c["scheduled_ts"]) <= at), key=lambda c: c["scheduled_ts"])
    if live and live[-1].get("fee_type_override") is not None:
        fee_type, mult = live[-1]["fee_type_override"], live[-1]["fee_multiplier_override"]
    return schedule(fee_type, mult)
