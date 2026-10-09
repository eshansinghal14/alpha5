# Build Prompt: Kalshi "Coin Race 15 minutes" (KXCRYPTOLEAD15M) Liquidity-Incentive Market Maker

## Goal

Build a market-making system for Kalshi's `KXCRYPTOLEAD15M` series. The **primary objective is to earn Kalshi Liquidity Incentive Program (LIP) rewards** by resting two-sided size. The **secondary objective is to avoid adverse selection**. To support both, compute a real-time theoretical fair value ("theo") for each of the 5 markets in every event, and quote relative to it.

Integrate this into the existing codebase. Follow its conventions for config, logging, async/event loop, exchange clients, and tests. If a needed component already exists (e.g., a Kalshi REST/WS client or an order manager), reuse it rather than duplicating.

**Leave the realized-volatility / covariance forecasting model as a stub behind a clean interface (spec below). Do NOT implement a vol model. We will design it separately.** Wire everything else to call that interface.

---

## 1. Contract mechanics (must be replicated exactly)

- Each event (e.g., `KXCRYPTOLEAD15M-26OCT061445`) has 5 binary markets: `-BTC`, `-ETH`, `-SOL`, `-XRP`, `-HYPE`.
- The event covers a 15-minute window `[S, T]`, where `T` = market `close_time` and `S = T − 900s`. Markets open at `S` (`open_time`).
- A market settles YES if its coin has the **highest return** among the 5 over the window.
- Return = `(End − Start) / Start × 100`.
  - **Start** = time-weighted average of the CF Benchmarks Spot Rate over the 60 seconds immediately preceding `S`.
  - **End** = the same 60-second TWAP immediately preceding `T`.
- Returns are rounded to **3 decimal places in percent** (i.e., 1e-5 in return units).
- **Ties at the top:** each tied coin pays `floor(100 / n_tied)` cents; all others pay 0.
- Kalshi API fields: market `rules_primary` / `rules_secondary`, `open_time`, `close_time`, `result`, `expiration_value`. Series metadata: `fee_type: quadratic`, `fee_multiplier: 1`.
- Contract terms: https://assets.kalshi.com/contract_terms/CRYPTORETURNCOMPARE.pdf

---

## 2. Data required

### 2.1 Spot data (replicating the CF Spot Rate / RTI)

Kalshi settles on 60-second averages of the CF Real-Time Indices, which publish once per second. The CF real-time feed requires a paid license. **Decision: do NOT buy an aggregator feed.** Instead, consume the **free public L2 websockets of the CF constituent exchanges directly** and recompute the index ourselves.

**Phase 1 (build now): Coinbase ONLY.** Use `wss://advanced-trade-ws.coinbase.com`, `level2` channel, for all 5 coins (BTC-USD, ETH-USD, SOL-USD, XRP-USD, HYPE-USD).
- Coinbase is a CF constituent for every coin.
- Only *returns* matter, so a stable Coinbase-vs-CF basis cancels between the start and end TWAPs. The residual (a time-varying basis) is absorbed by the pricer's `sigma_feed` noise term (§4.4).
- Even with one venue, run the single book through the same CF Spot Rate pipeline below (size cap, depth-weighted exponential mid). Sample it every second.
- Build `spot_feed` as a **venue-pluggable interface** (one adapter per exchange), so adding venues later is config-only.

**Phase 2 (only if triggered): add Kraken, then Bitstamp, then the P1 venues.** Add them when any of these happen:
- Settlement-replication agreement falls below **98%** over a rolling window.
- Fill markouts show losses concentrated in close races (top-2 margin < a few bps).
- Coinbase outages or staleness cost meaningful LIP uptime.

Once multiple venues exist, a second venue also serves as **failover**.

**Feed-health kill switch (required in Phase 1):** if the Coinbase book for any coin is stale (no update for more than X ms, configurable), fails a sequence/integrity check, or is resyncing:
- Immediately mark theo for that event INVALID.
- Cancel all resting orders in that event.
- Do not requote until the book has been healthy for N seconds.

Show this state in the viz.

The venue table below is the roadmap; **only Coinbase is implemented in Phase 1.**

