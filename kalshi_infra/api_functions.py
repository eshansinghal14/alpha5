"""
Kalshi REST helpers: authenticated/public GET with 429 backoff, market
discovery, orderbook midpoints, historical candles, and the series lists the
raw orderbook collector sweeps.
"""
import datetime
import time

import requests
import pandas as pd

from kalshi_infra.kalshi import auth
from kalshi_infra.keys import KALSHI_API_KEY as API_KEY

BASE_URL = "https://api.elections.kalshi.com"

# Every series on Kalshi with frequency == "fifteen_min", confirmed directly
# against /trade-api/v2/series with no category filter (13,029 series
# total, 19 matched) -- so this is exhaustive as of the check date, not a
# hand-picked subset. All but KXINX15M/KXNDQ15M were confirmed to actually
# produce strike-range markets (floor_strike populated once a market opens);
# KXINX15M/KXNDQ15M exist as
# series but currently have zero markets in any status -- kept in anyway
# since register_markets() just no-ops on an empty result, so they'll pick
# up automatically if/when Kalshi activates them. Used by
# ws_raw_ob_collector.py.
FIFTEEN_MIN_SERIES_TICKERS = [
    # crypto (14)
    "KXBTC15M", "KXETH15M", "KXBNB15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
    "KXADA15M", "KXTON15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M", "KXBCH15M",
    "KXCRYPTOCOMP15M", "KXCRYPTOLEAD15M",
    # commodities (3)
    "KXWTI15M", "KXGOLD15M", "KXSILVER15M",
    # financials (2) -- see note above re: currently zero live markets
    "KXINX15M", "KXNDQ15M",
]

# S&P 500 range markets (the exact series behind
# kalshi.com/markets/kxinx/sp-500-range/...) -- confirmed via API: daily
# series with hourly-bucketed events (e.g. KXINX-26AUG21H1600), 58 open
# strike-range markets at check time. Used by ws_raw_ob_collector.py.
SP500_SERIES_TICKERS = ["KXINX"]

# Hourly-cadence "price at time X" siblings of the above -- same
# crypto/commodities/financials category sweep as FIFTEEN_MIN, this time
# frequency == "hourly". Confirmed individually, not just by
# frequency label: every ticker here either had open markets with
# floor_strike populated, or (WTI/gold/silver, which are Sat/Sun
# market-hours-closed) had floor_strike populated on recent finalized
# markets. Excluded a long list of same-asset "hourly" tickers that turned
# out to be dead/retired duplicates with zero markets ever, in any status
# (confirmed directly, not assumed) -- e.g. bare KXSOL/KXTON/KXNEAR/KXZEC/
# KXRIPPLE/KXZECH/KXZECD/KXTONH/KXTOND/KXNEARH/KXNEARD all had 0 markets
# ever (legacy tickers kept listed after a rename).
# Also excluded KXDOGE/KXDOGED specifically: alive, but strike_type="custom"
# with floor_strike always None (verbal strikes via yes_sub_title instead),
# a fundamentally different shape the collector can't use regardless of
# whether the series is live. Used by ws_raw_ob_collector.py.
HOURLY_SERIES_TICKERS = [
    # crypto (12)
    "KXBTC", "KXBTCD", "KXETH", "KXETHD", "KXBNB", "KXBNBD", "KXSOLE", "KXSOLD",
    "KXXRP", "KXXRPD", "KXHYPE", "KXHYPED",
    # commodities (3) -- Sat/Sun closed, not dead; see note above
    "KXWTIH", "KXGOLDH", "KXSILVERH",
    # financials (2)
    "KXINXU", "KXNASDAQ100U",
]

_load_private_key = auth.load_private_key
_auth_headers = auth.rest_headers


