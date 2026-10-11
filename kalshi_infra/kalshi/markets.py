"""Market/event discovery and per-market metadata from Kalshi REST.

Generic across series: the per-series "which event do I trade" rule lives
with the strategy, but every rule needs these lookups.
"""
from __future__ import annotations

import dataclasses

import pandas as pd

from kalshi_infra.api_functions import kalshi_get


@dataclasses.dataclass(frozen=True)
class EventInfo:
    event_ticker: str
    open_time: pd.Timestamp | None
    close_time: pd.Timestamp | None
    markets: list[dict]


def _markets(series: str, status: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": status, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/trade-api/v2/markets", params=params)
        batch = data.get("markets", [])
        out.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            return out


def open_events(series: str) -> list[EventInfo]:
    """Open events of `series`, soonest close first."""
    return _group(_markets(series, "open"))


def upcoming_events(series: str) -> list[EventInfo]:
    """Open and listed-but-unopened events, soonest close first. Each
    carries Kalshi's scheduled open_time/close_time, so the session can
    sleep to the next open instead of polling for it. The API takes one
    status per call (a comma list is a 400)."""
    return _group(_markets(series, "open") + _markets(series, "unopened"))


def _group(markets: list[dict]) -> list[EventInfo]:
    """Kalshi keeps a market "open" for a few seconds past close_time, so
    already-closed events are dropped -- otherwise a just-ended event is
    picked again at rollover."""
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)
    now = pd.Timestamp.now(tz="UTC")
    out = []
    for et, ms in by_event.items():
        info = _info(et, ms)
        if info.close_time is not None and info.close_time <= now:
            continue
        out.append(info)
    out.sort(key=lambda e: e.close_time or pd.Timestamp.max.tz_localize("UTC"))
    return out


def event(event_ticker: str) -> EventInfo:
    data = kalshi_get("/trade-api/v2/markets", params={"event_ticker": event_ticker, "limit": 1000})
    ms = data.get("markets", [])
    if not ms:
        raise ValueError(f"no markets for {event_ticker}")
    return _info(event_ticker, ms)


def _info(event_ticker: str, markets: list[dict]) -> EventInfo:
    # Latest open and earliest close: strictly inside every leg's window.
    opens = [pd.Timestamp(m["open_time"]) for m in markets if m.get("open_time")]
    closes = [pd.Timestamp(m["close_time"]) for m in markets if m.get("close_time")]
    return EventInfo(event_ticker=event_ticker,
                     open_time=max(opens) if opens else None,
                     close_time=min(closes) if closes else None,
                     markets=markets)


def event_shard(event_ticker: str) -> int:
    """Exchange shard (exchange_index) the event's markets trade on.
    KXCRYPTOLEAD15M is on 2, KXINX/KXINXU on 0 (GET /events, 2026-10-10)."""
    return int(kalshi_get(f"/trade-api/v2/events/{event_ticker}", auth=True)["event"]["exchange_index"])


def series_shard(series: str) -> int | None:
    """Shard of the series' next listed event, or None if nothing is listed."""
    evs = upcoming_events(series)
    return event_shard(evs[0].event_ticker) if evs else None


def incentive_programs(tickers: set[str]) -> dict[str, dict]:
    """Active LIP programs keyed by market ticker, for `tickers` only."""
    out: dict[str, dict] = {}
    cursor = None
    while True:
        params = {"status": "active", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/trade-api/v2/incentive_programs", params=params, auth=True)
        batch = data.get("incentive_programs", [])
        for p in batch:
            if p.get("market_ticker") in tickers:
                out[p["market_ticker"]] = p
        cursor = data.get("next_cursor") or data.get("cursor")
        if not cursor or not batch:
            return out
