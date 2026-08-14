"""
Kalshi API functions for vol trading analysis.
Handles auth, market discovery, orderbook midpoints, and historical candlestick data.
"""
import base64
import datetime
import os
import time

import requests
import pandas as pd
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from keys import KALSHI_API_KEY as API_KEY

BASE_URL = "https://api.elections.kalshi.com"
SERIES_TICKER = "KXHIGHLAX"

# Every active daily highest/lowest temperature series with "sufficient"
# volume -- surveyed directly against today's open-event volume_fp, summed
# per series: every KXHIGH*/KXLOW* daily-temp series cleared >=10,000
# contracts of cumulative volume, vs. a sharp drop-off to ~8,500 for the
# next-highest excluded series (a handful of legacy/retired tickers, e.g.
# bare "HIGHAUS" vs. "KXHIGHAUS", showed 0 open markets and were dropped
# outright). Used by ws_raw_ob_collector.py.
HIGH_LOW_TEMP_SERIES_TICKERS = [
    # highest temperature (17)
    "KXHIGHLAX", "KXHIGHNY", "KXHIGHMIA", "KXHIGHCHI", "KXHIGHTPHX", "KXHIGHTDAL",
    "KXHIGHTSFO", "KXHIGHTBOS", "KXHIGHAUS", "KXHIGHTATL", "KXHIGHTHOU", "KXHIGHDEN",
    "KXHIGHTSEA", "KXHIGHPHIL", "KXHIGHTLV", "KXHIGHTNOLA", "KXHIGHTOKC",
    # lowest temperature (5)
    "KXLOWTNYC", "KXLOWTOKC", "KXLOWTAUS", "KXLOWTMIN", "KXLOWTLAX",
]

_KEY_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "kalshi_btc_hedge", "eshan-kalshi-rsa-private-key.txt"),
    os.path.join(os.path.dirname(__file__), "eshan-kalshi-rsa-private-key.txt"),
]


def _load_private_key():
    for path in _KEY_CANDIDATES:
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            with open(abs_path, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    raise FileNotFoundError(f"RSA private key not found; checked: {_KEY_CANDIDATES}")


def _auth_headers(method: str, path: str) -> dict:
    private_key = _load_private_key()
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "accept": "application/json",
    }


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

def event_ticker_from_date(date: datetime.date, series_ticker: str = SERIES_TICKER) -> str:
    """Convert a date to an event ticker for the given series, e.g.
    (2026-08-01, "KXHIGHLAX") → KXHIGHLAX-26AUG01. Defaults to SERIES_TICKER
    (KXHIGHLAX) so every existing single-series caller keeps working
    unchanged."""
    return f"{series_ticker}-{date.strftime('%y%b%d').upper()}"


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


def get_snapshot_midpoints(event_ticker: str) -> pd.DataFrame:
    """
    Snapshot of current midpoints for every market in an event.
    Returns DataFrame: ticker, floor_strike, midpoint (in [0,1]).
    """
    markets = get_event_markets(event_ticker)
    rows = []
    for _, row in markets.iterrows():
        try:
            ob = get_orderbook(row["ticker"])
            mid = compute_midpoint(ob)
            rows.append({
                "ticker": row["ticker"],
                "floor_strike": row["floor_strike"],
                "cap_strike": row.get("cap_strike"),
                "midpoint": mid,
            })
        except Exception as e:
            print(f"  [warn] {row['ticker']}: {e}")
    return pd.DataFrame(rows)


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
    candles for a WAP). bid/ask are the raw candle CLOSE prices, meant for
    black_scholes.fit_normal_to_bucket_bid_ask (fits within each market's
    [bid, ask] band rather than to the single mid point estimate) -- don't
    repurpose those two for "did price touch X this minute" checks, since a
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
    retention window, confirmed directly by date-bisecting KXHIGHLAX
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
