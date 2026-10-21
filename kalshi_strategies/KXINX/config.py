"""S&P daily config schema. config.json holds one complete section per traded
series ticker (KXINXU, KXINXDUD) plus "hub", the shared pricing process's
own settings."""
from __future__ import annotations

import os

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section
from kalshi_infra.trading.params import (LipSettings, QuotingParams, RiskParams, RuntimeParams,
                                         TradingTimeParams)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
HUB_KEY = "hub"


@section
class SpxPricingParams:
    event_suffix: str = param(
        "Only trade events whose ticker ends with this (H1600 = the 16:00 ET daily close). SPXW "
        "0DTE options settle 16:00 ET, so pricing an intraday hourly off them applies the wrong "
        "horizon.", apply=APPLY_NEXT_EVENT)
    event_open_et: str = param(
        "HH:MM ET before which a new event is not opened (SPXW 0DTE isn't listed earlier and IB "
        "qualification fails). Ignored with trade_overnight.", apply=APPLY_NEXT_EVENT)
    trade_overnight: bool = param(
        "Start each event at the Cboe overnight session that leads into its close day (20:15 ET the "
        "prior weekday) and price off that day's SPXW expiry all night. Quotes are pulled across the "
        "09:25-09:30 ET handover and the overnight session's first 2 minutes (KXINX/sessions.py). IB streamed "
        "the next-day chain from 20:15 ET on 2026-10-20 (~110 ticks/s, 26 lines).",
        apply=APPLY_NEXT_EVENT)
    scope_margin_buckets: int = param(
        "Buckets subscribed beyond max_markets on each side, so the traded set can drift without "
        "leaving the priced region (2026-10-08: KXINX 7 buckets = 48 IB lines, KXINXU 13 = 30). "
        "The hub subscribes the union of every series' scope.", min=0, step=1, apply=APPLY_NEXT_EVENT)
    rescope_drift_buckets: int = param(
        "Re-centre this series' scope when the 50/50 bucket moves this far (KXINX at 1 "
        "ping-ponged every ~60s on a boundary).", min=1, step=1)
    rescope_check_s: float = param("How often to check for drift.", min=1, step=1)
    monotone_levels: bool = param(
        "Force P(S > K) non-increasing across the ladder before differencing into buckets. Off, "
        "noisy levels make some bucket mids negative, the clip adds mass, and KXINX buckets summed "
        "to ~1.05 live (2026-10-12 15:50-16:00 ET).")
    round_decimals: int = param(
        "Dispatch a market only when its (prob, lo, hi) moves at this rounding.", min=1, max=6, step=1)
    noise_jump: float = param(
        "Spike filter: a move this large is a candidate spike. Calibrated on a real session: clean "
        "buckets spiked <=4% of ticks, deep-ITM/far-OTM 17-25%.", min=0, step=0.005)
    noise_revert_frac: float = param("...if the next tick retraces at least this fraction of it.",
                                     min=0, max=1, step=0.05)
    noise_max_rate: float = param("Drop a market whose spike rate exceeds this.", min=0, max=1, step=0.01)
    noise_window: int = param("Ticks of history the spike rate is measured over.", min=10, step=10)
    noise_min_samples: int = param(
        "A market is untradeable until it has this many ticks (2026-08-28 KXINXU took 3 lots on a "
        "22ms band collapse at 35 ticks seen).", min=1, step=5)


@section
class SpxConfig:
    quoting: QuotingParams
    trading_time: TradingTimeParams
    risk: RiskParams
    runtime: RuntimeParams
    lip: LipSettings
    pricing: SpxPricingParams


@section
class HubConfig:
    """The shared pricing process: one IB feed and one curve fit per SPXW
    expiry for every S&P series."""
    port: int = param("Localhost TCP port the series connect to.", min=1024, max=65535, step=1,
                      apply=APPLY_NEXT_EVENT, editable=False)
    ib_client_id_offset: int = param(
        "Added to keys.IB_CLIENT_ID for the hub's one IB connection.", min=0, step=1,
        apply=APPLY_NEXT_EVENT)
    ib_reconnect_delay_s: float = param("Pause before reconnecting a dropped IB session.", min=0.5,
                                        step=0.5, apply=APPLY_NEXT_EVENT)
    hold_baseline_s: float = param(
        "Option spread-blowout hold: trailing window for the median spread. Same as the backtest "
        "(3min).", min=10, step=10, apply=APPLY_NEXT_EVENT)
    hold_multiplier: float = param(
        "Hold a contract's last quote while its spread exceeds this x the median (2026-10-08 KXINX: "
        "98 >3x episodes/min drove one band to [0.12, 0.53]).", min=1, step=0.5, apply=APPLY_NEXT_EVENT)
    hold_settle_s: float = param("Spread must be normal this long before the hold releases.",
                                 min=0, step=0.05, apply=APPLY_NEXT_EVENT)
    hold_min_samples: int = param("Samples before the median is trusted (backtest min_periods).",
                                  min=1, step=1, apply=APPLY_NEXT_EVENT)
    hold_sample_s: float = param("One baseline sample per contract per this (the backtest's grid step).",
                                 min=0.1, step=0.5, apply=APPLY_NEXT_EVENT)
    combined_max_loss_usd: float | None = param(
        "Worst-case settlement loss allowed across ALL S&P positions together (they settle on the "
        "same close). Past it, every side that would add to that worst case is blocked; sides that "
        "reduce it stay open. null = off: only each series' own risk.max_loss_usd applies (the worst "
        "case is still computed and reported).", min=0, step=10)
    risk_check_s: float = param("How often the combined risk is recomputed.", min=0.1, step=0.5)
