"""
Config for the Coin Race market maker.

Everything the strategy may vary at runtime lives in `CoinRaceConfig` --
one dataclass, following the project convention from
`kalshi_latency_arbs/spxw_kxinx/sp_options_lag_quoting.py`'s QuotingConfig.
Fields the dashboard is allowed to tune at runtime are listed in
`EDITABLE_FIELDS` so viz/live config-override polling knows the whitelist.

Several numeric knobs here (sigma_feed and kappa in the original spec)
were deliberately dropped: the user's build decision is to reuse the SPX
strategy's edge model (constant_edge + CI-width-driven edge + fee + lean)
rather than carry a separate adverse-selection noise term. The LIP credit
is subtracted from the required edge, floored at zero, exactly as the
spec in Section 6.2 describes.
"""
import dataclasses

from .series import TICK, COINS


# ---------------------------------------------------------------------------
# Static per-coin parameters -- CF Spot Rate methodology (CF Spot Rate
# Methodology Guide, Sept 2026). These are the parameters CF uses to decide
# which venue books contribute each second. Pulled from the methodology
# table; must be re-verified on startup against the live PDF.
# ---------------------------------------------------------------------------

# Staleness threshold per coin, in milliseconds. HYPE is explicitly 30s in
# the methodology guide; BTC/ETH/SOL/XRP use the shorter default. Making
# this per-coin configurable so a future HYPE-specific tune doesn't drag
# the others along.
STALENESS_MS_DEFAULT = {
    "BTC": 10_000,
    "ETH": 10_000,
    "SOL": 10_000,
    "XRP": 10_000,
    "HYPE": 30_000,   # spec section 2.1, step 2
}

# Deviation-from-mid parameter for the "potentially erroneous" venue check.
# Fraction, not bps. 10% for HYPE per the methodology; keeping the others
# conservative at 2% until the actual table entries are re-verified.
MID_DEVIATION_FRACTION = {
    "BTC": 0.02,
    "ETH": 0.02,
    "SOL": 0.02,
    "XRP": 0.02,
    "HYPE": 0.10,
}

# Deviation-from-mid parameter for utilized-depth calculation (step 4). 1%
# for HYPE per the guide.
UTILIZED_DEPTH_DEVIATION = {
    "BTC": 0.005,
    "ETH": 0.005,
    "SOL": 0.005,
    "XRP": 0.005,
    "HYPE": 0.01,
}


# ---------------------------------------------------------------------------
# Fee multiplier for KXCRYPTOLEAD15M.
#
# The series metadata reports `fee_type: quadratic, fee_multiplier: 1`,
# which is a per-series multiplier different from KXINX/KXINXU's advertised
# general 0.07. User explicitly requested hardcoding 1 for this strategy
# rather than exposing it as a knob. Scoped to this module ONLY -- the
# kalshi_backtest.core.kalshi_fee() default stays 0.07 so the SPX strategies
# are not silently repriced.
# ---------------------------------------------------------------------------

CRYPTO_FEE_MULTIPLIER = 1.0
# Maker fee is zero on the published Kalshi schedule for most series. Keep
# as a module constant that can be adjusted if KXCRYPTOLEAD15M ever lists
# a maker fee; recheck once per-series.
CRYPTO_MAKER_FEE_MULTIPLIER = 0.0


