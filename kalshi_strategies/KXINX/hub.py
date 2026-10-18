"""
S&P pricing hub: one IB connection, one option book and one curve fit per
SPXW expiry, shared by every S&P series (KXINXU, KXINXDUD).

    python -m kalshi_strategies.KXINX.hub

The launcher (kalshi_strategies.run) starts it before the series, restarts
it if it dies, and reuses one that is already listening. Each series process connects over localhost TCP
(newline-delimited JSON), registers its event and the buckets it wants
priced, and receives fairs for exactly its own markets, all off the same
book snapshot. It reports its positions back so the hub can enforce one
combined worst-case loss across all series.

Protocol, series -> hub:
    {"op": "hello", "series", "event_ticker", "expiry", "markets", "scope"}
    {"op": "scope", "scope": [tickers]}
    {"op": "positions", "legs": [[ticker, yes_qty, no_qty, cost], ...]}
hub -> series:
    {"op": "fairs", "seq", "t", "fairs": {ticker: [lo, mid, hi]}}
    {"op": "status", "feed_problem", "ib_farms", "ib_ticks_per_s", "ib_held_ticks",
     "feed_age_s", "applied_age_s", "strikes", "n_lines", "blocks", "combined"}
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
import traceback

from kalshi_infra import keys
from kalshi_infra import ib_functions as ibf
from kalshi_infra.config import ConfigStore
from kalshi_strategies.KXINX import risk
from kalshi_strategies.KXINX.config import CONFIG_PATH, HUB_KEY, HubConfig
from kalshi_strategies.KXINX.ib_feed import HoldParams, LiveIBFeed
from kalshi_strategies.KXINX.live_pricing import LevelPricer, bucket_fairs

HOST = "127.0.0.1"
# asyncio's default 64 KiB line limit: KXINXU's hello with 60 full market
# dicts overran it on 2026-10-13. Messages are trimmed too (MARKET_FIELDS).
LINE_LIMIT = 16 * 1024 * 1024
MARKET_FIELDS = ("ticker", "floor_strike", "cap_strike", "strike_type")
STATUS_INTERVAL_S = 1.0
# Wait this long after the last tick for more before fitting: a burst of
# ticks across strikes should land in one fit, not one fit per strike.
COALESCE_S = 0.002


def _log(msg: str):
    print(f"[hub] {msg}", flush=True)


class _Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.series: str | None = None
        self.event_ticker: str | None = None
        self.expiry: datetime.date | None = None
        self.markets: list[dict] = []
        self.by_ticker: dict[str, dict] = {}
        self.scope: list[str] = []
        self.monotone = False
        self.legs: dict[str, tuple[float, float, float]] = {}
        self.last_fairs: dict[str, list[float]] = {}

    def scoped_markets(self) -> list[dict]:
        return [self.by_ticker[t] for t in self.scope if t in self.by_ticker]

    def send(self, msg: dict):
        try:
            self.writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except Exception:
            pass


class _Expiry:
    """One SPXW expiry's feed and the clients priced off it."""

    def __init__(self, hub: "Hub", expiry: datetime.date):
        self.hub, self.expiry = hub, expiry
        self.clients: set[_Client] = set()
        self.feed: LiveIBFeed | None = None
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.seq = 0

    def wanted_strikes(self) -> list[float]:
        boundaries = set()
        for c in self.clients:
            boundaries |= ibf.market_boundaries(c.scoped_markets())
        return ibf.strikes_for_boundaries(boundaries)

    async def resubscribe(self):
        want = self.wanted_strikes()
        if not want:
            return
        if self.feed is None:
            cfg = self.hub.cfg
            # Expiries share one IB login, so each needs its own clientId.
            offset = cfg.ib_client_id_offset + len(self.hub.expiries) - 1
            self.feed = LiveIBFeed(self.expiry, want, client_id=keys.IB_CLIENT_ID + offset,
                                   hold=HoldParams(cfg.hold_baseline_s, cfg.hold_multiplier,
                                                   cfg.hold_settle_s, cfg.hold_min_samples,
                                                   cfg.hold_sample_s),
                                   reconnect_delay_s=cfg.ib_reconnect_delay_s, label=f"hub {self.expiry}")
            self.feed.on_tick_hook = self.wake.set
            self.task = asyncio.create_task(self._run())
            _log(f"{self.expiry}: subscribing {len(want)} strikes ({2 * len(want)} lines)")
        elif await self.feed.set_strikes(want):
            _log(f"{self.expiry}: now {len(want)} strikes ({2 * len(want)} lines)")

    async def _run(self):
        feed_task = asyncio.create_task(self.feed.run())
        try:
            while True:
                await self.wake.wait()
                await asyncio.sleep(COALESCE_S)
                self.wake.clear()
                try:
                    self.price()
                except Exception:
                    _log(f"{self.expiry}: ERROR in pricing:")
                    traceback.print_exc()
        finally:
            self.feed.stop()
            feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)

    def price(self):
        if not self.feed.drain():
            return
        b = self.feed.book
        a = b.snapshot_arrays()
        pricer = LevelPricer(b.call_strikes, a[0], a[1], a[2], b.put_strikes, a[3], a[4], a[5])
        self.seq += 1
        now = time.time()
        for c in list(self.clients):
            fairs = bucket_fairs(c.scoped_markets(), pricer, monotone=c.monotone)
            c.last_fairs = {t: [lo, mid, hi] for t, (lo, mid, hi) in fairs.items()}
            c.send({"op": "fairs", "seq": self.seq, "t": now, "fairs": c.last_fairs})

    def status(self) -> dict:
        f = self.feed
        if f is None:
            return {"feed_problem": "hub feed not started", "ib_farms": {}, "ib_ticks_per_s": 0.0,
                    "ib_held_ticks": 0, "feed_age_s": None, "applied_age_s": None, "strikes": [], "n_lines": 0}
        return {"feed_problem": f.gateway_problem(), "ib_farms": dict(f.farm_status),
                "ib_ticks_per_s": f.records_per_s(), "ib_held_ticks": f.book.n_held,
                "feed_age_s": f.seconds_since_record(), "applied_age_s": f.seconds_since_applied(),
                "strikes": f.strikes, "n_lines": 2 * len(f.strikes)}

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class Hub:
    def __init__(self, parent_pid: int | None = None):
        self.store = ConfigStore(HubConfig, CONFIG_PATH, HUB_KEY)
        self.expiries: dict[datetime.date, _Expiry] = {}
        self.clients: set[_Client] = set()
        self.blocks: dict[str, str] = {}
        self.combined: dict = {}
        self.parent_pid = parent_pid

    @property
    def cfg(self) -> HubConfig:
        return self.store.config

    async def serve(self):
        server = await asyncio.start_server(self._handle, HOST, self.cfg.port, limit=LINE_LIMIT)
        _log(f"listening on {HOST}:{self.cfg.port}")
        async with server:
            await asyncio.gather(self._status_loop(), self._risk_loop(), self._parent_loop())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = _Client(writer)
        self.clients.add(c)
        try:
            while line := await reader.readline():
                try:
                    await self._on_msg(c, json.loads(line))
                except Exception:
                    _log(f"bad message from {c.series}:")
                    traceback.print_exc()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._drop(c)

    async def _on_msg(self, c: _Client, msg: dict):
        op = msg.get("op")
        if op == "hello":
            await self._drop_from_expiry(c)
            c.series, c.event_ticker = msg["series"], msg["event_ticker"]
            c.expiry = datetime.date.fromisoformat(msg["expiry"])
            c.markets = msg["markets"]
            c.by_ticker = {m["ticker"]: m for m in c.markets}
            c.scope = list(msg["scope"])
            c.monotone = bool(msg.get("monotone", False))
            c.legs = {}
            ex = self.expiries.get(c.expiry)
            if ex is None:
                ex = self.expiries[c.expiry] = _Expiry(self, c.expiry)
            ex.clients.add(c)
            _log(f"{c.series} joined: {c.event_ticker}, {len(c.scope)} buckets in scope")
            await ex.resubscribe()
        elif op == "scope":
            c.scope = list(msg["scope"])
            if c.expiry in self.expiries:
                await self.expiries[c.expiry].resubscribe()
        elif op == "monotone":
            c.monotone = bool(msg["on"])
        elif op == "positions":
            c.legs = {t: (float(y), float(n), float(cost)) for t, y, n, cost in msg["legs"]}

    async def _drop_from_expiry(self, c: _Client):
        ex = self.expiries.get(c.expiry) if c.expiry else None
        if ex is None:
            return
        ex.clients.discard(c)
        if not ex.clients:
            await ex.close()
            del self.expiries[c.expiry]
            _log(f"{c.expiry}: no series left; feed closed")
        else:
            await ex.resubscribe()

    async def _drop(self, c: _Client):
        self.clients.discard(c)
        await self._drop_from_expiry(c)
        if c.series:
            _log(f"{c.series} left")
        try:
            c.writer.close()
        except Exception:
            pass

    async def _status_loop(self):
        while True:
            await asyncio.sleep(STATUS_INTERVAL_S)
            for c in self.store.poll():
                _log(f"config: {c}")
            for ex in self.expiries.values():
                st = ex.status()
                for c in list(ex.clients):
                    c.send({"op": "status", **st,
                            "blocks": {k: v for k, v in self.blocks.items() if k.split(":")[0] in c.by_ticker},
                            "combined": self.combined.get(ex.expiry, {})})

    def _risk_for(self, ex: _Expiry) -> tuple[dict, dict]:
        legs, markets, prices = [], {}, {}
        for c in ex.clients:
            for t, (y, n, cost) in c.legs.items():
                m = c.by_ticker.get(t)
                if m is None:
                    continue
                legs.append(risk.Leg(t, y, n, cost, m.get("floor_strike"), m.get("cap_strike"),
                                     m.get("strike_type")))
            for m in c.scoped_markets():
                markets[m["ticker"]] = (m.get("floor_strike"), m.get("cap_strike"), m.get("strike_type"))
            prices.update({t: f[1] for t, f in c.last_fairs.items()})
        lim = self.cfg.combined_max_loss_usd
        worst, at = risk.worst_case(legs)
        blocks = risk.blocked_sides(legs, markets, lim, prices)
        return blocks, {"worst_usd": worst, "worst_close": at, "limit_usd": lim,
                        "n_legs": len(legs), "n_blocked_sides": len(blocks)}

    async def _risk_loop(self):
        while True:
            await asyncio.sleep(self.cfg.risk_check_s)
            try:
                blocks, combined = {}, {}
                for ex in self.expiries.values():
                    b, s = self._risk_for(ex)
                    blocks.update(b)
                    combined[ex.expiry] = s
                if blocks and not self.blocks:
                    _log(f"combined risk limit hit: {next(iter(blocks.values()))}")
                elif self.blocks and not blocks:
                    _log("combined risk back under the limit")
                self.blocks, self.combined = blocks, combined
            except Exception:
                _log("ERROR in risk check:")
                traceback.print_exc()

    async def _parent_loop(self):
        """Exit with the launcher: a force-killed launcher would otherwise
        leave an orphan hub holding the port and the IB clientId."""
        if self.parent_pid is None:
            return
        import psutil
        while True:
            await asyncio.sleep(2.0)
            if not psutil.pid_exists(self.parent_pid):
                _log(f"launcher {self.parent_pid} is gone; exiting")
                for ex in list(self.expiries.values()):
                    await ex.close()
                raise SystemExit(0)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="S&P pricing hub")
    ap.add_argument("--parent-pid", type=int, default=None, help="Exit when this process exits.")
    args = ap.parse_args(argv)
    try:
        asyncio.run(Hub(parent_pid=args.parent_pid).serve())
    except KeyboardInterrupt:
        _log("stopped")


if __name__ == "__main__":
    main()
