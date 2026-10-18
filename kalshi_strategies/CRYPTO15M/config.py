"""Single-coin 15-minute up/down config schema. config.json holds one complete
section per series ticker (KXBTC15M, KXETH15M, ...)."""
from __future__ import annotations

import os

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section
from kalshi_infra.trading.params import (LipSettings, QuotingParams, RiskParams, RuntimeParams,
                                         TradingTimeParams)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


@section
class UpDownPricingParams:
    t_nu: float | None = param(
        "Unit-variance Student-t tails on the return (null = normal). 5, as the Coin Race pricer.",
        min=2.01, step=0.5)
    vol_mult: float = param(
        "Multiplier on the HAR per-second variance rate. 2.0 from KXETH15M 2026-10-18 20:48-21:10 UTC: "
        "the Kalshi mid priced ETH at ~1.75bp/sqrt(s) against our 0.91 (CF realized 0.64-1.09 that "
        "hour), so our fair ran 4-12c above the ask on every up move; one hour, recheck.",
        min=0.1, max=5, step=0.05)
    basis_sd_bp: float = param(
        "Sd (bp) of the error in our end average, added in quadrature to the diffusion sd. Start and "
        "observed end seconds are the CF index itself, so this only covers the mid->CF basis on the "
        "seconds still to come.", min=0, step=0.05)
    ci_vol_multiplier: float = param("Fair band half-width = this x theo_vol.", min=0, max=10, step=0.1)
    reaction_latency_s: float = param("Horizon for theo_vol (the fair move we charge for).",
                                      min=0.001, step=0.1)
    min_rv_seconds: int = param(
        "Withhold until the coin's RV has this many 1s samples (cold start without persisted bars).",
        min=0, step=60)
    min_trade_tau_s: float = param("Block both sides with fewer than this many seconds to close.",
                                   min=0, step=5)
    tradeable_min: float = param(
        "Block when the fair or the Kalshi touch is below this. These markets tick 0.1c under 0.10 and "
        "over 0.90, which the shared book/quoter round to whole cents.", min=0.0, max=0.5, step=0.01)
    tradeable_max: float = param("As tradeable_min, upper side.", min=0.5, max=1.0, step=0.01)
    burst_own_coin_only: bool = param(
        "Spot bursts pull quotes only on the bursting coin's market (off: any coin's burst pulls all).")
    market_shift_alpha: float = param(
        "Share of the market-implied shift in x (the one putting our fair on the Kalshi mid) applied, "
        "after EMA smoothing. 0 disables.", min=0, max=1, step=0.05)
    market_shift_half_life_s: float = param("EMA half-life of the market-implied shift.", min=1, step=10)
    market_shift_fit_s: float = param("How often a new shift is estimated.", min=0.5, step=0.5)
    market_shift_max_bp: float = param("Cap on one estimate of the shift, bp of return.", min=0, step=0.5)
    # Read by the shared KXCRYPTOLEAD15M SpotFeed (same meaning as there).
    burst_usd_1s: dict[str, float] | None = param("Coinbase 1s print notional burst fallback per coin.",
                                                  editable=False)
    burst_hold_s: float = param("How long a burst keeps quotes pulled.", min=0, step=0.25)
    burst_pull_coins: dict[str, list[str]] = param("Per burst source, the coins whose trips pull.",
                                                   editable=False)
    soft_stale_s: float = param("Feed health: DEGRADED after this.", min=0.1, step=0.5)
    hard_stale_s: float = param("Feed health: DOWN after this.", min=0.5, step=1)
    recovery_s: float = param("A DOWN coin must be healthy this long before it is used again.", min=0, step=0.5)
    warmup_s: float = param("Persisted bars replayed into the vol forecaster at start.", min=0, step=600,
                            apply=APPLY_NEXT_EVENT)
    composite_flush_s: float = param("Persist 1s composite bars this often.", min=1, step=5)


@section
class UpDownConfig:
    quoting: QuotingParams
    trading_time: TradingTimeParams
    risk: RiskParams
    runtime: RuntimeParams
    lip: LipSettings
    pricing: UpDownPricingParams
