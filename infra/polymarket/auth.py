"""Ed25519 request signing for Polymarket US: keys.POLYMARKET_US_API_KEY
(key id) and keys.POLYMARKET_US_SECRET_KEY (base64). Signs the same message
as Kalshi (timestamp_ms + METHOD + path) under X-PM-* headers. The market
websocket needs these too, though the REST market data does not."""
import base64
import functools
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@functools.lru_cache(maxsize=1)
def _key() -> tuple[str, Ed25519PrivateKey]:
    from infra.keys import POLYMARKET_US_API_KEY, POLYMARKET_US_SECRET_KEY
    # The secret is seed + public key (64 bytes); the seed is the first half.
    seed = base64.b64decode(POLYMARKET_US_SECRET_KEY)[:32]
    return POLYMARKET_US_API_KEY, Ed25519PrivateKey.from_private_bytes(seed)


def headers(method: str, path: str) -> dict[str, str]:
    key_id, key = _key()
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(key.sign(f"{ts}{method.upper()}{path}".encode())).decode()
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig}
