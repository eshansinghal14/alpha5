"""
Diagnostic script — run from the kalshi_vol_trading directory.
Tests various endpoint paths for historical market data.
"""
import base64
import datetime
import os
import sys
import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_infra.keys import KALSHI_API_KEY as API_KEY
from kalshi_infra.kalshi.auth import private_key_path

BASE_URL = "https://api.elections.kalshi.com"
KEY_PATH = private_key_path()

TICKER = "KXHIGHLAX-26AUG01-B82.5"  # one market to probe
SERIES = "KXHIGHLAX"

# Use a recent narrow window so we know data should exist
NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
START = NOW - 4 * 3600  # 4 hours ago
END = NOW


def _auth_headers(method, path):
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + method.upper() + path).encode("utf-8")
    sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "accept": "application/json",
    }


def probe(label, path, params, auth=True):
    headers = _auth_headers("GET", path) if auth else {"accept": "application/json"}
    url = BASE_URL + path
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        body = resp.text[:500]
        print(f"\n[{label}]  {resp.status_code}  {url}")
        print(f"  params : {params}")
        print(f"  body   : {body}")
    except Exception as e:
        print(f"\n[{label}] EXCEPTION: {e}")


# ---- period_interval with various values ----
for val in [60, 1, 5, 10, 15, 30, 1440]:
    probe(f"period_interval={val}",
          f"/trade-api/v2/series/{SERIES}/markets/{TICKER}/candlesticks",
          {"start_ts": START, "end_ts": END, "period_interval": val}, auth=True)

# ---- Full market detail to see floor_strike ----
probe("market detail",
      f"/trade-api/v2/markets/{TICKER}",
      {}, auth=False)
