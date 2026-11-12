import types

import pytest

from infra.kalshi import orders as O


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    b = O.SharedWriteBudget(300.0, 0, path=str(tmp_path / "read.bucket"))
    monkeypatch.setattr(O, "_READ", b)
    return b


def _resp(method, path, status=200):
    return types.SimpleNamespace(request=types.SimpleNamespace(method=method, path_url=path), status_code=status)


def test_reads_spend_tokens_and_a_429_empties_the_bucket(bucket):
    t0 = bucket.tokens_now()
    O._count_read(_resp("GET", "/trade-api/v2/portfolio/fills?limit=200"))
    assert t0 - bucket.tokens_now() == pytest.approx(O.READ_COST, abs=1.0)
    O._count_read(_resp("POST", "/trade-api/v2/portfolio/orders"))          # a write: not a read
    assert t0 - bucket.tokens_now() == pytest.approx(O.READ_COST, abs=1.0)
    O._count_read(_resp("GET", "/trade-api/v2/portfolio/positions", 429))
    assert bucket.tokens_now() < 5.0


def test_balance_read_held_back_when_the_bucket_is_low(bucket):
    sent = []
    c = object.__new__(O.KalshiOrderClient)
    c._session = types.SimpleNamespace(get=lambda *a, **kw: sent.append(a))
    c._headers = lambda *a: {}
    c.timeout_s = 1.0
    bucket.take(250.0)
    with pytest.raises(O.ReadBudgetLow):
        c.shard_balances(reserve=100.0)
    assert not sent
    assert bucket.tokens_now() > 49.0                                       # nothing spent
