> **Historical (pre-2026-10-09 reorg).** Paths and run commands below refer to the
> removed `kalshi_crypto/` package. Current layout: `kalshi_strategies/KXCRYPTOLEAD15M/`;
> run with `python -m kalshi_strategies.run --series KXCRYPTOLEAD15M`. See `docs/live-trading.md`.

# kalshi_crypto -- Coin Race 15m Market Maker

Market-making for Kalshi's `KXCRYPTOLEAD15M` series. Primary objective:
Kalshi Liquidity Incentive Program (LIP) rewards from resting two-sided
size on all 5 markets of each event. Secondary objective: avoid adverse
selection via a per-coin theoretical fair value computed in real time.

Spec: `OpenCode Build Prompt  Kalshi Coin Race LIP Market Maker.md` in
this folder.

## Status

**First-pass skeleton. Not yet calibrated.** Every module in the
"Deliverables" section of the spec exists and is importable, but
several knobs are deliberately uncalibrated:

- `CoinRaceConfig.lip_period_reward_divisor` -- the Kalshi API's
  `period_reward` field has an unverified unit. Default assumes
  centi-cents (200000 -> $20); verify against a real payout.
- `CoinRaceConfig.lip_discount_factor_default`, `lip_target_size_default`
  -- discovered per market from `/incentive_programs` at session start;
  the defaults are only used when the API query fails.
- `ConstantCovForecaster` -- seeded with "operator-plausible"
  annualized vols (BTC 55%, ETH 65%, SOL 85%, XRP 75%, HYPE 110%) and a
  hand-written correlation matrix (BTC-ETH 0.80 down to HYPE-XRP 0.35);
  see `cov_forecaster.DEFAULT_ANNUAL_VOL`. Produces reasonable fairs
  for smoke/paper runs -- not calibrated against our own composite
  returns. The real realised-vol / covariance model is a separate
  design (section 5 of the spec); the pricer is wired to the
  interface, not the model.
- `fill_intensity` for the LIP credit per-contract estimate -- currently
  returns the FULL remaining-period reward (section 6.2 upper bound).
  Replace with a measured fill_intensity when 1s/5s/30s markouts for
  this stack exist.

Reconciling these numbers against first real payouts is a
post-session action, not a precondition for shipping -- the spec is
explicit that "paper mode default ON" so the uncalibrated defaults
only affect what we would SHADOW-trade.

## Package layout

```
kalshi_crypto/
├── __init__.py
├── README.md
├── OpenCode Build Prompt  Kalshi Coin Race LIP Market Maker.md    # the spec
├── config.py                # CoinRaceConfig, fee multiplier
├── series.py                # SERIES_TICKER, COINS, CF_CONSTITUENTS, etc.
├── spot_feed/
│   ├── __init__.py
│   ├── base.py              # VenueBook, VenueAdapter protocol
│   ├── coinbase.py          # Phase 1 adapter -- all 5 coins on one WS
│   └── health.py            # FeedHealthMonitor (section 2.1 kill switch)
├── cf_rate.py               # CF Spot Rate methodology replica
├── composite.py             # CompositeFeed + CompositeBus (1s bars)
├── settlement_tracker.py    # SettlementTracker (section 3)
├── cov_forecaster.py        # STUB interface + ConstantCovForecaster
├── pricer.py                # CoinRacePricer: phase math + MC fairs
├── lip_scorer.py            # LIP scoring replica (section 6.1)
├── quoter.py                # edge arithmetic (section 6.2)
├── event_state.py           # EventState, event discovery, LIP params
├── strategy.py              # CoinRaceStrategy (strategy_fn)
├── viz_shim.py              # Dashboard series registration
├── backtest/
│   ├── __init__.py
│   └── run_backtest.py      # Composite replay + calibration reports
├── live/
│   ├── __init__.py
│   ├── run_live.py          # Paper-first live runner
│   ├── kalshi_feed.py       # KXCRYPTOLEAD15M-specific Kalshi WS
│   └── persist.py           # viz/static JSON + parquet writers
└── tests/
    ├── test_pricer.py
    ├── test_lip_scorer.py
    ├── test_settlement.py
    ├── test_quoter.py
    └── test_cf_rate.py
```

## The math (section 4 of the spec, in code comments)

The pricer works in LOG returns because ranking is invariant and the
Brownian-average variance identities (var of mean of BM over [0, L] is
L/3) show up as clean closed-form expressions. Kalshi settles on
ARITHMETIC returns (rounded to 3 decimals in percent); the
`SettlementTracker` is the only module that works in arithmetic, so
the two never cross without a conversion.

