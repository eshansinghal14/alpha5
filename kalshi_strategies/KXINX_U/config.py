"""KXINX / KXINXU config schema. Values: config.json in this folder, one
complete section per series ticker."""
from __future__ import annotations

import os

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section
from kalshi_infra.trading.params import (CloseParams, FeeParams, KalshiWsParams, LipSettings,
                                         QuotingParams, RiskParams, RuntimeParams)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


@section
class SpxPricingParams:
    event_suffix: str = param(
        "Only trade events whose ticker ends with this (H1600 = the 16:00 ET daily close). SPXW "
        "0DTE options settle 16:00 ET, so pricing an intraday hourly off them applies the wrong "
        "horizon.", apply=APPLY_NEXT_EVENT)
    event_open_et: str = param(
        "HH:MM ET before which a new event is not opened (SPXW 0DTE isn't listed earlier and IB "
        "qualification fails).", apply=APPLY_NEXT_EVENT)
    ib_client_id_offset: int = param(
        "Added to keys.IB_CLIENT_ID. Each concurrent series needs its own clientId -- a duplicate "
        "disconnects the older.", min=0, step=1, apply=APPLY_NEXT_EVENT)
    scope_margin_buckets: int = param(
        "Buckets subscribed beyond max_markets on each side, so the traded set can drift without "
        "leaving the priced region (2026-10-08: KXINX 7 buckets = 48 IB lines, KXINXU 13 = 30).",
        min=0, step=1, apply=APPLY_NEXT_EVENT)
    rescope_drift_buckets: int = param(
        "Re-centre the IB subscription when the 50/50 bucket moves this far (KXINX at 1 "
        "ping-ponged every ~60s on a boundary).", min=1, step=1)
    rescope_check_s: float = param("How often to check for drift.", min=1, step=1)
    round_decimals: int = param(
        "Dispatch a market only when its (prob, lo, hi) moves at this rounding.", min=1, max=6, step=1)
    hold_baseline_s: float = param(
        "Option spread-blowout hold: trailing window for the median spread. Same as the backtest "
        "(3min).", min=10, step=10)
    hold_multiplier: float = param(
        "Hold a contract's last quote while its spread exceeds this x the median (2026-10-08 KXINX: "
        "98 >3x episodes/min drove one band to [0.12, 0.53]).", min=1, step=0.5)
    hold_settle_s: float = param("Spread must be normal this long before the hold releases.",
                                 min=0, step=0.05)
    hold_min_samples: int = param("Samples before the median is trusted (backtest min_periods).",
                                  min=1, step=1)
    hold_sample_s: float = param("One baseline sample per contract per this (the backtest's grid step).",
                                 min=0.1, step=0.5)
    ib_reconnect_delay_s: float = param("Pause before reconnecting a dropped IB session.", min=0.5,
                                        step=0.5, apply=APPLY_NEXT_EVENT)
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
    fees: FeeParams
    close: CloseParams
    risk: RiskParams
    runtime: RuntimeParams
    kalshi_ws: KalshiWsParams
    lip: LipSettings
    pricing: SpxPricingParams
