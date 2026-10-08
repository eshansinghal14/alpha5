"""
Free S&P 500 cash-index quotes.

Databento sells no SPX cash-index feed (confirmed in databento_functions'
own notes, which is also why an SPY proxy was tried and abandoned), but the
cash leg is free elsewhere: Yahoo's chart endpoint serves ^GSPC 1-minute
bars with no key and no meaningful delay -- measured 2026-08-28, the newest
bar was 2-9s old over three samples with a ~200ms round trip.
"""
import datetime

import pandas as pd
import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
# Yahoo rejects a bare programmatic UA.
_HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT_S = 15


def spx_1m(start: datetime.datetime, end: datetime.datetime) -> pd.Series:
    """SPX cash 1-minute closes over [start, end], UTC-indexed.

    Bar timestamps align 1:1 with Databento's ohlcv-1m ts_event -- verified
    by shifting +/-2 minutes and confirming zero shift minimises the spread
    of ES - SPX (std 0.45 at 0, 1.55 at +/-1). So the two can be joined on
    the index with no adjustment.
    """
    resp = requests.get(CHART_URL, headers=_HEADERS, timeout=TIMEOUT_S,
                        params={"interval": "1m",
                                "period1": int(start.timestamp()),
                                "period2": int(end.timestamp())})
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    idx = pd.to_datetime(result["timestamp"], unit="s", utc=True)
    return pd.Series(closes, index=idx, name="spx").dropna()


def spx_last() -> tuple[float, datetime.datetime]:
    """(price, as_of) for the latest SPX print. Seconds old in practice."""
    resp = requests.get(CHART_URL, headers=_HEADERS, timeout=TIMEOUT_S,
                        params={"interval": "1m", "range": "1d"})
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    return (float(meta["regularMarketPrice"]),
            datetime.datetime.fromtimestamp(meta["regularMarketTime"], datetime.timezone.utc))