Final log return vector `Y ~ N(mu, V)` by phase, with averaging length
`L = 60`:

| Phase | `mu` | `V` |
|---|---|---|
| Pre-open (`now < S - L`) | `0` | integral over `[now, T-L]` + `L/3 * Sigma_bar_start` + `L/3 * Sigma_bar_end` |
| Forming start (`S-L <= now < S`) | `0` | middle integral + `(remaining_start/L) / 3` of start-avg vol + `L/3` of end-avg vol |
| In-window (`now >= S, tau >= L`) | `x` | middle integral + `L/3 * Sigma_bar_end` |
| Last minute (`u = tau < L`) | `I + (u/L) x` | `u^3 / 10800 * Sigma_bar_rem` |

Section 4.6 sensitivities (`delta[i, j] = dp_i/dx_j`, `theo_vol`) are
computed by common-random-number finite difference on the same MC
sample -- that's why the pricer pre-generates `Z` once at construction
and reuses it on every call. Spec: section 4.5 "Reuse the same Z every
tick so theo is smooth". Measurably mattered in a prior repo.

## The quoting objective (section 6.2)

Reused from the SPX quoter in
`kalshi_latency_arbs/spxw_kxinx/sp_options_lag_quoting.py` with one
addition and one adaptation:

Required edge, per side:
```
required_edge = constant_edge + ci_edge + lean + fee - lip_credit
required_edge = max(required_edge, adverse_buffer)   # LIP never floors below constant_edge
reservation = fair -/+ required_edge
quote_level = first_tick_outside(reservation), capped a tick inside opposite touch
```

The user-specified design decision: `sigma_feed` and `kappa` from the
original spec are NOT used; the SPX template's `constant_edge` (flat
floor) plus `ci_vol_multiplier * theo_vol` (vol-scaled edge) covers
the adverse-selection buffer role.

## Running

### Tests
```
python -m pytest kalshi_crypto/tests/ -q
```
Expected: 29 passing on a clean checkout.

### Paper trading (default)
```
python kalshi_main/run_live.py --series KXCRYPTOLEAD15M
```
(or, equivalently, `python -m kalshi_crypto.live.run_live` -- same code
underneath). Picks the soonest-closing open event. Writes:
- `data/live/KXCRYPTOLEAD15M/{date}/summary.json` (and parquet files)
- `viz/static/live_KXCRYPTOLEAD15M__*.json` (one per market)
- `data/kalshi_crypto/composite/{coin}_{date}.parquet` (1s bars)

Open the dashboard (`streamlit run viz/app.py`), pick `KXCRYPTOLEAD15M`
and Live mode. Each of the 5 coins appears in the market dropdown; the
usual edge-decomposition chart renders for each.

### Backtest replay
Needs persisted 1s composite bars at
`data/kalshi_crypto/composite/{coin}_*.parquet`:
```python
from kalshi_crypto.backtest.run_backtest import run_one
r = run_one("KXCRYPTOLEAD15M-26OCT061445")
print(r.lip_accrued_usd, r.lip_snapshots_qualified)
```

### Historical dashboard (partially wired)
The viz shim registers the series in `viz/data_loader.py`, but
historical mode is not yet fully supported for KXCRYPTOLEAD15M -- the
precompute_signal shim returns empty grids, so the probability panel
is blank. Live mode is the primary surface for now.

## Config overrides at runtime

Writing a JSON file to
`data/live/KXCRYPTOLEAD15M/config_override.json` is picked up by the
runner within 1 second; only fields in `config.EDITABLE_FIELDS` are
applied (unknown keys silently dropped). Same convention as the SPX
runner. The dashboard's config panel writes to this file.

## Known gaps / followups

1. **Phase 2 venues (Kraken, Bitstamp).** `spot_feed/` is designed to
   accept them; adapters are not written. Only required if the
   settlement-replication rate drops below 98% or Coinbase has an
   outage mid-event.
2. **Real covariance model.** `ConstantCovForecaster` is a stub.
3. **fill_intensity.** The LIP credit computation currently treats the
   candidate order as resting the full remaining period, which is an
   upper bound. Replace with a measured fill-intensity function once we
   have own-fill markouts.
4. **Analytic pricer backend.** The spec asks for one; tests use
   `scipy.stats.norm.cdf` in a reduced-dimensional cross-check instead.
   Add a full `scipy.stats.multivariate_normal.cdf`-based backend if a
   production reason to cross-check MC at a specific tail arises.
5. **Historical dashboard mode.** Needs a dedicated chart path that
   reads composite bars instead of SPX option quotes.
