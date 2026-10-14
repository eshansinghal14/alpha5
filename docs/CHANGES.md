# Change log

Reverse chronological. One section per material change. Entries before
2026-10-09 name paths that no longer exist (see the reorg entry).

## 2026-10-12..14 — Crypto live tuning, audit race, signal age

Findings and numbers: `kalshi_strategies/KXCRYPTOLEAD15M/research/HANDOFF.md` §7 #5-#8.

- `quoting.enable_taking` (dashboard checkbox): off = maker-only. Takes
  lost money on every window tested; crypto runs with it off.
- `quoting.constant_edge_ref_s` (all series, default empty = off):
  constant_edge x sqrt(ref / time left) in the last ref seconds before close.
- `CONFIRM_S` (0.1s, measured send -> booked fill) and `TAKE_HOLD_S` (0.5s)
  replace `inflight_timeout_s` / `take_confirm_s`. The lean is flat linear
  (`lean_dollars` only; `lean_multiplier` / `lean_exponent` removed);
  `fair_clip_eps` is hardcoded.
- Crypto `pricing.theo_vol_reval_draws`: theo_vol by full revaluation over
  joint coin moves (0 = the old slope x 1-sigma). Fixes the last minute's
  missing tau/60 factor, which read 60/tau too high.
- Position audit compares Kalshi against our ledger before AND after the
  read and halts only after 2 consecutive disagreements. The four
  "ledger disagrees" halts were fills racing the read; the books matched.
- `risk.fresh_recovery_s` removed: quotes return as soon as the signal age
  is back under `max_signal_age_s`. `signal_age_s` is logged on every
  decision row.
- Dashboard: strategy performance panel (PnL with 1s/10s markout by
  window, lots filled, drawdown); only `max_markets` markets are charted
  when it's set.
- Overnight safety: balance watchdog (portfolio floor) runs on EC2; the
  review agent's EC2 queries run under a 1 GiB memory cap
  (`deploy/review_py.sh`).

## 2026-10-13 — S&P folder renamed, KXINXDUD, shared pricing hub

- `kalshi_strategies/KXINX_U` -> `kalshi_strategies/KXINX`. It now also
  serves KXINXDUD (one "above prior close" market per day).
- `run --series` takes folder names and runs all their series;
  `--only` names single series. `--series KXINX` = KXINX + KXINXU +
  KXINXDUD.
- One pricing hub process (`KXINX/hub.py`) replaces three IB connections:
  union subscription, one curve fit per tick batch, fairs streamed per
  series. IB/hold settings moved from each series' `pricing` section to
  `config.json["hub"]`.
- Combined risk: `hub.combined_max_loss_usd` caps the worst-case
  settlement loss across all S&P positions; sides that would worsen it are
  blocked (`combined_risk`).
- `pricing.monotone_levels` (default off): monotone P(S>K) before
  differencing into buckets.
- `ib_functions` strike candidates snapped to the $5 grid (a 7808.69
  boundary asked IB for 7803/7808/7813).
- Write groups: the three S&P series run in one process on one write
  bucket, one `cancels_token` reserve and one quote priority queue. The
  per-series split (26.7 tokens each) sat below reserve + one create, so no
  S&P quote was ever placed with three series running.

## 2026-10-09 — Reorg: kalshi_strategies + generic runtime + kalshi_viz

**Layout.** `kalshi_crypto/`, `kalshi_latency_arbs/`, `kalshi_main/` and
`viz/` are removed. Pricing lives in `kalshi_strategies/<FOLDER>/`
(`KXINX` serves KXINX and KXINXU; `KXCRYPTOLEAD15M`); everything else is
shared in `kalshi_infra/` (`kalshi/`, `trading/`, `runtime/`). One launcher:
`python -m kalshi_strategies.run --series ...`. One dashboard:
`streamlit run kalshi_viz/app.py`. All code is packages imported from the
repo root (no `sys.path` inserts); loose `kalshi_infra` scripts now run as
`python -m kalshi_infra.<script>`. See [live-trading.md](live-trading.md)
and [adding-a-strategy.md](adding-a-strategy.md).

