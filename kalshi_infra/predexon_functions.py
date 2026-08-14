"""
Predexon API functions - tick-level (sub-cent orderbook) historical data for Kalshi markets.
Predexon aggregates Kalshi orderbook history at finer granularity than Kalshi's own
1-minute candlestick API, useful for backfilling dates the WS collector didn't run for.
"""
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

import api_functions
from keys import PREDEXON_API_KEY as API_KEY

BASE_URL = "https://api.predexon.com/v2"

# Data layout: data/{predexon,kalshi_1min_historical}/{SERIES_TICKER}/... --
# one subfolder per market type (series), so KXHIGHLAX and e.g. KXLOWTLAX
# never share a flat folder. Every cache path below is derived per-call from
# an event_ticker rather than fixed at import time, since which series a
# given event belongs to isn't known until then.
_DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
PREDEXON_ROOT = os.path.join(_DATA_ROOT, "predexon")
KALSHI_1MIN_HISTORICAL_ROOT = os.path.join(_DATA_ROOT, "kalshi_1min_historical")
KALSHI_TRADES_ROOT = os.path.join(_DATA_ROOT, "kalshi_trades")


def _series_of(event_ticker: str) -> str:
    return event_ticker.split("-")[0]


def predexon_orderbooks_dir(event_ticker: str) -> str:
    return os.path.join(PREDEXON_ROOT, _series_of(event_ticker), "orderbooks")


def predexon_event_markets_dir(event_ticker: str) -> str:
    return os.path.join(PREDEXON_ROOT, _series_of(event_ticker), "event_markets")


def kalshi_1min_historical_dir(event_ticker: str) -> str:
    return os.path.join(KALSHI_1MIN_HISTORICAL_ROOT, _series_of(event_ticker))


def kalshi_trades_dir(event_ticker: str) -> str:
    return os.path.join(KALSHI_TRADES_ROOT, _series_of(event_ticker))


def all_cached_predexon_event_tickers() -> list[str]:
    """Every event_ticker with cached raw Predexon orderbook ticks, across every series subfolder."""
    import glob as _glob
    pattern = os.path.join(PREDEXON_ROOT, "*", "orderbooks", "*.parquet")
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in _glob.glob(pattern))


def all_cached_kalshi_1min_event_tickers() -> list[str]:
    """Every event_ticker with a cached Kalshi 1-min candle backup, across every series subfolder."""
    import glob as _glob
    pattern = os.path.join(KALSHI_1MIN_HISTORICAL_ROOT, "*", "*.parquet")
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in _glob.glob(pattern))

# Confirmed for real (not just from docs) by tripping it: 8 concurrent
# ticker-pagination threads immediately drew a real 429 body
# ({"error":"rate_limited","retryAfterMs":963}) even though sequential
# requests never hit it (~1.4s network RTT/call kept us under the burst
# threshold by accident, no matter how low the artificial sleep was). Real
# limit is 1 req/sec sustained, burst 2 - match _MAX_CONCURRENT_TICKERS to
# that burst capacity and keep spacing at ~1/sec.
_MIN_CALL_INTERVAL = 1.05
_last_call_ts = 0.0
_rate_lock = threading.Lock()
_MAX_CONCURRENT_TICKERS = 2


class PredexonQuotaExceeded(Exception):
    """Raised when Predexon returns 429 after retries are exhausted."""


