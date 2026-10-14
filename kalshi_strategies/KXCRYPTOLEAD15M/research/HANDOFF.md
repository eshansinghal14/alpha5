> **Historical (pre-2026-10-09 reorg).** Paths and run commands below refer to the
> removed `kalshi_crypto/` package. Current layout: `kalshi_strategies/KXCRYPTOLEAD15M/`;
> run with `python -m kalshi_strategies.run --series KXCRYPTOLEAD15M`. See `docs/live-trading.md`.

# kalshi_crypto — Handoff / State of Work

**Last updated:** 2026-10-14 (live-trading findings: §7 #5-#8)
**Series:** `KXCRYPTOLEAD15M` (Kalshi "Coin Race 15 minutes")
**Spec:** `OpenCode Build Prompt  Kalshi Coin Race LIP Market Maker.md` (this folder)
**Also read:** `README.md` (this folder) for the math + usage quick-start.

This file is a running log of what has been built, the design decisions
made, what is verified, and — importantly — the **open bug being
debugged right now**. Read the "OPEN ISSUES" section first if you are
picking this up live.

---

## 0. TL;DR status

- **All 11 spec modules built and importable.** 102 unit tests pass
  (`python -m pytest kalshi_crypto/tests/ -q`).
- **HAR realized-vol model fitted from real data** and wired into the
  pricer (`har_fit.json` + `HarCovarianceForecaster`).
- **Live paper runner works end-to-end** and writes viz output; the SPX
  `LiveEngine` is reused unmodified.
- **Single unified entrypoint**: `python kalshi_main/run_live.py --series KXCRYPTOLEAD15M`.
- **RESOLVED 2026-10-07:** the "no decisions / coins flap DOWN" bug. Root
  cause was **asyncio event-loop starvation**, NOT the feed-health state
  machine. `compute_cf_spot` ran the full depth-weighted CF rate on the
  ENTIRE Coinbase book (24k bids / 18k asks on BTC) on EVERY WS message
  (~50/s across coins) — ~235 ms/message, pinning the process at 100% CPU.
  The starved loop couldn't answer websocket pings (→ `keepalive ping
  timeout` reconnects → `is_resyncing` → DOWN) or run the snapshot loop
  (→ frozen `summary.json` showing stale DOWN with `last_reason:"fresh"`).
  Fixed; steady-state CPU is now ~2% and no coin flaps DOWN. See bug
  history §6 #9/#10.

---

## 1. What this strategy does

Market-makes all 5 markets of each `KXCRYPTOLEAD15M` event (one market
per coin: BTC, ETH, SOL, XRP, HYPE). Each event is a 15-minute window
`[S, T]`; the coin with the highest return over the window settles YES.

- **Primary objective:** earn Kalshi Liquidity Incentive Program (LIP)
  rewards by resting two-sided size.
- **Secondary objective:** avoid adverse selection via a real-time
  per-coin theoretical fair value ("theo") = P(coin i wins), a JOINT
  5-way probability.

The winner is a **joint** comparison, which drives a key design
constraint: you cannot price one coin without data for all five (see
OPEN ISSUES #1 for why this matters operationally).

---

## 2. Architecture / data flow

```
Coinbase level2 WS ──> CoinbaseAdapter ──> CompositeBus ──> CompositeFeed (per coin)
  (spot_feed/coinbase.py)                  (composite.py)      │
                                                               │ 1s CF-style composite bar
                                                               ▼
                                              SettlementTracker (settlement_tracker.py)
                                              + HarCovarianceForecaster.update()
                                                               │
Kalshi WS (5 markets) ──> LiveCryptoKalshiFeed ──> LiveEngine ──> CoinRaceStrategy.__call__(ctx)
  (live/kalshi_feed.py)                      (reused SPX engine)   (strategy.py)
                                                               │
                                   on each composite tick: on_composite_tick() ->
                                     feed-health gate -> pricer.fairs_at() -> _act() per coin
                                                               │
                                              quoter.py (edge only; LIP measured, not priced)
                                                               │
                                   debug_log + _market_log ──> live/persist.py ──> viz/static/*.json
                                                                                   data/live/.../*.parquet
```

Everything the strategy needs for a tick is in `EventState`
(`event_state.py`): the event window, the 5 market tickers, the
settlement tracker, the LIP params, and the cached `FairResult`.

---

## 3. Module-by-module

| File | Purpose |
|---|---|
| `config.py` | `CoinRaceConfig` dataclass (all tunables) + `EDITABLE_FIELDS` whitelist for dashboard overrides. Fee multiplier hardcoded to 1.0 (`CRYPTO_FEE_MULTIPLIER`) for this series only. |
| `series.py` | `SERIES_TICKER`, canonical `COINS` order, Coinbase product map, CF constituent table, `market_ticker()` helpers. |
| `spot_feed/base.py` | `VenueBook`, `VenueAdapter` protocol, `OrderBookLevel`. `receive_ts` is `time.monotonic()`. |
| `spot_feed/coinbase.py` | Phase-1 adapter: one WS, all 5 coins, `level2` channel. `max_size=8MiB` (BTC snapshot ~1MB). **Self-heals phantom crossed levels in `to_venue_book()`** (see bug history §6). |
| `spot_feed/health.py` | **Three-tier** `FeedHealthMonitor` (OK / DEGRADED / DOWN). `event_usable()` is the gate. Hard bound per-coin (HYPE 30s). |
| `cf_rate.py` | CF Spot Rate methodology replica. Tolerant of a crossed single-venue book (returns fallback mid, not None). |
| `composite.py` | `CompositeFeed` (per coin) + `CompositeBus` (all). 1s bars persisted to `data/kalshi_crypto/composite/{coin}_{date}.parquet`. `nowcast()` keeps last-good value. |
| `settlement_tracker.py` | `SettlementTracker`: start/end 60s TWAPs, projected returns, `replicated_winner()`, tie handling (`floor(100/n)` cents), 1e-5 rounding. Arithmetic returns (Kalshi's convention). |
| `cov_forecaster.py` | `CovarianceForecaster` protocol + `PlaceholderForecaster` + `ConstantCovForecaster` (fallback). Has `DEFAULT_ANNUAL_VOL` + `DEFAULT_CORRELATION`. |
| `har_forecaster.py` | **`HarCovarianceForecaster`** — the production forecaster. Loads `rv_modeling/har_fit.json`; falls back to fixed-point mode if absent. Rolling 60s/900s/3600s RV ring buffers. See §5. |
| `pricer.py` | `CoinRacePricer`: phase math (pre-open / forming-start / in-window / last-minute), Monte Carlo with common random numbers, Student-t fat tails (ν=5 default), delta matrix, theo_vol, shrinkage. `BaseRateTracker`. |
| `lip_scorer.py` | LIP scoring replica: Reference Price, DF^N weighting, snapshot exclusion, `marginal_reward_rate`, `period_payout`. |
| `quoter.py` | SPX-style edge decomposition (constant + CI-vol + lean + fee, floored; the LIP-credit term exists but the strategy passes 0 — see §6 #17), first-tick-inside-reservation quote level (`desired_quote_level`), 5-leg event constraints. `choose_quote_level` / `lip_credit_per_contract` are kept but UNUSED. |
| `event_state.py` | `EventState`, event discovery, Kalshi market lookup, LIP params fetch from `/incentive_programs`. |
| `strategy.py` | `CoinRaceStrategy` — the `strategy_fn(ctx)` plugged into both the backtest and the live `LiveEngine`. Implements `live_quote_order_ids` / `clear_quotes` / `drop_quote`. |
| `viz_shim.py` | Registers `KXCRYPTOLEAD15M` as a first-class series in `viz/data_loader.py` (exposes `QuotingConfig = CoinRaceConfig`, `strat`, `run_one`, `discover_runnable_events`). |
| `backtest/run_backtest.py` | Replay harness: composite bars + Kalshi book history → `external_events` into `kalshi_backtest.engine.Backtest`. Calibration report. |
| `live/run_live.py` | Live/paper runner. `session_loop()` rolls events; `run()` is one event. Paper (shadow) by default. |
| `live/kalshi_feed.py` | `LiveCryptoKalshiFeed` — KXCRYPTOLEAD15M-specific Kalshi WS (5 markets). |
| `live/persist.py` | Writes `summary.json`, 4 parquets, and per-market `viz/static/*.json` in the SPX-compatible schema. |

---

## 4. Entry points

```bash
# Tests (fast, ~2s, 60 tests)
python -m pytest kalshi_crypto/tests/ -q

# Live PAPER session (default — no real orders)
python kalshi_main/run_live.py --series KXCRYPTOLEAD15M

# Refit the HAR vol model from rv_modeling/data/
python kalshi_crypto/rv_modeling/fit_har.py

# Dashboard (separate process; reads what run_live writes)
streamlit run viz/app.py        # pick KXCRYPTOLEAD15M, Live mode

# Backtest one event (needs persisted composite bars)
python -c "from kalshi_crypto.backtest.run_backtest import run_one; print(run_one('KXCRYPTOLEAD15M-26OCT061445').lip_accrued_usd)"
```

`kalshi_main/run_live.py` is a thin dispatcher: `--series KXINX|KXINXU`
→ the SPX runner; `--series KXCRYPTOLEAD15M` → this one. Each strategy's
own `run_live.py` still works directly.

---

## 5. The realized-vol model (rv_modeling/)

**Model:** 3-term log-HAR on realized variance over 1m / 15m / 1h windows,
one coefficient set per horizon (60s / 300s / 900s), pooled across coins:

```
log σ²_h = a_h + b_1m·log(RV_1m) + b_15m·log(RV_15m) + b_1h·log(RV_1h)
Var(r_{t→t+h}) = h · σ²_h · coin_scale
```

**Fitted on 113,760 rows** (`rv_modeling/data/training_features_targets.parquet`,
22 days per coin / 8 for HYPE, 2025–26). Reproduces the notebook exactly:

| horizon | b_1m | b_15m | b_1h | sum |
|---|---|---|---|---|
| 60s | 0.234 | 0.359 | 0.288 | 0.880 |
| 300s | 0.165 | 0.270 | 0.459 | 0.893 |
| 900s | 0.093 | 0.302 | 0.489 | 0.884 |

**Per-coin calibration scale:** BTC 0.98, ETH 1.02, SOL 0.98, XRP 0.97,
HYPE 1.13 (HYPE's sticky 1s mid under-forecasts by ~13%).

**Fat tails:** standardized returns show excess kurtosis 3–5 → Student-t
ν≈5. `CoinRaceConfig.t_nu = 5.0` by default.

**Correlation:** notebook says it's low-impact; v1 uses the static
`DEFAULT_CORRELATION`. `set_correlation()` hook exists for a future
online estimator.

**Artifact:** `rv_modeling/har_fit.json` (checked in). Regenerate with
`fit_har.py`. If missing, `HarCovarianceForecaster` runs in fixed-point
fallback mode (pins intercept to operator-plausible annual vols).

**Files:** `fit_har.py` (fitter → json), `rvlib.py (RV model helpers).py`
+ `fetch_data.py (RV model data download).py` + the `.ipynb` (research;
note the two `.py` files have spaces/parens in their names — load via
`importlib.util.spec_from_file_location`, which `fit_har.py` already does).

Sanity check (reproduces notebook sample ETH @ iloc[50000]):
`{30s: 3.7bp, 60s: 5.2bp, 300s: 11.5bp, 900s: 20.1bp}`.

---

## 6. Bug history (fixes already landed — do not re-introduce)

1. **Coinbase 1009 MESSAGE_TOO_BIG.** BTC `level2` snapshot for 5
   products ~1.03MB > websockets default 1MiB. Fixed: `max_size=8MiB`
   on `websockets.connect`.

2. **Coinbase seq-gap false positives.** Original code tracked one global
   seq across products and nuked the socket on any gap. Fixed: per-connection
   seq (schema says per-connection), gaps logged + tolerated, reconnect
   only on timeout/hard error. `level2` self-heals via absolute sizes.

3. **Clock mismatch in feed health.** `VenueBook.receive_ts` is
   `time.monotonic()` but the health check was called with `now_s`
   (`time.time()` epoch) → ~1.7e9s "staleness" → every coin DOWN forever
   → strategy never ran `_act` → empty `quote_df` → viz "no decisions".
   Fixed: health observes on `time.monotonic()`; everything else stays
   wall-clock.

4. **Missing viz parquets.** `load_live_day` needs ALL of
   `summary.json`, `quote_df`, `fills_df`, `pnl_df`, `prob_grid`; a
   missing one raises `FileNotFoundError` → "no live session". Fixed:
   persist helpers always write empty frames with correct columns;
   heavy flush runs on cycle 1 (not only every 10th).

5. **`max_position` KeyError.** Viz reads `config.max_position`;
   `CoinRaceConfig` had `max_position_per_market`. Renamed to
   `max_position` project-wide (the per-market semantics are unchanged;
   SPX convention).

6. **Summary schema mismatch (`n_fills` KeyError).** Viz `_render_metrics`
   reads a flat SPX schema (`n_fills`, `total_realized_pnl`, `total_fees`,
   `max_abs_net_position`, `n_tickers_traded`, …). Rewrote
   `_build_summary` to emit the full superset; crypto extras ride along.

7. **Phantom crossed levels blanking the composite.** Coinbase `level2`
   sends ABSOLUTE sizes + explicit `new_quantity=0` removals. A dropped
   removal (seq gap) leaves a stale level on the wrong side of the touch
   that NEVER clears (absolute, not additive) → book crossed for minutes
   → `compute_cf_spot` returned None → coin went DOWN → event theo
   invalid. Confirmed BTC composite had 230–278s gaps while Coinbase
   itself sent 13 msg/s with 0.5s max gap. Fixed TWO places:
   - `cf_rate.compute_cf_spot`: a crossed single-venue book yields a
     fallback mid (0.5·(bid+ask)) instead of None.
   - `spot_feed/coinbase.py to_venue_book()`: prune crossing levels on
     read (walk both tops, drop the smaller crossing level). Raw dict
     keeps the level so a genuine later update still lands.
   - `spot_feed/health.py _classify`: a crossed-but-fresh book counts as
     usable (its mid is fine), matching the composite's tolerance.

8. **Three-tier feed health (replaced all-or-nothing gate).** The winner
   is joint, so a coin with NO usable data makes every fair unknowable —
   but a few-seconds-stale crypto mid is fine for a multi-minute ranking.
   Replaced the hard all-coins-fresh gate with:
   - **OK** (≤ soft_stale_s, default 3s): price normally.
   - **DEGRADED** (soft..hard): keep last price, keep pricing the event.
   - **DOWN** (> hard_stale_s, default 15s; HYPE 30s / no data): event
     theo invalid, pull all legs.
   `event_usable()` = no coin DOWN. A DEGRADED coin does NOT block.

9. **Event-loop starvation from full-book CF compute (the "no decisions /
   DOWN flap" bug).** `CompositeFeed.observe()` recomputed the full
   depth-weighted CF rate on EVERY Coinbase WS message, over the ENTIRE
   book. A real BTC `level2` snapshot is **24k bids / 18.4k asks**
   (measured 2026-10-07); `to_venue_book` (sorting the 42k-level dict) +
   `compute_cf_spot` cost **~235 ms per message** at ~50 msg/s across the
   5 coins — so the process needed multiple seconds of CPU per wall
   second and sat pinned at 100%. Downstream symptoms, all one cause:
   - websockets `keepalive ping timeout` (code 1011) on BOTH the Kalshi
     and Coinbase sockets — the starved loop couldn't answer pings — →
     reconnect → `is_resyncing=True` → coins classify DOWN.
   - `snapshot_loop` couldn't run → `summary.json` frozen for 20-50s,
     so the dashboard read a stale snapshot where a coin had blipped DOWN
     and never recovered (hence the paradox `last_reason:"fresh 219ms"`
     but `status:"down"`, `down_transitions:0`).
   The earlier conclusion "NOT our code, Coinbase is healthy" was right
   about the venue but missed that OUR per-message cost was the gap.
   Fixed in three places:
   - `spot_feed/coinbase.py to_venue_book()`: cap the emitted view at
     `MAX_BOOK_LEVELS=1000` per side via `heapq.nlargest/nsmallest`. The
     CF utilized-depth region is a few hundred levels either side of the
     touch (416/241 within 0.5% of spot on that BTC book), so 1000
     reproduces the full-book composite to 0.0 bp. Raw dict keeps the
     full book. ~235 ms → ~12 ms/message; steady-state CPU ~2%.
   - `cf_rate.py _curves`: vectorised the per-grid-point price lookup
     (`np.searchsorted` over the whole grid in one call) instead of a
     Python closure per grid point.

10. **Inverted CF utilized-depth criterion (latent, found while fixing
    #9).** `compute_cf_spot` chose the utilized depth with
    `mid_spread = 0.5*(bid(v) - ask(v)) <= threshold` — but for an
    uncrossed book that quantity is ≤ 0 at every depth, so the test was
    vacuously true and utilized depth ran to the whole thinner side. The
    exponential-weighted mid then averaged in levels far from the touch:
    on the real BTC book the composite came out **92963 vs the true BBO
    mid 85529** (an 8% error — it would have poisoned every fair). The CF
    methodology means `|mid(v) − spot| ≤ deviation·spot`; fixed to that.
    Regression test: `test_utilized_depth_stays_near_touch_with_deep_garbage`.

11. **Feed-health DOWN debounce (defence-in-depth for #9).** Even with
    the loop healthy, a single-tick blip — an empty venue list on one
    dispatch (race between the Coinbase callback and the sampling loop),
    a one-tick resync — would reset a warmed-up coin's recovery window
    and flap `event_usable`. Added a `down_debounce_s=2.0` hold: a coin
    that is currently not-DOWN must classify TRANSIENT-DOWN (absence of a
    usable book) continuously for the debounce before it actually
    transitions. A MEASURED hard-stale age is not debounced (the book
    really is that old), and cold start is not debounced. See
    `_observe_one` and `test_transient_down_blip_is_debounced`.

12. **Non-atomic composite flush left corrupt parquet (ArrowInvalid
    storm).** `CompositeFeed.flush()` did an in-place read-modify-write:
    read the day's file, concat the new bars, `to_parquet` back over the
    SAME path. A process killed mid-write (asyncio task cancelled on
    shutdown, Ctrl-C) left a half-written target whose parquet footer
    pointed past the real bytes — observed as a 25KB file claiming 827MB
    (`ETH_2026-10-07`, `HYPE_2026-10-07`). Every later flush then raised
    `ArrowInvalid` on the read and aborted, wedging the flush loop. Fixed
    in `composite.py flush()`:
    - ATOMIC write: serialise to `*.tmp-<pid>.parquet`, then `os.replace`
      onto the target (atomic on one filesystem). An interrupted write can
      only leave a stray `.tmp`, never a corrupt target.
    - RECOVER: if reading the existing file raises, quarantine it to
      `*.corrupt-<ts>.parquet` and start the day's file fresh instead of
      failing forever.
    - KEEP pending bars until the write succeeds (the old code cleared
      `_pending_disk` up front, so any exception dropped that cadence's
      bars). Tests: `test_composite_flush.py` (5).

13. **Theo far from the Kalshi mid (two causes).** Live fairs sat 13-19c
    off the market mid — e.g. BTC theo 0.17 vs market 0.035 — with the
    lowest-vol coins getting mid-range win probs (physically impossible in
    a highest-return race). Two independent root causes:
    - **No start TWAP on mid-window adoption.** The runner adopts an event
      already in progress, so `start_quality` was "missing", `start_locked`
      False, and the strategy fed the pricer `x = I = 0`. With `mu = 0` the
      fair collapses to a pure variance-ranking prior that ignores where
      each coin actually is. Fix: `run_live._backfill_tracker` seeds the
      SettlementTracker (and the forecaster's RV windows) from PERSISTED
      composite bars on adoption, so the start TWAP resolves and `x` is a
      real return. `SettlementTracker.backfill` already existed but was
      never called live. Test: `test_backfill_resolves_start_quality`.
    - **Inflated / disproportionate forecast variances.** The HAR RV ring
      counted (a) squared returns across DATA GAPS — a 263s session-gap on
      BTC booked a 9.2% "1-second" return, dragging whole-day RV to ~880%
      annualized vs ~40% within a session — and (b) HYPE's thin-composite
      BID-ASK BOUNCE (lag-1 autocorr -0.36) as real vol. The result was an
      ~800x variance ratio across coins, so HYPE won ~45% of MC draws and
      everyone else sat near the base rate regardless of price. Fix in
      `_RvRings`: skip a return whose previous price is >`MAX_RETURN_GAP_S`
      (2s) old (teleport, not diffusion), and apply a first-order
      Hansen-Lunde autocovariance correction `RV* = Σr² + 2Σr_t r_{t-1}`
      floored at 25% of raw. Brought the coins' vols into a plausible band
      (BTC ~40%, HYPE highest) and the theo to within a few cents of the
      mid. Tests: `test_return_across_time_gap_is_skipped`,
      `test_bounce_correction_shrinks_mean_reverting_series`.
    Still approximate: HYPE's composite remains intrinsically noisier
    (thin book) than the correction fully removes — its win prob is the
    least trustworthy. **Superseded by #14.**

14. **Vol rebuilt on the reference pipeline (2026-10-08).** Source:
    `rv_modeling/vol_fix_reference/` (README + code + golden cases; the PDF
    is the same report). Diagnosis confirmed on our own bars with its
    `diagnose_bars.py`:
    - The **CF replica was the broken input**. HYPE replica vs BBO mid:
      40 bp rms, 1 s signature 9.4×, corr vs BTC 0.20–0.26, r²/f 0.04.
      The raw BBO mid (already persisted as `fallback_mid`): signature
      0.89–1.01 on every coin, corr vs BTC 0.58–0.83, r²/f 0.88–1.26.
      The mid also picked the Kalshi winner on 6/7 settled events vs 4/7
      for the replica.
    - Changes: `CompositeBar.mid` / `CompositeBus.mid_nowcast` are the
      pricing series for x, the TWAPs and the forecaster (replica kept for
      diagnostics). New `robust_har.RobustHarForecaster`: 10 s returns at
      every offset clipped at 6× lagged sd, `har_coefs_robust.json` with
      log-h interpolation at h = time left, causal EWMA correlation of 60 s
      returns (4 h half-life), online scale monitor (clamped [0.6, 1.6],
      alarm → no pricing outside [0.4, 2.5]); no HYPE 1.13. Pricer:
      τ_eff = τ−40 / u³/10800, shrink OFF, **unit-variance** t5 (the old
      mixture inflated every sd ~29%), draws precomputed per covariance and
      scaled by √τ_eff (~1 ms/tick), bump deltas on the same draws. Strategy
      reprices the fair on every wakeup (no cache), deltas every 0.5 s,
      kill switch when the newest book is >1.5 s old, never prices in-window
      without a start TWAP. Also fixed: last-minute `I` was the partial
      AVERAGE instead of the partial SUM/60, so mu double-counted the
      observed end-window returns.
    - Verified: the online forecaster reproduces the reference features and
      rates to ~1e-13 and its fairs within ±0.005 (`test_vol_golden.py`).
      Logged tick (…0015, τ=275): **ETH 0.883 vs market 0.842** (was 0.39),
      HYPE 0.037 vs 0.041 (was 0.34), implied k 1.44, live scale 0.99–1.00.
      Replay of 11 settled events on 2026-10-08 (523 snapshots vs Kalshi
      1-min candles): log loss 1.291 ours vs 1.302 market; per-coin bias
      ≤ 2c. The last minute is still worse than the market (0.547 vs 0.426,
      n=31): ours goes to ~1.00 where the market holds ~0.90–0.94, and one
      event (…2345) was decided in the final seconds against the leader.
      One day, small n — not evidence of an edge, only of calibration.

15. **Quote level, viz bands, BTC false-leader takes (2026-10-08).**
    - **LIP reward is real**: `/incentive_programs` docs say
      `period_reward` is in centi-cents; every KXCRYPTOLEAD15M program
      (338 seen, active + paid out) is 200000 = **$20 per market per
      15-min period**, target 1000, DF 0.5, no per-account cap. $20/900 ≈
      $0.022/s split across everyone resting; at 1 contract against books
      of 500–6,500 our share is ~0.1%, hence the fractions of a cent.
      Size, not price, is what scales it.
    - **Level selection** (`quoter.choose_quote_level`): for each price
      from the best allowed down 12 ticks, EV = LIP $/s at that queue
      position × E[rest] + P(fill) × (fair − P − adverse edge); pick the
      lowest price within 2% of the best EV. With LIP dominant that is the
      LIP reference price. Replaces "first tick outside the reservation".
      **SUPERSEDED same day — see #17.**
    - **Viz**: the outer band was the TAKER reservation, which moves with
      the Kalshi touch (fee = ask·(1−ask)) and the LIP credit every tick,
      so it moved while theo was flat. Now the band is the MAKER
      reservation we actually quote from; taker reservation is a dashed
      line; LIP reference price is plotted. `quote_*_reservation` in the
      log now records the maker side.
    - **BTC bad trades** (…1245, 16:34:33, four 26c YES takes, fee 19c
      each): a 5 s Coinbase stall (no books for any coin 16:34:29–:32),
      then BTC's book resumed ~0.5 s before the other four. x compared a
      +0.18% BTC with four frozen coins, theo jumped 0.28 → 0.63 for
      ~0.3 s, the reservation crossed the 0.26 ask and four takes fired
      within 20 ms. A second later theo was 0.19; BTC market went to 0.04.
      Fixes: withhold the fair when the newest books across coins differ
      by > `max_cross_coin_skew_s` (1 s); a take needs the signal to hold
      `take_confirm_s` (0.75 s) and fires at most once per window. Replay
      of that second: fair withheld exactly at 16:34:33.

16. **Stale theo near expiry, quoting edge, real-trading cutoff
    (2026-10-08).**
    - Event …1300: runner at 98% CPU; no Coinbase books processed in 98 of
      the last 180 s (2–9 s freezes). At 110–100 s left theo said BTC
      0.35–0.44 while the market was at 0.68–0.72, catching up in one jump
      at 90 s. The kill switch missed it because a backlog processed
      after a stall was stamped "fresh" with time.monotonic().
    - Fixes: staleness now measured from Coinbase's message timestamp less
      a rolling-min clock offset (`CoinbaseAdapter._data_age`), so queued
      data reads as old; staleness keys on the OLDEST coin's newest book;
      tighter 0.75 s limit in the last 120 s; after any stale spell quotes
      stay off until the feed has been fresh for 2 s; a withheld fair now
      pulls quotes (it used to return before reaching the pull).
    - CPU: heavy flush (~4.7 s with a full event's log) moved off the event
      loop; order book on SortedDict with a 25-level per-message view and
      the CF replica once a second (~10 ms → ~3 ms/message).
    - `quoting_edge` = 0 for this strategy.
    - `real_trading_cutoff_min` (dashboard sidebar): disarms REAL orders X
      min before expiry (resting real orders cancelled); paper continues;
      a re-arm inside the window is ignored; next event re-arms. Startup
      log now prints whether real trading actually took effect.

17. **Quote pricing: edge only, no fill model, no LIP (2026-10-08).**
    - Symptom: live quotes sat a median 3 ticks (up to 7) behind the
      touch, below the LIP reference price (≈ the touch), so they neither
      filled nor earned LIP.
    - Cause: `choose_quote_level` scored fills as P(fill before tau) ×
      edge. Over 400 s+ the fitted `fill_intensity.json` gives P(fill)
      ≈ 1 even 2–4 ticks back (0.998 / 0.97 / 0.66 at 0 / 2 / 4 ticks), so
      the deepest price always won. A per-second rate version was tried
      and also rejected: the fill model's coarse price buckets are not
      trustworthy enough to rank adjacent ticks.
    - **Current rule** (`strategy._act`): rest at
      `desired_quote_level(maker reservation)` — the best tick at or
      inside `fair − (CI + lean + quoting_edge)` for the YES bid (mirror
      for the NO bid), capped one tick inside the opposite touch and at
      most `max_improve_ticks` better than our own side's best. No quote
      if that tick is outside [0.01, 0.99] or the cap/phase pulls block it.
    - **LIP**: `lip_credit_bid/ask = 0` in `decompose_edge` for both quotes
      and takes. LIP is still MEASURED (`_lip_accrued_usd`, dashboard
      "LIP accrued") but has no effect on price. The level-choice log
      columns (`lvl_*`, `lip_ref_*`) are now always empty.
    - Left in the code, unused, for when LIP is revisited:
      `quoter.choose_quote_level`, `quoter.lip_credit_per_contract`,
      `strategy._queue_position`, `fill_intensity.py` (+ its tests).
    - **OPEN — how to price LIP** (operator to decide): see §7 #4.

18. **Live order plumbing (2026-10-08; shared `kalshi_latency_arbs/live/`).**
    - **Exchange shards.** Crypto events trade on shard 2 (`exchange_index`
      on `/events/{ticker}`); collateral is per-shard. All $ on shard 0 →
      every order 404 `insufficient_shard_balance` → circuit breaker.
      Arming now reads the balance on the EVENT's shard and refuses at $0.
      Target allocation set to `{2: 100}`; manage with
      `python kalshi_latency_arbs/live/shard_allocation.py [0=40 2=60 | --off]`.
      **Before running SPX live** set a split that funds shard 0 too.
    - **Quote fills booked.** The crypto runner had no `quote_fill_loop` /
      `position_audit_loop` (SPX runner did), so resting-quote fills never
      reached the ledger. Added both (1 s / 5 s), plus a last fill poll at
      shutdown.
    - **Orders stacking in the book.** A cancel issued while the quote's
      POST was still in flight had no exchange id and did nothing; the
      order landed untracked and every requote added another. Now: at most
      one real order per (ticker, side) — a new quote is refused
      (`prior_quote_not_cleared`) until the previous one is confirmed
      cancelled; an order that lands after being cancelled (or after
      disarm) is cancelled on arrival; untracked cancels carry the ticker so
      they route to shard 2.
    - **Stragglers after disarm/shutdown.** `cancel_resting` now repeats
      list-and-cancel every 0.5 s until two empty passes (cap 15 s, > the
      5 s order timeout), warning if it gives up. Shutdown pulls quotes
      and sweeps the event. Account-wide cancel-all deliberately NOT used:
      it would also kill manual orders.

19. **Rollover latency (2026-10-08).** ~90 s from event open to the viz
    showing it: 60 s post-close grace + up to 30 s rollover poll + a first
    heavy flush that was always empty (next one 30 s later). Now: 1 s
    grace, 2 s rollover poll for 2 min after an event ends, events past
    `close_time` skipped, heavy flush every cycle until the first non-empty
    one. Expected ~8–10 s. Still open: in-event chart updates every 30 s
    (heavy flush cadence); faster needs incremental stream writes.

---

## 7. OPEN ISSUES (what to debug next)

### #1 — Coins flap into DOWN, blocking theo (RESOLVED 2026-10-07)

Root cause was event-loop starvation, not the feed-health state machine.
See bug history §6 #9/#10/#11. Verified fixed: a 20s live probe shows all
5 coins steady OK after the recovery window with zero flaps, steady-state
CPU ~2%, `summary.json` updating, `fairs` SET, `down=[]`.

The notes below are kept for the record — the "where to look" turned out
to be a red herring (the state machine was fine; it was starved of
dispatch ticks, so `not_down_since` never got the chance to age past
`recovery_s`).

<details><summary>Original (pre-fix) investigation notes</summary>

**Where to look:**
- `spot_feed/health.py` `_observe_one` / `status` — the `not_down_since`
  reset + recovery-window interaction. Consider: debounce DOWN (require
  N consecutive DOWN ticks before actually transitioning), OR track
  cumulative not-DOWN time instead of a single `not_down_since` anchor,
  OR lower `recovery_s`.
- WHY does `_classify` return DOWN on some ticks for a coin with fresh
  books? Candidates: (a) `venue_books_per_coin()` returns an empty list
  for that coin on some ticks (CompositeFeed has no venue entry yet /
  race between the Coinbase callback and the dispatch loop); (b) the
  book is briefly `is_resyncing=True` after a reconnect and that state
  lingers; (c) `is_one_sided()` momentarily true mid-update.

**Debug tooling that was being built:** a probe
(`C:\Users\eshan\AppData\Local\Temp\opencode\probe_health.py`) that runs
the REAL `CoinbaseAdapter` + `CompositeBus` + `FeedHealthMonitor` for
~12s and prints each coin's per-tick `status/age/flags(R=resync,
1=one-sided, X=crossed)`. **It hung once** (asyncio task + sampling loop
not cleanly bounded) — fix the probe's shutdown or just add a print
inside `_classify` / `_observe_one` and run a short live session.
**Recommended approach: add temporary logging to `_observe_one` (coin,
tier, len(venues), vb flags) rather than more timed live runs.**

**Fastest repro path:** unit-test the flap. Feed `FeedHealthMonitor`
a realistic alternating sequence (fresh book, then empty list, then
fresh book, …) at 0.5s dispatch spacing and assert `status` eventually
reaches OK. That will pin the state-machine bug in <1s without any live
process.

</details>

### #2 — Start-TWAP "missing" when adopting an event mid-window

If the runner starts after `[S−60, S)`, there are no composite bars for
the start TWAP → `start_quality: "missing"` → no settlement anchor →
`start_locked=False` for that event. The pricer still runs (pre-open
phase math), so this doesn't block theo, but that event can't
settlement-replicate. Consider a startup guard that warns / skips to the
next event when adopted mid-window.

### #3 — Uncalibrated constants (expected, documented)

Not bugs, but flagged for when live data accrues:
- `lip_period_reward_divisor` — Kalshi API `period_reward` unit is
  unverified (assumed centi-cents → $20). Reconcile against a real payout.
- **`fill_intensity` — now a real model, but UNCALIBRATED.** The LIP
  credit is `reward_rate_per_s * E[rest]` with
  `E[rest] = (1 - e^{-lambda*tau})/lambda` and
  `lambda = lambda_side(tau,p) * g(ticks_from_best, queue_ahead)` (see
  `fill_intensity.py`). `rv_modeling/fit_fill_intensity.py` fits
  `lambda_side` from `data/kalshi_ws/{SERIES}/trades` (bucketed filled
  `count` by `taker_book_side` × tau × price ÷ seconds ÷ markets). As of
  2026-10-07 there is NO KXCRYPTOLEAD15M trade capture and the only
  KXBTC15M capture is a single deep-ITM event — unrepresentative — so the
  artifact is deliberately absent and the model runs its fixed-rate
  fallback (`FALLBACK_LAMBDA_PER_S = 0.02/s`, operator-plausible). This is
  already far better than the old full-period upper bound (E[rest] ~50s vs
  900s), but refit once real KXCRYPTOLEAD15M trades + raw_ob are captured:
  `python kalshi_crypto/rv_modeling/fit_fill_intensity.py`. The queue
  adjustment `g` (depth_decay, queue_half) is still operator-plausible —
  the virtual-order OB study (simulate a resting order vs real book+trades
  to learn fill prob vs ticks-from-best / queue-ahead) is the next step.
  Final step: switch to OWN fills once trading, and refit lambda on actual
  fills + kappa on post-fill theo moves at 1s/5s/30s.
- `ci_vol_multiplier`, `constant_edge`, lean params — operator-chosen;
  tune against markouts.

### #4 — LIP is not priced into quotes (deliberate, 2026-10-08)

Quotes are on edge alone (§6 #17). LIP is the strategy's stated primary
objective, so this needs a decision. Facts to start from:
- $20 per market per 15-min period, target 1000, DF 0.5 (§6 #15); at 1
  contract vs 500–6,500 resting our share is ~0.1% — size, not price,
  is what scales LIP.
- The LIP reference price sat at the touch in every logged market, so a
  quote off the touch earns ~0 LIP regardless of how it is priced.
- `fill_intensity.json` is not trusted for pricing (§6 #17); anything
  that needs E[rest] or P(fill) depends on fixing it first.
- Hooks already in place: `decompose_edge(lip_credit_bid/ask=...)`,
  `lip_scorer.marginal_reward_rate`, `score_book(...).reference_price`.

### #5 — LIP quote is picked off on big moves; depth-ahead rule doesn't protect (2026-10-12)

Backtest: 57 events 2026-10-12 02:30-16:30 UTC, Predexon ladders (~0.5s),
Kalshi prints for fills, live decision journal for the edge, live
`choose_lip_level` (20 lots, prop_max 0.7). Script and per-fill output:
`%TEMP%/opencode/lip_bt.py`, `lip_live_fills.csv` (not in the repo).

| depth rule | reward | fills | 10s markout | settled |
|---|---|---|---|---|
| none | $56 | 189 | -$190 | -$53 |
| live (>=150 ahead) | $55 | 161 | -$183 | -$55 |
| >=500 ahead | $17 | 25 | -$2 | +$27 |
| >=150 excl. largest level | $6 | 31 | -$36 | +$14 |

- Depth ahead was almost always ~240 = two 120-lot levels (`[120, 120, 3]`),
  a ladder repeated every few ticks, likely one maker. In 131/161 fills >75%
  of it vanished without trading; in 73 it all left within one ~0.5s
  snapshot, median 0.00s before our fill. They pull, the sweep hits us.
- Concentration and level counts don't separate it: one maker spans levels.
- Fills cluster across coins in the same instant (one coin move reprices the
  event); worst stretch 1-3 min before close.
- Sim is optimistic (0.1s cancel on every snapshot vs live's 1s queue poll).
- Not yet tried: pull LIP on a coin jump, no LIP in the last 3 min, deeper
  (lower prop_max) or smaller lots.
- $1 payout floor: on 2026-10-12/13 our edge quotes alone accrued a median
  $0.02 per market-period (max $0.07; 0 of 485 reached $1). One order held
  the whole period at the best bid on BOTH sides needs ~50 lots to clear $1
  in ~90% of periods (20 lots: 3%); one side alone needs ~100. LIP quoting
  is off (`lip.enable_lip_quoting` false) until adverse fills are solved.

### #6 — Live edge, adverse fills and the last minute (2026-10-11..14)

Everything below is maker-only unless stated: taking lost money on every
window tested and is off (`quoting.enable_taking` false, dashboard checkbox).

**Taking.** 985 real taker fills (2026-10-12): 10s markout -3.1c/contract
(t -5), settled -$49. More fair-vs-touch edge was WORSE (>15c edge: -5.7c
at 10s, -13.5c settled): a big disagreement is usually our fair being wrong.
Raising the theo_vol horizon only cut take volume; per-take quality didn't
improve at any horizon, constant-edge-matched or not.

**Where maker fills go wrong.** Spread captured at the fill is ~5.7c; ~80%
of it is gone within 1s (we're filled by the move that's repricing through
the quote). Adverse fills are predicted by the race moving AFTER the fill
(|dz| >= 0.5 within 10s: 75-88% adverse) and by theo_vol at the fill, not by
the coin's distance from first or by time of day. The last 1-3 min is the
worst stretch in every cut (sim, live, LIP).

**theo_vol.** `pricing.theo_vol_reval_draws` (32, live): full revaluation,
E|fair change| over joint coin moves for `reaction_latency_s`, scaled to
1-sigma-equivalent. Also fixes the missing tau/60 factor in the last minute
(the old slope x 1-sigma read 60/tau too high there: 2x at 30s). Live it
reads ~1.3x lower >3 min out, ~2.4x lower 1-3 min out, ~6x lower <60s than
the old calc; the 1-3 min gap is larger than the smoke test predicted and
is not explained yet (suspect MC noise inflating the old slope^2). Neither
calc prices a race flip: 26OCT131200 lost $7.38 when SOL moved ~8bp in 2s
(~6 model sigma) at 35-47s left and swept 5.5 lots of ETH-yes/SOL-no; no
model-vol draw puts weight on that move.

**Edge fit** (965 maker fills, old theo_vol, 2s horizon): A = edge - markout
= a + b*theo_vol per time bucket. >3 min: edge_fraction ~0.63 (90% CI
0.17-1.02); 1-3 min and <60s: b ~ 0, a ~13c and ~17c (flat risk, not
theo_vol). R^2 ~0.01 and the halves disagree; partly circular (we're only
filled where our own rule put the quote).

**Live settings history.** 2s horizon + edge_fraction 0.5 + constant 3.5c
(10-12 21:23) ; new theo_vol (10-13 18:51) ; edge_fraction 0.7 (20:20) ;
0.8 + 60s cutoff (23:15) ; max_signal_age 0.1 (10-14 ~05:50). Per period:

| period | events | lots/ev | 10s c/lot | PnL |
|---|---|---|---|---|
| old theo_vol, 0.5 | 82 | 12.9 | +1.3 | +$20.80 |
| new theo_vol, 0.5 | 7 | 20.4 | -0.4 | +$8.56 |
| new theo_vol, 0.7 | 12 | 19.0 | +0.8 | -$0.81 |
| new theo_vol, 0.8, 60s cutoff | 22 | 19.3 | +1.5 | +$33.36 (t 2.6) |

Per-event noise in 10s markout is ~5.4c/lot: a 1c/lot difference between two
settings or sessions needs ~230 events each, a 2c difference ~60.

**Last minute.** Cutoff 60s (`trading_time.real_trading_cutoff_s`): 30-60s
was 8% of lots at ~break-even and holds the jump risk above. 1-3 min still
shows weak 1s markout; a flat edge add-on there is the next thing to try.

**Pulls.** spot_burst pulls (~15% of quote time) avoided flow that was worse
than our fills (-1.0c 10s, -2.9c settled per contract, upper-bound sim); keep
them. stale_signal pulls are ~0.5% of time and cost nothing measurable.
spot_burst is not logged as a block (shows as priced rows with 0 levels);
log it before relying on that inference.

**Signal age.** `signal_age_s` is now on every decision row (10-14). First
2h: p50 8ms, p99 51ms, 0.14% of decisions over 100ms. `fresh_recovery_s` was
removed (10-14): 83% of stale-blocked decisions were the 0.5s recovery hold
with the age already under the limit.

**Time of day.** No session is worse yet (maker 10s markout +0.4c Asia,
+1.0c Europe, +0.5c US, +1.1c late US, all +-0.6-1.0c), but settings changed
across those hours; needs ~a week on fixed settings. Faster check not yet
run: share of our fills that are part of a multi-level sweep, by session.

### #7 — Position-audit halts were a race, not bad bookkeeping (fixed 2026-10-14)

Four "ledger disagrees with Kalshi" halts (26OCT111900, 111945, 121245,
130915). Kalshi's /fills matched our journal execution for execution every
time; net positions per coin were identical. The audit read Kalshi's
positions while a burst of fills was landing: one side had them, the other
didn't, and a quote that filled fully no longer counted as resting, so the
tolerance didn't cover it. Fix (`engine.audit_positions`): compare against
our ledger before AND after the read, and halt only after AUDIT_STRIKES (2)
consecutive disagreements (5s apart). A real gap still halts, ~10s instead
of 5s.

**Tail-conditioned theo_vol (2026-10-14): no.** 2,121 maker fills, pricer
state rebuilt from composite mids (fair corr 0.98 with the log), repriced
along each market's steepest 2s move direction from -8 to +8 sigma.
E[|dfair| given |move| > c] / E|dfair|, relative to a linear fair, is
0.94-1.00 in every bucket incl. contested <60s (0% of contested fills over
1.5): over 2s the fair is ~linear even near expiry, so tail conditioning is
a constant x2.4 on theo_vol. As predictors of 10s adverse cost: R^2 0.091
(normal reval) vs 0.096 (tail c=3); logged theo_vol 0.048. One pooled fit
over all buckets: A10 = 4.1c + 1.5 x reval theo_vol (live is 3.5c + 1.2x);
it leaves >3 min ~0.8c over-charged (t -3..-4) and 1-3 min ~2c under (t
-0.2). Script: `%TEMP%/opencode/tail_tv.py`.

**Time-scaled constant edge:** `quoting.constant_edge_ref_s` (dashboard,
under constant_edge; empty = flat). Inside the last ref seconds,
constant_edge x sqrt(ref / time left): a fixed spot shock moves a contested
fair like 1/sqrt(time left), as theo_vol grows. One parameter, set from the
mechanism, not fit: 180 gives x1.22 at 120s, x1.41 at 90s, x1.73 at the 60s
cutoff (~+1c over 1-3 min at 3.5c, about half the fit's shortfall).
Evaluate on 10s markout in the 1-3 min window, ideally alternating events.

### #8 — Open

- Run `constant_edge_ref_s` 180 and judge on 1-3 min 10s markout (~60
  events/arm for a 2c gap); refit edge_fraction on the new theo_vol after a
  day on fixed settings. Next if needed: a jump floor (fair change from a
  fixed 3-5bp spot shock).
- The 2.4x live gap between theo_vol calcs 1-3 min out is likely different
  market conditions (the two agree within ~10% on identical inputs).
- Log spot_burst as a block; consider a shorter burst hold (2-5s pulls were
  slightly positive).
- Time-of-day and max_signal_age (50ms) once there's a week of data.
- LIP: needs ~50 lots/side for the $1 floor and a fix for sweep pickoffs.


---

## 8. Key design decisions (so you don't relitigate them)

- **Reuse the SPX `LiveEngine`** (`kalshi_latency_arbs/live/live_context.py`)
  unmodified. `CoinRaceStrategy` implements the exact `ctx` surface it
  expects. Shadow (paper) mode is the default; arming real trading is a
  deliberate dashboard action.
- **Fee multiplier hardcoded to 1.0** for this series only (API reports
  `fee_type: quadratic, fee_multiplier: 1`). Did NOT touch
  `kalshi_backtest.core.kalshi_fee`'s 0.07 default (that would reprice
  KXINX/KXINXU).
- **`sigma_feed` and `kappa` from the spec were dropped** per the user:
  the SPX template's `constant_edge` + `ci_vol_multiplier·theo_vol`
  cover the adverse-selection-buffer role.
- **Pricer works in LOG returns** (ranking-invariant, clean Brownian
  variance identities); `SettlementTracker` works in ARITHMETIC returns
  (Kalshi's convention). Nothing silently converts between them.
- **Monte Carlo with common random numbers** (pre-generated `Z`, reused
  every tick) so theo is smooth and doesn't jitter on sampling noise.
  Analytic backend exists in tests only.
- **Three-tier health** over all-or-nothing: see §6 #8.
- **Viz: minimal integration** — register the series, emit the same
  per-market JSON schema the SPX live chart already reads. The 5 markets
  appear in the dropdown. No new chart module.

---

## 9. Test inventory (96 total, all passing; adds test_vol_golden.py 9 and test_theo_freshness.py 5)

| File | Count | Covers |
|---|---|---|
| `test_pricer.py` | 8 | phase continuity @ tau=60, tau→0 convergence, symmetric-coins=0.2, monotonicity (x, own-vol, correlation), shrinkage sum-to-1, V=0 tie, MC vs analytic |
| `test_lip_scorer.py` | 7 | help-page $16 example, Reference Price mechanics, qualify/exclude, min-$1 payout, marginal reward |
| `test_settlement.py` | 5 | basic winner, tie split, partial start quality, 3-decimal rounding tie |
| `test_quoter.py` | 8 | quadratic fee, LIP floor at adverse buffer, tick rounding, cap-inside-touch, event Σ constraints, **maker edge excludes taker fee, LIP reduces maker edge** |
| `test_cf_rate.py` | 7 | single healthy venue, resync drop, fallback median, outlier drop, **crossed single-venue → fallback mid**, clean-beats-crossed, **utilized-depth stays near touch with deep garbage** |
| `test_har_forecaster.py` | 16 | fixed-point steady state, log-h interp, clipping, forecast_cov vs rate, ring buffer, large-shock reaction, subset update, correlation; +4 fitted-mode (notebook slopes, HYPE scale, plausible range, interp) |
| `test_feed_health.py` | 13 | OK/DEGRADED/DOWN tiers, recovery hold-off, HYPE longer bound, event_usable logic, crossed-book-usable, snapshot schema, **transient-DOWN debounce, persistent-absence-still-down, measured-hard-stale-not-debounced** |
| `test_coinbase_book.py` | 6 | clean passthrough, phantom-bid prune, phantom-ask prune, raw-dict retention, **depth cap keeps touch, custom max_levels** |
| `test_fill_intensity.py` | 6 | fallback fixed-rate, expected-rest-time limits, g() bounds/monotonicity, expected credit < upper bound, recovers upper bound as λ→0, zero-size |
| `test_composite_flush.py` | 5 | write+reload, append dedup (keep last), corrupt-file quarantine+recover, pending kept on write failure, no leftover tmp |

---

## 10. Git state

Nothing in `kalshi_crypto/` is committed yet (whole dir is untracked —
`?? kalshi_crypto/` in `git status`). Also untracked: `kalshi_main/`,
`viz/`, `kalshi_latency_arbs/`, `CLAUDE.md`, `docs/`. The `viz/data_loader.py`
edit (registering the series) is in the untracked `viz/` tree.

When ready to commit, the crypto work is self-contained in:
`kalshi_crypto/`, `kalshi_main/`, and the one edit to `viz/data_loader.py`
(SERIES tuple + `_RB` dict + shim path).