def kalshi_get(path: str, params: dict = None, auth: bool = False, max_retries: int = 8) -> dict:
    """GET with built-in 429 backoff -- Kalshi's rate-limit cooldown can
    outlast a few seconds of naive exponential backoff (confirmed directly:
    a caller doing 5 attempts capped at ~15s cumulative wait still hit 429
    on every attempt), so this honors Retry-After when Kalshi sends it and
    otherwise backs off up to 60s."""
    headers = _auth_headers("GET", path) if auth else {"accept": "application/json"}
    for attempt in range(max_retries):
        resp = requests.get(BASE_URL + path, headers=headers, params=params, timeout=10)
        if resp.status_code == 429 and attempt < max_retries - 1:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = min(2 ** attempt, 60)
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def get_event_markets(event_ticker: str) -> pd.DataFrame:
    """
    Return DataFrame of all markets within an event.
    Columns: ticker, floor_strike, cap_strike, status, subtitle.
    """
    data = kalshi_get("/trade-api/v2/markets", params={"event_ticker": event_ticker})
    markets = data.get("markets", [])
    if not markets:
        # Fall back to series-level search
        series = event_ticker.split("-")[0]
        data = kalshi_get("/trade-api/v2/markets", params={"series_ticker": series})
        markets = [m for m in data.get("markets", []) if m.get("event_ticker") == event_ticker]
    rows = [
        {
            "ticker": m["ticker"],
            "floor_strike": m.get("floor_strike"),
            "cap_strike": m.get("cap_strike"),
            "status": m.get("status"),
            "subtitle": m.get("subtitle", ""),
        }
        for m in markets
    ]
    df = pd.DataFrame(rows)
    if not df.empty and "floor_strike" in df.columns:
        df = df.dropna(subset=["floor_strike"]).sort_values("floor_strike").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Orderbook & midpoint
# ---------------------------------------------------------------------------

def get_orderbook(ticker: str) -> dict:
    data = kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook")
    return data.get("orderbook", {})


def compute_midpoint(orderbook: dict) -> float | None:
    """
    Return yes-side midpoint in [0, 1].
    Uses WAP when the spread is exactly 1 tick (1 cent), otherwise simple mid.
    orderbook['yes']: list of [price_cents, qty], sorted ascending → best bid = last.
    orderbook['no']:  list of [price_cents, qty], sorted ascending → best no bid = last.
    yes ask = 100 - best_no_bid_cents.
    """
    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    if not yes_bids or not no_bids:
        return None

    best_bid_cents, bid_vol = yes_bids[-1]
    best_no_bid_cents, ask_vol = no_bids[-1]
    best_ask_cents = 100 - best_no_bid_cents

    if best_ask_cents <= best_bid_cents:
        # Crossed book — use bid as conservative estimate
        return best_bid_cents / 100.0

    spread = best_ask_cents - best_bid_cents
    if spread == 1:
        # WAP: weight each side's price by the opposing side's depth
        wap = (best_bid_cents * ask_vol + best_ask_cents * bid_vol) / (bid_vol + ask_vol)
        return wap / 100.0
    else:
        return (best_bid_cents + best_ask_cents) / 200.0


# ---------------------------------------------------------------------------
# Historical candlestick data
# ---------------------------------------------------------------------------