@dataclasses.dataclass
class CoinRaceConfig:
    # -------------------- Monte Carlo pricer --------------------
    # Fairs backend: 'mc' is production. Analytic exists in tests only.
    pricer_backend: str = "mc"
    # Common-random-number count. 20k gets ~0.3% stderr on a corner tail
    # (p ~ 0.05) and runs in ~1-2 ms with vectorised numpy at 5 coins.
    mc_n: int = 20_000
    mc_seed: int = 20260815

    # Shrinkage toward the base rate. OFF: with the corrected vol it capped
    # the leader at 0.88 and floored trailing coins at 0.03, costing ~0.09
    # nats in the last minute on the 2-day held-out validation
    # (rv_modeling/vol_fix_reference/README.md). The t mixture below handles
    # overconfidence without pinning theo away from 0 and 1.
    shrinkage_lambda: float = 0.0
    # Rolling window (# of past settled events) used to form the base-rate
    # vector. Only matters if shrinkage_lambda > 0.
    base_rate_window: int = 500

    # Student-t fat-tail mixture, UNIT variance (only the tails change).
    # nu=5 from the notebook's excess kurtosis 3-5; slightly better than a
    # normal 1-7 min out on validation, about equal elsewhere.
    t_nu: float | None = 5.0

    # Multiplier on the forecast variance rate. 1.0 in production; a knob
    # for the implied-k diagnostic and for stress tests.
    vol_mult: float = 1.0

    # Clip for QUOTING only -- the raw probability still sums to 1 and is
    # what gets logged. Any coin whose fair sits outside [eps, 1-eps] after
    # shrinkage is clipped to it before being handed to the quoter.
    quote_clip_eps: float = 0.001

    # -------------------- Variance-clock inputs --------------------
    # Averaging-window length that bakes into the "end" average -- 60s per
    # the contract. Keeping as config rather than hardcoding because a
    # future series variant could change it.
    end_avg_s: float = 60.0
    start_avg_s: float = 60.0

    # Pre-open quoting switch (section 4.2 note on the start-TWAP phase).
    # False means "don't quote during [S-60, S)" where the start average is
    # mid-formation; the pricer code still handles that interval, it's just
    # not exposed to the market until the start TWAP is locked.
    quote_during_start_avg: bool = False

    # Reaction latency used to scale theo_vol (section 4.6). 0.05s is the
    # SPX live loop's dispatch-to-arrival envelope and a conservative
    # placeholder here until a Coinbase-WS-to-order-arrival round trip is
    # measured specifically for this stack.
    reaction_latency_s: float = 0.05

    # Kill switch: if the newest venue book under the fair is older than
    # this (monotonic seconds), pull the event's quotes and don't take.
    # The fair itself is recomputed on every wakeup, so this only fires
    # when the PRICE FEED (or this loop) is behind. Coinbase sends ~8-14
    # msg/s per product (2026-10-06 probe), so 1.5s without any book on any
    # coin means the stream or the loop has stalled; HYPE alone can be
    # quiet for a second, hence max over coins.
    theo_max_age_s: float = 1.5
    # Tighter staleness bound in the last `near_expiry_s` of the event,
    # where a delayed theo costs the most (2026-10-08 ...1300: theo
    # 5-10s behind the market at 110-90s left).
    near_expiry_s: float = 120.0
    theo_max_age_near_expiry_s: float = 0.75
    # After a stale episode, the feed must be continuously fresh for this
    # long before quotes go back up (no flapping requotes on a stuttering
    # feed).
    fresh_recovery_s: float = 2.0
    # Cadence of the bump deltas / theo_vol (edge sizing only).
    delta_refresh_s: float = 0.5
    # Minutes before expiry at which REAL trading is switched off for the
    # event (resting real orders cancelled). Paper/shadow trading continues
    # to the close. 0 disables. Re-arms automatically on the next event if
    # the dashboard toggle is still on.
    real_trading_cutoff_min: float = 0.0
    # Max gap between the newest and oldest coin's latest book before the
    # fair is withheld. Each product updates every ~60ms (15-19 msg/s,
    # 2026-10-08 probe; max quiet gap 0.3-0.8s), so a >1s spread means one
    # coin has resumed after a stall and the others haven't.
    max_cross_coin_skew_s: float = 1.0
    # Takes need a fair that has held for this long: a single-tick jump
    # (one coin updating before the others) must not trigger a cross.
    take_confirm_s: float = 0.75

    # -------------------- Edge/quoting model --------------------
    # Mirrors QuotingConfig.constant_edge: a flat $ floor on both sides,
    # the one term that doesn't scale with anything (see sp_options_lag_
    # quoting.py's constant_edge docstring for the full rationale). 1c =
    # Kalshi's tick -- the smallest floor that can push a reservation
    # across a real price level.
    constant_edge: float = 0.01
    # Fraction of (hi - prob) / (prob - lo) that must be covered on each
    # side. 1.0 = all the way to the bound. Equivalent to QuotingConfig
    # .edge_fraction; see its comment there.
    edge_fraction: float = 1.0
    # The confidence interval here is a model-driven half-width scaled by
    # theo_vol (which already captures expected one-sigma theo move over
    # `reaction_latency_s`). This is the multiplier for that scaling.
    # Half-width = ci_vol_multiplier * theo_vol.
    ci_vol_multiplier: float = 1.5

    # Inventory lean (SPX style, 5-leg aware). lean_dollars is the flat
    # floor priced to the side that is adding to inventory; lean_multiplier
    # scales the CI contribution.
    lean_multiplier: float = 1.0
    lean_dollars: float = 0.02
    lean_exponent: float = 2.0

    # Resting-quote-only extras.
    enable_quoting: bool = True
    # Edge demanded on resting quotes in place of constant_edge. 0 for this
    # strategy (operator decision 2026-10-08): resting quotes are bounded by
    # fair minus the CI/lean terms only; LIP and the level-selection EV
    # (which charges adverse_edge on fills) decide where in that range.
    quoting_edge: float = 0.0
    quote_size: float = 1.0

    # Max ticks a quote may push inside the near touch -- defensive against
    # being the only level inside the spread (see QuotingConfig
    # .max_improve_ticks).
    max_improve_ticks: int | None = 1
    requote_improve_ticks: float = 2.0
    requote_delay_s: float = 1.0

    # In-flight timeout. Same meaning as QuotingConfig.inflight_timeout_s.
    inflight_timeout_s: float = 0.5

    # -------------------- Inventory limits --------------------
    # Per-MARKET cap on |yes_qty - no_qty|. The 5 markets of one event sum
    # to a known one-hot payoff, so the per-event risk cap below is the
    # binding one in practice; this is a per-leg safety floor. Named
    # `max_position` (not `_per_market`) to match the SPX QuotingConfig
    # convention -- the viz/dashboard reads `.max_position` on every
    # strategy config.
    max_position: float = 10.0
    # Soft cap on worst-case event P&L. Dollars. Used by the risk check in
    # quoter.py to block any new order whose fill would push a theoretical
    # worst-case leg over this.
    max_event_loss_usd: float = 200.0

    # -------------------- Time-phase policy --------------------
    # "Last-N" seconds where the quoter switches to tight-OTM-only mode.
    last_phase_s: float = 150.0
    # Theo-vol threshold above which a leg is pulled during the last phase.
    last_phase_theo_vol_pull: float = 0.08
    # Max fair for a leg to still carry a 1c YES bid during the last phase
    # (the "far-out-of-the-money LIP presence" idea from section 6.4).
    last_phase_otm_fair_max: float = 0.005

    # -------------------- LIP --------------------
    # Discount factor: 0.5 per observed `discount_factor_bps=5000` on this
    # series. Discovered from /incentive_programs at startup; this is the
    # pricing default if the API query fails.
    lip_discount_factor_default: float = 0.5
    # Target size per side (contracts). Observed `target_size_fp=1000` on
    # the series. Discovered from the API at startup.
    lip_target_size_default: float = 1000.0
    # Period reward conversion: Kalshi's API field is in a unit that is NOT
    # confirmed. 2026-10-06 best-guess: centi-cents (so 200000 -> $20).
    # This is the ONLY place that conversion lives; reconcile against real
    # payouts and update once per period until we have a real payout log.
    lip_period_reward_divisor: float = 10_000.0
    # Snapshots per 15-minute period. Spec says "once per second at a
    # random time within the second" -- 900 snapshots per 900-second
    # window. Verified against the help page math example (which checks
    # out at 100 snapshots / $100 reward = $1/snapshot => $16 at 20%
    # score x 8000/10000 non_excluded fraction).
    lip_snapshots_per_period: int = 900

    # -------------------- Feed health / kill switch --------------------
    #
    # Two-threshold staleness model. The Coin Race winner is a JOINT
    # comparison across all 5 coins, so a missing coin makes EVERY fair
    # unknowable (you can't marginalise out the coin you have no data
    # for). But a crypto mid that is a few seconds old is still a fine
    # input to a multi-minute-horizon ranking -- the price barely moves.
    # So we distinguish:
    #
    #   SOFT stale (<= composite_soft_stale_s): keep the coin's
    #     last-known composite price and keep pricing the whole event.
    #     This is normal -- CF publishes ~1/s and a one-tick blip or a
    #     quiet HYPE second must not invalidate anything.
    #
    #   HARD stale (> composite_hard_stale_s) on ANY coin, OR no data at
    #     all for a coin this event: the joint is unknowable, so the
    #     event's theo is marked INVALID and every resting leg is pulled
    #     (the spec's section 2.1 kill switch).
    #
    # Between soft and hard a coin is "degraded": still priced off its
    # last value, but the pricer already widens its variance so the fair
    # drifts toward the base rate. HYPE's 30s methodology staleness
    # bound (config.STALENESS_MS_DEFAULT) is the natural hard bound for
    # it; the others use the shorter default below.
    composite_soft_stale_s: float = 3.0
    composite_hard_stale_s: float = 15.0
    # How long the feed must stay healthy before we requote (section 2.1
    # "do not requote until the book has been healthy for N seconds").
    composite_recovery_s: float = 5.0
    # Legacy single threshold, kept for the quoter's per-leg pull. Equal
    # to the soft bound: a leg whose own feed is soft-stale is still
    # quoted (off last price); only hard-stale pulls it, which the event
    # invalidation above already handles.
    composite_staleness_pull_s: float = 3.0

    # -------------------- Phase 1 flags --------------------
    # In phase 1 only Coinbase is wired. The spec promises these can be
    # flipped to true once Kraken/Bitstamp adapters are added.
    enable_kraken: bool = False
    enable_bitstamp: bool = False

    # Fallback-median spot used ONLY when the CF-style rate pipeline
    # cannot produce an output this second (e.g. one venue, impossibly
    # thin book). The spec calls for logging the fallback-vs-CF-style
    # difference; see cf_rate.CFSpotRate.
    allow_fallback_median: bool = True


