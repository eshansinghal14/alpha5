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


def test_queue_positions_parses_entries():
    body = {"queue_positions": [{"order_id": "a", "queue_position_fp": "120.00"},
                                {"order_id": "b", "queue_position_fp": None}, {"order_id": None}]}
    assert _client(body).queue_positions("E") == {"a": 120.0, "b": 0.0}