def predexon_get(path: str, params: dict, max_retries: int = 3) -> dict:
    global _last_call_ts
    for attempt in range(max_retries + 1):
        with _rate_lock:
            wait = _MIN_CALL_INTERVAL - (time.time() - _last_call_ts)
            if wait > 0:
                time.sleep(wait)
            _last_call_ts = time.time()
        resp = requests.get(BASE_URL + path, headers={"x-api-key": API_KEY}, params=params, timeout=30)
        if resp.status_code == 429:
            if attempt == max_retries:
                raise PredexonQuotaExceeded(
                    f"429 on {path} after {max_retries} retries: {resp.text[:300]}"
                )
            retry_after = float(resp.headers.get("Retry-After", 5))
            print(f"  [429] {path} - retrying in {retry_after:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()


def get_event_markets(event_ticker: str, cache_dir: str = None) -> list[dict]:
    """
    Return raw market dicts for an event (ticker, open_time, close_time,
    floor/cap via subtitle parsing). Cached to disk per event_ticker,
    treated as immutable once fetched -- same convention as every other
    historical cache in this project, since a closed event's own market
    listing never changes -- so this becomes a local read (no Predexon call
    at all) for any event already seen once, not just load_predexon_long_df's
    raw ticks.

    This also fixes a real cross-process rate-limit bug: predexon_get's own
    throttle (_rate_lock/_MIN_CALL_INTERVAL, module-level) is a per-PROCESS
    lock, so parallelizing across multiple OS processes (e.g. regen_cache.py's
    ProcessPoolExecutor) doesn't coordinate between them at all -- N processes
    each independently self-throttle to ~1 req/sec, so N processes hitting
    this endpoint collectively land at ~N req/sec, well past Predexon's real
    1 req/sec sustained limit (confirmed directly: 11 parallel worker
    processes triggered a 429 within seconds even though each one's own
    in-process throttle was working correctly). Caching removes the call
    from the hot path entirely for any previously-seen event, rather than
    trying to coordinate the existing lock across process boundaries.
    """
    if cache_dir is None:
        cache_dir = predexon_event_markets_dir(event_ticker)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{event_ticker}.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            return json.load(f)
    data = predexon_get("/kalshi/markets", {"event_ticker": event_ticker, "limit": 50})
    markets = data.get("markets", [])
    with open(cache_path, "w") as f:
        json.dump(markets, f)
    return markets


def get_orderbook_subcent_paginated(ticker: str, start_ms: int, end_ms: int, limit: int = 2000) -> list[dict]:
    """Fetch all sub-cent orderbook snapshots for a market over [start_ms, end_ms], following pagination."""
    snapshots = []
    pagination_key = None
    while True:
        params = {"ticker": ticker, "start_time": start_ms, "end_time": end_ms, "limit": limit}
        if pagination_key:
            params["pagination_key"] = pagination_key
        data = predexon_get("/kalshi/orderbooks-subcent", params)
        batch = data.get("snapshots", [])
        snapshots.extend(batch)
        pagination = data.get("pagination", {})
        if not pagination.get("has_more"):
            break
        pagination_key = pagination.get("pagination_key")
        if not pagination_key:
            break
    return snapshots


def _snapshot_to_row(snap: dict) -> dict:
    return {
        "ticker": snap.get("ticker"),
        "timestamp": snap.get("timestamp"),
        "best_bid": float(snap["best_bid"]) if snap.get("best_bid") is not None else None,
        "best_ask": float(snap["best_ask"]) if snap.get("best_ask") is not None else None,
        "bid_depth": snap.get("bid_depth"),
        "ask_depth": snap.get("ask_depth"),
        "sequence": snap.get("sequence"),
        "source": snap.get("source"),
        "yes_bids": json.dumps(snap.get("yes_bids", [])),
        "yes_asks": json.dumps(snap.get("yes_asks", [])),
    }


def fetch_event_orderbooks(event_ticker: str, out_dir: str = None) -> pd.DataFrame:
    """
    Fetch sub-cent orderbook snapshot history for every market in an event and
    save raw snapshots to data/predexon/{series}/orderbooks/{event_ticker}.parquet.

    Returns the combined DataFrame (columns: ticker, timestamp, time, best_bid,
    best_ask, bid_depth, ask_depth, sequence, source, yes_bids, yes_asks).
    yes_bids/yes_asks are JSON strings (full book depth, decimal-cent price levels).
    """
    if out_dir is None:
        out_dir = predexon_orderbooks_dir(event_ticker)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{event_ticker}.parquet")

    markets = get_event_markets(event_ticker)
    if not markets:
        print(f"  [warn] no markets found for {event_ticker}")
        return pd.DataFrame()

    def _fetch_one(m):
        ticker = m["ticker"]
        open_dt = datetime.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
        close_dt = datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        start_ms = int(open_dt.timestamp() * 1000)
        end_ms = int(close_dt.timestamp() * 1000)
        snaps = get_orderbook_subcent_paginated(ticker, start_ms, end_ms)
        print(f"  {ticker}: {len(snaps)} snapshots")
        return snaps

    all_rows = []
    with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_TICKERS, len(markets))) as pool:
        future_to_ticker = {pool.submit(_fetch_one, m): m["ticker"] for m in markets}
        for fut in as_completed(future_to_ticker):
            try:
                all_rows.extend(_snapshot_to_row(s) for s in fut.result())
            except Exception as e:
                print(f"  [warn] {future_to_ticker[fut]}: {e} (keeping other tickers' data for this event)")

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.sort_values(["ticker", "time"]).reset_index(drop=True)
        df.to_parquet(out_path, index=False)
        print(f"Saved {len(df)} total snapshots -> {out_path}")
    return df


