"""
Live model for one single-coin 15-minute up/down event (KXBTC15M etc.):
YES iff the mean of the CF index's whole seconds over [T-60, T) is at least
the mean over [S-60, S) (the published floor_strike).

Same inputs as the Coin Race (KXCRYPTOLEAD15M/model.py): the CF index from
Kalshi's relay for the start and the observed end seconds, the venue-median
mid as the fast signal between CF ticks, put on CF's level by a 30s basis:

    x = ln(mid / start) - basis
    I = (1/60) * sum of ln(CF_s / start) over end seconds in, with elapsed
        seconds whose tick hasn't landed filled with x
"""
from __future__ import annotations

import math
import time

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import Fair
from kalshi_strategies.CRYPTO15M.pricer import implied_mu, prob_up
from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import STALE_S as CF_STALE_S
from kalshi_strategies.KXCRYPTOLEAD15M.model import BASIS_MIN_S, BASIS_WINDOW_S
from kalshi_strategies.KXCRYPTOLEAD15M.series import START_END_AVG_S
from kalshi_strategies.KXCRYPTOLEAD15M.settlement_tracker import EventWindow

L = float(START_END_AVG_S)


class UpDownModel:
    def __init__(self, ev: EventInfo, cfg, coin: str, spot):
        if len(ev.markets) != 1:
            raise ValueError(f"{ev.event_ticker}: expected one market, got {len(ev.markets)}")
        self.cfg = cfg
        self.coin = coin
        self.event_ticker = ev.event_ticker
        self.ticker = ev.markets[0]["ticker"]
        self.tickers = [self.ticker]
        self.window = EventWindow(event_ticker=ev.event_ticker, S=ev.open_time.timestamp(),
                                  T=ev.close_time.timestamp())
        self.spot, self.cf = spot, spot.cf
        self.bus, self.health = spot.bus, spot.health
        try:
            self.kalshi_strike = float(ev.markets[0].get("floor_strike")) or None
        except (TypeError, ValueError):
            self.kalshi_strike = None
        self.start: float | None = None
        self.start_src: str | None = None
        self.basis: float | None = None
        self._basis_key = None
        self.result = None
        self.withheld_reason: str | None = "warming"
        self._wake = None
        self._dirty = True
        self._newest_mono = 0.0
        self._kbid: float | None = None
        self._kask: float | None = None
        # EMA of the market-implied shift in x (log units), see _market_shift.
        self._shift = 0.0
        self._shift_raw = 0.0
        self._shift_t: float | None = None
        self._shift_fit_t = -math.inf

    # --- inputs -------------------------------------------------------------

    def on_book(self, book):
        if book.ticker == self.ticker:
            self._kbid, self._kask = book.bid, book.ask

    def _kmid(self) -> float | None:
        if self._kbid is None or self._kask is None or self._kask <= self._kbid:
            return None
        return 0.5 * (self._kbid + self._kask)

    def set_wakeup(self, fn):
        self._wake = fn

    def _mid_moved(self):
        self._dirty = True
        if self._wake is not None:
            self._wake()

    def cf_ticked(self):
        self._mid_moved()

    # --- CF -----------------------------------------------------------------

    def _start(self) -> float | None:
        """CF mean over [S-60, S) once all 60 seconds are in, else Kalshi's
        published floor_strike (the same number to <= 0.02bp, cf_index)."""
        if self.start_src == "cf":
            return self.start
        w = self.window
        vals = self.cf.window(self.coin, w.start_avg_begin, w.S)
        if len(vals) == START_END_AVG_S:
            self.start, self.start_src = sum(vals) / L, "cf"
        elif self.kalshi_strike is not None:
            self.start, self.start_src = self.kalshi_strike, "kalshi"
        elif time.time() > w.S + 2:
            self.cf.request_fill(self.coin)
        return self.start

    def _basis_now(self) -> float | None:
        """mean ln(mid / CF) over the last BASIS_WINDOW_S seconds both hold."""
        feed = self.bus.feeds[self.coin]
        key = (feed.newest_ts(), self.cf.version)
        if key == self._basis_key:
            return self.basis
        latest = self.cf.latest(self.coin)
        b = None
        if latest is not None:
            bars = {int(x.ts_s): x.mid for x in feed.tail(BASIS_WINDOW_S + 5)}
            d = [math.log(bars[t] / v) for t in range(latest[0] - BASIS_WINDOW_S + 1, latest[0] + 1)
                 if (v := self.cf.value(self.coin, t)) is not None and bars.get(t)]
            b = sum(d) / len(d) if len(d) >= BASIS_MIN_S else None
        self.basis, self._basis_key = b, key
        return b

    def _cf_age(self) -> float | None:
        t = self.cf._last_rx_mono.get(self.coin)
        return None if t is None else time.monotonic() - t

    # --- pricing ----------------------------------------------------------------

    def _withhold(self, reason: str):
        self.result = None
        self.withheld_reason = reason
        return {self.ticker: None}

    def _rate(self, h: float) -> float:
        f = self.spot.forecaster
        return float(f.rate_per_s(h, time.time())[f.coin_order.index(self.coin)]) * self.cfg.pricing.vol_mult

    def _market_shift(self, now_s: float, x: float, I: float, tau: float, rate: float, basis: float):
        """EMA toward the shift in x that would put our fair on the Kalshi mid.
        Tracked at alpha 0 too (logged as shift_ema_bp: how far the market
        disagrees). Frozen in the last minute, like the race's."""
        p = self.cfg.pricing
        if tau < L:
            return
        if self._shift_t is not None:
            lam = 1.0 - 0.5 ** (max(0.0, now_s - self._shift_t) / p.market_shift_half_life_s)
            self._shift += lam * (self._shift_raw - self._shift)
        self._shift_t = now_s
        km = self._kmid()
        if km is None or now_s - self._shift_fit_t < p.market_shift_fit_s:
            return
        base = prob_up(x, I, tau, rate, nu=p.t_nu, basis_sd=basis, with_theo_vol=False)
        cap = p.market_shift_max_bp * 1e-4
        self._shift_raw = max(-cap, min(cap, implied_mu(km, base.sd, p.t_nu) - base.mu))
        self._shift_fit_t = now_s

    def _price(self, now_s: float):
        p = self.cfg.pricing
        mono = time.monotonic()
        self.health.observe({self.coin: self.bus.feeds[self.coin].venue_books()}, now=mono)
        if self.health.down_coins([self.coin], mono):
            return self._withhold("coin_down")
        f = self.spot.forecaster
        if self.coin in f.alarms():
            return self._withhold("vol_alarm")
        if f.rv[self.coin].n < p.min_rv_seconds:
            return self._withhold("vol_warming")
        if now_s < self.window.S:
            return self._withhold("before_start")
        start = self._start()
        if start is None:
            return self._withhold("no_start")
        books = [vb.receive_ts for vb in self.bus.feeds[self.coin].venue_books() if not vb.is_resyncing]
        mid = self.bus.mid_nowcast(self.coin)
        if not books or mid is None or mid <= 0:
            return self._withhold("no_mid")
        b = self._basis_now()
        if b is None:
            return self._withhold("no_cf_basis")

        w = self.window
        tau = max(0.0, w.T - now_s)
        x = math.log(mid / start) - b
        I, n_end = 0.0, 0
        if tau < L:
            vals = self.cf.window(self.coin, w.end_avg_begin, min(math.ceil(now_s), w.T))
            n_end = len(vals)
            n_eff = min(L, max(float(n_end), L - tau))
            I = (sum(math.log(v / start) for v in vals) + x * (n_eff - n_end)) / L
        rate = self._rate(max(tau, 1.0))
        bsd = p.basis_sd_bp * 1e-4
        self._market_shift(now_s, x, I, tau, rate, bsd)
        sh = p.market_shift_alpha * self._shift
        r = prob_up(x + sh, I, tau, rate, nu=p.t_nu, basis_sd=bsd, reaction_s=p.reaction_latency_s)
        self.result = r
        self._newest_mono = max(books)
        self._dirty = False
        self.withheld_reason = None
        k = p.ci_vol_multiplier
        return {self.ticker: Fair(prob=r.prob, lo=r.prob - k * r.theo_vol, hi=r.prob + k * r.theo_vol,
                                  extras={"coin": self.coin, "theo_vol": r.theo_vol, "tau": tau, "phase": r.phase,
                                          "x_bp": x * 1e4, "mu_bp": r.mu * 1e4, "sd_bp": r.sd * 1e4,
                                          "shift_bp": sh * 1e4, "shift_ema_bp": self._shift * 1e4,
                                          "start": start, "start_src": self.start_src, "cf_basis_bp": b * 1e4,
                                          "cf_end_n": n_end, "spot_mid": mid, "kmid": self._kmid()})}

    # --- FairModel ----------------------------------------------------------------

    def reprice(self, now_s: float):
        self.spot.sample(now_s)
        return self._price(now_s)

    def on_market_event(self, ticker, now_s):
        if not self._dirty:
            return None
        return self._price(now_s)

    def signal_age_s(self):
        """Age of the newest venue book under the fair (live while no mid has
        moved since it), or the CF feed's whole age once it is past its
        STALE_S, as the race does."""
        if self.result is None or self._newest_mono <= 0:
            return None
        cf_age = self._cf_age()
        if cf_age is None:
            return None
        cf_x = cf_age if cf_age > CF_STALE_S else 0.0
        if not self._dirty:
            live = [vb.receive_ts for vb in self.bus.feeds[self.coin].venue_books() if not vb.is_resyncing]
            if live:
                return max(time.monotonic() - max(live), cf_x)
        return max(time.monotonic() - self._newest_mono, cf_x)

    def feed_age_s(self):
        ts = [vb.receive_ts for vb in self.bus.feeds[self.coin].venue_books()]
        return None if not ts else time.monotonic() - max(ts)

    def applied_age_s(self):
        return self.signal_age_s()

    def side_block(self, ticker, side, fair, now_s):
        p = self.cfg.pricing
        if self.window.T - now_s < p.min_trade_tau_s:
            return "near_close"
        # Outside [0.10, 0.90] these markets tick in 0.1c, which the shared book
        # and quoter round to whole cents; prices there are not trustworthy.
        lo, hi = p.tradeable_min, p.tradeable_max
        if not (lo <= fair.prob <= hi):
            return "fair_in_tail"
        if self._kbid is None or self._kask is None or self._kbid < lo or self._kask > hi:
            return "book_in_tail"
        return None

    def quote_block(self, ticker, now_s):
        until = (self.spot.burst_until_by_coin.get(self.coin, 0.0) if self.cfg.pricing.burst_own_coin_only
                 else self.spot.burst_until)
        if time.monotonic() < until:
            return "spot_burst"
        return None

    def summary_extras(self) -> dict:
        r = self.result
        return {
            "feed_problem": (None if self.withheld_reason in (None, "before_start")
                             else f"fair withheld: {self.withheld_reason}"),
            "coin": self.coin, "withheld_reason": self.withheld_reason,
            "phase": None if r is None else r.phase, "tau_s": max(0.0, self.window.T - time.time()),
            "fair": None if r is None else r.prob, "theo_vol": None if r is None else r.theo_vol,
            "sd_bp": None if r is None else r.sd * 1e4,
            "kalshi_strike": self.kalshi_strike, "start": self.start, "start_src": self.start_src,
            "cf_basis_bp": None if self.basis is None else self.basis * 1e4, "cf_age_s": self._cf_age(),
            "kalshi_mid": self._kmid(), "market_shift_bp": self.cfg.pricing.market_shift_alpha * self._shift * 1e4,
            "spot_burst": {"active": time.monotonic() < self.spot.burst_until_by_coin.get(self.coin, 0.0),
                           "n": self.spot.n_bursts},
            "feed_health": self.health.snapshot(),
        }

    # --- lifecycle -------------------------------------------------------------------

    def feed_tasks(self):
        self.spot.ensure_running()
        self.spot.attach(self)
        return []

    async def close(self):
        self.spot.detach(self)


__all__ = ["UpDownModel"]