Why direct feeds rather than an aggregator:
- CF computes from **order books**, not trades.
- The feeds are free and the lowest latency available.
- They are the exact inputs CF uses.
- Paid aggregators (CoinAPI order-book websockets on the ~$599/mo tier, Kaiko enterprise, CoinDesk Data with no free tier) add latency, cost money, and still are not the CF number.

**Constituent exchanges and public endpoints:**

CF constituents (from the CME CF and CF constituent docs, Sept 2026):
- **BTC:** Coinbase, Kraken, Bitstamp, Gemini, Crypto.com, Bullish, LMAX Digital. itBit is suspended as of 27-Jul-26.
- **ETH:** same as BTC (Bullish included).
- **SOL:** Coinbase, Kraken, Gemini, Bitstamp, Crypto.com, LMAX, plus possibly Bullish.
- **XRP:** Coinbase, Kraken, Bitstamp, Crypto.com, LMAX.
- **HYPE** (`HYPEUSD_RTI`): Bitstamp, Coinbase, Kraken only.
- **Re-verify this list on startup from config.** CF changes constituents periodically.

| Priority | Venue | Public endpoint (no auth for market data) | Notes |
|---|---|---|---|
| Phase 1 | Coinbase | `wss://advanced-trade-ws.coinbase.com`, `level2` channel | No JWT required. Authenticating with a CDP key is recommended for connection stability. Covers all 5 coins. |
| Phase 2a | Kraken | `wss://ws.kraken.com/v2`, `book` channel (depth 10/25/100/500/1000, CRC32 checksum) | Covers all 5. Validate the checksum on every update. |
| Phase 2b | Bitstamp | Bitstamp WebSocket API v2, order-book channels | Covers all 5. |
| P1 | Gemini | `wss://api.gemini.com/v2/marketdata`, `l2` | BTC, ETH, SOL. |
| P1 | Crypto.com | Exchange v1 websocket, `book` channel | BTC, ETH, SOL, XRP. |
| P1 | Bullish | `/trading-api/v1/market-data/orderbook` websocket (L1/L2) | BTC, ETH (SOL possibly). |
| P2 | LMAX Digital | Requires an institutional account | Skip initially. Measure the replication error without it. |

- Coinbase + Kraken + Bitstamp cover every coin and are the only HYPE constituents. They are the natural Phase 2 additions, after the Phase 1 Coinbase-only system is validated.
- Do not use Binance, Bybit, or OKX for the composite. They are not constituents and are geo-restricted for US users.
- Hyperliquid spot/perp may be used as a leading signal only.

