"""
Shared types for venue-adapter spot feeds.

Every exchange's L2 feed is reduced to a `VenueBook` -- a timestamped
snapshot of (bid levels, ask levels, meta) -- and emitted via an
async-queue hook. The CF rate pipeline consumes this and only this. New
venues wrap their native WS in a class matching `VenueAdapter`.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Protocol, Callable, Awaitable


@dataclasses.dataclass(frozen=True)
class OrderBookLevel:
    """One price level. Price is in quote-currency units (USD), size in
    base-currency units. Kept as a dataclass rather than a tuple so a
    future adapter adding e.g. order count can extend it without breaking
    callers."""
    price: float
    size: float


@dataclasses.dataclass
class VenueBook:
    """One venue's view of one product at one instant. Sorted: bids
    descending, asks ascending.

    `receive_ts` is the local arrival time (monotonic -- wall clock would
    be comparable across venues but is subject to NTP steps that would
    look like gaps). `exchange_ts` is whatever the venue stamped the
    update with (may be None if the venue doesn't ship timestamps on
    deltas). Only `receive_ts` is used by the staleness check; the
    exchange ts is persisted for post-event drift analysis.
    """
    venue: str
    product: str              # venue's native product id, e.g. "BTC-USD"
    coin: str                 # canonical COINS entry
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    receive_ts: float         # time.monotonic()
    exchange_ts: float | None = None
    is_resyncing: bool = False
    # Monotonic sequence number -- most exchanges ship these; the CF rate
    # pipeline uses them to drop out-of-order deltas. None if the venue
    # doesn't publish them.
    seq: int | None = None

    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return 0.5 * (bb + ba)

    def is_crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    def is_one_sided(self) -> bool:
        return not self.bids or not self.asks

    def staleness_s(self, now: float | None = None) -> float:
        """Seconds since last update. `now` defaults to time.monotonic().
        Pass an explicit `now` when sampling a batch of venue books so they
        all compare against the same reference instant."""
        n = time.monotonic() if now is None else now
        return n - self.receive_ts


class VenueAdapter(Protocol):
    """One instance per venue. Owns its own async task + WS connection and
    pushes one `VenueBook` per product per update into a callback.

    The adapter DOES NOT drop updates; it is the pipeline's job to decide
    which snapshots belong in this second's composite. An adapter that
    coalesces internally would hide staleness from the pipeline.
    """

    venue: str

    def latest(self, coin: str) -> VenueBook | None:
        """Most recent book for this coin on this venue. None before the
        first update arrives, or after a `clear` on reconnect."""

    async def run(self, on_update: Callable[[VenueBook], Awaitable[None] | None]):
        """Long-running coroutine; raises only on fatal auth/config
        errors. Reconnect is internal. `on_update` MAY be sync or async;
        the pipeline wraps both."""
