"""
Live Coin Race model for one KXCRYPTOLEAD15M event: venue books -> per-coin
composite mid; CF settlement index -> start and end averages; HAR vol
forecaster -> MC winner probabilities -> per-market Fair.

The race settles on the CF index (settlement_tracker). The mid is only the
fast signal between CF ticks, so it is put on CF's level first:

    x_i = ln(mid_i / start_i) - basis_i,  basis_i = mean ln(mid/CF) over the
                                           last BASIS_WINDOW_S CF seconds
    I_i = (1/60) * sum of ln(CF_s / start_i) over end seconds already in

Replay 10-12 17:00..10-18 05:00 UTC (516 events): last-minute winner log loss
0.238 -> 0.115 on the held-out half (mid-only start and end), no change
before the last minute (+0.0002, CI +-0.006).

Band = prob +/- ci_vol_multiplier * theo_vol, where theo_vol is the
sigma-equivalent fair move over reaction_latency_s.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import Fair
from kalshi_strategies.KXCRYPTOLEAD15M.composite import DATA_ROOT, CompositeBar, CompositeBus, bar_files
from kalshi_strategies.KXCRYPTOLEAD15M.pricer import BaseRateTracker, CoinRacePricer, FairResult
from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS, START_END_AVG_S, coin_from_market_ticker
from kalshi_strategies.KXCRYPTOLEAD15M.settlement_tracker import EventWindow, SettlementTracker
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import FeedHealthMonitor

# Seconds of (mid, CF) pairs the basis averages. 30 in the replay above; the
# basis is a slow level offset (HYPE -1.9bp, XRP -1.8bp mean, 10-12..18) plus
# ~1bp of noise per coin that a shorter window would chase.
BASIS_WINDOW_S = 30
BASIS_MIN_S = 10


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
    def __init__(self, ev: EventInfo, cfg, *, cf, bus: CompositeBus | None = None, forecaster=None, spot=None):
        """`cf`: the session's CfIndex. Live: pass `spot` (a session-lifetime
        live_feed.SpotFeed), which supplies the bus, forecaster and health.
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
        self.cf = cf
        self.tracker = SettlementTracker(self.window, cf)
        p = cfg.pricing
        self.spot = spot
        if spot is not None:
            bus, forecaster = spot.bus, spot.forecaster
            self.health = spot.health
        else:
            self.health = FeedHealthMonitor(soft_stale_s=p.soft_stale_s, hard_stale_s=p.hard_stale_s,
                                            recovery_s=p.recovery_s)
        self.forecaster = forecaster or RobustHarForecaster()
        self.pricer = CoinRacePricer(self.forecaster, p, base_rate=BaseRateTracker(p.base_rate_window).rate())
        self.bus = bus
        self.result: FairResult | None = None
        self.withheld_reason: str | None = "warming"
        self._last_delta_s = 0.0
        self._newest_mono = 0.0     # oldest coin's newest book under the current fair
        self._down: list[str] = list(COINS)
        self._wake = None
        # A mid has moved since the last _price, so the fair lags its inputs.
        self._dirty = True
        self.basis_bp: dict[str, float] = {}
        self._basis_cache: dict[str, tuple] = {}
        self.vol_bp: dict[str, float] = {}
        self.vol_src: dict[str, str] = {}

    def set_wakeup(self, fn):
        self._wake = fn

    def _on_book(self, vb):
        """Venue book (without a SpotFeed). Wakes the reprice only when the
        coin's mid moves: level2 sends ~64 msg/s across the five coins but the
        mid changes ~4.6/s (2026-10-09)."""
        before = self.bus.mid_nowcast(vb.coin) if vb.coin in self.bus.feeds else None
        self.bus.observe(vb)
        if self.bus.mid_nowcast(vb.coin) != before:
            self._mid_moved()

    def _mid_moved(self):
        self._dirty = True
        if self._wake is not None:
            self._wake()

    def cf_ticked(self):
        """A new CF second arrived: in the last minute it moves I directly."""
        self._mid_moved()

    # --- pricing ----------------------------------------------------------

    def _basis(self, coin: str) -> float | None:
        """mean ln(mid / CF) over the last BASIS_WINDOW_S seconds both hold.
        Changes only when a bar or a CF second lands, so cached on those."""
        feed = self.bus.feeds[coin]
        key = (feed.newest_ts(), getattr(self.cf, "version", None))
        hit = self._basis_cache.get(coin)
        if hit is not None and hit[0] == key and key[1] is not None:
            return hit[1]
        b = self._basis_calc(coin, feed)
        self._basis_cache[coin] = (key, b)
        return b

    def _basis_calc(self, coin: str, feed) -> float | None:
        bars = {int(b.ts_s): b.mid for b in feed.tail(BASIS_WINDOW_S + 5)}
        latest = self.cf.latest(coin)
        if latest is None:
            return None
        diffs = []
        for t in range(latest[0] - BASIS_WINDOW_S + 1, latest[0] + 1):
            v, m = self.cf.value(coin, t), bars.get(t)
            if v is not None and m is not None and m > 0:
                diffs.append(math.log(m / v))
        return float(np.mean(diffs)) if len(diffs) >= BASIS_MIN_S else None

    def _implied(self):
        """The session's ImpliedRates (on the SpotFeed), or None without one."""
        return None if self.spot is None else getattr(self.spot, "implied_rates", None)

    def _spot_move(self, coin: str, now_s: float) -> float:
        """|ln(mid now / mid implied_lag_s ago)|: how far Kalshi may lag spot."""
        lag = self.cfg.pricing.implied_lag_s
        bars = self.bus.feeds[coin].tail(int(lag) + 3)
        old = [b.mid for b in bars if b.ts_s <= now_s - lag]
        cur = self.bus.mid_nowcast(coin)
        return abs(math.log(cur / old[-1])) if old and cur else 0.0

    def _withhold(self, reason: str):
        self.result = None
        self.withheld_reason = reason
        return {t: None for t in self.tickers}

    def _ingest_bars(self, now_s: float):
        if self.spot is not None:
            self.spot.sample(now_s)
            return
        sampled = self.bus.sample_all(now_s)
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
        if now_s < self.window.S:
            return self._withhold("before_start")
        snap = self.tracker.snapshot(now_s)
        missing = [c for c, v in snap.start.items() if v is None]
        if missing:
            if now_s > self.window.S + 2:
                for c in missing:
                    self.cf.request_fill(c)
            return self._withhold("no_cf_start")

        books = self.bus.venue_books_per_coin()
        newest = []
        for c in COINS:
            ts = [vb.receive_ts for vb in books.get(c, []) if not vb.is_resyncing]
            newest.append(max(ts) if ts else 0.0)
        if min(newest) <= 0 or max(newest) - min(newest) > p.max_cross_coin_skew_s:
            return self._withhold("cross_coin_skew")

        tau = max(0.0, self.window.T - now_s)
        L = float(START_END_AVG_S)
        x, I = np.zeros(len(COINS)), np.zeros(len(COINS))
        for i, c in enumerate(COINS):
            m, b = self.bus.mid_nowcast(c), self._basis(c)
            if m is None or m <= 0:
                return self._withhold("no_mid")
            if b is None:
                return self._withhold("no_cf_basis")
            self.basis_bp[c] = b * 1e4
            x[i] = math.log(m / snap.start[c]) - b
            # The pricer takes the end average as elapsed (L - tau) + remaining
            # tau. An elapsed second whose CF tick hasn't landed yet (~70ms
            # after it) is filled with x, the best guess for it, so I and the
            # pricer's remaining-time term never both leave it out.
            n_eff = min(L, max(float(snap.end_n[c]), L - tau)) if tau < L else 0.0
            I[i] = (snap.end_log_sum[c] + x[i] * (n_eff - snap.end_n[c])) / L
        rate = None
        iv = self._implied()
        if iv is not None and tau >= L:
            # Read whatever vol_source says, so switching to implied finds the EMAs warm.
            for i, c in enumerate(COINS):
                iv.observe(now_s, c, float(x[i]), tau, self._spot_move(c, now_s), p)
        if p.vol_source == "implied":
            if iv is None:
                return self._withhold("no_implied_vol:feed")
            rates = iv.rates(now_s, p)
            short = [c for c in COINS if rates[c] is None]
            if short and not p.implied_fallback_realized:
                return self._withhold(f"no_implied_vol:{','.join(short)}")
            if short:
                # A coin whose 15M market says nothing about vol right now (mid near
                # 0.5, pinned, or the event just opened) prices on the HAR model
                # instead of withholding the whole race.
                har = self.pricer._rates_corr(max(tau, 1.0), now_s)[0]
                for i, c in enumerate(COINS):
                    if rates[c] is None:
                        rates[c] = float(har[i])
            self.vol_src = {c: ("realized" if c in short else "implied") for c in COINS}
            rate = np.array([rates[c] for c in COINS])
            self.vol_bp = {c: math.sqrt(rates[c]) * 1e4 for c in COINS}
        if compute_delta is None:
            compute_delta = self.result is None or now_s - self._last_delta_s >= p.delta_refresh_s
        self.result = self.pricer.fairs_at(x=x, I=I, tau=tau, now_s=now_s,
                                           S=self.window.S, T=self.window.T, start_locked=True,
                                           compute_delta=compute_delta, prev_result=self.result,
                                           rate_override=rate)
        if compute_delta:
            self._last_delta_s = now_s
        self._newest_mono = min(newest)
        self._dirty = False
        self.withheld_reason = None
        k = p.ci_vol_multiplier
        out = {}
        for i, c in enumerate(COINS):
            prob, tv = float(self.result.probs[i]), float(self.result.theo_vol[i])
            # Not clipped to [0, 1]: the band is an edge distance, and clipping
            # would erase the edge on tail coins (fair 0.002, band 0.015).
            out[self.ticker_of[c]] = Fair(prob=prob, lo=prob - k * tv, hi=prob + k * tv,
                                          extras={"coin": c, "theo_vol": tv, "tau": self.result.tau,
                                                  "phase": self.result.phase, "cf_basis_bp": self.basis_bp[c],
                                                  "cf_end_n": snap.end_n[c],
                                                  "vol_bp": math.sqrt(float(self.pricer._last_rate[i])) * 1e4,
                                                  "vol_src": (self.vol_src.get(c, "implied")
                                                              if p.vol_source == "implied" else "realized")})
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
        """Age of the oldest input under the current fair: the oldest coin's
        venue book, or the CF feed if it has gone quiet (pricing on a frozen
        start/basis/end average is exactly the stale case). While no mid has
        moved since the fair was computed, the fair is still exact, so the
        book age follows the books' live freshness instead of the last reprice."""
        if self.result is None or self._newest_mono <= 0:
            return None
        cf_age = self.cf.age_s()
        if cf_age is None:
            return None
        if not self._dirty:
            books = self.bus.venue_books_per_coin()
            live = [max((vb.receive_ts for vb in books.get(c, []) if not vb.is_resyncing), default=0.0)
                    for c in COINS]
            if min(live) > 0:
                return max(time.monotonic() - min(live), self._cf_excess(cf_age))
        return max(time.monotonic() - self._newest_mono, self._cf_excess(cf_age))

    @staticmethod
    def _cf_excess(cf_age: float) -> float:
        """CF ticks once a second, so its age alone runs 0-1s on a healthy
        feed. Only age past cf_index.STALE_S counts, and then in full, so it
        trips max_signal_age_s at once."""
        from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import STALE_S
        return cf_age if cf_age > STALE_S else 0.0

    def feed_age_s(self):
        books = [vb.receive_ts for vbs in self.bus.venue_books_per_coin().values() for vb in vbs]
        return None if not books else time.monotonic() - max(books)

    def applied_age_s(self):
        return self.signal_age_s()

    def side_block(self, ticker, side, fair, now_s):
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
            "cf_start": dict(snap.start), "cf_start_n": dict(snap.start_n), "cf_end_n": dict(snap.end_n),
            "cf_age_s": self.cf.age_s(), "cf_basis_bp": {c: round(v, 3) for c, v in self.basis_bp.items()},
            "withheld_reason": self.withheld_reason,
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
            "vol_source": self.cfg.pricing.vol_source,
            "vol_bp": {c: round(v, 4) for c, v in self.vol_bp.items()},
            "vol_src_by_coin": dict(self.vol_src),
            "implied_vol": (None if self._implied() is None
                            else self._implied().snapshot(time.time(), self.cfg.pricing)),
        }

    # --- lifecycle -----------------------------------------------------------

    def feed_tasks(self):
        # The session's feeds keep running across events; attach() routes
        # their bars and wakeups here.
        self.spot.ensure_running()
        self.spot.attach(self)
        iv = self._implied()
        # Polled whatever vol_source says, so flipping it to implied mid-event
        # finds the EMAs already warm.
        return [] if iv is None else [iv.poll(self.event_ticker)]

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
