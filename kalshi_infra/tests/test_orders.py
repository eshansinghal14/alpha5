import time
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


_TWO = r"""
import sys, time
sys.path.insert(0, {root!r})
from kalshi_infra.kalshi.orders import SharedWriteBudget
b = SharedWriteBudget(1.0, 2, path={path!r})       # 1 token/s: no meaningful refill during the test
print(sum(b.take(0.1) for _ in range(20)))
"""


def test_shared_bucket_is_one_budget_across_processes(tmp_path):
    """Two processes on one bucket file: together they get the 1-token bucket
    once (10 takes of 0.1), not once each."""
    import subprocess, sys
    from kalshi_infra import REPO_ROOT
    from kalshi_infra.kalshi.orders import SharedWriteBudget
    path = str(tmp_path / "b.bucket")
    SharedWriteBudget(1.0, 2, path=path).tokens_now()
    code = _TWO.format(root=str(REPO_ROOT), path=path)
    t0 = time.monotonic()
    outs = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
            for _ in range(2)]
    refill = int((time.monotonic() - t0) * 10) + 1          # 0.1-token takes the refill paid for
    assert 10 <= sum(int(o) for o in outs) <= 10 + refill


def test_shared_bucket_refills_and_empties(tmp_path):
    from kalshi_infra.kalshi.orders import SharedWriteBudget
    b = SharedWriteBudget(1.0, 2, path=str(tmp_path / "b.bucket"))
    assert b.take(1.0) and not b.take(0.5)
    assert b.take(0.2, force=True) and b.tokens_now() < 0
    b2 = SharedWriteBudget(1.0, 2, path=str(tmp_path / "b.bucket"))        # second handle, same bucket
    assert b2.tokens_now() < 0.1
    time.sleep(0.5)
    assert 0.2 < b2.tokens_now() < 0.6                                       # refilled at 1/s


def test_per_series_cap_limits_creates_never_cancels():
    from kalshi_infra.kalshi.orders import WriteBudget
    c = KalshiOrderClient.__new__(KalshiOrderClient)
    c.budget = WriteBudget(1000.0)
    c._lock = __import__("threading").Lock()
    c._spent, c._refused = {}, {}
    cap = {"v": 20.0}
    c.cap_fn = lambda: cap["v"]
    assert c.allow_now("order") and c.allow_now("order") and not c.allow_now("order")
    assert c.allow_now("cancel", force=True)
    assert c.tokens_now() < 0                                     # the cap's own bucket went negative
    cap["v"] = None
    assert c.allow_now("order")                                   # cap off: shared bucket only


def test_queue_positions_parses_entries():
    body = {"queue_positions": [{"order_id": "a", "queue_position_fp": "120.00"},
                                {"order_id": "b", "queue_position_fp": None}, {"order_id": None}]}
    assert _client(body).queue_positions("E") == {"a": 120.0, "b": 0.0}
