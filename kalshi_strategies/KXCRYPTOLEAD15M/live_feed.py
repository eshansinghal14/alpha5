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
import atexit
import time

from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBus
from kalshi_strategies.KXCRYPTOLEAD15M.prints import (AUTO_SOURCES, HL_BURST_USD_1S, PERP_BURST_USD_1S, PERP_SOURCES,
                                                       REFRESH_S,
                                                       SAMPLE_S, BurstCalibrator, BurstDetector, PrintLog,
                                                       seed_rows)
from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS, SPOT_VENUES
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import FeedHealthMonitor
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import CoinbaseAdapter
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venue_process import VenueProcess
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venues import LEADING_VENUES

# A venue whose book is older than this leaves its coin's median. Bitstamp's
# snapshots arrive every 0.2-0.5 s and Bullish's every ~0.5 s (recorder,
# 2026-10-11); an unchanged book on a live connection doesn't age (keep-alive
# from the venue process), so this only bites on a venue that has gone quiet.
VENUE_STALE_S = 5.0
# How often the venue process's batches are applied on the trading loop.
VENUE_DRAIN_S = 0.05


def venue_plan() -> dict[str, list[str]]:
    """Non-Coinbase venues to run, and for which coins: whatever SPOT_VENUES
    medians on, plus every leading-signal venue for every coin."""
    from kalshi_strategies.KXCRYPTOLEAD15M.series import CF_CONSTITUENTS
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venue_process import RECORD_IN_VENUE_PROCESS
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venues import ADAPTERS
    # Recording needs every constituent book, not just the ones each coin's
    # median uses; pricing still filters on SPOT_VENUES (CompositeFeed.venues).
    source = CF_CONSTITUENTS if RECORD_IN_VENUE_PROCESS else SPOT_VENUES
    plan: dict[str, list[str]] = {}
    for coin, venues in source.items():
        for v in venues:
            if v != "coinbase" and v in ADAPTERS:
                plan.setdefault(v, []).append(coin)
    for v in LEADING_VENUES:
        plan[v] = list(COINS)
    return plan


