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
        assert len(client.creates) == 2
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


def test_cancel_does_not_wait_behind_a_full_default_executor():
    """Default executor saturated with slow work (creates, REST polls, parquet
    writes): a cancel still starts at once on its own pool."""
    async def main():
        loop = asyncio.get_running_loop()
        import concurrent.futures
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(2))
        eng, _, _ = setup(real=True)
        client = SlowClient()
        eng.order_client = client
        eng.order_events = []
        fair(eng)
        await _settle(eng, 0.15)
        assert len(eng.om.live()) == 2
        for _ in range(4):
            loop.run_in_executor(None, time.sleep, 0.5)
        eng.pull_quotes()
        await asyncio.sleep(0.1)
        ca = [r for r in eng.order_events if r["event"] == "cancel_ack"]
        assert len(ca) == 2 and all(r["pickup_s"] < 0.05 for r in ca)

    asyncio.run(main())


class ArmClient(SlowClient):
    """Arm reads and the disarm sweep's listing, each a slow REST call."""

    def __init__(self, resting=()):
        super().__init__()
        self.resting = list(resting)
        self.lists = 0

    def exchange_index(self, ev):
        time.sleep(0.05)
        return 2

    def balance(self, exchange_index=None):
        time.sleep(0.05)
        return 100.0

    def positions(self, ev):
        time.sleep(0.05)
        return {}

    def fills(self, ev, max_pages=20, min_ts=None):
        time.sleep(0.2)
        return []

    def resting_orders(self, ev):
        self.lists += 1
        time.sleep(0.05)
        out, self.resting = self.resting, []
        return out


async def _max_lag(stop, out):
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(0.005)
        out.append(time.perf_counter() - t0 - 0.005)


def test_arm_and_disarm_do_not_block_the_loop():
    """Arm reads (~0.35s here) and the disarm sweep (>=1s of pass sleeps) ran
    on the loop and stalled every feed; now neither holds it past a tick."""
    async def main():
        eng, _, _ = setup()
        client = ArmClient(resting=[("x1", "A")])
        eng.order_client = client
        lags, stop = [], asyncio.Event()
        probe = asyncio.create_task(_max_lag(stop, lags))
        assert await eng.arm_async() is True and eng.live_trading
        fair(eng)
        await _settle(eng, 0.15)
        assert len(client.creates) == 2
        eng.disarm_nowait()
        assert not eng.live_trading and eng.sweep_in_flight()
        assert await eng.arm_async() is None          # refused while the sweep runs
        await eng.wait_sweep()
        stop.set(); await probe
        assert not eng.sweep_in_flight() and client.lists >= 2
        assert "x1" in client.cancels
        assert max(lags) < 0.1, max(lags)

    asyncio.run(main())


def test_arm_abandoned_if_no_longer_wanted():
    async def main():
        eng, _, _ = setup()
        eng.order_client = ArmClient()
        assert await eng.arm_async(still_wanted=lambda: False) is None
        assert not eng.live_trading

    asyncio.run(main())


def test_disarm_sweep_keeps_quotes_placed_after_it():
    """The sweep's forget drops only what the disarm pulled; the new shadow
    session's quotes survive it."""
    async def main():
        eng, _, client = setup(real=True)
        eng.order_client = ArmClient()
        fair(eng)
        await _settle(eng, 0.15)
        eng.disarm_nowait()
        await _settle(eng, 0.1)                        # the pulled quotes' cancels land
        fair(eng, 0.55)                                # shadow quotes while the sweep runs
        await _settle(eng, 0.1)
        shadow = set(eng.om.quotes)
        assert eng.sweep_in_flight()
        await eng.wait_sweep()
        assert shadow and shadow <= set(eng.om.quotes)

    asyncio.run(main())


def test_shadow_ladder_under_loop_lands_after_send_latency():
    async def main():
        eng, _, _ = setup()
        fair(eng)
        assert all(q.state == om.CREATING for q in eng.om.live())   # not landed yet
        await _settle(eng, 0.2)
        assert len(eng.om.live()) == 2 and all(q.state == om.RESTING for q in eng.om.live())

    asyncio.run(main())
