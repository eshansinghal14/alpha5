import pytest
import requests

from infra.kalshi import api


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self.body, self.headers = code, body or {}, {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _serve(monkeypatch, codes):
    seq = iter(codes)
    slept = []
    sess = type("S", (), {"get": staticmethod(lambda *a, **k: _Resp(next(seq), {"ok": True}))})()
    monkeypatch.setattr(api, "_session", lambda: sess)
    monkeypatch.setattr(api.time, "sleep", slept.append)
    return slept


def test_a_kalshi_5xx_is_retried_with_backoff(monkeypatch):
    slept = _serve(monkeypatch, [500, 503, 200])
    assert api.kalshi_get("/x") == {"ok": True}
    assert slept == [1, 2]


def test_a_5xx_that_lasts_raises_after_the_last_try(monkeypatch):
    slept = _serve(monkeypatch, [500] * 3)
    with pytest.raises(requests.exceptions.HTTPError):
        api.kalshi_get("/x", max_retries=3)
    assert slept == [1, 2]


def test_a_client_error_raises_at_once(monkeypatch):
    slept = _serve(monkeypatch, [404, 200])
    with pytest.raises(requests.exceptions.HTTPError):
        api.kalshi_get("/x")
    assert slept == []
