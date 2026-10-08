"""
CoinRaceStrategy -- the strategy_fn plugged into Backtest / LiveEngine.

Interfaces required by the live harness
(kalshi_latency_arbs/live/live_context.py):

  * `__call__(ctx)` -- one wakeup. Reads composite nowcast, re-prices
    all 5 markets, decides quotes + takes for the ticker in
    ctx.last_event.
  * `live_quote_order_ids() -> list[int]` -- resting-quote ids the
    LiveEngine's pull_quotes may cancel.
  * `clear_quotes()` -- after the engine has cancelled quotes itself.
  * `drop_quote(order_id)` -- an order never made it to the book
    (rate-limited / rejected).

Convention mirrors `sp_options_lag_quoting.SPOptionsLagQuotingStrategy`
so a reader familiar with the SPX quoter sees the same shape here.

Signal plumbing: the strategy OWNS a `CompositeBus` reference set by
the harness (`attach_composite`). On every wakeup it pulls the latest
nowcast for every coin, feeds the pricer, and caches a `FairResult`
onto `event_state.fair_result`. Composite updates are PUSHED as
external events into the backtest via `strategy.push_composite_event()`.
"""
from __future__ import annotations

import collections
import dataclasses
import math
import time
from typing import Callable

import numpy as np
import pandas as pd

from .config import CoinRaceConfig, EDITABLE_FIELDS, CRYPTO_FEE_MULTIPLIER
from .composite import CompositeBus
from .cov_forecaster import CovarianceForecaster, ConstantCovForecaster
from .event_state import EventState
from .fill_intensity import FillIntensityModel
from .lip_scorer import score_book, LipLevel, LipParams
from .pricer import CoinRacePricer, FairResult, BaseRateTracker
from .quoter import (EdgeDecomposition, decompose_edge, desired_quote_level,
                     apply_event_constraints, crypto_fee)
from .series import COINS, TICK, START_END_AVG_S, coin_from_market_ticker
from .spot_feed import FeedHealthMonitor


# Signal payloads the strategy pushes into the engine on composite ticks
# (via LiveEngine.dispatch or Backtest.external_events) have this shape.
# Mirrors sp_options_lag_quoting's dict payload.
# Keys: "kind": "composite_tick", "coin_prices": {coin: px}, "ts": epoch_s.
COMPOSITE_EVENT_KIND = "composite_tick"


