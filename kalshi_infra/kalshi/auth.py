"""RSA-PSS request signing for Kalshi REST and websocket.

One key for both: keys.KALSHI_PRIVATE_KEY_PATH, paired with
keys.KALSHI_API_KEY. A mismatched key only shows up on genuinely
authenticated endpoints (balance, positions, orders) -- market/orderbook
reads are public and accept any signature.
"""
import base64
import datetime
import functools
import os

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kalshi_infra import REPO_ROOT
from kalshi_infra.keys import KALSHI_API_KEY as API_KEY, KALSHI_PRIVATE_KEY_PATH

WS_PATH = "/trade-api/ws/v2"


def private_key_path() -> str:
    p = KALSHI_PRIVATE_KEY_PATH
    return p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)


@functools.lru_cache(maxsize=1)
def load_private_key():
    with open(private_key_path(), "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def sign(method: str, path: str, key=None) -> tuple[str, str]:
    """(timestamp_ms, base64 signature) for `method path`. The key type picks
    the algorithm (Kalshi verifies by the registered public key).

    Ed25519 is the fast one: on the c7g box 10-20, RSA-PSS 1.32ms per sign
    vs Ed25519 0.044ms, and the RSA sign held the GIL (a spinning main
    thread ran at half speed beside a signing thread), so every write cost
    the trading loop ~1.3ms."""
    key = key or load_private_key()
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + method.upper() + path).encode("utf-8")
    if isinstance(key, Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    return ts, base64.b64encode(sig).decode("utf-8")


def rest_headers(method: str, path: str, key=None) -> dict:
    ts, sig = sign(method, path, key)
    return {"KALSHI-ACCESS-KEY": API_KEY, "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts, "accept": "application/json"}


def ws_headers() -> list[tuple[str, str]]:
    ts, sig = sign("GET", WS_PATH)
    return [("KALSHI-ACCESS-KEY", API_KEY), ("KALSHI-ACCESS-SIGNATURE", sig),
            ("KALSHI-ACCESS-TIMESTAMP", ts)]