# Fields the dashboard is allowed to overwrite at runtime via
# `data/live/KXCRYPTOLEAD15M/config_override.json`. Narrow by design:
# anything NOT in this set is baked in at session start and only moves on a
# process restart. Mirrors the pattern used by live_persist.CONFIG_WRITE_
# WHITELIST for KXINX/KXINXU.
EDITABLE_FIELDS = frozenset({
    "constant_edge", "edge_fraction", "ci_vol_multiplier",
    "lean_multiplier", "lean_dollars", "lean_exponent",
    "enable_quoting", "quoting_edge", "quote_size",
    "max_improve_ticks", "requote_improve_ticks", "requote_delay_s",
    "max_position", "max_event_loss_usd",
    "last_phase_s", "last_phase_theo_vol_pull", "last_phase_otm_fair_max",
    "quote_during_start_avg", "shrinkage_lambda", "quote_clip_eps",
    "real_trading_cutoff_min",
})


__all__ = [
    "CoinRaceConfig", "EDITABLE_FIELDS",
    "CRYPTO_FEE_MULTIPLIER", "CRYPTO_MAKER_FEE_MULTIPLIER",
    "STALENESS_MS_DEFAULT", "MID_DEVIATION_FRACTION",
    "UTILIZED_DEPTH_DEVIATION",
]
