"""
Pull KXCRYPTOLEAD15M trade history from the Kalshi REST API and write it
in the same parquet schema the WS collector produces, so
fit_fill_intensity.py can consume it with no special-casing.

Why REST and not only WS: the public trades endpoint
(GET /trade-api/v2/markets/trades, via api_functions.get_market_trades)
retains ~2.5 months, so we can backfill the fill-rate fit from already-
closed events WITHOUT having had a live WS capture running. The WS path is
still the long-run source (it also records raw_ob for the queue study);
this is the cold-start / backfill path.

Field mapping REST -> WS schema:
  * taker_side (yes/no) -> taker_book_side (ask/bid): a taker buying NO
    lifts a resting YES bid (book side "bid"); a taker buying YES lifts a
    resting YES ask == NO bid (book side "ask"). This is the exact mapping
    the fill-rate model keys on, so we materialise it here.
  * event_ticker derived by stripping the -COIN suffix.
  * ts_ms from the trade time.

Usage:
    python kalshi_crypto/rv_modeling/fetch_trades_api.py            # last ~N events
    python kalshi_crypto/rv_modeling/fetch_trades_api.py --max-events 400
      -> writes data/kalshi_ws/KXCRYPTOLEAD15M/trades/{event}.parquet
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT = _HERE.parents[1]
_INFRA = _PROJECT / "kalshi_infra"
for _p in (_PROJECT, _INFRA):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from kalshi_crypto.series import SERIES_TICKER, COINS, coin_from_market_ticker

OUT_DIR = _PROJECT / "data" / "kalshi_ws" / SERIES_TICKER / "trades"


def _event_of(ticker: str) -> str:
    """KXCRYPTOLEAD15M-26OCT071630-BTC -> KXCRYPTOLEAD15M-26OCT071630."""
    coin = coin_from_market_ticker(ticker)
    return ticker[: -(len(coin) + 1)] if coin else ticker


def _list_markets(status: str, limit: int = 1000) -> list[dict]:
    import api_functions as api
    out, cursor = [], None
    while True:
        params = {"series_ticker": SERIES_TICKER, "status": status, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        try:
            data = api.kalshi_get("/trade-api/v2/markets", params=params, auth=True)
        except Exception as e:
            # A rejected status (400) shouldn't abort the whole backfill --
            # skip it and keep whatever the other statuses returned.
            print(f"[fetch_trades_api] status={status!r} query failed ({e!r}); skipping")
            break
        ms = data.get("markets", [])
        out.extend(ms)
        cursor = data.get("cursor")
        if not cursor or not ms or len(out) >= limit:
            break
    return out


def fetch(max_events: int, statuses: tuple[str, ...]) -> int:
    import api_functions as api

    # Collect candidate market tickers across the requested statuses.
    markets: dict[str, dict] = {}
    for st in statuses:
        for m in _list_markets(st):
            markets[m["ticker"]] = m
    # Group by event, newest first (event ticker sorts lexicographically by
    # the YYMMDDHHMM stamp, so reverse-sort approximates newest-first).
    by_event: dict[str, list[str]] = {}
    close_by_event: dict[str, str] = {}
    for tk, m in markets.items():
        ev = _event_of(tk)
        by_event.setdefault(ev, []).append(tk)
        if m.get("close_time"):
            close_by_event[ev] = m["close_time"]
    events = sorted(by_event, reverse=True)[:max_events]
    print(f"[fetch_trades_api] {len(markets)} markets across "
          f"{len(by_event)} events; fetching trades for {len(events)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for ev in events:
        rows = []
        for tk in by_event[ev]:
            df = api.get_market_trades(tk)
            if df.empty:
                continue
            coin = coin_from_market_ticker(tk)
            for r in df.itertuples(index=False):
                # taker_side yes -> resting YES ask lifted (book "ask");
                #            no  -> resting YES bid hit  (book "bid").
                book_side = "ask" if r.taker_side == "yes" else "bid"
                rows.append({
                    "ticker": tk,
                    "event_ticker": ev,
                    "coin": coin,
                    "ts_ms": int(pd.Timestamp(r.time).timestamp() * 1000),
                    "time": pd.Timestamp(r.time),
                    "yes_price": float(r.yes_price),
                    "no_price": float(r.no_price),
                    "count": float(r.count),
                    "taker_side": r.taker_side,
                    "taker_book_side": book_side,
                })
        if not rows:
            continue
        out = pd.DataFrame(rows).sort_values("ts_ms").reset_index(drop=True)
        path = OUT_DIR / f"{ev}.parquet"
        out.to_parquet(path, index=False)
        written += 1
        print(f"  {ev}: {len(out)} trades -> {path.name}")
    print(f"[fetch_trades_api] wrote {written} event files to {OUT_DIR}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-events", type=int, default=400,
                    help="Newest N events to pull (API retains ~2.5 months).")
    ap.add_argument("--statuses", default="settled,closed",
                    help="Comma-separated market statuses to enumerate "
                         "(valid: open, closed, settled, unopened).")
    args = ap.parse_args()
    fetch(args.max_events, tuple(s.strip() for s in args.statuses.split(",") if s.strip()))


if __name__ == "__main__":
    main()