class SpotFeed:
    def __init__(self, cfg, *, persist: bool = True, venues: bool = True):
        """`venues=False` keeps it Coinbase-only (tests, backtest)."""
        p = cfg.pricing
        self.cfg = cfg
        self.bus = CompositeBus(coins=COINS, persist=persist,
                                venues=SPOT_VENUES if venues else None,
                                venue_stale_s=VENUE_STALE_S if venues else None)
        self.coinbase = CoinbaseAdapter(coins=list(COINS))
        self.venues = VenueProcess(venue_plan()) if venues else None
        self._venue_atexit = False
        self.hl_bursts = BurstDetector(COINS)
        # Offshore perps get their own windows: a sweep on one venue should
        # trip on that venue's volume, not be diluted or doubled by another's.
        self.perp_bursts = {v: BurstDetector(COINS) for v in PERP_BURST_USD_1S}
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
        self.bursts = BurstDetector(COINS)
        self.prints = PrintLog(persist=persist)
        self.coinbase.on_print = self._on_print
        # Monotonic time until which quotes stay pulled, and the coin that set it.
        self.burst_until = 0.0
        self.burst_coin: str | None = None
        self.n_bursts = 0
        # Coinbase falls back to config.json's burst_usd_1s until calibrated.
        self.calib = {"coinbase": BurstCalibrator(COINS, p.burst_usd_1s),
                      "hyperliquid": BurstCalibrator(COINS, HL_BURST_USD_1S),
                      **{v: BurstCalibrator(COINS, None) for v in PERP_SOURCES}}
        self._calib_seeded = not persist        # tests: nothing on disk to seed from
        self._calib_tasks: list[asyncio.Task] = []

    def _threshold(self, source: str, coin: str) -> float | None:
        if source in AUTO_SOURCES:
            return self.calib[source].threshold(coin)
        return None

    def burst_thresholds(self) -> dict:
        return {s: {"calibrated": c.ready, "usd_1s": {k: round(c.threshold(k) or 0) for k in COINS}}
                for s, c in self.calib.items()}

    async def _calib_seed(self):
        """Rebuild the last CALIB_WINDOW_S of samples from disk once, so a
        restart doesn't fall back to the static thresholds for half an hour."""
        loop = asyncio.get_running_loop()
        try:
            for src, cal in self.calib.items():
                rows = await loop.run_in_executor(None, seed_rows, src, COINS, time.time())
                cal.seed(rows)
                cal.adopt(await loop.run_in_executor(None, cal.quantiles, cal.snapshot()))
        except Exception as e:
            print(f"[KXCRYPTOLEAD15M] burst threshold seed failed ({e!r}); calibrating from live prints", flush=True)
        self._calib_seeded = True

    async def _calib_sample_loop(self):
        detectors = {"coinbase": self.bursts, "hyperliquid": self.hl_bursts, **self.perp_bursts}
        while True:
            await asyncio.sleep(SAMPLE_S)
            if not self._calib_seeded:
                continue
            now = time.monotonic()
            for src, det in detectors.items():
                self.calib[src].sample({c: det.level(c, now) for c in COINS})

    async def _calib_refresh_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(REFRESH_S)
            for cal in self.calib.values():
                try:
                    cal.adopt(await loop.run_in_executor(None, cal.quantiles, cal.snapshot()))
                except Exception as e:
                    print(f"[KXCRYPTOLEAD15M] burst threshold refresh failed: {e!r}", flush=True)

    def _on_print(self, coin: str, t: dict, as_of: float, exch_ts: float | None, venue: str = "coinbase"):
        try:
            price, size = float(t["price"]), float(t["size"])
        except (KeyError, TypeError, ValueError):
            return
        if venue in self.perp_bursts:
            # A batch notional, not a real print (venue_process ships sums);
            # the child already persisted the real prints to perp_prints/.
            usd_1s = self.perp_bursts[venue].add(coin, price * size, as_of)
            thr = self._threshold(venue, coin)
        elif venue == "coinbase":
            self.prints.add(coin, t.get("trade_id"), price, size, t.get("side"), exch_ts, venue=venue)
            usd_1s = self.bursts.add(coin, price * size, as_of)
            thr = self._threshold("coinbase", coin)
        else:
            self.prints.add(coin, t.get("trade_id"), price, size, t.get("side"), exch_ts, venue=venue)
            # Perp and spot share one window: a sweep often hits both.
            usd_1s = self.hl_bursts.add(coin, price * size, as_of)
            thr = self._threshold("hyperliquid", coin)
        if thr is None or usd_1s < thr:
            return
        if as_of >= self.burst_until:
            self.n_bursts += 1
        self.burst_until = max(self.burst_until, as_of + self.cfg.pricing.burst_hold_s)
        self.burst_coin = coin if venue == "coinbase" else f"{coin}@{venue}"
        m = self.model
        if m is not None:
            # A pull must reach the quoter now, not at the next mid move.
            m._mid_moved()

    def _on_venue_print(self, coin, t, as_of, exch_ts, kind):
        self._on_print(coin, t, as_of, exch_ts, venue=kind)

    async def _venue_loop(self):
        """Apply the venue process's batches. Its books go through the same
        _on_book as Coinbase's, so a venue moving the median wakes the reprice."""
        while True:
            await asyncio.sleep(VENUE_DRAIN_S)
            try:
                self.venues.ensure_running()
                self.venues.drain(self._on_book, self._on_venue_print, self.bus.touch)
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] venue drain error: {e!r}", flush=True)

    def ensure_running(self):
        """Start (or restart, if one died) the feed tasks on the running loop.
        The venue child is shared across restarts of these tasks (ensure_running
        on it only starts one when none is alive) and is stopped at exit."""
        if self.venues is not None and not self._venue_atexit:
            atexit.register(self.venues.stop)
            self._venue_atexit = True
        if self._tasks and all(not t.done() for t in self._tasks):
            return
        for t in self._tasks:
            if t.done() and not t.cancelled() and t.exception() is not None:
                print(f"[KXCRYPTOLEAD15M] spot feed task died ({t.exception()!r}); restarting", flush=True)
            t.cancel()
        coros = [self.coinbase.run(self._on_book), self._sample_loop(), self._flush_loop(),
                 self._calib_sample_loop(), self._calib_refresh_loop()]
        if self.venues is not None:
            coros.append(self._venue_loop())
        if not self._calib_seeded and not any(not t.done() for t in self._calib_tasks):
            self._calib_tasks = [asyncio.create_task(self._calib_seed())]
        self._tasks = [asyncio.create_task(c) for c in coros]

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
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.prints.write, self.prints.drain())
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] prints flush failed: {e!r}")
