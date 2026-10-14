"""Hub end to end with a fake IB feed: two series on one expiry get fairs
from one book, the subscription is the union of their scopes, and combined
risk blocks reach the series' side_block."""
import asyncio
import datetime
import json
import types

import numpy as np
import pytest

from kalshi_strategies.KXINX import hub as hubmod
from kalshi_strategies.KXINX.ib_feed import HoldParams, LiveOptionBook
from kalshi_strategies.KXINX.live_pricing import compute_live_bucket_probs
from kalshi_strategies.KXINX.tests.test_monotone_levels import _chain


class FakeFeed:
    instances: list = []

    def __init__(self, expiry, strikes, *, client_id, hold, reconnect_delay_s, label=None):
        self.expiry, self.strikes_set = expiry, sorted(strikes)
        self.on_tick_hook = None
        self.book = LiveOptionBook(np.array(self.strikes_set), np.array(self.strikes_set),
                                   HoldParams(180, 3, 0.3, 10**9, 2))
        self.farm_status = {"market_data": "ok"}
        self.drained = 0
        FakeFeed.instances.append(self)

    @property
    def strikes(self):
        return self.strikes_set

    async def set_strikes(self, strikes):
        new = sorted(set(strikes))
        changed = new != self.strikes_set
        self.strikes_set = new
        self.book = LiveOptionBook(np.array(new), np.array(new), self.book.hold)
        return changed

    async def run(self):
        await asyncio.Event().wait()

    def tick_all(self, fwd=7802.0):
        k = np.array(self.strikes_set)
        c, p = _chain(k, fwd, 12.0)
        for i, s in enumerate(k):
            self.book.set_quote(s, "C", c[i] - 0.05, c[i] + 0.05)
            self.book.set_quote(s, "P", p[i] - 0.05, p[i] + 0.05)
        self.drained = 1
        self.on_tick_hook()

    def drain(self):
        n, self.drained = self.drained, 0
        return n

    def stop(self):
        pass

    def gateway_problem(self):
        return None

    def records_per_s(self):
        return 10.0

    def seconds_since_record(self):
        return 0.01

    def seconds_since_applied(self):
        return 0.01


def _bucket_markets():
    out = []
    for lo in range(7700, 7900, 25):
        out.append({"ticker": f"KXINX-26OCT13H1600-B{lo + 12}", "floor_strike": lo,
                    "cap_strike": lo + 24.9999, "strike_type": "between"})
    return out


def _ladder_markets():
    return [{"ticker": f"KXINXU-26OCT13H1600-T{k - 0.0001:.4f}", "floor_strike": k, "cap_strike": None,
             "strike_type": "greater_or_equal"} for k in range(7780, 7830, 5)]


async def _client(port, hello):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write((json.dumps(hello) + "\n").encode())
    await w.drain()
    return r, w


async def _next(r, op, timeout=3.0, where=lambda m: True):
    while True:
        msg = json.loads(await asyncio.wait_for(r.readline(), timeout))
        if msg["op"] == op and where(msg):
            return msg


@pytest.fixture
def hub(monkeypatch):
    FakeFeed.instances.clear()
    monkeypatch.setattr(hubmod, "LiveIBFeed", FakeFeed)
    monkeypatch.setattr(hubmod, "STATUS_INTERVAL_S", 0.05)
    h = hubmod.Hub()
    h.store = types.SimpleNamespace(config=types.SimpleNamespace(
        port=0, ib_client_id_offset=0, ib_reconnect_delay_s=1.0, hold_baseline_s=180, hold_multiplier=3,
        hold_settle_s=0.3, hold_min_samples=5, hold_sample_s=2, combined_max_loss_usd=5.0, risk_check_s=0.05),
        poll=lambda: [])
    return h


