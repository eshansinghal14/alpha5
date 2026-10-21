"""KXCRYPTOLEAD15M config schema. Values: config.json in this folder."""
from __future__ import annotations

import os

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section
from kalshi_infra.trading.params import (LipSettings, QuotingParams, RiskParams, RuntimeParams,
                                         TradingTimeParams)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


@section
class CryptoPricingParams:
    mc_n: int = param("Monte Carlo draws (common random numbers). 20k ~ 0.3% stderr on a p~0.05 tail, "
                      "~1-2ms at 5 coins.", min=1000, step=1000, apply=APPLY_NEXT_EVENT)
    mc_seed: int = param("Seed for the fixed MC draws (theo must not jitter on sampling noise).",
                         apply=APPLY_NEXT_EVENT)
    t_nu: float | None = param(
        "Unit-variance Student-t tail mixture (null = normal). 5 from 3-5 excess kurtosis; slightly "
        "better than normal 1-7 min out on validation.", min=2.01, step=0.5, apply=APPLY_NEXT_EVENT)
    vol_mult: float = param("Multiplier on the forecast variance rate (stress/diagnostic knob).",
                            min=0.1, max=5, step=0.05)
    shrinkage_lambda: float = param(
        "Shrink fairs toward the base rate. 0: with the fixed vol it capped the leader at 0.88 and cost "
        "~0.09 nats in the last minute on the 2-day held-out validation.", min=0, max=1, step=0.01)
    base_rate_window: int = param("Settled events in the base-rate vector (only if shrinkage > 0).",
                                  min=1, step=10, apply=APPLY_NEXT_EVENT)
    ci_vol_multiplier: float = param(
        "Fair band half-width = this x theo_vol (the 1-sigma fair move over reaction_latency_s).",
        min=0, max=10, step=0.1)
    reaction_latency_s: float = param(
        "Horizon for theo_vol: the fair move we charge for is the one over this many seconds.",
        min=0.001, step=0.01)
    theo_vol_reval_draws: int = param(
        "theo_vol by full revaluation: reprice under this many joint coin moves over the horizon and "
        "take E|fair change| (sigma-equivalent). Prices a race flip the slope x 1-sigma estimate misses "
        "(26OCT131200: 5-13c shown, 90c move). 0 = the old slope x 1-sigma estimate.",
        min=0, max=512, step=8)
    delta_refresh_s: float = param("Cadence of the bump deltas / theo_vol (edge sizing only).",
                                   min=0.05, step=0.05)
    max_cross_coin_skew_s: float = param(
        "Withhold fairs when the newest and oldest coin books are further apart than this -- one coin "
        "resuming before the others makes a false leader (2026-10-08 16:34: four 26c lifts).",
        min=0.05, step=0.1)
    burst_usd_1s: dict[str, float] | None = param(
        "Pull every quote in the event when one coin's trailing-1s Coinbase print notional (USD) reaches "
        "this. Per coin: each coin's p99.5 over 10/09 21:00-10/10 04:50 UTC. Pulling there 5.4% of the "
        "time covered 66 of 231 maker fills and $90 of their $153 5s adverse markout. Takes are left "
        "alone: takes in those windows were not worse. null disables.", editable=False)
    burst_hold_s: float = param(
        "How long a burst keeps quotes pulled after the last print over the threshold. Replay of every "
        "Kalshi print 10-15 17:00 to 10-16 08:45 UTC: the toxic flow is while the trailing-1s trigger is "
        "still over (+1.1c/lot to the taker at 10s); from 0.25s after it the flow favours makers, so 1.5s "
        "sat out good trades. 0.5s leaves margin for the cancel/requote round trip.", min=0, step=0.25)
    burst_pull_coins: dict[str, list[str]] = param(
        "Per burst source, the coins whose trips pull quotes; any other (source, coin) trip is logged "
        "only (burst_trips/). Replay 10-16 19:05 to 10-18 06:35 UTC on public Kalshi prints: time pulled "
        "only by a Hyperliquid trip had flow favouring makers (+1.96c/lot at 10s, except HYPE -1.1c), so "
        "Hyperliquid pulls on HYPE alone: maker $ on unpulled prints +$2,627 -> +$2,895, pulled 12.8% -> "
        "~10.5%, big-move coverage 83% -> 81.5%. Requiring 2+ sources or 2-4x the threshold pulled less "
        "but exposed worse flow (+$1,880 to +$2,040). Key \"perp_move\" is the price-move trigger "
        "(prints.MOVE_*): 2 of 3 perp venues moving >= their own p99.5 of |0.5s move| one way.",
        editable=False)
    perp_priced_markets: list[str] | None = param(
        "Coins whose x is the latest-mover perp nowcast (perp_nowcast.increment: spot + Binance's move "
        "since spot last moved, capped 2bp) instead of the spot mid. Race decisions 10-20 16:30-19:30 UTC: "
        "on HYPE, a Binance increment over 1bp was followed by Coinbase the same way 73-75% of the time "
        "(0.25-2s ahead), covering 0.50-0.73 of it; on SOL 28-56% and 0.20-0.46, so SOL stays on spot. "
        "Coinbase HYPE prints ~9/min vs Binance's ~900/min. The start TWAP and CF basis stay on spot. "
        "null = every coin.")
    quote_markets: list[str] | None = param(
        "Markets that rest quotes (edge and LIP, LIP exit included), by coin. null = every market. A market "
        "left out is still priced (the race is joint) and keeps any position to settlement. HYPE left out "
        "10-21: its edge fills were -$250 of the race's -$79 over 10-19 04:15..10-20 15:51 UTC and -$27 of "
        "+$8.5 over 10-21 00:31-02:48.")
    soft_stale_s: float = param("Feed health: a coin older than this is DEGRADED (priced off last value).",
                                min=0.1, step=0.5)
    hard_stale_s: float = param("A coin older than this is DOWN: the joint is unknowable, all fairs withheld.",
                                min=0.5, step=1)
    recovery_s: float = param("A DOWN coin must be healthy this long before it is used again.",
                              min=0, step=0.5)
    warmup_s: float = param("Trailing persisted bars replayed into the forecaster at event start "
                            "(1h RV window + 1h correlation seed + slack).", min=0, step=600,
                            apply=APPLY_NEXT_EVENT)
    composite_flush_s: float = param("Persist 1s composite bars this often.", min=1, step=5)
    market_shift_alpha: float = param(
        "Share of the market-implied per-coin return shift (the one that would move our five fairs "
        "onto the Kalshi mids) applied to pricing, after EMA smoothing. 0 disables. 0.25: winner log "
        "loss a wash on 568 events 10-12..18 (-0.0000, CI +-0.0022), but live edge fills while a market "
        "sat 3-10c outside Kalshi's touch ran +1.0c/lot with it (10-18 11:14-20:14 UTC) vs -0.5c "
        "without (20:14-02:32).", min=0, max=1, step=0.05)
    market_shift_half_life_s: float = param(
        "EMA half-life of the market-implied shift. 30/60/120 scored within noise of each other.",
        min=1, step=10)
    market_shift_fit_s: float = param(
        "How often a new market-implied shift is estimated (one ridge solve on the bump deltas).",
        min=0.5, step=0.5)
    market_shift_max_bp: float = param(
        "Cap on any one coin's estimated shift, per estimate, in bp of return.", min=0, step=0.5)
    vol_source: str = param(
        "\"implied\": each coin's vol is the one its single-coin 15M market (KXBTC15M, ...) implies over "
        "the same window (implied_rates.py), correlation stays HAR; every fair is withheld (no_implied_vol) "
        "while any coin lacks one. \"realized\": the HAR model x vol_mult. 10-19 01:45-01:57 UTC: implied "
        "0.53-0.82bp/sqrt(s) vs HAR 0.72-1.30, and within 4-8% of what the 15M mids priced.")
    corr_fit: bool = param(
        "Fit the coin correlation to the race's own Kalshi mids (corr_fit.py): the HAR correlation scaled "
        "toward independence by g, R_g = g R + (1 - g) I. Replay 10-19 02:33-04:32 UTC: g 0.9 cut "
        "|theo - mid| 2.70 -> 2.16c and halved the ETH/SOL/HYPE bias; settled log loss unchanged. Off = g 1.")
    corr_fit_s: float = param("Refit g this often (s).", min=1, step=1)
    corr_half_life_s: float = param("EMA half-life of g (s); carried across events.", min=1, step=30)
    corr_mult_min: float = param("Lowest g the fit may choose (0 = independent coins).", min=0, max=1, step=0.05)
    corr_mult_max: float = param("Highest g the fit may choose (1 = HAR as is).", min=0, max=1, step=0.05)
    corr_fit_grid: int = param("Grid points over [corr_mult_min, corr_mult_max].", min=3, max=41, step=2)
    corr_fit_mc_n: int = param("Monte Carlo draws per fit reprice (own pricer, off the loop).", min=1000,
                               step=1000)
    corr_fit_max_spread: float = param(
        "A race leg counts in the fit only while its spread is at most this ($); wider legs are left out "
        "(weight 0) but still renormalise the mids.", min=0.01, step=0.01)
    corr_fit_min_legs: int = param("Fit only with at least this many legs within corr_fit_max_spread.", min=2,
                                   max=5, step=1)
    corr_fit_min_tau_s: float = param("No fit with fewer than this many seconds left (the end average "
                                      "dominates; the shift freezes there too).", min=0, step=30)
    implied_fallback_realized: bool = param(
        "With vol_source implied: a coin with no implied or held estimate prices on the HAR rate instead of "
        "withholding the race. 10-20 16:00-22:00 UTC: seconds with any coin on HAR were 34% of the time and "
        "-$71 of -$84 settled (-$35/h vs -$10 implied, +$10 held); the HAR rate ran 20-44% under the next "
        "implied reading on every coin. Off = withhold (no_implied_vol).")
    implied_hold_s: float = param(
        "A coin's last implied estimate is held this long after its 15M mid stops giving readings, before "
        "the HAR fallback (or withholding). Gaps are mostly the next event's open (15M mid ~0.5 says nothing "
        "about vol) and its last minutes (mid pinned). 10-20 16:00-22:00 UTC, vs the next live reading: "
        "holding 180-600s old was |ln| 0.23-0.28 off vs HAR's 0.32-0.34; 97% of HAR seconds had a reading "
        "under 600s old. 10-18/19 found holding past ~5 min worse than HAR, hence held_vol_* guards it. "
        "0 = no hold.", min=0, step=30)
    held_vol_check: bool = param(
        "While a coin prices on a HELD implied vol, compare its realized spot vol over held_vol_window_s "
        "(10s returns on our 1s bars) with the held value; at or above held_vol_ratio (or at or below "
        "held_vol_min_ratio) the whole race pulls quotes (held_vol) for held_vol_hold_s. A held value can't "
        "see vol move; this does.")
    held_vol_window_s: float = param("Realized-vol window for held_vol_check (s).", min=20, step=10)
    held_vol_ratio: float = param(
        "Pull when realized / held reaches this. 2: the single-coin vol_burst calibration (ETH 10-11..18 "
        "had a >=2x minute 3.4% of the time).", min=1.1, step=0.1)
    held_vol_min_ratio: float | None = param(
        "Also pull when realized / held falls to this. null = off. Uncalibrated: 10-21 00:31-02:48 UTC at "
        "0.5 it pulled the race 25% of the time (held rows median 0.65x, never above 2x) while held-vol "
        "fills still ran -8.9c/lot at 10s (22 fills).", min=0.05, max=0.95, step=0.05)
    held_vol_hold_s: float = param("A held_vol pull lasts this long after the last tripping second.",
                                   min=0, step=5)
    held_vol_edge: float = param(
        "Extra $ edge on a coin's market (quotes and takes) while that coin prices on a held implied vol. "
        "Real edge fills 10-19..10-21 02:58 UTC on held vol: +0.84c/lot at 10s (+-0.93, 303 fills) vs "
        "-0.05c on live implied, but -8.9c on 10-21 00:31-02:48 (22 fills) -- a hedge, not a fitted cost.",
        min=0, max=0.2, step=0.005)
    implied_ema_s: float = param(
        "Time constant (s) of each coin's implied-vol EMA. 120: KXETH15M 10-18 22:39-23:10 replay, theo-mid "
        "+0.60c at 900s -> -0.12c at 120s.", min=10, step=10)
    implied_min_obs: float = param("Decayed accepted readings a coin's EMA needs before it prices.", min=1, step=1)
    implied_mid_min: float = param("Read a coin's 15M mid only inside [this, implied_mid_max].", min=0.01, max=0.5,
                                   step=0.01)
    implied_mid_max: float = param("Upper bound of the 15M mid read.", min=0.5, max=0.99, step=0.01)
    implied_max_spread: float = param("Read the 15M mid only while its spread is at most this ($).", min=0.001,
                                      step=0.005)
    implied_max_mid_age_s: float = param("Ignore a polled 15M quote older than this (s).", min=0.5, step=0.5)
    implied_mid_noise: float = param("Floor on the 15M mid's own error ($) in a reading's weight.", min=0.001,
                                     step=0.001)
    implied_spot_err_bp: float = param("Spot level error (bp) in a reading's weight (the delta term).", min=0,
                                       step=0.1)
    implied_lag_s: float = param("Spot move over this many seconds is added to the spot error (Kalshi lag).",
                                 min=0, step=1)
    implied_basis_sd_bp: float = param("Basis sd (bp) assumed when backing vol out of a 15M mid.", min=0, step=0.05)
    implied_max_obs_se: float = param("Drop a reading whose SE of ln(vol) exceeds this.", min=0.05, step=0.05)
    implied_max_curvature: float = param("Drop a reading where |volga| x SE / |vega| exceeds this.", min=0.05,
                                         step=0.05)
    implied_vol_min_bp: float = param("Reject implied vols below this (bp/sqrt(s)).", min=0.01, step=0.05)
    implied_vol_max_bp: float = param("Reject implied vols above this (bp/sqrt(s)).", min=0.5, step=0.5)


@section
class CryptoConfig:
    quoting: QuotingParams
    trading_time: TradingTimeParams
    risk: RiskParams
    runtime: RuntimeParams
    lip: LipSettings
    pricing: CryptoPricingParams
