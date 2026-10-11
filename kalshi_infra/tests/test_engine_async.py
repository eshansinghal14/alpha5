"""The quote path under a running event loop: creates and cancels run on
executor threads and their results come back via call_soon_threadsafe. The
synchronous tests can't see ordering or thread bugs; this can."""
import asyncio
import time

import pandas as pd

from kalshi_infra.trading import order_manager as om
from kalshi_infra.tests.test_engine_quotes import FakeClient, book, setup, fair


class SlowClient(FakeClient):
    """Every REST call takes 20ms, like a real round trip."""

    def place_resting(self, order):
        time.sleep(0.02)
        return super().place_resting(order)

    def cancel(self, exchange_id, ticker=None):
        time.sleep(0.02)
        return super().cancel(exchange_id, ticker)


async def _settle(eng, s=0.3):
    t = time.monotonic() + s
    while time.monotonic() < t:
        eng.update_book(book())
        await asyncio.sleep(0.01)


def test_real_ladder_under_loop_rests_and_pulls_cleanly():
    async def main():
        eng, quoter, _ = setup(real=True)
        client = SlowClient()
        eng.order_client = client
        fair(eng)
        await _settle(eng)
        assert len(client.creates) == 6
        assert all(q.state == om.RESTING for q in eng.om.live())
        # Fair moves twice while orders are in flight: no duplicate creates
        # at a price, and nothing ends up live inside the edge.
        fair(eng, 0.45)
        fair(eng, 0.42)
        await _settle(eng)
        d = eng.om.desired[("A", "yes")]
        live = [q for q in eng.om.live("A", "yes") if q.state != om.CANCELLING]
        assert all(q.price <= d.edge_limit + 1e-9 for q in live)
        prices = [q.price for q in eng.om.live("A", "yes")]
        assert len(prices) == len(set(prices))
        quoter.model.age = 5.0
        fair(eng)
        await _settle(eng)
        assert eng.om.live() == []
        assert len(set(client.cancels)) == len(client.cancels)     # each order cancelled once

    asyncio.run(main())


def test_shadow_ladder_under_loop_lands_after_send_latency():
    async def main():
        eng, _, _ = setup()
        fair(eng)
        assert all(q.state == om.CREATING for q in eng.om.live())   # not landed yet
        await _settle(eng, 0.2)
        assert len(eng.om.live()) == 6 and all(q.state == om.RESTING for q in eng.om.live())

    asyncio.run(main())