def _parse_strike_from_ticker(ticker: str) -> tuple[float, float | None]:
    """
    Parse (floor, cap) directly from a KXHIGHLAX ticker suffix -- Kalshi's naming
    is consistent: "B79.5" -> a [79, 80) range bucket (floor=79, cap=80),
    "T82"/"T75" -> a tail threshold at 82/75 (cap=None). Used instead of trusting
    floor_strike/cap_strike fields from either source, since Predexon's are
    unpopulated (None) for this series and api_functions.get_event_markets
    (Kalshi's own endpoint) was found to silently omit the lower-tail market
    for every event checked.
    """
    suffix = ticker.rsplit("-", 1)[-1]
    if suffix.startswith("B"):
        center = float(suffix[1:])
        return center - 0.5, center + 0.5
    if suffix.startswith("T"):
        return float(suffix[1:]), None
    raise ValueError(f"can't parse strike from ticker: {ticker}")


def load_predexon_long_df(event_ticker: str, cache_dir: str = None) -> pd.DataFrame:
    """
    Build a long-form DataFrame (ticker, floor_strike, cap_strike, strike_type,
    time, bid, ask, bid_depth, ask_depth) from cached Predexon sub-cent
    orderbook snapshots, at full tick resolution.

    Sourced from per-update orderbook snapshots instead of 1-min Kalshi candles.
    Auto-fetches + caches raw snapshots from Predexon if not already on disk.

    Market list (which tickers exist) and strike_type ('between'/'greater'/
    'less') come from Predexon's own listing (get_event_markets), not Kalshi's
    -- see _parse_strike_from_ticker's docstring for why. strike_type is what
    lets black_scholes.fit_normal_to_bucket_bid_ask evaluate each market's own
    correct model probability formula (a range bucket's is a *difference* of
    two Gaussian CDFs; a tail's is a single one) instead of the cumulative-SF
    shortcut, which turned out to have real correctness gaps (an unbounded
    sf_hi under unpriced strike gaps, and no clean way to fold in a lower-tail
    market's opposite-signed contribution).

    NOTE: floor_strike is no longer a unique row-grouping key by itself -- a
    range bucket and the lower-tail market can share the same floor (e.g.
    B75.5 and T75 both parse to floor=75). Group/merge by ticker, not
    floor_strike, anywhere downstream.

    bid/ask are the market's own best bid/ask in [0, 1] (cents / 100), passed
    through as-is -- no mid/WAP point price is synthesized here. A missing bid
    is reported as 0.0, a missing ask as 1.0. bid_depth/ask_depth are the raw
    resting sizes (0 if that side is missing).
    """
    if cache_dir is None:
        cache_dir = predexon_orderbooks_dir(event_ticker)
    cols = ["ticker", "floor_strike", "cap_strike", "strike_type", "time", "bid", "ask", "bid_depth", "ask_depth"]
    raw_path = os.path.join(cache_dir, f"{event_ticker}.parquet")
    if os.path.exists(raw_path):
        raw = pd.read_parquet(raw_path)
    else:
        raw = fetch_event_orderbooks(event_ticker, out_dir=cache_dir)
    if raw.empty:
        return pd.DataFrame(columns=cols)

    markets = get_event_markets(event_ticker)
    if not markets:
        return pd.DataFrame(columns=cols)
    strike_rows = []
    for m in markets:
        try:
            floor, cap = _parse_strike_from_ticker(m["ticker"])
        except ValueError:
            continue
        strike_rows.append({
            "ticker": m["ticker"], "floor_strike": floor, "cap_strike": cap,
            "strike_type": m.get("strike_type"),
        })
    if not strike_rows:
        return pd.DataFrame(columns=cols)
    strike_map = pd.DataFrame(strike_rows).set_index("ticker")

    df = raw.merge(strike_map, left_on="ticker", right_index=True, how="inner")
    if df.empty:
        return pd.DataFrame(columns=cols)

    has_bid = df["bid_depth"] > 0
    has_ask = df["ask_depth"] > 0
    df["bid"] = df["best_bid"].where(has_bid, 0.0) / 100.0
    df["ask"] = df["best_ask"].where(has_ask, 100.0) / 100.0
    df["bid_depth"] = df["bid_depth"].clip(lower=0)
    df["ask_depth"] = df["ask_depth"].clip(lower=0)

    long_df = df[cols].sort_values(["time", "floor_strike"]).reset_index(drop=True)
    return long_df