**Config.** `config_override.json` and CLI tuning flags are gone. Each
strategy folder has a tracked `config.json`, loaded strictly with no code
defaults, edited by the dashboard and hot-reloaded (live fields now,
`next_event` fields at rollover).

**From Kalshi instead of config:**
- Fees, per event (series fee type/multiplier + event overrides). Measured
  on this account: 216 KXINX/KXINXU taker fills → 0.07·p(1−p); 10
  KXCRYPTOLEAD15M maker fills → 0. **Crypto taker fee changed from 1.0 to
  0.07·p(1−p):** the old crypto code read Kalshi's "multiplier 1" as the
  rate itself, so it demanded ~14× the real fee before taking.
- LIP program terms (target, discount, reward in centi-cents, period
  length, per-account cap).
- Write-rate budget (`/account/limits`), split evenly across running series.

**One quoter for every series** (`trading/quoter.py`). Replay against the
old SPX strategy: identical orders and fills on 150k recorded decisions per
series and on full backtest days (KXINX/KXINXU-26AUG27H1600). Behaviour
choices: SPX lean formula for both; `take_confirm_s` and
`min_quote_life_s` per series (crypto 0.75 / 0, SPX 0 / 2.0).

**New: LIP-farming quote** (`lip.lip_lots`, `prop_max_lip`,
`lip_min_depth_ahead`), separate from the edge quote (`quoting.order_lots`,
renamed from `quote_size`/`lot_size`). See live-trading.md § Quoting.

**Removed:** the crypto last-phase pull rules, the 10s pre-close order
stop, the near-expiry signal-age limit, the ladder-wide bias halt, and the
fees / most close / most risk parameters. Shadow send latency (34ms
measured) and the 5-error breaker are now fixed constants.

**Rollback:** commit `0a82f3c` is the full pre-reorg tree.

## 2026-10-08 — Quote churn, IB scope, shadow fills

Measured on today's journals (KXINX/KXINXU-26OCT08H1600). Before: KXINX
~20–40 orders/s and KXINXU ~11–20/s, against a 5/s-per-series budget.
After: both ~5–7/s in a live shadow run (4 min each, side by side).

Causes and fixes:

- **Fair value moves ~0.3¢ per signal at ~11 signals/s,** and every move
  that crossed a tick replaced the quote. New
  `QuotingConfig.min_quote_life_s = 2.0`: a quote younger than that is only
  replaced if fair value crosses it outright. The journal replay with this
  setting gave 41 → 7 orders/s (KXINX) and 20 → 7/s (KXINXU).
- **Option spread blowouts went straight into pricing** (98/min, p50
  125ms), swinging B7737's band to [0.12, 0.53]. `LiveOptionBook` now holds
  through them the same way the backtest's `hold_through_wide_spread`
  does: 3-min trailing median, 3×, 300ms settle.
- **The 5th/6th ATM buckets swapped places constantly** (27 flips/min on
  KXINXU). New `QuotingConfig.atm_hysteresis = 2`: a member stays until it
  ranks below `max_markets + 2`.
- **KXINXU was subscribed to 15 buckets but traded only 5.** The IB scope
  is now a contiguous window: the tradeable set plus a margin
  (`SCOPE_MARGIN_BUCKETS`). `run_live` pins `max_markets` to the tradeable
  count, and `rescope_loop` moves the window without reconnecting when ATM
  drifts. Lines: KXINX 48, KXINXU 30, 78 of 100 combined.
- **Shadow takes never used up Kalshi depth.** A 0.01-contract ask got
  "filled" 779 times in 4 minutes. `LiveEngine` now nets its own shadow
  takes against the depth at the touch until that depth changes.
- **`n_unaccounted` counted every shadow cancel** (tens of thousands per
  session). Shadow cancels are now their own outcome, so the invariant
  reads 0 again.
- **A `config_override.json` from September was silently applied** at
  every start. It's now ignored unless it's saved after the session starts.

