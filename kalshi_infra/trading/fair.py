"""
The contract between a series' pricing model and the generic quoter.

A model publishes one `Fair` per market: a probability and a band. The band
is what the edge is measured against -- for SPX the call/put bound union,
for Coin Race `prob +/- ci_vol_multiplier * theo_vol`. Everything downstream
(edge, lean, quoting, LIP, guards, dashboard) is series-agnostic.
"""
from __future__ import annotations

import dataclasses
from typing import Protocol


@dataclasses.dataclass(frozen=True)
class Fair:
    prob: float
    lo: float
    hi: float
    # Free-form per-market values the model wants on the decision log /
    # dashboard (e.g. theo_vol, coin). Keys are prefixed "x_" when logged.
    extras: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class FairUpdate:
    """Dispatch payload: new fairs for some markets. `None` for a market
    means "no trustworthy fair" -- the quoter pulls its quotes there."""
    fairs: dict[str, Fair | None]


class FairModel(Protocol):
    """What a series' pricing must provide to the generic runtime."""

    def reprice(self, now_s: float) -> dict[str, Fair | None] | None:
        """Called after new inputs arrive (see EventModel.set_wakeup). Return
        the fairs to dispatch, or None when nothing changed."""

    def on_market_event(self, ticker: str, now_s: float) -> dict[str, Fair | None] | None:
        """Called on every Kalshi book/trade wakeup, before acting. Return
        refreshed fairs if the model reprices cheaply off its latest inputs,
        else None."""

    def signal_age_s(self) -> float | None:
        """Age of the newest input under the current fairs (the freshness
        clock that gates trading). None = unknown, treated as stale."""

    def side_block(self, ticker: str, side: str, fair: Fair, now_s: float) -> str | None:
        """Series-specific veto on one side of one market; return a reason
        string to block quotes and takes there, or None."""

    # Optional: quote_block(ticker, now_s) -> str | None. Pulls resting quotes
    # (edge and LIP) without touching takes; the quoter calls it only if present.
    # Optional: on_book(book) -> None. Every Kalshi BBO update, before the
    # engine acts on it; the session calls it only if present.
    # Optional: attach_engine(engine) -> None. Called once per event after the
    # LiveEngine is built, for models that need this session's positions.

    def summary_extras(self) -> dict:
        """Series-specific scalars for summary.json."""