def load_kalshi_backup_long_df(event_ticker: str, cache_dir: str = None) -> pd.DataFrame:
    """
    Backup BBO source for black_scholes.compute_implied_spot_timeseries: Kalshi's
    own official 1-minute candlesticks for every market in the event, long-form
    (ticker, time, bid, ask). Coarser than Predexon's sub-second ticks, but
    authoritative -- used only when Predexon's feed for a given ticker has gone
    stale or disagrees with Kalshi's own record. Predexon's capture pipeline has
    real, unrecoverable gaps: confirmed directly against Kalshi's candles on a
    real case (KXHIGHLAX-26JUL22-B75.5, ~21 minutes with zero Predexon updates
    while Kalshi's own record shows real price movement throughout), and
    confirmed via a live re-fetch straight from Predexon's API (bypassing our
    own cache) that the missing history isn't a fetch-side bug -- Predexon
    itself has nothing stored for that window.

    Cached to data/kalshi_1min_historical/{series}/{event_ticker}.parquet, one
    fetch per event regardless of how many times/slices it's later queried.

    Columns: ticker, time, bid, ask (candle CLOSE prices), bid_high, ask_low
    (the candle's intra-minute high/low -- use these, not bid/ask, for "did
    price touch X this minute" checks; see get_candlesticks's docstring for
    why the close alone can miss a touch-and-revert). A cache file written
    by the pre-OHLC version of this function (no 'bid_high' column) is
    treated as stale and transparently re-fetched/overwritten.
    """
    if cache_dir is None:
        cache_dir = kalshi_1min_historical_dir(event_ticker)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{event_ticker}.parquet")
    cols = ["ticker", "time", "bid", "ask", "bid_high", "ask_low"]
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        if "bid_high" in df.columns:
            if not df.empty:
                df["time"] = pd.to_datetime(df["time"], utc=True)
            return df

    markets = get_event_markets(event_ticker)
    if not markets:
        return pd.DataFrame(columns=cols)

    frames = []
    for m in markets:
        ticker = m.get("ticker")
        try:
            open_dt = datetime.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
            close_dt = datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            candles = api_functions.get_candlesticks(ticker, int(open_dt.timestamp()), int(close_dt.timestamp()), period_interval=1)
        except Exception as e:
            print(f"  [warn] kalshi backup candles for {ticker}: {e}")
            continue
        if candles.empty:
            continue
        candles = candles[["time", "bid", "ask", "bid_high", "ask_low"]].copy()
        candles["ticker"] = ticker
        frames.append(candles)

    df = pd.concat(frames, ignore_index=True).sort_values(["ticker", "time"]).reset_index(drop=True)[cols] if frames else pd.DataFrame(columns=cols)
    df.to_parquet(cache_path, index=False)
    return df


def load_kalshi_trades_long_df(event_ticker: str, cache_dir: str = None) -> pd.DataFrame:
    """
    Every individual trade for every market in an event, long-form (ticker,
    time, yes_price, no_price, count, taker_side) -- see
    api_functions.get_market_trades's docstring for field meaning.

    Cached to data/kalshi_trades/{series}/{event_ticker}.parquet, one fetch
    per event regardless of how many times it's later queried -- an event's
    own trade history is immutable once the event has closed, same
    convention as load_kalshi_backup_long_df.

    IMPORTANT: get_market_trades has no historical-namespaced fallback and
    a real rolling retention window (confirmed ~2.5 months back from
    whenever this is run) -- an event outside that window caches as
    genuinely empty here, indistinguishable from "no trades happened."
    Don't treat an empty result as confirmation of zero volume unless the
    event is recent enough to be in the window.
    """
    if cache_dir is None:
        cache_dir = kalshi_trades_dir(event_ticker)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{event_ticker}.parquet")
    cols = ["ticker", "time", "yes_price", "no_price", "count", "taker_side"]
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], utc=True)
        return df

    markets = get_event_markets(event_ticker)
    if not markets:
        return pd.DataFrame(columns=cols)

    frames = []
    for m in markets:
        ticker = m.get("ticker")
        try:
            trades = api_functions.get_market_trades(ticker)
        except Exception as e:
            print(f"  [warn] kalshi trades for {ticker}: {e}")
            continue
        if trades.empty:
            continue
        trades = trades[["time", "yes_price", "no_price", "count", "taker_side"]].copy()
        trades["ticker"] = ticker
        frames.append(trades)

    df = pd.concat(frames, ignore_index=True).sort_values(["ticker", "time"]).reset_index(drop=True)[cols] if frames else pd.DataFrame(columns=cols)
    df.to_parquet(cache_path, index=False)
    return df


if __name__ == "__main__":
    import sys
    event_tickers = sys.argv[1:] or ["KXHIGHLAX-26JUL27"]
    for et in event_tickers:
        print(f"=== {et} ===")
        fetch_event_orderbooks(et)
