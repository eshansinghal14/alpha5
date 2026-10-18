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
    basis_tie_gap: float = param(
        "Last minute: block both sides of every coin within this projected-return gap (fraction, not %) "
        "of the leader on EITHER the Coinbase mid or the CF replica. Coinbase-only prices miss the "
        "settlement index on near-ties: on 97 events 2026-10-06..10, 7.5e-5 flagged ~20% of last "
        "minutes and caught every wrong mid leader from tau<=15s (8/10 at 30s). 0 disables.",
        min=0, step=0.00001)
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
    quote_before_start: bool = param(
        "Quote before the start TWAP is locked (pre-open / forming start average).")
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
        "onto the Kalshi mids) applied to pricing, after EMA smoothing. 0 disables. 0.25 on 1,485 "
        "events 2026-09-21..10-07 (Coinbase trades + Kalshi 1m candles): log loss -0.0040, t -2.9; "
        "244 events 10-09..12 on recorded books: -0.0101, t -1.9. 0.5 was noisier, 1.0 worse than none.",
        min=0, max=1, step=0.05)
    market_shift_half_life_s: float = param(
        "EMA half-life of the market-implied shift. 30/60/120 scored within noise of each other.",
        min=1, step=10)
    market_shift_fit_s: float = param(
        "How often a new market-implied shift is estimated (one ridge solve on the bump deltas).",
        min=0.5, step=0.5)
    market_shift_max_bp: float = param(
        "Cap on any one coin's estimated shift, per estimate, in bp of return.", min=0, step=0.5)
    kalshi_start_offset_events: int = param(
        "Once Kalshi publishes the start value (single-coin 15M expiration_value), price off it, "
        "shifted onto our mid's level by the mean offset over this many previous boundaries. "
        "8 on 59 events 2026-10-11: BTC-HYPE gap error 0.89 -> 0.73bp. 0 = our own start TWAP.",
        min=0, step=1, apply=APPLY_NEXT_EVENT)


@section
class CryptoConfig:
    quoting: QuotingParams
    trading_time: TradingTimeParams
    risk: RiskParams
    runtime: RuntimeParams
    lip: LipSettings
    pricing: CryptoPricingParams