def test_two_series_one_book(hub):
    async def go():
        server = await asyncio.start_server(hub._handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loops = [asyncio.create_task(hub._status_loop()), asyncio.create_task(hub._risk_loop())]
        bm, lm = _bucket_markets(), _ladder_markets()
        exp = datetime.date(2026, 10, 13).isoformat()
        rx, wx = await _client(port, {"op": "hello", "series": "KXINX", "event_ticker": "KXINX-26OCT13H1600",
                                      "expiry": exp, "markets": bm, "scope": [m["ticker"] for m in bm[2:6]]})
        ru, wu = await _client(port, {"op": "hello", "series": "KXINXU", "event_ticker": "KXINXU-26OCT13H1600",
                                      "expiry": exp, "markets": lm, "scope": [m["ticker"] for m in lm]})
        await asyncio.sleep(0.1)
        assert len(FakeFeed.instances) == 1, "both series must share one feed"
        feed = FakeFeed.instances[0]
        # Union of both scopes' bracketing strikes.
        assert min(feed.strikes) <= 7745 and max(feed.strikes) >= 7830

        feed.tick_all()
        fx, fu = await _next(rx, "fairs"), await _next(ru, "fairs")
        assert fx["seq"] == fu["seq"], "both priced off the same fit"
        assert set(fx["fairs"]) == {m["ticker"] for m in bm[2:6]}
        assert set(fu["fairs"]) == {m["ticker"] for m in lm}

        # Same numbers as the standalone path on the same book.
        b = feed.book
        a = b.snapshot_arrays()
        ref = compute_live_bucket_probs(lm, b.call_strikes, a[0], a[1], a[2], b.put_strikes, a[3], a[4], a[5])
        for t, v in fu["fairs"].items():
            assert np.allclose(v, ref[t])

        # Short 20 "above 7800" on KXINXU (bought no at 0.5): past the $5
        # combined limit, so adding more KXINXU no / KXINX yes above 7800 is blocked.
        t7800 = next(m["ticker"] for m in lm if m["floor_strike"] == 7800)
        wu.write((json.dumps({"op": "positions", "legs": [[t7800, 0, 20, 10.0]]}) + "\n").encode())
        await wu.drain()
        hit = lambda m: (m.get("combined") or {}).get("worst_usd", 0) < -5.0
        st = await _next(rx, "status", where=hit)
        assert any(k.endswith(":yes") for k in st["blocks"]), st["blocks"]
        stu = await _next(ru, "status", where=hit)
        assert f"{t7800}:no" in stu["blocks"] and f"{t7800}:yes" not in stu["blocks"]

        wx.close()
        wu.close()
        await asyncio.sleep(0.1)
        assert not hub.expiries, "feed closes when the last series leaves"
        for t in loops:
            t.cancel()
        server.close()

    asyncio.run(go())


def test_large_hello_fits(hub):
    """A 200-market event with every Kalshi field must not overrun the line
    limit (KXINXU's 60-market hello did, live 2026-10-13)."""
    async def go():
        server = await asyncio.start_server(hub._handle, "127.0.0.1", 0, limit=hubmod.LINE_LIMIT)
        port = server.sockets[0].getsockname()[1]
        big = [{**m, "rules_primary": "x" * 2000, "rules_secondary": "y" * 2000}
               for m in _ladder_markets() * 20]
        r, w = await asyncio.open_connection("127.0.0.1", port, limit=hubmod.LINE_LIMIT)
        w.write((json.dumps({"op": "hello", "series": "KXINXU", "event_ticker": "E",
                             "expiry": "2026-10-13", "markets": big,
                             "scope": [m["ticker"] for m in big[:10]]}) + "\n").encode())
        await w.drain()
        await asyncio.sleep(0.2)
        assert any(c.series == "KXINXU" for c in hub.clients)
        w.close()
        server.close()

    asyncio.run(go())


def test_model_side_block_and_staleness():
    from kalshi_strategies.KXINX.model import SpxEventModel
    m = SpxEventModel.__new__(SpxEventModel)
    m._blocks = {"A:yes": "combined_risk (x)"}
    m._connected = False
    m._last_fair_mono = None
    assert m.side_block("A", "yes", None, 0) == "combined_risk (x)"
    assert m.side_block("A", "no", None, 0) is None
    assert m.signal_age_s() is None   # no hub -> stale, never "fresh"
