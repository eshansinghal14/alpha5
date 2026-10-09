"""
Session-lifetime spot feed: the Coinbase socket, composite bars, vol
forecaster and feed-health state outlive any one event.

Per-event, rebuilding them cost every 15-minute rollover a fresh socket and
~1MB snapshot, a disk replay of 4h of bars into the forecaster (1.6-3.5s
with the loop blocked), and recovery_s of every coin held DOWN as a cold
start -- ~7s stale at the open of each event (2026-10-09). Each event's
CoinRaceModel attaches to this and only owns its settlement tracker and
pricer.
"""
from __future__ import annotations

import asyncio
import time

from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBus
from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import FeedHealthMonitor
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import CoinbaseAdapter


class SpotFeed:
    def __init__(self, cfg, *, persist: bool = True):
        p = cfg.pricing
        self.cfg = cfg
        self.bus = CompositeBus(coins=COINS, persist=persist)
        self.coinbase = CoinbaseAdapter(coins=list(COINS))
        # No deep_book_fn: rebuilding a 1000-level view and the CF replica
        # over it every second was 200-300ms on the trading loop per pass
        # (2026-10-09 live profile), the cause of the remaining stale
        # windows. The replica `price` is diagnostics only -- pricing and the
        # TWAPs use the mid (CompositeBar.mid) -- so bars are sampled from the
        # 25-level per-message view instead (~1.5-7ms per coin).
        self.forecaster = RobustHarForecaster()
        self.health = FeedHealthMonitor(soft_stale_s=p.soft_stale_s, hard_stale_s=p.hard_stale_s,
                                        recovery_s=p.recovery_s)
        self.forecaster_warmed = False
        self.model = None
        self._tasks: list[asyncio.Task] = []

    def ensure_running(self):
        """Start (or restart, if one died) the feed tasks on the running loop."""
        if self._tasks and all(not t.done() for t in self._tasks):
            return
        for t in self._tasks:
            if t.done() and not t.cancelled() and t.exception() is not None:
                print(f"[KXCRYPTOLEAD15M] spot feed task died ({t.exception()!r}); restarting", flush=True)
            t.cancel()
        self._tasks = [asyncio.create_task(c) for c in
                       (self.coinbase.run(self._on_book), self._sample_loop(), self._flush_loop())]

    def attach(self, model):
        p = model.cfg.pricing
        self.health.soft_stale_s, self.health.hard_stale_s = p.soft_stale_s, p.hard_stale_s
        self.health.recovery_s = p.recovery_s
        # Bars sampled while the model was being built went nowhere; the
        # tracker dedupes on ts_s.
        model.tracker.backfill(self.recent_bars(model.start_window_since()))
        self.model = model

    def detach(self, model):
        if self.model is model:
            self.model = None

    def _on_book(self, vb):
        before = self.bus.mid_nowcast(vb.coin) if vb.coin in self.bus.feeds else None
        self.bus.observe(vb)
        m = self.model
        if m is not None and self.bus.mid_nowcast(vb.coin) != before:
            m._mid_moved()

    def sample(self, now_s: float):
        """Pin this second's bars into the forecaster and the attached
        event's tracker. Idempotent per second."""
        sampled = self.bus.sample_all(now_s)
        if not sampled:
            return
        m = self.model
        if m is not None:
            for bar in sampled.values():
                m.tracker.ingest(bar)
        try:
            self.forecaster.update(max(b.ts_s for b in sampled.values()), {c: b.mid for c, b in sampled.items()})
        except NotImplementedError:
            pass

    def recent_bars(self, since_s: float) -> dict:
        """On the loop only: the deques are appended by _sample_loop."""
        return {c: f.bars(since_s) for c, f in self.bus.feeds.items()}

    def adopt_forecaster(self, warmed: RobustHarForecaster):
        """Swap in a forecaster replayed off the loop, first catching it up on
        the bars sampled live while it was being built. On the loop."""
        last = warmed._grid_t if warmed._grid_t is not None else -1
        by_s: dict[int, dict[str, float]] = {}
        for c, bars in self.recent_bars(last + 1).items():
            for b in bars:
                by_s.setdefault(int(b.ts_s), {})[c] = b.mid
        for ts in sorted(by_s):
            warmed.update(float(ts), by_s[ts])
        self.forecaster = warmed
        self.forecaster_warmed = True

    async def _sample_loop(self):
        """Per-second bars aligned to second boundaries (Kalshi's own
        snapshots are second-aligned)."""
        while True:
            now = time.time()
            try:
                self.sample(now)
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] composite sampling error: {e!r}")
            await asyncio.sleep(max(0.0, int(now) + 1 - now + 0.02))

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self.cfg.pricing.composite_flush_s)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.bus.flush_all)
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] composite flush failed: {e!r}")