def get_candlesticks(ticker: str, start_ts: int, end_ts: int, period_interval: int = 60) -> pd.DataFrame:
    """
    Historical candlesticks for a single market.

    The Kalshi API parameter is `period_interval` (not period_seconds).
    Valid values: 1 (1-minute), 60 (1-hour), 1440 (1-day).

    Response prices come as dollar strings in [0, 1]:
      yes_bid.close_dollars, yes_ask.close_dollars -- and also
      yes_bid.high_dollars/low_dollars, yes_ask.high_dollars/low_dollars
      (confirmed directly the API returns full OHLC per candle, not just
      close).

    Returns DataFrame with columns: time (UTC), bid, ask, mid, volume,
    bid_high, ask_low. mid is a simple mid (no depth info available from
    candles for a WAP). bid/ask are the raw candle CLOSE prices -- don't
    use them for "did price touch X this minute" checks, since a
    price can touch and revert within one candle without it showing in the
    close (confirmed directly: a case where a resting 99c bid should have
    been reachable earlier than the close-based columns showed). Use
    bid_high/ask_low for that instead.
    """
    series = ticker.split("-")[0]
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_interval}
    try:
        path = f"/trade-api/v2/series/{series}/markets/{ticker}/candlesticks"
        data = kalshi_get(path, params=params, auth=True)
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        # The regular endpoint has a rolling retention window (empirically ~4
        # months back) -- markets older than that 404 here but are still served
        # by the historical-namespaced endpoint, just with different response
        # field names ("close"/"volume" instead of "close_dollars"/"volume_fp").
        path = f"/trade-api/v2/historical/markets/{ticker}/candlesticks"
        data = kalshi_get(path, params=params, auth=True)

    candles = data.get("candlesticks", [])
    rows = []
    for c in candles:
        ts = c.get("end_period_ts")
        yes_bid = c.get("yes_bid") or {}
        yes_ask = c.get("yes_ask") or {}
        bid_str = yes_bid.get("close_dollars", yes_bid.get("close"))
        ask_str = yes_ask.get("close_dollars", yes_ask.get("close"))
        vol = float(c.get("volume_fp", c.get("volume", 0)) or 0)
        if bid_str is None or ask_str is None:
            continue
        bid = float(bid_str)
        ask = float(ask_str)
        bid_high_str = yes_bid.get("high_dollars", yes_bid.get("high"))
        ask_low_str = yes_ask.get("low_dollars", yes_ask.get("low"))
        bid_high = float(bid_high_str) if bid_high_str is not None else bid
        ask_low = float(ask_low_str) if ask_low_str is not None else ask
        # WAP degrades to simple mid without depth; still apply 1-tick check
        mid = (bid + ask) / 2.0
        rows.append({
            "time": ts, "bid": bid, "ask": ask, "mid": mid, "volume": vol,
            "bid_high": bid_high, "ask_low": ask_low,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.sort_values("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Trade history
# ---------------------------------------------------------------------------

def get_market_trades(ticker: str) -> pd.DataFrame:
    """
    Every individual trade for a single market, via GET
    /trade-api/v2/markets/trades, following cursor pagination to
    completion.

    Returns DataFrame with columns: time (UTC), yes_price, no_price
    (dollars, in [0, 1], complementary), count (contracts in that trade,
    fractional lots are real -- confirmed directly, e.g. count_fp="0.10"),
    taker_side.

    Unlike get_candlesticks, there is NO historical-namespaced fallback for
    trades -- both /trade-api/v2/historical/markets/trades and the
    per-ticker variant 404. The regular endpoint has a real rolling
    retention window, confirmed directly by date-bisecting daily
    markets: 2026-05-20 returns 0 trades (even with explicit min_ts/max_ts
    spanning that day), 2026-05-25 onward returns real trade data -- so
    this silently returns an empty DataFrame for markets outside roughly
    the last ~2.5 months, not just ones that genuinely had zero volume.
    Treat an empty result as "no data available for this market", not
    "confirmed zero trades," unless the market is recent enough to be in
    the window.
    """
    rows = []
    cursor = None
    while True:
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/trade-api/v2/markets/trades", params=params, auth=True)
        trades = data.get("trades", [])
        for t in trades:
            rows.append({
                "time": t["created_time"],
                "yes_price": float(t["yes_price_dollars"]),
                "no_price": float(t["no_price_dollars"]),
                "count": float(t.get("count_fp", t.get("count", 0)) or 0),
                "taker_side": t.get("taker_side"),
            })
        cursor = data.get("cursor")
        if not cursor or not trades:
            break

    df = pd.DataFrame(rows, columns=["time", "yes_price", "no_price", "count", "taker_side"])
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.sort_values("time").reset_index(drop=True)
    return df
