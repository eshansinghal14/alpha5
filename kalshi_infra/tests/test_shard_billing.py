"""Per-shard write billing: every order write carries the client's
exchange_index, and series only split the budget with their own shard."""
import types

from kalshi_backtest.core import Order
from kalshi_infra.kalshi.orders import KalshiOrderClient
from kalshi_strategies.run import write_shares


class _Resp:
    status_code = 200
    reason = "OK"
    url = "x"
    text = ""

    def json(self):
        return {"order_id": "o1", "fill_count": "0", "remaining_count": "1"}


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, json, None))
        return _Resp()

    def delete(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("delete", url, None, params))
        return _Resp()


def _client(shard):
    c = KalshiOrderClient.__new__(KalshiOrderClient)
    c._session = _Session()
    c._cancel_session = c._session          # one recorder for every write, in order
    c._key = None
    c.timeout_s = 1.0
    c.shard = shard
    c._headers = lambda m, p: {}
    return c


def _order():
    return Order(order_id=1, ticker="KXCRYPTOLEAD15M-X-BTC", side="yes", order_type="limit",
                 price=0.41, size=1.0, decision_time=None, intent="quote")


def test_create_amend_cancel_carry_exchange_index():
    c = _client(2)
    c.place_resting(_order())
    c.amend("o1", _order(), 1.0, 0.0, price=0.42)
    c.cancel("o1", "KXCRYPTOLEAD15M-X-BTC")
    (_, _, create, _), (_, _, amend, _), (_, _, _, cancel) = c._session.calls
    assert create["exchange_index"] == 2 and amend["exchange_index"] == 2
    assert cancel["exchange_index"] == 2 and cancel["market_ticker"] == "KXCRYPTOLEAD15M-X-BTC"


def test_resting_quotes_are_post_only_takes_are_not():
    c = _client(2)
    c.place_resting(_order())
    c.place_ioc(_order())
    resting, take = c._session.calls[0][2], c._session.calls[1][2]
    assert resting["post_only"] is True and resting["time_in_force"] == "good_till_canceled"
    assert "post_only" not in take and take["time_in_force"] == "immediate_or_cancel"


def test_unpinned_client_sends_no_index():
    c = _client(None)
    c.place_resting(_order())
    assert "exchange_index" not in c._session.calls[0][2]


def test_shares_split_only_within_a_shard():
    shard = {"KXCRYPTOLEAD15M": 2, "KXINX": 0, "KXINXU": 0}
    s = write_shares([["KXCRYPTOLEAD15M"], ["KXINX"], ["KXINXU"]], shard.get)
    assert s == {"KXCRYPTOLEAD15M": 1.0, "KXINX": 0.5, "KXINXU": 0.5}


def test_write_group_is_one_process_with_the_whole_shard():
    shard = {"KXCRYPTOLEAD15M": 2, "KXINX": 0, "KXINXU": 0, "KXINXDUD": 0}
    s = write_shares([["KXCRYPTOLEAD15M"], ["KXINX", "KXINXU", "KXINXDUD"]], shard.get)
    assert s == {"KXCRYPTOLEAD15M": 1.0, "KXINX": 1.0}


def test_unknown_shard_falls_back_to_equal_split():
    def boom(s):
        raise RuntimeError("down")
    s = write_shares([["A"], ["B"]], boom)
    assert s == {"A": 0.5, "B": 0.5}
