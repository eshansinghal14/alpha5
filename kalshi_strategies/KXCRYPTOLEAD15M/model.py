"""
Live Coin Race model for one KXCRYPTOLEAD15M event: Coinbase books ->
per-coin composite mid -> settlement tracker + HAR vol forecaster -> MC
winner probabilities -> per-market Fair.

Band = prob +/- ci_vol_multiplier * theo_vol, where theo_vol is the
sigma-equivalent fair move over reaction_latency_s: full revaluation under
joint coin moves (theo_vol_reval_draws > 0), else the bump deltas x 1-sigma. The pricer works in
log returns; only the SettlementTracker works in Kalshi's arithmetic returns.
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import Fair
from kalshi_strategies.KXCRYPTOLEAD15M.composite import DATA_ROOT, CompositeBar, CompositeBus, bar_files
from kalshi_strategies.KXCRYPTOLEAD15M.kalshi_start import kalshi_values, level_offsets
from kalshi_strategies.KXCRYPTOLEAD15M.pricer import BaseRateTracker, CoinRacePricer, FairResult
from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.series import (APPLY_BASIS_SD, BASIS_SD_BP, COINS, START_END_AVG_S,
                                                       coin_from_market_ticker)
from kalshi_strategies.KXCRYPTOLEAD15M.settlement_tracker import EventWindow, SettlementTracker
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import FeedHealthMonitor
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import CoinbaseAdapter


# Kalshi published 21:45 values within seconds of the boundary (2026-10-11
# check); poll every few seconds and give up after 2 min.
KALSHI_START_POLL_S = 3.0
KALSHI_START_GIVE_UP_S = 120.0


def load_composite_bars(coin: str, start_s: float, end_s: float, root: Path = DATA_ROOT) -> list[CompositeBar]:
    """Persisted 1s bars for one coin over [start_s, end_s]."""
    out: list[CompositeBar] = []
    if not root.exists():
        return out
    frames = []
    for path in bar_files(coin, start_s, end_s, root):
        try:
            df = pd.read_parquet(path)
        except Exception as e:
            print(f"[KXCRYPTOLEAD15M] skipping unreadable {path.name} ({e!r})")
            continue
        frames.append(df[(df["ts_s"] >= start_s) & (df["ts_s"] <= end_s)])
    if frames:
        # A day written by both the old daily and the new hourly layout
        # holds some seconds twice.
        df = pd.concat(frames, ignore_index=True).drop_duplicates("ts_s", keep="last").sort_values("ts_s")
        for r in df.itertuples(index=False):
            out.append(CompositeBar(
                coin=coin, ts_s=float(r.ts_s), price=float(r.price), utilized_depth=float(r.utilized_depth),
                n_venues_in=int(r.n_venues_in), n_venues_dropped=int(r.n_venues_dropped),
                is_fallback=bool(r.is_fallback),
                fallback_mid=None if pd.isna(r.fallback_mid) else float(r.fallback_mid),
                nowcast_count=int(r.nowcast_count)))
    return out


class CoinRaceModel:
    def __init__(self, ev: EventInfo, cfg, *, bus: CompositeBus | None = None,
                 coinbase: CoinbaseAdapter | None = None, forecaster=None, spot=None):
        """Live: pass `spot` (a session-lifetime live_feed.SpotFeed), which
        supplies the bus, Coinbase feed, forecaster and health monitor.
        Tests/backtest: pass `bus` (and optionally `forecaster`) instead."""
        self.cfg = cfg
        self.event_ticker = ev.event_ticker
        by_coin = {coin_from_market_ticker(m["ticker"]): m["ticker"] for m in ev.markets}
        missing = [c for c in COINS if c not in by_coin]
        if missing:
            raise ValueError(f"{ev.event_ticker}: markets missing for {missing}")
        self.ticker_of = {c: by_coin[c] for c in COINS}
        self.tickers = [self.ticker_of[c] for c in COINS]
        self.window = EventWindow(event_ticker=ev.event_ticker, S=ev.open_time.timestamp(),
                                  T=ev.close_time.timestamp())
        self.tracker = SettlementTracker(self.window)
        p = cfg.pricing
        self.spot = spot
        if spot is not None:
            bus, coinbase, forecaster = spot.bus, spot.coinbase, spot.forecaster
            self.health = spot.health
        else:
            self.health = FeedHealthMonitor(soft_stale_s=p.soft_stale_s, hard_stale_s=p.hard_stale_s,
                                            recovery_s=p.recovery_s)
        self.forecaster = forecaster or RobustHarForecaster()
        self.pricer = CoinRacePricer(self.forecaster, p,
                                     base_rate=BaseRateTracker(p.base_rate_window).rate(),
                                     basis_sd=(np.array([BASIS_SD_BP[c] for c in COINS]) * 1e-4
                                               if APPLY_BASIS_SD else None))
        self.bus = bus
        self.coinbase = coinbase
        self.result: FairResult | None = None
        self.withheld_reason: str | None = "warming"
        self._last_delta_s = 0.0
        self._newest_mono = 0.0     # oldest coin's newest book under the current fair
        self._down: list[str] = list(COINS)
        self._wake = None
        # A mid has moved since the last _price, so the fair lags its inputs.
        self._dirty = True
        self.basis_tie: frozenset[str] = frozenset()
        # Market-implied relative return shift (see _update_market_shift).
        self._kmid: dict[str, float] = {}
        self._shift = np.zeros(len(COINS))      # EMA, applied as alpha * _shift
        self._shift_raw = np.zeros(len(COINS))  # latest estimate the EMA chases
        self._shift_t: float | None = None
        self._shift_fit_t = -math.inf

    def on_book(self, book):
        """Kalshi BBO for one leg. An empty side is still a price (no bid = 0,
        no ask = 1); both empty means no market, so the leg's mid is dropped."""
        c = coin_from_market_ticker(book.ticker)
        if c is None:
            return
        bid = 0.0 if book.bid is None else float(book.bid)
        ask = 1.0 if book.ask is None else float(book.ask)
        if bid <= 0.0 and ask >= 1.0:
            self._kmid.pop(c, None)
        elif ask > bid:
            self._kmid[c] = (bid + ask) / 2.0

    def _update_market_shift(self, now_s: float, x: np.ndarray, I: np.ndarray, start_locked: bool):
        """Advance the EMA toward the latest market-implied shift and, every
        market_shift_fit_s, re-estimate it: the mean-zero return shift dx that
        moves our five fairs onto the Kalshi mids to first order, dp = D dx,
        with D the bump deltas. Only relative shifts are identified (the five
        probabilities sum to 1). Frozen once the end average starts: a stale
        shift there overshoots near-ties (backtest 2026-10-12)."""
        p = self.cfg.pricing
        if p.market_shift_alpha <= 0 or now_s >= self.window.end_avg_begin:
            return
        if self._shift_t is not None:
            lam = 1.0 - 0.5 ** (max(0.0, now_s - self._shift_t) / p.market_shift_half_life_s)
            self._shift += lam * (self._shift_raw - self._shift)
        self._shift_t = now_s
        if now_s - self._shift_fit_t < p.market_shift_fit_s or len(self._kmid) < len(COINS):
            return
        base = self.pricer.fairs_at(x=x, I=I, tau=max(0.0, self.window.T - now_s), now_s=now_s,
                                    S=self.window.S, T=self.window.T, start_locked=start_locked,
                                    compute_delta=True)
        Dm = base.delta
        if not np.any(Dm):
            return
        m = np.array([self._kmid[c] for c in COINS])
        m = m / m.sum()
        A = Dm.T @ Dm
        A = A + 1e-2 * np.eye(len(COINS)) * np.trace(A) / len(COINS)
        dx = np.linalg.solve(A, Dm.T @ (m - base.probs))
        cap = p.market_shift_max_bp * 1e-4
        self._shift_raw = np.clip(dx - dx.mean(), -cap, cap)
        self._shift_fit_t = now_s

    def set_wakeup(self, fn):
        self._wake = fn

    def _on_book(self, vb):
        """Coinbase callback. Wakes the reprice only when the coin's mid
        moves: level2 sends ~64 msg/s across the five coins but the mid
        changes ~4.6/s (2026-10-09), and the mid is the only per-message
        input to pricing."""
        before = self.bus.mid_nowcast(vb.coin) if vb.coin in self.bus.feeds else None
        self.bus.observe(vb)
        if self.bus.mid_nowcast(vb.coin) != before:
            self._mid_moved()

    def _mid_moved(self):
        self._dirty = True
        if self._wake is not None:
            self._wake()

    # --- warmup -----------------------------------------------------------

    def backfill(self) -> dict[str, str]:
        """Seed the start TWAP and forecaster from persisted bars, so an event
        adopted mid-window prices off real returns. Without it x = 0 and the
        fair is a pure variance prior (2026-10-07: BTC theo 0.17 vs a 0.035
        market). Returns per-coin start_quality. Live sessions use
        backfill_start instead (the session's SpotFeed owns the forecaster)."""
        now_s = time.time()
        since = self.window.S - 2 * START_END_AVG_S
        bars = {c: load_composite_bars(c, now_s - self.cfg.pricing.warmup_s, self.window.T) for c in COINS}
        self.tracker.backfill({c: [b for b in bl if b.ts_s >= since] for c, bl in bars.items()})
        by_s: dict[int, dict[str, float]] = {}
        for c, bl in bars.items():
            for b in bl:
                by_s.setdefault(int(b.ts_s), {})[c] = b.mid
        for ts in sorted(by_s):
            try:
                self.forecaster.update(float(ts), by_s[ts])
            except NotImplementedError:
                break
        return dict(self.tracker.snapshot(time.time()).start_quality)

    def start_window_since(self) -> float:
        return self.window.S - 2 * START_END_AVG_S

    @staticmethod
    def _mids(coin: str, lo: float, hi: float) -> dict[float, float]:
        """Persisted 1s mids over [lo, hi). Disk only: bars older than the
        session's memory are what the offset needs, and the composite flush
        (composite_flush_s) has written everything before S long since."""
        return {b.ts_s: b.mid for b in load_composite_bars(coin, lo, hi) if b.ts_s < hi}

    def fetch_kalshi_start(self) -> dict[str, float] | None:
        """Kalshi-published start, moved onto our mid's level (kalshi_start).
        Blocking network + disk: call off the loop. None until Kalshi has
        published all five values or when there's too little history for the
        offset; the TWAP start stays in force then."""
        n = self.cfg.pricing.kalshi_start_offset_events
        if n <= 0:
            return None
        S = self.window.S
        anchors = {}
        # Recomputed while any coin lacks history: the previous boundary's
        # bars may not have been flushed yet (level_offsets caches the rest).
        offs = getattr(self, "_kalshi_offs", None)
        if offs is None or any(v is None for v in offs.values()):
            self._kalshi_offs = level_offsets(S, n, self._mids)
        offs = self._kalshi_offs
        for c in COINS:
            vals = kalshi_values(c, S, S)
            if S not in vals or offs.get(c) is None:
                return None
            anchors[c] = vals[S] * math.exp(offs[c])
        return anchors

    async def kalshi_start_loop(self):
        """Poll for Kalshi's start value after S; adopt it once, then stop."""
        loop = asyncio.get_running_loop()
        if self.cfg.pricing.kalshi_start_offset_events <= 0:
            return
        if time.time() < self.window.S:
            # A cold start reads ~12h of bars (~20s); do it before S so the
            # anchor lands seconds after Kalshi publishes, not 20s later.
            try:
                await loop.run_in_executor(None, self.fetch_kalshi_start)
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] kalshi start offsets failed ({e!r}); retrying after S", flush=True)
                self._kalshi_offs = None
        while time.time() < self.window.S + 2:
            await asyncio.sleep(1.0)
        deadline = self.window.S + KALSHI_START_GIVE_UP_S
        while time.time() < min(deadline, self.window.T - START_END_AVG_S):
            try:
                anchors = await loop.run_in_executor(None, self.fetch_kalshi_start)
            except Exception as e:
                print(f"[KXCRYPTOLEAD15M] kalshi start fetch failed ({e!r}); retrying", flush=True)
                anchors = None
            if anchors:
                old = self.tracker.snapshot(time.time()).start_twap
                self.tracker.set_start_override(anchors)
                moved = {c: round((anchors[c] / old[c] - 1) * 1e4, 2) for c in COINS if old.get(c)}
                print(f"[KXCRYPTOLEAD15M] start anchored on Kalshi values (bp vs our TWAP): {moved}", flush=True)
                self._mid_moved()
                return
            await asyncio.sleep(KALSHI_START_POLL_S)
        print(f"[KXCRYPTOLEAD15M] Kalshi start not available for {self.event_ticker}; pricing on our TWAP",
              flush=True)

    def backfill_start(self, mem_bars: dict[str, list[CompositeBar]]) -> dict[str, str]:
        """Seed only the settlement tracker. `mem_bars` are the session's
        in-memory bars from start_window_since(); disk fills in only when
        the session started after that (the next event's start average is
        the previous event's last minute, already in memory). Safe off the
        loop: nothing else touches this model's tracker until feed_tasks()."""
        since = self.start_window_since()
        if any(not bl or bl[0].ts_s > since + 1 for bl in mem_bars.values()):
            disk = {c: load_composite_bars(c, since, self.window.T) for c in COINS}
        else:
            disk = {}
        self.tracker.backfill({c: disk.get(c, []) + mem_bars.get(c, []) for c in COINS})
        return dict(self.tracker.snapshot(time.time()).start_quality)

    # --- pricing ----------------------------------------------------------

    def _basis_tie_coins(self, now_s: float) -> frozenset[str]:
        """Coins still in contention for the win once the CF replica is
        considered alongside the mid: each source's leader plus anything within
        basis_tie_gap of it. Empty outside the last minute or when one coin
        clearly leads on both."""
        gap = self.cfg.pricing.basis_tie_gap
        if gap <= 0 or now_s < self.window.end_avg_begin:
            return frozenset()
        out: set[str] = set()
        for source in ("mid", "cf"):
            r = self.tracker.projected_returns(now_s, source)
            if any(v is None for v in r.values()):
                return frozenset()
            best = max(r.values())
            out |= {c for c, v in r.items() if best - v < gap}
        return frozenset(out) if len(out) > 1 else frozenset()

    def _withhold(self, reason: str):
        self.result = None
        self.basis_tie = frozenset()
        self.withheld_reason = reason
        return {t: None for t in self.tickers}

    def _ingest_bars(self, now_s: float):
        if self.spot is not None:
            self.spot.sample(now_s)
            return
        sampled = self.bus.sample_all(now_s)
        for bar in sampled.values():
            self.tracker.ingest(bar)
        if sampled:
            try:
                self.forecaster.update(max(b.ts_s for b in sampled.values()),
                                       {c: b.mid for c, b in sampled.items()})
            except NotImplementedError:
                pass

    def _price(self, now_s: float, compute_delta: bool | None):
        p = self.cfg.pricing
        mono = time.monotonic()
        # Health runs on the monotonic clock (VenueBook.receive_ts); wall
        # clock vs monotonic once read every coin as 1.7e9s stale.
        self.health.observe(self.bus.venue_books_per_coin(), now=mono)
        self._down = self.health.down_coins(COINS, mono)
        if self._down:
            # The winner is a joint comparison: one DOWN coin makes every fair unknowable.
            return self._withhold(f"coin_down:{','.join(self._down)}")
        alarms = self.forecaster.alarms() if hasattr(self.forecaster, "alarms") else []
        if alarms:
            return self._withhold(f"vol_alarm:{','.join(alarms)}")

        snap = self.tracker.snapshot(now_s)
        in_window = now_s >= self.window.S
        start_locked = in_window and all(snap.start_quality.get(c) in ("ok", "partial", "kalshi") for c in COINS)
        if in_window and not start_locked:
            return self._withhold("no_start_twap")
        if not in_window and not p.quote_before_start:
            return self._withhold("before_start")

        books = self.bus.venue_books_per_coin()
        newest = []
        for c in COINS:
            ts = [vb.receive_ts for vb in books.get(c, []) if not vb.is_resyncing]
            newest.append(max(ts) if ts else 0.0)
        if min(newest) <= 0 or max(newest) - min(newest) > p.max_cross_coin_skew_s:
            return self._withhold("cross_coin_skew")

        x, I = np.zeros(len(COINS)), np.zeros(len(COINS))
        if start_locked:
            for i, c in enumerate(COINS):
                m, s = self.bus.mid_nowcast(c), snap.start_twap.get(c)
                if m is None or s is None or s <= 0:
                    return self._withhold("no_mid")
                x[i] = math.log(m / s)
                if snap.projected_return_log.get(c) is not None and now_s >= self.window.end_avg_begin:
                    # Pricer wants a partial SUM over the end average, not the
                    # partial average (passing the average double-counted it).
                    I[i] = snap.projected_return_log[c] * snap.end_sample_count.get(c, 0) / float(START_END_AVG_S)
        if start_locked:
            # Estimated off the unshifted fair, so the shift can't feed on itself.
            self._update_market_shift(now_s, x, I, start_locked)
            sh = p.market_shift_alpha * self._shift
            x = x + sh
            if now_s >= self.window.end_avg_begin:
                # The shift moves every end-average sample already taken too.
                n = min(START_END_AVG_S, max(0.0, now_s - self.window.end_avg_begin))
                I = I + sh * n / float(START_END_AVG_S)
        if compute_delta is None:
            compute_delta = self.result is None or now_s - self._last_delta_s >= p.delta_refresh_s
        self.result = self.pricer.fairs_at(x=x, I=I, tau=max(0.0, self.window.T - now_s), now_s=now_s,
                                           S=self.window.S, T=self.window.T, start_locked=start_locked,
                                           compute_delta=compute_delta, prev_result=self.result)
        if compute_delta:
            self._last_delta_s = now_s
        self._newest_mono = min(newest)
        self._dirty = False
        self.withheld_reason = None
        self.basis_tie = self._basis_tie_coins(now_s)
        k = p.ci_vol_multiplier
        out = {}
        for i, c in enumerate(COINS):
            prob, tv = float(self.result.probs[i]), float(self.result.theo_vol[i])
            # Not clipped to [0, 1]: the band is an edge distance, and clipping
            # would erase the edge on tail coins (fair 0.002, band 0.015).
            out[self.ticker_of[c]] = Fair(prob=prob, lo=prob - k * tv, hi=prob + k * tv,
                                          extras={"coin": c, "theo_vol": tv, "tau": self.result.tau,
                                                  "phase": self.result.phase,
                                                  "basis_tie": c in self.basis_tie})
        return out

    # --- FairModel ----------------------------------------------------------

    def reprice(self, now_s: float):
        self._ingest_bars(now_s)
        return self._price(now_s, None)

    def on_market_event(self, ticker, now_s):
        # Keeps theo on the newest Coinbase mid if the reprice task is queued
        # behind a Kalshi burst. Only when a mid has actually moved: pricing
        # on every Kalshi delta (~37/s, 2026-10-09) was 56% of the event loop
        # and starved the Coinbase reader into stale_signal.
        if self._down or not self._dirty:
            return None
        return self._price(now_s, False)

    def signal_age_s(self):
        """Age of the oldest coin's data under the current fair. While no mid
        has moved since the fair was computed, the fair is still exact, so
        the age follows the books' live freshness (advanced by every message
        on the connection) instead of freezing at the last reprice."""
        if self.result is None or self._newest_mono <= 0:
            return None
        if not self._dirty:
            books = self.bus.venue_books_per_coin()
            live = [max((vb.receive_ts for vb in books.get(c, []) if not vb.is_resyncing), default=0.0)
                    for c in COINS]
            if min(live) > 0:
                return time.monotonic() - min(live)
        return time.monotonic() - self._newest_mono

    def feed_age_s(self):
        books = [vb.receive_ts for vbs in self.bus.venue_books_per_coin().values() for vb in vbs]
        return None if not books else time.monotonic() - max(books)

    def applied_age_s(self):
        return self.signal_age_s()

    def side_block(self, ticker, side, fair, now_s):
        # Both sides: our fair on a contender is confidently wrong either way
        # (KXCRYPTOLEAD15M-26OCT092330: bought HYPE at a 0.97 fair; BTC won on CF).
        if fair is not None and fair.extras.get("basis_tie"):
            return "basis_tie"
        return None

    def quote_block(self, ticker, now_s):
        # The whole event, not just the bursting coin: the race is a joint
        # comparison, so one coin's move reprices all five markets.
        if self.spot is not None and time.monotonic() < self.spot.burst_until:
            return "spot_burst"
        return None

    def summary_extras(self) -> dict:
        r = self.result
        snap = self.tracker.snapshot(time.time())
        return {
            "feed_problem": (None if self.withheld_reason in (None, "before_start")
                             else f"fair withheld: {self.withheld_reason}"),
            "phase": None if r is None else r.phase, "tau_s": snap.tau, "tau_eff": None if r is None else r.tau_eff,
            "start_quality": dict(snap.start_quality), "withheld_reason": self.withheld_reason,
            "basis_tie": sorted(self.basis_tie),
            "market_shift_bp": {c: round(float(self.cfg.pricing.market_shift_alpha * self._shift[i]) * 1e4, 3)
                                for i, c in enumerate(COINS)},
            "spot_burst": (None if self.spot is None else
                            {"active": time.monotonic() < self.spot.burst_until,
                             "coin": self.spot.burst_coin, "n": self.spot.n_bursts,
                             "thresholds": self.spot.burst_thresholds()}),
            "fairs": None if r is None else {c: float(r.probs[i]) for i, c in enumerate(COINS)},
            "theo_vol": None if r is None else {c: float(r.theo_vol[i]) for i, c in enumerate(COINS)},
            "feed_health": self.health.snapshot(),
            "venue_process": (None if self.spot is None or self.spot.venues is None
                              else self.spot.venues.summary()),
            "venues_in_mid": {c: sorted(b.venue for b in f._live_books(time.monotonic()))
                              for c, f in self.bus.feeds.items()},
            "vol_model": self.forecaster.diagnostics() if hasattr(self.forecaster, "diagnostics") else None,
        }

    # --- lifecycle -----------------------------------------------------------

    def feed_tasks(self):
        if self.spot is not None:
            # The session's feed keeps running across events; attach() routes
            # its bars and wakeups here.
            self.spot.ensure_running()
            self.spot.attach(self)
            return [self.kalshi_start_loop()]
        tasks = [self._sample_loop(), self._flush_loop()]
        if self.coinbase is not None:
            tasks.append(self.coinbase.run(self._on_book))
        return tasks

    async def _sample_loop(self):
        """Pin per-second bars aligned to second boundaries (Kalshi's own
        snapshots are second-aligned)."""
        while True:
            now = time.time()
            try:
                self._ingest_bars(now)
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

    async def close(self):
        if self.spot is not None:
            self.spot.detach(self)
            return
        self.bus.flush_all()


def warm_forecaster(cfg, until_s: float) -> RobustHarForecaster:
    """A forecaster replayed over the persisted warmup window. Built fresh so
    it can run off the loop while the live one keeps sampling."""
    f = RobustHarForecaster()
    by_s: dict[int, dict[str, float]] = {}
    for c in COINS:
        for b in load_composite_bars(c, until_s - cfg.pricing.warmup_s, until_s):
            by_s.setdefault(int(b.ts_s), {})[c] = b.mid
    for ts in sorted(by_s):
        f.update(float(ts), by_s[ts])
    return f