**Composite = re-implementation of the CF Spot Rate methodology** (CF Spot Rate Methodology Guide, https://docs.cfbenchmarks.com/CF%20Spot%20Rate%20Methodology%20Guide.pdf):

1. Maintain the full L2 book per venue with sequence/checksum validation. Resync on gaps.
2. Every second, discard bad books:
   - Stale: retrieval lag above the threshold. HYPE uses 30s; make it configurable per coin.
   - Erroneous: unparseable, one-sided, or crossed.
   - Potentially erroneous: venue mid deviates from the cross-venue median mid by more than the parameter (10% for HYPE). Reinstate it once the deviation is below 50% of the parameter.
3. Merge into a **consolidated book**. Cap each level at the order-size cap (dynamic: trimmed mean + 5 × winsorized SD of nearby level sizes). **Uncross** it by removing the lesser size at crossing levels until bid < ask.
4. Build bid/ask price-volume curves, the mid curve (their average), and the mid-spread curve.
   - **Utilized depth** = the maximum cumulative volume where the mid-spread is within the deviation-from-mid parameter (1% for HYPE). Floor it at the spacing.
   - Spacing is dynamic: the min of level-based spacing (thinner side's liquidity / 100 target levels) and KDE-based spacing.
5. Spot Rate = the mid curve weighted by a normalized exponential density with λ = 1 / (0.3 × utilized depth), summed over the curve up to utilized depth.
6. Emit at every wall-clock second (CF publishes ~1/s). Also expose a fast "nowcast" on every book update for quoting.
7. Keep a **simple fallback** (venue mid; the median of venue mids once there are multiple venues) behind a flag, and log the difference vs the CF-style rate.
- **Per-coin parameters must be config**, not hard-coded. Pull them from the CF methodology parameter table and verify each one.
- **Optional leading signals** (for adverse-selection protection, not settlement): perpetual futures mids (Hyperliquid perps, Coinbase International, etc.). Store them, and expose a `lead_signal` hook, but do not feed them into theo by default.
- **Persist everything:** raw ticks plus 1-second composite bars to disk (parquet or the existing store) for backtesting.

### 2.2 Kalshi data

- REST base: `https://api.elections.kalshi.com/trade-api/v2`
- `GET /series/KXCRYPTOLEAD15M`: series metadata.
- `GET /markets?event_ticker=...` and `GET /markets?series_ticker=KXCRYPTOLEAD15M&status=...`: market discovery. Price fields are `*_dollars` strings and sizes are `*_fp` strings; parse them as Decimals.
- `GET /markets/{ticker}/orderbook`: returns `orderbook_fp.yes_dollars` and `no_dollars`, each a list of `[price, size]` pairs. **These are bids only. A YES ask at p equals a NO bid at 1 − p.**
- Websocket orderbook deltas, trades, and fills: use the existing client if present.
- `GET /markets/trades?ticker=...`: historical trades, with fields `taker_side`, `yes_price_dollars`, `count_fp`, `created_time`.
- `GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1`: historical BBO and price.
- `GET /incentive_programs?status=active`: LIP parameters per market. Fields: `market_ticker`, `start_date`, `end_date`, `period_reward`, `target_size_fp`, `discount_factor_bps`, `incentive_type`, `paid_out`.
  - Currently observed values for this series: `target_size_fp = 1000`, `discount_factor_bps = 5000` (0.5), `period_reward = 200000`. Each program covers one 15-minute event.
  - **The unit of `period_reward` is unverified** (likely centi-cents, i.e., $20). Make it a config value and add a check that reconciles it against actual payouts.
- **Settled history:** for calibration and for validating the composite against outcomes.

---

## 3. Settlement replication module

Implement `SettlementTracker` per event:

- `start_twap[coin]`: accumulate composite 1-second samples in `[S−60, S)`. It is final at `S`. If we start up late, backfill from stored bars. Flag `start_quality` if any samples are missing.
- `end_partial[coin]`: running sum of composite 1-second log returns (vs `start_twap`) over `[T−60, now)`, plus the sample count `k`.
- `projected_return(coin)`: current return estimate.
- `replicated_winner()` after `T`: compare against Kalshi `result`. Log every event.
  - **Target agreement: at least 98%.** A naive single-exchange 1-minute proxy gets about 90.6%, which is not acceptable.
  - Log the margin (gap between first and second place) for every mismatch, to diagnose composite error.
- Apply rounding and tie handling exactly as in the rules.

---

## 4. Pricing engine (the core; implement exactly)

### 4.1 State inputs per tick

- `x[i] = ln(S_now[i] / start_twap[i])`: current log return vs the locked start TWAP (5-vector).
- `I[i] = (1/60) · Σ_{samples s in [T−60, now)} ln(S_s[i] / start_twap[i])`: zeros before the last minute.
- `tau = T − now` in seconds.
- `Sigma`: per-second 5×5 covariance of log returns, from the vol model interface (§5). It may be time-varying, so see the variance clock in §4.3.

Ranking on log returns is equivalent to ranking on arithmetic returns, so work in logs.

### 4.2 Distribution of final returns

The final log return vector satisfies `Y ~ N(mu, V)`:

| Phase | `mu` | `V` |
|---|---|---|
| Pre-open (`now < S`; start TWAP unknown, quoting the next event early) | `0` | `Sigma × (900 − 20) = Sigma × 880` (precisely: middle segment + start-avg `L/3` + end-avg `L/3` with `L = 60`) |
| During `[S−60, S)` (start TWAP partially formed) | Generalize: unknown remainder of the start average contributes variance with the same `u³/(3·60²)` logic, mirrored. Implement this, or simply don't quote in this sub-interval initially (config flag). | |
| In-window, `tau ≥ 60` | `x` | `Sigma × (tau − 40)` |
| Last minute, `u = tau < 60` | `I + (u/60) · x` | `Sigma × u³ / 10800` |

Both phases meet at `tau = 60` (`mu = x`, effective time 20s). Unit-test this continuity.

**Derivations, to put in code comments:**
- The variance of the average of a Brownian motion over length `L` is `L/3`.
- For the last-minute remainder, `(1/60)∫_0^u W dv` has variance `u³/3 / 3600`.

### 4.3 Variance clock (time-varying vol)

Do not assume constant vol. Generalize `tau_eff` to forecast integrated variance:

- Before the last minute: `V = ∫_now^{T−60} Σ(s) ds + (1/3600)·∫∫ min(...)` weighting over the end window. Practically: `V = Σ_bar_1 × (T−60−now) + Σ_bar_2 × 20`, where `Σ_bar_1` and `Σ_bar_2` are the vol model's average forecast covariance over each segment.
- In the last minute: `V = Σ_bar_remaining × u³/10800`.
- The vol model interface must therefore provide **forecast covariance over an arbitrary future interval** `[t0, t1]`. Seasonality and scheduled events live inside the model; the pricer just integrates.

### 4.4 Additional variance terms (configurable)

- **Feed/basis noise floor:** add `diag(sigma_feed²) × c_phase` to `V`. This accounts for our composite differing from the CF index. Without it, fairs snap to 0/1 too early in the last seconds. Estimate `sigma_feed` from the settlement replication logs.
- **Fat tails:** optional scale mixture. For each Monte Carlo draw, multiply the shock by `sqrt(w)` with `w ~ InvGamma(ν/2, ν/2)` (multivariate Student-t, configurable `ν`).
- **Shrinkage:** final `p = (1 − λ)·p_model + λ·p_base`, where `p_base` is the rolling empirical winner frequency over the last N events. Configurable `λ`, default 0.15.
  - Backtest evidence: the plain normal model is overconfident. Predicted 0.336 realized 0.305 in the top bucket.

### 4.5 Computing fairs

`p_i = P(Y_i > Y_j for all j ≠ i)`. Implement two backends and cross-check them in tests:

1. **Monte Carlo with common random numbers:** pre-generate fixed `Z ~ N(0, I)` of shape `(N, 5)`, with N around 20k–50k (configurable). Reuse the same `Z` every tick so theo is smooth and does not jitter from sampling noise. Use `Y = mu + Z @ chol(V).T` and `p = bincount(argmax(Y)) / N`. Vectorize with numpy; target under 2 ms per event per tick.
2. **Analytic:** for each `i`, take `D_i` with rows `e_i − e_j`, then `p_i = Φ_4(D_i mu ; D_i V D_iᵀ)` as an orthant probability (scipy `multivariate_normal.cdf`, or a Genz algorithm).

Guarantees:
- Fairs sum to 1. Renormalize after shrinkage.
- Clip to `[0.001, 0.999]` for quoting purposes only.
- **Ties:** only relevant when `V` is tiny. If the top-two gap in `mu` is under 1e-5 and `V` is near 0, split probability per the tie rule.

### 4.6 Sensitivities (needed for quoting width)

- `dp_i / dx_j` for all `i, j` (5×5 delta matrix), via finite differences on the same `Z` (bump `x_j` by 1 bp).
- `theo_vol_i = sqrt(gradᵢᵀ · (Σ × latency_s) · gradᵢ)`: expected one-sigma theo move over our reaction latency. This drives spread width and when to pull.
- `d p / d tau` (theta), for monitoring.

### 4.7 Pricer API

```python
class CoinRacePricer:
    def fairs(self, event_state: EventState, now: float) -> FairResult: ...
    # FairResult: probs[5], mu[5], V[5x5], tau, tau_eff, phase, delta[5x5], theo_vol[5], diagnostics

    def fairs_at(self, x, I, tau, sigma_provider, phase) -> np.ndarray:  # pure function, used by backtests
        ...
```

Make the pure function stateless and deterministic (given seeded `Z`), so backtests and live trading share the exact same code.

---

## 5. Realized-vol model interface (STUB ONLY — do not implement a model)

```python
class CovarianceForecaster(Protocol):
    def update(self, ts: float, composite_prices: dict[str, float]) -> None:
        """Ingest 1s composite prices (or ticks)."""

    def forecast_cov(self, t0: float, t1: float) -> np.ndarray:
        """Return the 5x5 covariance of log returns integrated over [t0, t1] (units: return^2, NOT per-second).
        Coin order: BTC, ETH, SOL, XRP, HYPE."""

    def forecast_cov_rate(self, t: float) -> np.ndarray:
        """Instantaneous per-second covariance at time t (for sensitivities/latency)."""
```

- Provide `PlaceholderForecaster` that raises `NotImplementedError`, plus a `ConstantCovForecaster(Sigma_per_sec)` used only in tests and for smoke runs.
- The pricer must depend only on this interface.
- Notes to leave in a docstring for the future model:
  - Correlations estimated from 1-second returns are biased low by asynchronous trading (the Epps effect).
  - Seasonality and scheduled macro events must be reflected in `forecast_cov(t0, t1)`.

---

## 6. Quoting engine: LIP-aware level selection

**Key principle: quotes are NOT simply `fair ± spread`.** The level we rest at must explicitly trade off three things:
1. **LIP reward earned** by resting at that price level (depends on position relative to the LIP Reference Price, the discount factor, our share of qualifying score, and the reward rate).
2. **Expected adverse-selection cost if filled** (depends on distance from theo, `theo_vol`, time remaining, and fill probability).
3. **Inventory risk** across the 5 legs.

### 6.1 LIP scoring replica (implement exactly, from Kalshi's rules)

Sources: https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program and CFTC filing https://www.cftc.gov/filings/orgrules/rules07152610358.pdf

- A snapshot is taken once per second at a random time within the second.
- Treat each side separately: the YES side is YES bids; the NO side is NO bids, where **YES asks count as NO bids**.
- Walk down from the best bid, accumulating size:
  - The **Reference Price** is the first level where cumulative size ≥ `Target/5`.
  - Stop once cumulative size ≥ `Target`.
  - If total size is below `Target`, that side has no qualifying orders.
- Score per qualifying order = `size × DF^N`, where `N` = ticks below the Reference Price (0 if at or above it).
- Normalize within each side. A user's snapshot score = our normalized YES share + our normalized NO share (max 2.0 across all users).
- **Snapshot exclusion:** if either side fails to reach `Target`, the snapshot is excluded, and the payout scales by `non_excluded / total`.
- Payout = `(our total score / all scores) × period_reward × non_excluded / total`, rounded down to the cent. Minimum payout is $1.00.

Implement `LipScorer`:
- `score_book(book, our_orders, params) -> (our_yes_share, our_no_share, ref_yes, ref_no, qualifies_yes, qualifies_no)`.
- `marginal_reward_rate(book, our_orders, params, candidate_price, candidate_size, side) -> $/second`: the expected reward per second from adding or moving an order. Use `reward_per_snapshot = period_reward_usd / n_snapshots_in_period`.
- Live tracking: estimated accrued reward per market per period, to reconcile against actual payouts.

### 6.2 Level selection objective

**Required-edge model: base the edge/width logic on our existing S&P (SPX) option quoting strategy in the codebase.** Reuse its structure for required edge as a function of greeks, risk, and time to expiry: theo ± required edge, vol/latency-driven widening, and inventory skew. Here the "greeks" are the pricer's `delta[5×5]`, `theo_vol`, and theta. Then **add an explicit liquidity-incentive component** that *reduces* required edge by the expected LIP value earned at that level (and adds a penalty when a level earns no LIP credit). In other words: `required_edge = spx_style_edge(risk, theo_vol, tau, inventory) − lip_credit(level, size, book)`, floored by the adverse-selection buffer below. Locate the SPX quoter module, and factor out shared pieces rather than copy-pasting.

For each market and side, choose `(price, size)` from a candidate set of price levels near the touch and near the Reference Price. Maximize expected value per second:

```
EV(price, size) = LIP_rate(price, size)                                   # $/s from 6.1, given current book
                − fill_intensity(price, tau, book) × E[adverse_cost | fill at price]
                − inventory_penalty(position after fill)
```

- **Edge at a YES bid `q`:** `fair − q`. At a NO bid `q'` (a YES ask at `1 − q'`): `(1 − fair) − q'`.
- **`E[adverse_cost | fill]`:** model the conditional theo move after a fill as `−edge + κ·theo_vol` (informed flow hits us when theo has already moved). Calibrate `κ` from our own fill markouts at 1s, 5s, 30s, and at settlement.
- **`fill_intensity`:** start with a simple empirical function of distance-to-touch and time remaining, estimated from Kalshi trade history. Keep it pluggable.
- **Incentive-adjusted fair band:** the LIP reward can justify resting at **zero or even slightly negative edge.** Define per market:
  - `max_bid = fair + LIP_value_per_contract_filled − adverse_buffer(theo_vol, tau)`
  - `min_ask` symmetrically.
  - We never bid above `max_bid` or offer below `min_ask`, regardless of LIP.
- **Reference Price targeting:**
  - Orders at the Reference Price get full credit. With DF = 0.5, each tick below halves credit.
  - Prefer resting **at the Reference Price rather than the best bid** when the best-bid level is thin (under `Target/5` contracts from others). This earns full credit while others absorb first fills.
  - Account for our own size moving the Reference Price.
- **Both-sides requirement:** our NO-side presence matters equally, and both sides must reach `Target` for snapshots to count.
  - Consider topping up the thinner side so the market qualifies, but only within the fair band.
  - Expose a metric for the fraction of snapshots that currently qualify.

### 6.3 Event-level consistency constraints

- Σ over 5 markets of our YES bid prices < $1, and Σ of our YES ask prices > $1 (ask = 1 − NO bid). This prevents anyone from arbitraging the full set against us.
- **Inventory is a 5-leg one-hot payoff.**
  - Risk = variance of P&L under theo probabilities.
  - Skew each leg's levels using the risk gradient.
  - Hard caps: per leg, per event, and total worst-case loss.
- Buying all 5 YES pays exactly $1, minus fees. Use this for inventory netting logic and for detecting mispriced books.

### 6.4 Time-phase policy (configurable)

- **Pre-open / early window:** widest participation; theo is near base-rate driven.
- **Mid-window:** normal operation; spread scales with `theo_vol`.
- **Last `N` seconds** (default 120–180, configurable): reduce size, widen, or pull legs whose `theo_vol` exceeds a threshold. Option to keep only far-out-of-the-money quotes (e.g., YES bids at 1¢ on legs with fair < 0.5%) to maintain LIP presence safely.
- **Final 60s:** theo is driven by `I` and decays as `u³`. Requote frequency must increase.

### 6.5 Requote logic

- Recompute theo on every composite update (throttled to N ms) and on every book change.
- Requote when `|new_level − resting_level| ≥ threshold`, or when a resting order falls outside the fair band. **Cancel-first if the order is now through fair.**
- Respect Kalshi rate limits. Prioritize cancels over new orders.
- **Fees:** apply Kalshi's quadratic fee schedule. Make the maker fee configurable and verify it. Include fees in the edge calculation.

---

## 7. Backtesting and calibration

- **Replay engine:** stored composite 1-second bars, Kalshi book/candlestick history, and LIP params → theo time series per event → simulated quotes and fills (queue-position approximation) → P&L split into: spread/edge P&L, adverse selection P&L, settlement P&L, and LIP rewards.
- **Theo calibration report** at phase checkpoints (`tau` = 900, 600, 300, 120, 60, 30, 10s): log loss, Brier score, and reliability buckets.
  - Baseline from an earlier offline test on 1,995 events using a naive 1-minute Coinbase proxy and simple EWMA covariance:
    - Uniform log loss: 1.609
    - Base-rate log loss: 1.558
    - At-open covariance model: 1.548
    - Conditional 5 min in: 1.204
    - Conditional 10 min in: 0.838
    - Conditional 13 min in: 0.505
  - The new system should beat these.
- **Market comparison:** theo vs Kalshi mid over time; correlation between taker direction and theo − price.
- **Settlement replication report** (§3).
- **LIP reconciliation:** simulated vs actual payout per period.

---

## 8. Monitoring and logging

- **Per second, per event:** `x`, `I`, `tau_eff`, `V` diagonal, fairs, theo_vol, our quotes, best bid/ask, Reference Prices, our LIP share per side, qualifying flag, inventory.
- **Alerts:**
  - Composite venue staleness or divergence.
  - Start TWAP incomplete.
  - Theo vs market mid divergence above a threshold.
  - Inventory limits.
  - Replication mismatch.
- **Kill switch:** cancel all orders in the series.

### 8.1 Visualization wiring (required)

Wire all of this into **our existing visualization/dashboard in the codebase**. Use its existing data bus, schema, and components; do not build a separate UI. Every value below must be published on the same channel the viz already consumes, with timestamps, keyed by `event_ticker` and market ticker.

**Per-event panel:**
- Countdown, `tau`, `tau_eff`, and the current phase (pre-open / in-window / last-minute).
- Start TWAP status (complete/incomplete) per coin.
- Each coin's current return `x`, end-window partial `I`, and the projected final return `mu`, with a ±1σ band from `V`.
- A **5-line theo chart** (fair per coin over the event's life), overlaid with the Kalshi mid/BBO per market and our resting bid/ask levels.

**Edge/quote decomposition (per market, per side):**
- Theo.
- SPX-style required edge.
- LIP credit.
- Adverse-selection buffer.
- Inventory skew.
- The final quoted price.

Show these as stacked components, so we can see why a level was chosen.

**LIP panel:**
- Reference Price per side.
- Our normalized share per side.
- Qualifying/excluded snapshot flag.
- Fraction of qualifying snapshots this period.
- Estimated reward accrued vs period reward.
- Running estimated vs actual payouts.

**Spot/composite panel:**
- Per coin: our replicated CF rate, the per-venue mids, and which venues are included or excluded (stale/erroneous/outlier).
- Fallback-composite difference.
- Feed latency per venue.

**Risk panel:**
- 5-leg inventory.
- Worst-case event P&L.
- Σ bids / Σ asks constraint check.
- Realized P&L split into edge, adverse selection, settlement, and LIP.

**Post-event:**
- Replicated winner vs Kalshi result.
- Top-2 margin.
- Markouts of our fills at 1s/5s/30s.

Add a test or smoke check that the viz receives each new field, so new pricer/quoter outputs can't silently go unrendered.

---

## 9. Tests (required)

- **Phase continuity:** `tau_eff(60⁻) == tau_eff(60⁺) == 20`, and `mu` continuous at `tau = 60`.
- `tau_eff(0) = 0`; fairs converge to the replicated winner as `tau → 0`.
- With identical coins (equal vol, equal correlations), fairs are 0.2 each.
- **Monotonicity:**
  - Increasing `x_i` increases `p_i`.
  - Increasing coin i's vol with mean 0 increases `p_i` when correlations are equal.
  - Higher correlation of i with the others lowers `p_i`.
- MC and analytic backends agree within tolerance.
- Fairs sum to 1 after shrinkage.
- `LipScorer` matches hand-computed examples, including the help-page example: 20% score × $100 × 8,000/10,000 = $16.00.
- Event constraint: Σ bids < 1 and Σ asks > 1 always.
- Tie handling at the rounding boundary.

---

## 10. Config (single file)

Coins and order; exchange lists per coin; composite staleness and outlier params; MC `N` and seed; shrink `λ`; t-mixture `ν`; feed noise `sigma_feed`; latency; phase thresholds; LIP reward unit multiplier; κ; fill-intensity params; inventory caps; requote thresholds; fee params; dry-run/paper mode (**default ON**).

## Deliverables

Modules: `spot_feed` (venue-pluggable; Phase 1 = Coinbase adapter only, with a feed-health kill switch), `cf_rate` (CF Spot Rate replica + fallback median), `composite`, `settlement_tracker`, `cov_forecaster` (interface + stubs), `pricer`, `lip_scorer`, `quoter`, `risk`, `backtest`, `reports`. Also unit tests, a README describing the math in §4 and the objective in §6, and a paper-trading entrypoint.
