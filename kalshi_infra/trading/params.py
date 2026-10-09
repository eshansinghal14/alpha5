"""
Config sections shared by every strategy. A series' top-level config is a
`section` with these as fields (plus its own `pricing` section); values live
in that series' config.json. See kalshi_infra.config for the loading rules.
"""
from __future__ import annotations

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section


@section
class QuotingParams:
    order_size: float = param("Contracts per take and per resting quote.", min=0.01, step=1.0)
    max_position: float = param(
        "Cap on |yes - no| PER MARKET, on effective position (fills + in-flight + resting). "
        "Lowering it does not unwind, only caps growth.", min=0, step=1.0)
    constant_edge: float = param(
        "Flat $ edge floor on every TAKE. The one term that still bites when the band is tight "
        "and the touch is cheap.", min=0, max=0.5, step=0.005)
    edge_fraction: float = param(
        "Fraction of the distance from fair to the band edge (lo when buying, hi when selling) "
        "demanded as edge. 1.0 = only outside the band.", min=0, max=5, step=0.05)
    lean_multiplier: float = param(
        "Inventory lean as a multiple of that same band distance, scaled by "
        "(effective position / max_position) ** lean_exponent. Only charged to the side that grows "
        "the position.", min=0, max=5, step=0.1)
    lean_dollars: float = param(
        "Flat $ lean at full inventory, same scaling but independent of band width -- the brake "
        "that still works when the model is confident.", min=0, max=0.5, step=0.005)
    lean_exponent: float = param("Lean convexity. 1 = linear, 2 = barely any lean until near the cap.",
                                 min=0.1, max=5, step=0.5)
    fair_clip_eps: float = param(
        "Fair is clipped to [eps, 1-eps] before quoting (logged unclipped).", min=0, max=0.1, step=0.001)
    inflight_timeout_s: float = param(
        "How long a sent take counts against the cap/lean before we assume its fill has been seen. "
        "Must exceed send latency (~34ms measured) plus processing.", min=0.05, max=10, step=0.05)
    take_confirm_s: float = param(
        "A take must stay justified this long before it fires, and fires at most once per window "
        "per side. Guards against one-tick fair jumps; 0 disables (latency strategies).",
        min=0, max=10, step=0.05)
    max_markets: int | None = param(
        "Trade only the N markets with fair closest to 0.5 (null = all). Data/order-rate limit, "
        "not an edge filter.", min=1, step=1)
    atm_hysteresis: int = param(
        "A market in the traded set stays until it ranks below max_markets + this, so the edge "
        "markets don't flip on noise.", min=0, step=1)

    enable_quoting: bool = param("Rest quotes as well as taking. Off withdraws existing quotes on "
                                 "each market's next decision.")
    quoting_edge: float = param(
        "Flat $ floor for a RESTING quote, in place of constant_edge (CI and lean are shared).",
        min=0, max=0.5, step=0.005)
    max_improve_ticks: int | None = param(
        "Max ticks a quote may sit inside the near touch (null = uncapped). Caps edge handed over "
        "in a wide market.", min=0, step=1)
    requote_improve_ticks: float = param(
        "Ticks of improvement needed before cancel/replacing a still-acceptable quote; replacing "
        "forfeits queue priority.", min=0, step=1)
    min_quote_life_s: float = param(
        "A quote younger than this is only replaced if fair crosses it. Throttles order rate when "
        "fair ticks often (SPX: 41/s -> 8/s at 2s). 0 disables.", min=0, max=30, step=0.5)
    requote_delay_s: float = param(
        "After our quote on a (market, side) is hit, that side stays unquoted this long.",
        min=0, max=120, step=0.5)


@section
class FeeParams:
    taker_fee_multiplier: float = param(
        "Series taker fee multiplier in Kalshi's p(1-p) formula. Per-series fact; check the fee "
        "schedule.", min=0, max=2, step=0.01, apply=APPLY_NEXT_EVENT)
    maker_fee_multiplier: float = param(
        "Series maker fee multiplier (0 on most series).", min=0, max=2, step=0.01,
        apply=APPLY_NEXT_EVENT)
    taker_fee_edge_multiplier: float = param(
        "Multiple of the taker fee added to the take edge. 1.0 = breakeven after fees.",
        min=0, max=5, step=0.1)


@section
class CloseParams:
    last_phase_s: float = param(
        "Seconds before close at which the last-phase rules below apply. 0 disables.", min=0, step=10)
    last_phase_max_half_band: float | None = param(
        "In the last phase, pull a market whose band half-width exceeds this (null = never).",
        min=0, max=1, step=0.01)
    last_phase_otm_fair_max: float | None = param(
        "In the last phase, keep only markets whose fair is within this of 0 or 1 (null = keep all).",
        min=0, max=0.5, step=0.001)
    real_trading_cutoff_s: float = param(
        "Disarm REAL trading this long before close (paper continues). Re-arms next event if the "
        "dashboard toggle is still on. 0 disables.", min=0, step=10)
    close_buffer_s: float = param(
        "Stop sending any order this long before close (venue about to reject).", min=0, step=1)