class CoinRaceStrategy:
    """Stateful strategy_fn for the Coin Race market maker.

    One instance per event. Reused across all 5 markets of that event.
    """

    def __init__(
        self,
        event_state: EventState,
        config: CoinRaceConfig | None = None,
        forecaster: CovarianceForecaster | None = None,
        base_rate_tracker: BaseRateTracker | None = None,
        fill_model: FillIntensityModel | None = None,
        record_debug: bool = True,
    ):
        self.event_state = event_state
        self.config = config or CoinRaceConfig()
        self.forecaster = forecaster or ConstantCovForecaster()
        self.base_rate = base_rate_tracker or BaseRateTracker(self.config.base_rate_window)
        self.pricer = CoinRacePricer(
            self.forecaster, self.config, base_rate=self.base_rate.rate())
        # Fill-intensity model for the LIP credit's expected-rest-time. Loads
        # the fitted artifact if present, else a fixed-rate fallback (still
        # finite, so the credit stops being the full-period upper bound).
        self.fill_model = fill_model or FillIntensityModel.load()
        self.composite_bus: CompositeBus | None = None
        self.feed_health = FeedHealthMonitor(
            soft_stale_s=self.config.composite_soft_stale_s,
            hard_stale_s=self.config.composite_hard_stale_s,
            recovery_s=self.config.composite_recovery_s)
        # Coins currently DOWN (no usable book / hard-stale). Their legs
        # are pulled; a single DOWN coin invalidates the whole event's
        # theo (the winner is a joint comparison). Updated every tick.
        self._down_coins: list[str] = list(COINS)

        # Per-(ticker, side) resting quote state. Same shape as the SPX
        # strategy -- `_quotes` maps to {"order_id", "price", "size"} in
        # that side's price terms.
        self._quotes: dict[tuple[str, str], dict] = {}
        self._quote_block_until: dict[tuple[str, str], pd.Timestamp] = {}
        # Just-sent order sizes, used by the inventory cap and lean (see
        # QuotingConfig.inflight_timeout_s).
        self._sent: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
        self._fills_seen = 0

        # Current/last pricer output. Recomputed on every wakeup.
        self._last_fair_result: FairResult | None = None
        self._last_fair_s: float = 0.0
        self._last_delta_s: float = 0.0
        # Monotonic receive time of the newest venue book under the fair
        # (theo_stale kill switch).
        self._last_price_mono: float = 0.0
        self._fresh_since: float | None = None
        # Set by the runner once real trading has been cut for this event
        # (config.real_trading_cutoff_min). Paper trading continues.
        self.real_cutoff_hit: bool = False
        self._take_since: dict[tuple[str, str], float] = {}
        self._take_last: dict[tuple[str, str], float] = {}
        # Latest 5-vector of composite log returns relative to start twap.
        # Rebuilt on every composite tick when start is locked, else zeros.
        self._x = np.zeros(len(COINS))
        self._I = np.zeros(len(COINS))

        self.record_debug = record_debug
        self.debug_log: list[dict] = []
        # Rotating per-market decision log for the per-market viz JSONs.
        # Keyed by ticker; viz reads this via `per_market_series`.
        self._market_log: dict[str, list[dict]] = {t: [] for t in event_state.tickers.values()}

        # Accumulated LIP reward estimate, per market. Updated on every
        # qualifying snapshot (one per second); reconciled against
        # actual payouts post-event.
        self._lip_accrued_usd: dict[str, float] = {t: 0.0 for t in event_state.tickers.values()}
        self._lip_snapshots_qualified: dict[str, int] = {t: 0 for t in event_state.tickers.values()}
        self._lip_snapshots_total: dict[str, int] = {t: 0 for t in event_state.tickers.values()}
        self._last_lip_snapshot_s: float = 0.0

    # ------------------------------------------------------------------
    # Harness plumbing
    # ------------------------------------------------------------------

    def attach_composite(self, bus: CompositeBus):
        self.composite_bus = bus

    def live_quote_order_ids(self) -> list[int]:
        return [q["order_id"] for q in self._quotes.values()]

    def clear_quotes(self):
        self._quotes.clear()

    def drop_quote(self, order_id: int):
        for key, q in list(self._quotes.items()):
            if q["order_id"] == order_id:
                del self._quotes[key]
                return

    def apply_config_override(self, overrides: dict):
        """Apply a config-override.json read by the harness. Only
        whitelisted fields in EDITABLE_FIELDS are applied; unknown
        fields are silently dropped (otherwise a dashboard typo would
        crash the live loop)."""
        changed = {}
        for k, v in overrides.items():
            if k not in EDITABLE_FIELDS:
                continue
            if hasattr(self.config, k) and getattr(self.config, k) != v:
                setattr(self.config, k, v)
                changed[k] = v
        if changed:
            print(f"[kalshi_crypto] config override applied: {changed}")

    # ------------------------------------------------------------------
    # Composite -> fair value
    # ------------------------------------------------------------------

    def on_composite_tick(self, now_s: float):
        """Called every time a new 1s composite bar is sampled OR a
        fast nowcast arrives (same code path -- repricing is cheap).
        Updates x, I, and the pricer's FairResult cache.

        After this returns, `self._last_fair_result` is either a fresh
        FairResult or None (feed-health gate failed)."""
        if self.composite_bus is None:
            return

        es = self.event_state
        # Feed-health observation. Health runs on the MONOTONIC clock --
        # VenueBook.receive_ts is set from time.monotonic() in the
        # adapters, so comparing it against now_s (a time.time() Unix
        # epoch) would subtract two different clocks and produce ~1.7e9s
        # "staleness" that marks every coin unhealthy forever (observed
        # 2026-10-06). Everything else in this function stays on
        # wall-clock time (now_s), which the event window S/T and the
        # pricer all share.
        mono_now = time.monotonic()
        self.feed_health.observe(self.composite_bus.venue_books_per_coin(), now=mono_now)
        self._down_coins = self.feed_health.down_coins(COINS, mono_now)
        # Two-threshold kill switch. The winner is a JOINT comparison, so
        # a coin with NO usable data (DOWN -- hard-stale or never seen)
        # makes every fair unknowable: invalidate the whole event's theo
        # and let the quoter pull every leg. But a coin that is merely
        # soft-stale (DEGRADED) keeps its last composite price and the
        # event keeps pricing -- a crypto mid a few seconds old barely
        # moves a multi-minute ranking, and the pricer's variance term
        # already widens as that coin's RV windows thin. This is what
        # stops a one-coin warmup blip from zeroing all 5 fairs (observed
        # 2026-10-06: BTC/ETH flapped, quote_df stayed empty, viz showed
        # "no decisions").
        if self._down_coins:
            self._last_fair_result = None
            es.fair_result = None
            return

        # Feed the composite bus's samples into the SettlementTracker and
        # the forecaster, once per newly-sampled second. Both run on the
        # venue BBO mid (CompositeBar.mid), the same series x uses below.
        sampled = self.composite_bus.sample_all(now_s)
        for bar in sampled.values():
            es.tracker.ingest(bar)
            es.last_bar_ts = max(es.last_bar_ts, bar.ts_s)
        if sampled:
            try:
                ts = max(bar.ts_s for bar in sampled.values())
                self.forecaster.update(ts, {c: bar.mid for c, bar in sampled.items()})
            except NotImplementedError:
                pass

        self._reprice(now_s)

    def _reprice(self, now_s: float, compute_delta: bool | None = None):
        """Recompute the fair from the latest mid nowcast. Cheap (~1 ms):
        run on EVERY wakeup, composite or Kalshi, so theo never lags the
        price feed. The deltas cost ~5 extra argmaxes and only size the
        edge, so they refresh every `delta_refresh_s` unless forced."""
        es = self.event_state
        if self._down_coins:
            self._last_fair_result = None
            es.fair_result = None
            return
        alarms = self.forecaster.alarms() if hasattr(self.forecaster, "alarms") else []
        if alarms:
            # Vol-scale monitor says this coin's feed is broken (e.g. the
            # 2026-10-08 CF-replica HYPE read r^2/f = 0.06). Don't price.
            self._down_coins = list(alarms)
            self._last_fair_result = None
            es.fair_result = None
            return

        snap = es.tracker.snapshot(now_s)
        start_locked = (now_s >= es.window.S and
                        all(snap.start_quality.get(c) in ("ok", "partial") for c in COINS))
        if now_s >= es.window.S and not start_locked:
            # In the window without a start TWAP: x would silently be 0 and
            # the fair a pure variance prior that gets picked off. Refuse.
            self._last_fair_result = None
            es.fair_result = None
            return
        # Cross-coin freshness: the fair ranks the 5 coins against each
        # other, so their prices must be from the SAME moment. If one coin's
        # newest book is fresh and another's is seconds old, x compares a
        # moved coin against a frozen one and the fair jumps to a false
        # leader. 2026-10-08 16:34:33: after a 5s Coinbase stall BTC's book
        # resumed ~0.5s before the others, BTC read +0.18% against four
        # frozen coins, theo jumped 0.28 -> 0.63 for 0.3s and we lifted four
        # 26c YES offers (fee 19c each) that were worth ~0.19 a second later.
        books = self.composite_bus.venue_books_per_coin()
        newest = []
        for c in COINS:
            ts = [vb.receive_ts for vb in books.get(c, []) if not vb.is_resyncing]
            newest.append(max(ts) if ts else 0.0)
        if min(newest) <= 0 or (max(newest) - min(newest)) > self.config.max_cross_coin_skew_s:
            self._last_fair_result = None
            es.fair_result = None
            return
        x = np.zeros(len(COINS))
        I = np.zeros(len(COINS))
        if start_locked:
            for i, c in enumerate(COINS):
                m = self.composite_bus.mid_nowcast(c)
                s = snap.start_twap.get(c)
                if m is None or s is None or s <= 0:
                    self._last_fair_result = None
                    es.fair_result = None
                    return
                x[i] = math.log(m / s)
                if snap.projected_return_log.get(c) is not None and now_s >= es.window.end_avg_begin:
                    # The pricer wants I = (1/60) * sum of observed log
                    # returns (a partial SUM), so mu = I + (u/60) x is the
                    # expected final average. projected_return_log is the
                    # log of the partial AVERAGE; passing it unweighted
                    # double-counted the observed part (30s observed at +1%
                    # gave mu = 1.5%, not 1.0%).
                    n_obs = snap.end_sample_count.get(c, 0)
                    I[i] = snap.projected_return_log[c] * n_obs / float(START_END_AVG_S)

        if compute_delta is None:
            compute_delta = (self._last_fair_result is None
                             or now_s - self._last_delta_s >= self.config.delta_refresh_s)
        self._x = x
        self._I = I
        tau = max(0.0, es.window.T - now_s)
        self._last_fair_result = self.pricer.fairs_at(
            x=x, I=I, tau=tau,
            now_s=now_s, S=es.window.S, T=es.window.T,
            start_locked=start_locked,
            compute_delta=compute_delta,
            prev_result=self._last_fair_result,
        )
        if compute_delta:
            self._last_delta_s = now_s
        self._last_fair_s = now_s
        # The OLDEST coin's newest book: the fair needs all five, so one
        # fresh coin must not make a stale snapshot look fresh.
        self._last_price_mono = min(newest) if newest else 0.0
        es.fair_result = self._last_fair_result

    def _take_ok(self, key, now_s: float) -> bool:
        """True once the take signal for `key` has held continuously for
        take_confirm_s, and at most once per window after that."""
        since = self._take_since.setdefault(key, now_s)
        if now_s - since < self.config.take_confirm_s:
            return False
        last = self._take_last.get(key, -1e18)
        if now_s - last < self.config.take_confirm_s:
            return False
        self._take_last[key] = now_s
        return True

    def theo_stale(self) -> bool:
        """Kill switch: True when the newest price under the fair is older
        than `theo_max_age_s` (the feed or this loop is behind). The
        engine/strategy pulls quotes rather than rest on a frozen theo --
        a theo stuck at 0.10 while the market trades 0.84 is exactly the
        fill to avoid."""
        cfg = self.config
        now = time.monotonic()
        es = self.event_state
        tau = es.window.T - time.time()
        limit = (cfg.theo_max_age_near_expiry_s if tau <= cfg.near_expiry_s
                 else cfg.theo_max_age_s)
        stale = (self._last_fair_result is None or self._last_price_mono <= 0
                 or (now - self._last_price_mono) > limit)
        if stale:
            self._fresh_since = None
            return True
        # Fresh again: hold quotes off until it has STAYED fresh for
        # fresh_recovery_s, so a stuttering feed doesn't flap quotes on/off.
        if self._fresh_since is None:
            self._fresh_since = now
        return (now - self._fresh_since) < cfg.fresh_recovery_s

    # ------------------------------------------------------------------
    # Harness callback: one wakeup, one ticker
    # ------------------------------------------------------------------

    def __call__(self, ctx):
        """Mirrors sp_options_lag_quoting's dispatch shape:
          * sync quote fills first (across ALL tickers)
          * route by last_event: a composite tick repricing (dict with
            `kind == "composite_tick"`) refreshes theo; a Kalshi
            quote/trade re-evaluates its own ticker; a signal payload
            with a `ticker` field goes to that ticker.
        """
        if self._quotes:
            self._sync_quote_fills(ctx)
        else:
            self._fills_seen = len(ctx.portfolio.fills)

        evt = ctx.last_event
        now_s = ctx.time.timestamp() if hasattr(ctx.time, "timestamp") else float(ctx.time)
        if isinstance(evt, dict) and evt.get("kind") == COMPOSITE_EVENT_KIND:
            self.on_composite_tick(now_s)
            # Reprice every market on a composite tick -- all 5 fairs
            # change together.
            for coin in COINS:
                self._act(ctx, self.event_state.tickers[coin])
            # LIP snapshot bookkeeping -- once per wall-clock second.
            if now_s - self._last_lip_snapshot_s >= 1.0:
                self._record_lip_snapshot(ctx, now_s)
                self._last_lip_snapshot_s = now_s
            return

        ticker = getattr(evt, "ticker", None) if evt is not None else None
        if ticker is None and isinstance(evt, dict):
            ticker = evt.get("ticker")
        if ticker is None:
            return
        if coin_from_market_ticker(ticker) is None:
            return
        # Reprice from the latest mid on every Kalshi event too -- the fair
        # is ~1 ms, so there is no cache to go stale. The composite loop can
        # stall under a Kalshi burst near the close; with this, the theo
        # still tracks the newest Coinbase book the adapter has applied.
        if self.composite_bus is not None and not self._down_coins:
            self._reprice(now_s, compute_delta=False)
        # Always act: with no fair, _act pulls this market's quotes.
        self._act(ctx, ticker)

    def _record_lip_snapshot(self, ctx, now_s: float):
        """Score the current book + our resting quotes against the
        LIP rules and accrue what we would earn this snapshot."""
        es = self.event_state
        for coin in COINS:
            ticker = es.tickers[coin]
            params = es.lip_params.get(coin)
            if params is None or params.period_reward_usd <= 0:
                continue
            book = ctx.book(ticker)
            if book is None or getattr(book, "ladder", None) is None:
                continue
            yes_levels = self._lip_levels_yes(book, ticker)
            no_levels = self._lip_levels_no(book, ticker)
            score = score_book(yes_levels, no_levels, params)
            self._lip_snapshots_total[ticker] += 1
            if score.qualifies:
                self._lip_snapshots_qualified[ticker] += 1
                self._lip_accrued_usd[ticker] += score.expected_reward_this_snapshot_usd

    def _lip_levels_yes(self, book, ticker: str) -> list[LipLevel]:
        """YES bids, sorted highest-first. Pulls levels off book.ladder
        (an L2Book) plus our own resting yes quote if present."""
        ladder = book.ladder
        acc: dict[float, LipLevel] = {}
        for price, size in sorted(ladder.yes_bids.items(), key=lambda kv: -kv[0]):
            acc[price] = LipLevel(price=price, size=size, mine=0.0)
        q = self._quotes.get((ticker, "yes"))
        if q is not None:
            p = q["price"]; sz = q["size"]
            if p in acc:
                acc[p] = LipLevel(price=p, size=acc[p].size, mine=acc[p].mine + sz)
            else:
                acc[p] = LipLevel(price=p, size=sz, mine=sz)
        return sorted(acc.values(), key=lambda lv: -lv.price)

    def _lip_levels_no(self, book, ticker: str) -> list[LipLevel]:
        """NO bids, sorted highest-first (= yes asks sorted lowest-
        first, converted to no-side price). `_quotes[(ticker, 'no')]`
        is already in no-side price terms."""
        ladder = book.ladder
        acc: dict[float, LipLevel] = {}
        for price, size in sorted(ladder.no_bids.items(), key=lambda kv: -kv[0]):
            acc[price] = LipLevel(price=price, size=size, mine=0.0)
        q = self._quotes.get((ticker, "no"))
        if q is not None:
            p = q["price"]; sz = q["size"]
            if p in acc:
                acc[p] = LipLevel(price=p, size=acc[p].size, mine=acc[p].mine + sz)
            else:
                acc[p] = LipLevel(price=p, size=sz, mine=sz)
        return sorted(acc.values(), key=lambda lv: -lv.price)

    @staticmethod
    def _queue_position(levels: list[LipLevel], candidate_price: float
                        ) -> tuple[float, float]:
        """(ticks_from_best, queue_ahead) for a resting order at
        `candidate_price` on a side whose `levels` are sorted highest-price
        first (best bid first). `queue_ahead` is the size already resting at
        the candidate price that is NOT ours (we join the back of it);
        `ticks_from_best` is how many ticks inside the best price we'd sit
        (0 at the touch). Both feed the fill-intensity queue adjustment g()."""
        if not levels:
            return 0.0, 0.0
        best = levels[0].price
        ticks_from_best = max(0.0, round((best - candidate_price) / TICK))
        queue_ahead = 0.0
        for lv in levels:
            if abs(lv.price - candidate_price) < 1e-9:
                queue_ahead = max(0.0, lv.size - lv.mine)
                break
        return float(ticks_from_best), float(queue_ahead)

    # ------------------------------------------------------------------
    # Quote-fill bookkeeping (identical pattern to SPX quoter)
    # ------------------------------------------------------------------

    def _sync_quote_fills(self, ctx):
        fills = ctx.portfolio.fills
        if len(fills) < self._fills_seen:
            self._fills_seen = 0
        if len(fills) == self._fills_seen:
            return
        by_order = {q["order_id"]: key for key, q in self._quotes.items()}
        for fill in fills[self._fills_seen:]:
            key = by_order.pop(fill.order_id, None)
            if key is None:
                continue
            self._quotes.pop(key, None)
            ctx.cancel(fill.order_id)
            self._quote_block_until[key] = (fill.time
                                            + pd.Timedelta(seconds=self.config.requote_delay_s))
        self._fills_seen = len(fills)

    def _inflight(self, ticker: str, side: str, now: pd.Timestamp) -> float:
        key = (ticker, side)
        recent = self._sent.get(key)
        if not recent:
            return 0.0
        cutoff = now - pd.Timedelta(seconds=self.config.inflight_timeout_s)
        recent = [(t, s) for t, s in recent if t > cutoff]
        self._sent[key] = recent
        return sum(s for _, s in recent)

    def _resting(self, ticker: str, side: str) -> float:
        q = self._quotes.get((ticker, side))
        return 0.0 if q is None else q["size"]

    # ------------------------------------------------------------------
    # Decide: one ticker, one wakeup
    # ------------------------------------------------------------------

    def _act(self, ctx, ticker: str):
        cfg = self.config
        es = self.event_state
        fr = self._last_fair_result
        coin = coin_from_market_ticker(ticker)
        if coin is None:
            return
        if fr is None or self.theo_stale():
            # No trustworthy fair (withheld, feed behind, or still inside the
            # fresh-recovery hold): pull this market's resting quotes and
            # don't take. Previously a withheld fair returned BEFORE this,
            # leaving the last quotes resting on a theo we had just declared
            # unusable.
            now = ctx.time
            self._manage_quote(ctx, ticker, "yes", None, now)
            self._manage_quote(ctx, ticker, "no", None, now)
            return
        coin_i = COINS.index(coin)

        fair = float(fr.probs[coin_i])
        # Clip for quoting only (section 4.5).
        fair_q = min(max(fair, cfg.quote_clip_eps), 1.0 - cfg.quote_clip_eps)
        theo_vol = float(fr.theo_vol[coin_i])
        tau = fr.tau
        pos = ctx.position(ticker)
        now = ctx.time
        now_s = now.timestamp() if hasattr(now, "timestamp") else float(now)

        pos_net = pos.yes_qty - pos.no_qty
        inflight_yes = self._inflight(ticker, "yes", now)
        inflight_no = self._inflight(ticker, "no", now)
        resting_yes = self._resting(ticker, "yes")
        resting_no = self._resting(ticker, "no")
        eff_yes = pos_net + inflight_yes + resting_yes
        eff_no = pos_net - inflight_no - resting_no

        book = ctx.book(ticker)
        market_bid = book.bid if book is not None else None
        market_ask = book.ask if book is not None else None

        # Quotes and takes are priced on edge alone. LIP is still measured
        # (the accrual shown on the dashboard) but credits no edge: the
        # credit leaned on a fill-intensity model that wasn't trustworthy.
        lip_bid = 0.0
        lip_ask = 0.0
        decomp = decompose_edge(
            fair=fair_q, theo_vol=theo_vol,
            net_position=pos_net, eff_yes=eff_yes, eff_no=eff_no,
            max_pos=cfg.max_position,
            market_bid=market_bid, market_ask=market_ask,
            lip_credit_bid=lip_bid, lip_credit_ask=lip_ask,
            config=cfg,
        )

        # Phase policy (section 6.4).
        phase_pull_bid, phase_pull_ask = self._last_phase_pulls(coin_i, tau, fair_q)

        # Take decisions (section 6.2 crossing test).
        atm_ok = True   # Coin Race always has 5 markets; no ATM gating.
        cap_allows_yes = eff_yes < cfg.max_position - 1e-9
        cap_allows_no = eff_no > -cfg.max_position + 1e-9

        take_bid_price = None
        take_ask_price = None
        # A take must be justified by a fair that has HELD: the signal has to
        # have crossed continuously for take_confirm_s, so a one-tick jump
        # (a single coin updating ahead of the rest) can't trigger it. And at
        # most one take per side per confirm window -- on 2026-10-08 four
        # takes fired within 20ms because each saw the cap free in turn.
        key_b, key_a = (ticker, "take_yes"), (ticker, "take_no")
        want_b = (atm_ok and market_ask is not None and decomp.bid_reservation >= market_ask
                  and cap_allows_yes and not phase_pull_bid)
        want_a = (atm_ok and market_bid is not None and decomp.ask_reservation <= market_bid
                  and cap_allows_no and not phase_pull_ask)
        if want_b and self._take_ok(key_b, now_s):
            take_bid_price = round(market_ask, 2)
        if want_a and self._take_ok(key_a, now_s):
            take_ask_price = round(1.0 - market_bid, 2)
        if not want_b:
            self._take_since.pop(key_b, None)
        if not want_a:
            self._take_since.pop(key_a, None)

        if take_bid_price is not None:
            ctx.buy(ticker=ticker, side="yes", size=cfg.quote_size,
                    price=take_bid_price, order_type="limit")
            self._sent.setdefault((ticker, "yes"), []).append((now, cfg.quote_size))
        if take_ask_price is not None:
            ctx.buy(ticker=ticker, side="no", size=cfg.quote_size,
                    price=take_ask_price, order_type="limit")
            self._sent.setdefault((ticker, "no"), []).append((now, cfg.quote_size))

        # Quoting: build the resting-quote reservation from the MAKER edge
        # (no taker fee -- a resting quote pays none on this series).
        # Section 6.2 "quoting_edge replaces constant_edge": swap the
        # adverse-buffer term for quoting_edge, re-flooring against it.
        if cfg.enable_quoting and atm_ok:
            q_edge = cfg.quoting_edge
            # Pre-LIP, pre-floor maker edge excluding the adverse buffer:
            # (maker_bid_edge_total was floored at constant_edge, so strip
            # it back out and re-add quoting_edge as the new floor).
            quote_bid_edge = max(
                (decomp.maker_bid_edge_total - decomp.constant_edge) + q_edge, q_edge)
            quote_ask_edge = max(
                (decomp.maker_ask_edge_total - decomp.constant_edge) + q_edge, q_edge)
            quote_bid_res = fair_q - quote_bid_edge
            quote_ask_res = fair_q + quote_ask_edge
        else:
            quote_bid_res = None
            quote_ask_res = None

        # Resting quotes: the best price the edge allows -- the first tick at
        # or inside the maker reservation, capped one tick inside the
        # opposite touch and at most max_improve_ticks better than our own
        # side's best. No fill model, no LIP: see HANDOFF.md "Quote pricing".
        desired_yes = None
        desired_no = None
        self._level_dbg = {}
        if (quote_bid_res is not None and not phase_pull_bid
                and cap_allows_yes_quote(eff_yes, cfg.quote_size, cfg.max_position)):
            desired_yes = desired_quote_level(
                "yes", quote_bid_res, market_bid, market_ask,
                allowed=True, max_improve_ticks=cfg.max_improve_ticks)
        if (quote_ask_res is not None and not phase_pull_ask
                and cap_allows_no_quote(eff_no, cfg.quote_size, cfg.max_position)):
            desired_no = desired_quote_level(
                "no", quote_ask_res, market_bid, market_ask,
                allowed=True, max_improve_ticks=cfg.max_improve_ticks)

        self._manage_quote(ctx, ticker, "yes", desired_yes, now)
        self._manage_quote(ctx, ticker, "no", desired_no, now)

        if self.record_debug:
            self._log(ticker, coin, fair, fair_q, decomp, pos, pos_net,
                      eff_yes, eff_no, market_bid, market_ask,
                      take_bid_price, take_ask_price,
                      desired_yes, desired_no,
                      theo_vol, tau, phase_pull_bid, phase_pull_ask,
                      lip_bid, lip_ask, now)

    def _last_phase_pulls(self, coin_i: int, tau: float, fair_q: float) -> tuple[bool, bool]:
        """Section 6.4: during the last N seconds (default 120-180),
        pull a leg whose theo_vol exceeds a threshold; optionally keep
        only 1c OTM YES bids on legs with fair < 0.5%.

        Returns (pull_bid, pull_ask) booleans. True = this side must
        not be quoted / taken."""
        cfg = self.config
        if tau > cfg.last_phase_s or self._last_fair_result is None:
            return False, False
        tv = self._last_fair_result.theo_vol[coin_i]
        if tv > cfg.last_phase_theo_vol_pull:
            return True, True
        # Pull both-sides on legs whose fair isn't tiny -- keeps only
        # safe far-OTM YES bids as LIP presence.
        if fair_q > cfg.last_phase_otm_fair_max and fair_q < (1.0 - cfg.last_phase_otm_fair_max):
            return True, True
        return False, False

    def _manage_quote(self, ctx, ticker: str, side: str,
                      desired: float | None, now: pd.Timestamp):
        """Cancel-replace a single resting quote on one (ticker, side).
        `desired` is in SIDE'S own price terms."""
        cfg = self.config
        key = (ticker, side)
        blocked_until = self._quote_block_until.get(key)
        if blocked_until is not None and now < blocked_until:
            desired = None
        live = self._quotes.get(key)
        improve = cfg.requote_improve_ticks * TICK
        if live is not None:
            if desired is None:
                stale = True
            elif live["price"] > desired + 1e-9:
                stale = True
            else:
                stale = (desired - live["price"]) >= improve - 1e-9
            if stale:
                ctx.cancel(live["order_id"])
                self._quotes.pop(key, None)
                live = None
        if live is None and desired is not None:
            order = ctx.buy(ticker=ticker, side=side, size=cfg.quote_size,
                            price=desired, order_type="limit", intent="quote")
            self._quotes[key] = {"order_id": order.order_id,
                                 "price": desired, "size": cfg.quote_size}

    def _log(self, ticker, coin, fair, fair_q, decomp, pos, pos_net,
             eff_yes, eff_no, market_bid, market_ask,
             take_bid_price, take_ask_price,
             desired_yes, desired_no,
             theo_vol, tau, phase_pull_bid, phase_pull_ask,
             lip_bid, lip_ask, now):
        row = {
            "time": now,
            "ticker": ticker,
            "coin": coin,
            # SPX-compat column names so viz/charts.py's live_chart.py
            # doesn't need a parallel data path. Yes-side pricing.
            "prob": fair_q,
            "prob_lo": max(fair_q - decomp.ci_edge_bid, 0.0),
            "prob_hi": min(fair_q + decomp.ci_edge_ask, 1.0),
            "ci_half": max(decomp.ci_edge_bid, decomp.ci_edge_ask),
            "ci_edge": decomp.ci_edge_bid, "ci_edge_ask": decomp.ci_edge_ask,
            "constant_edge": decomp.constant_edge,
            "bid_edge": decomp.bid_edge_total, "ask_edge": decomp.ask_edge_total,
            "lean_bid": decomp.lean_bid, "lean_ask": decomp.lean_ask,
            "taker_fee_bid": decomp.fee_bid, "taker_fee_ask": decomp.fee_ask,
            "net_position": pos_net, "yes_qty": pos.yes_qty, "no_qty": pos.no_qty,
            "eff_yes": eff_yes, "eff_no": eff_no, "ratio": 0.0,
            "lean_dollars": self.config.lean_dollars,
            "event_kind": "composite_tick",
            "event_ticker": ticker,
            "market_bid": market_bid, "market_ask": market_ask,
            "bid_reservation": decomp.bid_reservation,
            "ask_reservation": decomp.ask_reservation,
            "bid_final": take_bid_price, "ask_final": take_ask_price,
            "inflight_yes": self._inflight(ticker, "yes", now),
            "inflight_no": self._inflight(ticker, "no", now),
            "resting_yes": self._resting(ticker, "yes"),
            "resting_no": self._resting(ticker, "no"),
            "cap_allows_yes": True, "cap_allows_no": True,
            "atm_tradeable": True, "max_markets": None,
            "maker_fee_bid": 0.0, "maker_fee_ask": 0.0,
            "quoting_edge": self.config.quoting_edge,
            "quote_bid_reservation": decomp.maker_bid_reservation,
            "quote_ask_reservation": decomp.maker_ask_reservation,
            "quote_bid": desired_yes,
            "quote_ask": None if desired_no is None else round(1.0 - desired_no, 2),
            # Crypto-strategy-specific fields:
            "fair_unclipped": fair,
            "theo_vol": theo_vol, "tau": tau,
            "lip_credit_bid": lip_bid, "lip_credit_ask": lip_ask,
            "phase_pull_bid": phase_pull_bid, "phase_pull_ask": phase_pull_ask,
            "lip_accrued_usd": self._lip_accrued_usd.get(ticker, 0.0),
            "lip_qualifying_fraction": (
                self._lip_snapshots_qualified[ticker] / self._lip_snapshots_total[ticker]
                if self._lip_snapshots_total[ticker] > 0 else 0.0),
            # Maker (quoting) reservation, YES terms -- what resting quotes
            # are actually bounded by. bid_reservation/ask_reservation above
            # are the TAKER reservation (fee included) used for crossing.
            "maker_bid_reservation": decomp.maker_bid_reservation,
            "maker_ask_reservation": decomp.maker_ask_reservation,
            # Level selection (EV per contract, LIP reference price, yes terms).
            "lvl_ev_bid": _lv(self, "yes", "ev"), "lvl_ev_ask": _lv(self, "no", "ev"),
            "lvl_lip_bid": _lv(self, "yes", "lip_usd"), "lvl_lip_ask": _lv(self, "no", "lip_usd"),
            "lip_ref_bid": _lv(self, "yes", "reference_price"),
            "lip_ref_ask": (None if _lv(self, "no", "reference_price") is None
                            else round(1.0 - _lv(self, "no", "reference_price"), 2)),
        }
        self.debug_log.append(row)
        # Mirror into the per-market log so the viz can slice by ticker
        # without re-filtering the whole debug_log.
        self._market_log[ticker].append(row)


# ---------------------------------------------------------------------------
# Small helpers used by _act. Defined at module level so tests can
# import them.
# ---------------------------------------------------------------------------

def _lv(strat, side: str, field: str):
    ch = getattr(strat, "_level_dbg", {}).get(side)
    return None if ch is None else getattr(ch, field)


def cap_allows_yes_quote(eff_yes: float, quote_size: float, max_pos: float) -> bool:
    return eff_yes + quote_size <= max_pos + 1e-9


def cap_allows_no_quote(eff_no: float, quote_size: float, max_pos: float) -> bool:
    return eff_no - quote_size >= -max_pos - 1e-9


__all__ = ["CoinRaceStrategy", "COMPOSITE_EVENT_KIND"]
