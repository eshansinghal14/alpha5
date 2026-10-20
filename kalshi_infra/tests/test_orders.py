import types

from kalshi_infra.kalshi.orders import KalshiOrderClient


def _client(body):
    c = KalshiOrderClient.__new__(KalshiOrderClient)
    resp = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    c._session = types.SimpleNamespace(get=lambda *a, **k: resp)
    c._headers = lambda method, path: {}
    c.timeout_s = 1.0
    return c


def test_queue_positions_null_list_is_empty():
    assert _client({"queue_positions": None}).queue_positions("E") == {}
    assert _client({}).queue_positions("E") == {}


def test_cancel_goes_out_on_its_own_session():
    c = KalshiOrderClient.__new__(KalshiOrderClient)
    used = []
    resp = types.SimpleNamespace(status_code=200)
    c._session = types.SimpleNamespace(delete=lambda *a, **k: used.append("shared") or resp)
    c._cancel_session = types.SimpleNamespace(delete=lambda *a, **k: used.append("cancel") or resp)
    c._headers = lambda method, path: {}
    c.timeout_s, c.shard = 1.0, 2
    assert c.cancel("x1", "T") is True and used == ["cancel"]


def test_sign_with_either_key_type():
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
    from kalshi_infra.kalshi import auth
    ek = ed25519.Ed25519PrivateKey.generate()
    ts, sig = auth.sign("DELETE", "/p", ek)
    ek.public_key().verify(base64.b64decode(sig), f"{ts}DELETE/p".encode())
    rk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ts, sig = auth.sign("GET", "/p", rk)
    rk.public_key().verify(base64.b64decode(sig), f"{ts}GET/p".encode(),
                           padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                           hashes.SHA256())


def test_queue_positions_parses_entries():
    body = {"queue_positions": [{"order_id": "a", "queue_position_fp": "120.00"},
                                {"order_id": "b", "queue_position_fp": None}, {"order_id": None}]}
    assert _client(body).queue_positions("E") == {"a": 120.0, "b": 0.0}
