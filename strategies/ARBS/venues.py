"""Kalshi and Polymarket US side by side in one ARBS process.

A market is Polymarket's when its ticker is a lowercase slug (Kalshi tickers
are upper case). Polymarket counts as one more shard (PM_SHARD) everywhere
ARBS keys clients and cash by shard.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from infra.polymarket import GATEWAY
from infra.polymarket.feed import PolyFeed
from infra.polymarket.orders import PM_SHARD
from strategies.ARBS import pricing
from strategies.ARBS.feed import MultiFeed

# Polymarket taker coefficient below $10k of trailing 30-day taker volume;
# makers pay nothing until $10M of maker volume (fees page, 11-12).
PM_TAKER_RATE = 0.095
PM_MAKER_RATE = 0.0
PM_META_BATCH = 50


def is_pm(ticker: str) -> bool:
    return bool(ticker) and ticker[0].islower()


def venue(ticker: str) -> str:
    return "polymarket" if is_pm(ticker) else "kalshi"


class Feeds:
    """MultiFeed for Kalshi markets and PolyFeed for Polymarket ones, routed by
    ticker. Up only while every venue with markets is up."""

    def __init__(self, tickers, on_quote, on_trade, *, label, on_fill=None, on_gone=None, on_error=None):
        k = [t for t in tickers if not is_pm(t)]
        p = [t for t in tickers if is_pm(t)]
        self.kalshi = MultiFeed(k, on_quote, on_trade, label=label, on_fill=on_fill, on_error=on_error)
        self.pm = PolyFeed(p, on_quote, on_trade, label=label, on_fill=on_fill, on_gone=on_gone, on_error=on_error)

    def _of(self, ticker: str):
        return self.pm if is_pm(ticker) else self.kalshi

    def _active(self):
        return [f for f in (self.kalshi, self.pm) if f._tickers]

    @property
    def up(self) -> bool:
        act = self._active()
        return bool(act) and all(f.up for f in act)

    @up.setter
    def up(self, v: bool):
        self.kalshi.up = self.pm.up = v

    @property
    def _books(self):
        # Kalshi's own dict while there are no Polymarket books, so writes into it land.
        return {**self.kalshi._books, **self.pm._books} if self.pm._books else self.kalshi._books

    @_books.setter
    def _books(self, books: dict):
        self.kalshi._books = {t: b for t, b in books.items() if not is_pm(t)}
        self.pm._books = {t: b for t, b in books.items() if is_pm(t)}

    @property
    def down_since(self) -> float | None:
        ds = [f.down_since for f in self._active() if f.down_since is not None]
        return min(ds) if ds else None

    def add(self, tickers):
        k = [t for t in tickers if not is_pm(t)]
        p = [t for t in tickers if is_pm(t)]
        if k:
            self.kalshi.add(k)
        if p:
            self.pm.add(p)

    def ladder(self, ticker: str):
        return self._of(ticker).ladder(ticker)

    def book(self, ticker: str):
        return self._of(ticker).book(ticker)

    def seconds_since_message(self) -> float | None:
        s = [f.seconds_since_message() for f in self._active()]
        s = [x for x in s if x is not None]
        return max(s) if s else None

    def __getattr__(self, name):
        # Counters for the summary: summed over both venues.
        if name in ("n_reconnects", "n_seq_gaps", "n_errors", "n_refreshes"):
            return getattr(self.kalshi, name, 0) + getattr(self.pm, name, 0)
        if name in ("kalshi", "pm"):
            raise AttributeError(name)
        return getattr(self.kalshi, name)

    async def run(self):
        import asyncio
        await asyncio.gather(self.kalshi.run(), self.pm.run())


def _get(path: str, query: dict | None = None) -> dict:
    url = GATEWAY + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "alpha5-arbs"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ticks(step: float | None) -> pricing.Ticks:
    st = float(step or 0.01)
    return pricing.Ticks(((0.0, 1.0, st),))


def pm_markets(slugs: list[str]) -> dict[str, dict]:
    """slug -> market record, from the public gateway in batches."""
    out = {}
    for i in range(0, len(slugs), PM_META_BATCH):
        for m in _get("/v1/markets", {"slug": slugs[i:i + PM_META_BATCH],
                                      "limit": PM_META_BATCH}).get("markets") or []:
            out[m["slug"]] = m
    return out


def pm_settlement(slug: str) -> float | None:
    try:
        v = _get(f"/v1/markets/{slug}/settlement").get("settlement")
    except Exception:
        return None
    return None if v is None else float(v)


def pm_status(m: dict) -> str:
    """Kalshi's status words, which ARBS's TRADEABLE and SETTLED test."""
    st = m.get("status") or ""
    if st == "MARKET_STATUS_RESOLVED":
        return "settled"
    if st == "MARKET_STATUS_OPEN" and m.get("active") and not m.get("closed"):
        return "active"
    return "closed" if m.get("closed") else st.replace("MARKET_STATUS_", "").lower() or "unknown"


def pm_result(value: float | None) -> tuple[str, float | None]:
    """(result, scalar value) from a settlement price: 1 is "yes", 0 "no",
    anything else (a game voided at its last fair price) "scalar"."""
    if value is None:
        return "", None
    if abs(value - 1.0) < 1e-9:
        return "yes", 1.0
    if abs(value) < 1e-9:
        return "no", 0.0
    return "scalar", value


def pm_event(m: dict) -> str:
    """The market's event: its slug less the outcome suffix."""
    return m["slug"].rsplit("-", 1)[0]


__all__ = ["PM_SHARD", "PM_TAKER_RATE", "PM_MAKER_RATE", "Feeds", "is_pm", "venue", "pm_markets",
           "pm_settlement", "pm_status", "pm_result", "pm_event", "ticks"]
