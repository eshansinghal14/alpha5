"""Venue facts: not tunables, so they live in code rather than config."""
import math

# Kalshi's price increment. Prices are dollars in [0, 1], YES-side unless a
# name says otherwise.
TICK = 0.01
MIN_PRICE = TICK
MAX_PRICE = 1.0 - TICK


def fee(price: float, contracts: float, multiplier: float) -> float:
    """Kalshi's per-fill fee: ceil(mult * n * p * (1 - p)) to 1/100 cent.
    The multiplier is per series (and per maker/taker) -- it comes from the
    strategy's config, never a default here."""
    if contracts <= 0:
        return 0.0
    return math.ceil(multiplier * contracts * price * (1.0 - price) * 10000.0) / 10000.0
