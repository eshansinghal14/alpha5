"""
What a series folder in kalshi_strategies provides to the generic runtime.

    SERIES   = {ticker: Series(...)}   # module attribute in kalshi_strategies/<FOLDER>/__init__.py
    SERVICES = [Service(...)]          # optional: processes the folder's series share

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
    # Series with the same write_group run in ONE process and share one write
    # bucket, one cancels_token reserve and one quote priority queue
    # (runtime.write_groups). None = its own process and its own budget slice.
    write_group: str | None = None
    # Within a write group, lower goes first: this series' writes get first
    # claim on the shared bucket at every step (order_manager.WriteGroup).
    write_priority: int = 0
    # A strategy that doesn't fit one-event-at-a-time (ARBS trades baskets
    # across ~70 events) supplies its own loop:
    # `await runner(series, live_trading=..., resume_arm_s=...)`. `ticker` is
    # then only the data/live folder and dashboard page name, and pick_event /
    # build_model are never called.
    runner: Callable[..., Awaitable[None]] | None = None


@dataclasses.dataclass(frozen=True)
class Service:
    """A process shared by a folder's series (e.g. one pricing feed). The
    launcher starts `python -m <module> --parent-pid <launcher>` before the
    series, keeps it running, and reuses one already listening on `port()`."""
    name: str
    module: str
    port: Callable[[], int]