## 2026-10-08 — `--series` accepts several series

- `kalshi_main/run_live.py --series KXINX KXINXU` (or `KXINX,KXINXU`)
  runs each series as its own child process, with output prefixed
  `[SERIES]`. The direct `kalshi_latency_arbs/live/run_live.py` takes the
  same form for the SPX series.
- `--max-orders-per-s` is now a total across all series: Kalshi's limit
  is per account, so it's split evenly (default 10/s → 5/s each for two).
- `--event-ticker` still needs exactly one series.
- Ctrl-C reaches every child. Each one pulls its quotes and exits; the
  parent waits up to 60s, then force-terminates whatever is left (and
  says to check Kalshi by hand).
- Ctrl-Break used to kill the process without running the quote-pull
  teardown on Windows. It now takes the same path as Ctrl-C.

## 2026-10-08 — IB feed wired into run_live; first live validation

- All IB connections are `readonly=True` (`keys.IB_READONLY`). IB is
  data-only; orders still go to Kalshi.
- `keys.IB_PORT` now 4001 (Gateway live), the port this account uses.
- `run_live` scopes the IB subscription to the ATM buckets
  (`ib_functions.DEFAULT_ATM_BUCKETS`: KXINX 5, KXINXU 15) and uses a
  separate clientId per series so both can run at once.
- `LiveIBFeed` refuses to start if the scope is over the 100-line budget.
- Error 10090 (no SPX index subscription) is filtered from the logs; the
  OPRA leg is unaffected.
- New tools: `kalshi_infra/measure_ib_latency.py`,
  `kalshi_latency_arbs/live/check_fair_value.py`.

Results: [ib-migration.md § Validation](ib-migration.md#validation-2026-10-08).

## 2026-10-05 — Live options feed: Databento ES → IB SPXW

**Summary.** Live-trading option feed switched from the retired
Databento ES-daily futures-options stream to native SPXW options on
Interactive Brokers TWS/Gateway. Historical option quotes (backtest
only) still come from Databento. ThetaData and the provider-selector
layer are removed entirely.

Details: [ib-migration.md](ib-migration.md).

### Files added

- `kalshi_infra/ib_functions.py` — IB contract helpers, strike
  resolution, deferred ib_async import.
- `kalshi_latency_arbs/live/live_ib_feed.py` — live SPXW feed
  implementing the `LiveIBFeed` interface `run_live.py` already
  expected.
- `docs/` — this directory (project-level docs).

### Files removed

- `kalshi_infra/thetadata_functions.py`
- `kalshi_infra/option_source.py`
- `kalshi_latency_arbs/live/__pycache__/live_databento_feed.cpython-312.pyc`
  (stale bytecode of the retired ES-daily feed).

### Files modified

- `kalshi_infra/keys.py` — added `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`,
  `IB_ACCOUNT` placeholders. IB uses no bearer token.
- `kalshi_latency_arbs/latency_arbs_functions.py` and
  `kalshi_latency_arbs/spxw_signal.py` — now import
  `databento_functions` directly (no `option_source` indirection).
- `kalshi_latency_arbs/live/run_live.py` — comments refreshed (no more
  ES/Databento-feed references); logic unchanged, already called
  `LiveIBFeed`.
- `viz/backtest_cache.py` — dropped `thetadata_functions.py` and
  `option_source.py` from the code-fingerprint globs; dropped
  `data/thetadata/` from the data-fingerprint globs.
- `kalshi_latency_arbs/live/live_context.py`,
  `kalshi_latency_arbs/live/live_persist.py`,
  `kalshi_latency_arbs/live/live_orders.py`,
  `kalshi_latency_arbs/live/live_kalshi_ws.py`,
  `kalshi_latency_arbs/live/live_signal.py` — stale ES/Databento
  comment references fixed.

### Comment policy

Per CLAUDE.md: docstrings and inline comments are being trimmed to
"fact + reason", with narrative moved to `docs/`. This pass trimmed
the files touched by the migration; others will be trimmed as they are
edited.
