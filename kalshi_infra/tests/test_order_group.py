"""Order group: created at arm, every create tagged, tripped past the limit
(pull + no creates), reset after order_group_reset_s, closed at disarm."""
import asyncio
import dataclasses
import time

import pandas as pd
import requests

from kalshi_infra.kalshi import orders as ko
from kalshi_infra.tests.test_engine_async import ArmClient, _settle, setup, fair
from kalshi_infra.trading import order_manager as om


class GroupClient(ArmClient):
    def __init__(self):
        super().__init__()
        self.order_group_id = None
        self.groups, self.resets, self.deleted = [], [], []
        self.reject_group = False

    def create_order_group(self, limit):
        self.groups.append(limit)
        return f"og{len(self.groups)}"

    def reset_order_group(self, gid):
        self.resets.append((time.monotonic(), gid))

    def delete_order_group(self, gid):
        self.deleted.append(gid)

    def place_resting(self, order):
        if self.reject_group:
            resp = requests.Response()
            resp.status_code = 400
            resp._content = b'{"error": {"code": "order_group_triggered", "message": "order group triggered"}}'
            raise requests.HTTPError("400", response=resp)
        return super().place_resting(order)


def _armed(limit=10.0, reset_s=0.2):
    eng, _, _ = setup()
    eng.quoting.order_group_limit, eng.quoting.order_group_reset_s = limit, reset_s
    client = GroupClient()
    eng.order_client = client
    eng.order_events = []
    return eng, client


def _fill(eng, size, exch_id="ex1"):
    eng.on_real_fill({"ticker": "A", "side": "yes", "price": 0.41, "size": size, "fee": 0.0,
                      "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker",
                      "order_id": exch_id, "trade_id": f"t{time.monotonic_ns()}"})


def test_group_created_at_arm_and_creates_tagged():
    async def main():
        eng, client = _armed()
        assert await eng.arm_async() is True
        assert client.groups == [10] and client.order_group_id == "og1"
        assert eng.og_summary()["limit"] == 10.0 and not eng.og_tripped()

    asyncio.run(main())


def test_no_group_when_limit_is_null():
    async def main():
        eng, client = _armed(limit=None)
        assert await eng.arm_async() is True
        assert client.groups == [] and client.order_group_id is None and eng.og_summary() is None

    asyncio.run(main())


def test_trips_past_limit_pulls_blocks_then_resets():
    async def main():
        eng, client = _armed(limit=10.0, reset_s=0.2)
        eng.quoting.requote_delay_s = 0.0           # the per-side requote hold is separate from the group
        assert await eng.arm_async() is True
        fair(eng)
        await _settle(eng, 0.15)
        live = [q for q in eng.om.live() if q.state == om.RESTING]
        assert len(live) == 2
        x = live[0].exchange_id
        _fill(eng, 10.0, x)                       # one full fill: at the limit, not past it
        assert not eng.og_tripped()
        _fill(eng, 1.0, live[1].exchange_id)      # the next contract trips it
        assert eng.og_tripped()
        assert any(r["event"] == "og_trip" for r in eng.order_events)
        n = eng.om.counts.get("create", 0)
        fair(eng, 0.48)
        await _settle(eng, 0.1)
        assert eng.om.counts.get("create", 0) == n and not eng.om.live()   # pulled, nothing re-created
        await _settle(eng, 0.3)                    # reset lands after 0.2 s
        assert client.resets and client.resets[0][1] == "og1" and not eng.og_tripped()
        fair(eng, 0.50)
        await _settle(eng, 0.15)
        assert eng.om.counts.get("create", 0) > n and eng.om.live()           # quoting again

    asyncio.run(main())


def test_group_reject_on_create_counts_as_trip_not_error():
    async def main():
        eng, client = _armed()
        assert await eng.arm_async() is True
        client.reject_group = True
        fair(eng)
        await _settle(eng, 0.15)
        assert eng.og_tripped() and eng.consecutive_order_errors == 0
        client.reject_group = False
        await _settle(eng, 0.3)
        assert not eng.og_tripped()

    asyncio.run(main())


def test_disarm_deletes_group_and_untags_client():
    async def main():
        eng, client = _armed()
        assert await eng.arm_async() is True
        eng.disarm_nowait()
        await eng.wait_sweep()
        await asyncio.sleep(0.05)
        assert client.deleted == ["og1"] and client.order_group_id is None and eng.og_summary() is None

    asyncio.run(main())


def test_order_group_reject_detection():
    resp = requests.Response()
    resp.status_code = 400
    resp._content = b'{"error": {"code": "order_group_limit_reached"}}'
    assert ko.is_order_group_reject(requests.HTTPError("x", response=resp))
    resp._content = b'{"error": {"code": "invalid_price"}}'
    assert not ko.is_order_group_reject(requests.HTTPError("x", response=resp))


def test_create_body_carries_group_id():
    class _Sess:
        def __init__(self):
            self.body = None

        def post(self, url, headers=None, json=None, timeout=None):
            self.body = json
            r = requests.Response()
            r.status_code = 201
            r._content = b'{"order_id": "x", "fill_count": "0", "remaining_count": "1"}'
            return r
    c = ko.KalshiOrderClient.__new__(ko.KalshiOrderClient)
    c._session, c.timeout_s, c.shard = _Sess(), 1.0, 2
    c._headers = lambda m, p: {}
    c.order_group_id = "og9"
    from kalshi_backtest.core import Order
    o = Order(order_id=1, ticker="T", side="yes", order_type="limit", price=0.4, size=1.0,
              decision_time=pd.Timestamp.now(tz="UTC"), intent="quote")
    c._create(o, "good_till_canceled", post_only=True)
    assert c._session.body["order_group_id"] == "og9" and c._session.body["exchange_index"] == 2
