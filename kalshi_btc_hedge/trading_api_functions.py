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
from websocket import create_connection
import threading


from api_keys import load_kalshi_api_key

def sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    # Before signing, we need to hash our message.
    # The hash is what we actually sign.
    # Convert the text to bytes
    message = text.encode('utf-8')

    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    except InvalidSignature as e:
        raise ValueError("RSA sign PSS failed") from e

def get_kalshi_btc_spreads(max_close_time_s):
    kalshi_api_key = load_kalshi_api_key()
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {kalshi_api_key}"
    }

    url = "https://api.elections.kalshi.com/trade-api/v2/markets"
    max_close_ts = int(datetime.now().timestamp() + max_close_time_s)
    params = {"series_ticker": "KXBTCD", "status": "open", "max_close_ts": max_close_ts}
    markets = requests.get(url, headers=headers, params=params).json()['markets']

    kalshi_df = pd.DataFrame(markets)
    kalshi_df = kalshi_df[['subtitle', 'no_ask', 'yes_ask', 'close_time']].copy()
    kalshi_df['ceil_price'] = kalshi_df['subtitle'].str.split().str[0].str[1:].str.replace(',', '', regex=False).astype(int)
    kalshi_df['close_time'] = pd.to_datetime(kalshi_df['close_time'], utc=True).dt.tz_convert('America/Chicago')
    return kalshi_df

    # if we need specific volume info
    # url = "https://api.elections.kalshi.com/trade-api/v2/markets/KXBTCD-25JUN0822-T105749.99/orderbook"
    # headers = {
    #     "accept": "application/json",
    #     "Authorization": f"Bearer {kalshi_api_key}"
    # }
    # params = {"depth": 1}
    # response = requests.get(url, headers=headers, params=params)

    # print(response.json())['yes'][-1] # its like 1 - this price should be equal to what we got before


def get_recent_btc_data():
    conn = http.client.HTTPSConnection("api.exchange.coinbase.com")
    headers = {
        'User-Agent':   "my-trading-bot/1.0",
        'Content-Type': 'application/json'
    }
    params = {
        "granularity": 60,
        "end": datetime.now(timezone.utc)
    }
    conn.request("GET", f"/products/BTC-USD/candles?{urlencode(params)}", '', headers)
    res = conn.getresponse()
    data = json.loads(res.read())

    btc_df = pd.DataFrame(data, columns=['Timestamp', 'Low', 'High', 'Open', 'Close', 'Volume'])
    btc_df['Date'] = pd.to_datetime(btc_df['Timestamp'], unit='s').dt.tz_localize('UTC').dt.tz_convert('America/Chicago').dt.tz_localize(None)
    btc_df['Price'] = (btc_df['High'] + btc_df['Low']) / 2.0
    btc_df = btc_df[['Timestamp', 'Date', 'Low', 'High', 'Open', 'Close', 'Volume', 'Price']]
    btc_df.sort_values('Date', ascending=True, inplace=True)
    btc_df = btc_df.drop(['Open', 'Close', 'Volume'], axis=1)
    return btc_df


if __name__ == "__main__":
    get_recent_btc_data()
    # get_kalshi_btc_spreads()
