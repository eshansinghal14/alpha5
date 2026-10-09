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

    def set_wakeup(self, fn: Callable[[], None]) -> None:
        """Register `fn` to be called (on the event loop) whenever a new
        pricing input arrives. The runtime reprices on it, coalescing a
        burst into one reprice."""

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
    # The next event to trade, or None if none is listed. Gets the open and
    # unopened events of the series (minus the one just traded), soonest
    # close first; the pick may not be open yet.
    pick_event: Callable[[list[EventInfo], object], EventInfo | None]
    # Builds the event's model. May await network setup.
    build_model: Callable[[EventInfo, object], Awaitable[EventModel]]
    # When to start trading the picked event. The session sleeps until then
    # without touching the API.
    trade_from: Callable[[EventInfo, object], pd.Timestamp | None] = lambda ev, cfg: ev.open_time
