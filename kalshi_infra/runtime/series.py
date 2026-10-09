"""
What a series folder in kalshi_strategies provides to the generic runtime.

    SERIES        = Series(...)   # module attribute in kalshi_strategies/<FOLDER>/__init__.py

The runtime owns everything else: rollover, the Kalshi feed, the engine,
the quoter, guards, control files, persistence.
"""
from __future__ import annotations

import dataclasses
from typing import Awaitable, Callable, Protocol

import pandas as pd

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import FairModel


class EventModel(FairModel, Protocol):
    """A FairModel bound to one event, plus its lifecycle."""

    tickers: list[str]

    def feed_tasks(self) -> list[Awaitable]:
        """Coroutines for this model's own data feeds (IB, Coinbase, ...).
        Cancelled at event end."""

    def feed_age_s(self) -> float | None:
        """Since the pricing feed last delivered anything (stall attribution)."""

    def applied_age_s(self) -> float | None:
        """Since an update was folded into pricing state."""

    async def close(self):
        """Release feed resources (disconnect IB, flush bars)."""


@dataclasses.dataclass(frozen=True)
class Series:
    """`ticker`: Kalshi series ticker. `config_key`: section of the folder's
    config.json (None = the whole file is this series' config)."""
    ticker: str
    config_cls: type
    config_path: str
    config_key: str | None
    # Which event to trade now, or None if nothing is tradeable yet. Gets
    # the open events of the series, soonest close first.
    pick_event: Callable[[list[EventInfo], object, pd.Timestamp], EventInfo | None]
    # Builds the event's model. May await network setup.
    build_model: Callable[[EventInfo, object], Awaitable[EventModel]]
