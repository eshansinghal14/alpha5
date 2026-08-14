import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
import requests
from datetime import datetime, timedelta, timezone
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import http.client
from urllib.parse import urlencode
import pandas as pd
from websocket import create_connection, WebSocket, WebSocketApp
import threading
from api_keys import load_kalshi_rsa_private_key, sign_pss_text, load_kalshi_api_key
from functools import partial
from requests.exceptions import ChunkedEncodingError


def get_kalshi_btc_markets():
    headers = {
        "accept": "application/json",
    }

    url = "https://api.elections.kalshi.com/trade-api/v2/markets"
    max_close_ts = int(datetime.now().timestamp() + 3600)
    params = {"series_ticker": "KXBTCD", "status": "open", "max_close_ts": max_close_ts}

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            kalshi_df = pd.DataFrame(resp.json()['markets'])
            kalshi_df = kalshi_df[['ticker', 'event_ticker', 'floor_strike']].copy()
            return kalshi_df
        except ChunkedEncodingError:
            print(f"ChunkedEncodingError, retrying attempt {attempt+1}/3...")
            time.sleep(1)
        except Exception as e:
            print(f"Other error: {e}")
            break
    raise RuntimeError("Failed to fetch Kalshi BTC spreads after 3 attempts.")

def get_kalshi_btc_trades(ticker, floor_price):
    headers = {
        "accept": "application/json",
    }

    url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    params = {}

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            rows = []
            if resp.json()['orderbook']['yes']:
                for price, vol in resp.json()['orderbook']['yes'][-3:]:
                    rows.append({'floor_price': floor_price, 'side': 'no', 'price': 1 - price / 100, 'contracts': vol})
            if resp.json()['orderbook']['no']:
                for price, vol in resp.json()['orderbook']['no'][-3:]:
                    rows.append({'floor_price': floor_price, 'side': 'yes', 'price': 1 - price / 100, 'contracts': vol})
            trades = pd.DataFrame(rows, columns=['floor_price', 'side', 'price', 'contracts'])
            return trades
        except ChunkedEncodingError:
            print(f"ChunkedEncodingError, retrying attempt {attempt+1}/3...")
            time.sleep(1)
        except Exception as e:
            print(f"Other error: {e}")
            break
    raise RuntimeError("Failed to fetch Kalshi BTC spreads after 3 attempts.")


def get_kalshi_btc_price_vol(start_price, end_price):
    kalshi_btc_markets = get_kalshi_btc_markets()
    kalshi_btc_markets = kalshi_btc_markets[(kalshi_btc_markets['floor_strike'] >= start_price) & (kalshi_btc_markets['floor_strike'] <= end_price)]
    rows = []
    for i in range(len(kalshi_btc_markets)):
        try:
            trades = get_kalshi_btc_trades(kalshi_btc_markets['ticker'].iloc[i], kalshi_btc_markets['floor_strike'].iloc[i])
            rows.append(trades)
        except RuntimeError as e:
            print(f"Error fetching trades for {kalshi_btc_markets['ticker'].iloc[i]}: {e}")

    kalshi_btc_trades = pd.concat(rows, ignore_index=True)
    return kalshi_btc_trades

def get_recent_btc_data(minutes):
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=minutes)
    earliest = now - window

    all_candles = []
    # Coinbase caps at 300 data points per request:
    chunk = timedelta(seconds=60 * 300)

    slice_start = earliest
    while slice_start < now:
        slice_end = min(slice_start + chunk, now)
        # print(f"fetching {slice_start} to {slice_end}")
        data = fetch_candles(slice_start, slice_end, 60)
        all_candles += data
        slice_start = slice_end  # advance window

    btc_df = pd.DataFrame(all_candles, columns=['Timestamp', 'Low', 'High', 'Open', 'Close', 'Volume'])
    btc_df['Date'] = pd.to_datetime(btc_df['Timestamp'], unit='s').dt.tz_localize('UTC').dt.tz_convert('America/Chicago').dt.tz_localize(None)
    btc_df['Price'] = (btc_df['High'] + btc_df['Low']) / 2.0
    btc_df = btc_df[['Timestamp', 'Date', 'Low', 'High', 'Open', 'Close', 'Volume', 'Price']]
    btc_df.sort_values('Date', ascending=True, inplace=True)
    btc_df = btc_df.drop(['Open', 'Close', 'Volume'], axis=1)
    return btc_df

def fetch_candles(start: datetime, end: datetime, granularity: int):
    conn = http.client.HTTPSConnection("api.exchange.coinbase.com")
    headers = {
        'User-Agent':   "my-trading-bot/1.0",
        'Content-Type': 'application/json'
    }
    params = {
        "start":       start.isoformat(),
        "end":         end.isoformat(),
        "granularity": granularity
    }
    conn.request(
        "GET",
        f"/products/BTC-USD/candles?{urlencode(params)}",
        '',
        headers
    )
    res = conn.getresponse()
    return json.loads(res.read())


if __name__ == "__main__":
    get_kalshi_btc_price_vol(105000, 107000)