@section
class RiskParams:
    max_loss_usd: float = param(
        "Halt new orders once session PnL reaches -this on the worse of touch/fair marking.",
        min=0, step=5)
    max_signal_age_s: float = param(
        "Pull quotes and stop taking when the fair's newest input is older than this.",
        min=0.01, max=60, step=0.05)
    near_expiry_s: float = param("Window before close where the tighter age limit applies. 0 disables.",
                                 min=0, step=10)
    max_signal_age_near_expiry_s: float = param("Fair age limit inside near_expiry_s.",
                                                min=0.01, max=60, step=0.05)
    fresh_recovery_s: float = param(
        "After a stale episode, fair must stay fresh this long before quotes return (no flapping).",
        min=0, max=60, step=0.5)
    max_book_age_s: float = param("Refuse to fill/rest against a Kalshi book with no tick in this long.",
                                  min=1, step=5)
    shadow_send_s: float = param(
        "Shadow-mode decision->exchange delay, so paper fills aren't more optimistic than real. "
        "0.034s measured warm (kalshi_backtest.execution.MEASURED_SEND_S).", min=0, step=0.005)
    max_consecutive_order_errors: int = param(
        "Real-order errors in a row before halting -- that's auth/balance/market state, not a "
        "moved touch.", min=1, step=1)
    bias_threshold: float = param(
        "Systematic-mispricing guard: median $ outside the touch, same direction across the "
        "ladder, that blocks trading. Set above ordinary edge (2026-08-28 basis error was ~3.2c).",
        min=0, max=0.5, step=0.005)
    bias_min_markets: int = param("Markets outside the touch needed before the bias guard can trip.",
                                  min=1, step=1)
    bias_same_side_fraction: float = param("Fraction of those that must lean the same way.",
                                           min=0.5, max=1, step=0.05)
    max_orders_per_s: float = param(
        "Client-side cap on REAL orders/s for this series. Kalshi's limit is per account -- when "
        "running several series these must sum below it.", min=0.1, step=0.5,
        apply=APPLY_NEXT_EVENT)
    order_timeout_s: float = param("HTTP timeout on order requests.", min=0.5, step=0.5,
                                   apply=APPLY_NEXT_EVENT)


@section
class RuntimeParams:
    reprice_interval_s: float = param("Pricing loop cadence.", min=0.005, step=0.01, apply=APPLY_NEXT_EVENT)
    flush_interval_s: float = param("summary.json cadence (dashboard poll).", min=0.5, step=0.5)
    heavy_flush_every: int = param("Every Nth flush also rewrites the parquet/chart files.", min=1, step=1)
    max_debug_rows: int | None = param(
        "In-memory decision log cap; oldest half is thinned past it (null = unbounded).", min=1000, step=1000)
    audit_interval_s: float = param("Position audit against Kalshi while armed.", min=1, step=1)
    quote_fill_poll_s: float = param(
        "Fills-endpoint poll for resting quotes; must stay within the audit's one-interval "
        "tolerance for resting size.", min=0.2, step=0.1)
    control_poll_s: float = param("config.json / trading_mode.json poll.", min=0.2, step=0.1)
    rollover_poll_s: float = param("Idle poll for the next event.", min=1, step=5)
    rollover_fast_poll_s: float = param("Poll right after an event ends.", min=0.5, step=0.5)
    rollover_fast_window_s: float = param("How long after an event ends the fast poll applies.",
                                          min=0, step=10)
    close_check_s: float = param("How often to check whether the event has closed.", min=0.5, step=0.5)
    close_grace_s: float = param("Keep running this long after close so the final snapshot lands.",
                                 min=0, step=1)


@section
class KalshiWsParams:
    resync_interval_s: float = param(
        "Reconnect this often for fresh snapshots (deltas drift; must stay < 1800s book age guard).",
        min=60, max=1799, step=60, apply=APPLY_NEXT_EVENT)
    reconnect_delay_s: float = param("Pause before reconnecting.", min=0.1, step=0.5, apply=APPLY_NEXT_EVENT)
    recv_timeout_s: float = param("recv() wait before re-checking the resync timer.", min=1, step=1,
                                  apply=APPLY_NEXT_EVENT)
    open_timeout_s: float = param("Websocket connect timeout.", min=1, step=1, apply=APPLY_NEXT_EVENT)


@section
class LipSettings:
    enabled: bool = param("Measure LIP accrual for markets with an active program.")
    period_reward_divisor: float = param(
        "API period_reward -> dollars. Unit undocumented; centi-cents assumed (200000 -> $20, "
        "2026-10-06). Reconcile against a real payout.", min=1, apply=APPLY_NEXT_EVENT)
    snapshots_per_period: int = param("Kalshi scores once per second: period length in seconds.",
                                      min=1, apply=APPLY_NEXT_EVENT)


SHARED_SECTIONS = ("quoting", "fees", "close", "risk", "runtime", "kalshi_ws", "lip")


def check_strategy_config(cfg) -> None:
    """A series config must carry every shared section under these names."""
    missing = [s for s in SHARED_SECTIONS if not hasattr(cfg, s)]
    if missing:
        raise TypeError(f"{type(cfg).__name__} is missing shared config sections: {missing}")
