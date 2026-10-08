"""
Level-selection and edge arithmetic for the Coin Race quoter.

Section 6.2 of the spec: the required edge is the SPX quoter's edge
model, minus LIP credit earned at that level. We reuse the same
constant_edge + CI_edge + lean_edge decomposition that
sp_options_lag_quoting.py uses, with two differences:

  1. "CI" here is not a model confidence interval -- the pricer doesn't
     ship one -- but a theo-vol-scaled edge:
         ci_edge_bid = edge_fraction * ci_vol_multiplier * theo_vol[coin]
     and symmetric on the ask. This replaces `sigma_feed` and `kappa`
     per the user's design: the SPX template's CI terms already serve
     the adverse-selection-buffer role.
  2. The LIP CREDIT is subtracted from the required edge (floored at
     zero), making it possible to rest at or slightly inside fair when
     the LIP reward per contract filled exceeds the raw edge demand.
     The floor at zero is strict: we never bid above fair just because
     LIP is paying us -- the ADVERSE-SELECTION BUFFER sets the floor.

The decision for one market, one side, is:

    reservation_bid = prob - (constant_edge + ci_edge_bid + lean_bid + fee_bid)
                            + lip_credit_bid
    reservation_bid = min(reservation_bid, fair - adverse_buffer)
    # (never above fair - adverse_buffer, section 6.2 "max_bid")

    place YES bid at = floor(reservation_bid / TICK) * TICK
    (first tick OUTSIDE the reservation)

Symmetric on the ask.

5-leg event-level constraint (section 6.3): sum of our YES bids over
the 5 markets must stay < $1 (and sum of our YES asks > $1), otherwise
a counterparty can lock in a risk-free profit by hitting us on all 5.
This is enforced in `apply_event_constraints`.

Everything here is PURE. The strategy is in strategy.py; this module
exposes functions it calls.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np

from .config import (CoinRaceConfig, CRYPTO_FEE_MULTIPLIER,
                     CRYPTO_MAKER_FEE_MULTIPLIER)
from .lip_scorer import LipParams, LipLevel, marginal_reward_rate
from .series import COINS, TICK, MIN_QUOTE_PRICE, MAX_QUOTE_PRICE


def crypto_fee(price: float, contracts: float = 1.0,
               fee_multiplier: float = CRYPTO_FEE_MULTIPLIER) -> float:
    """Kalshi's fee formula with the KXCRYPTOLEAD15M multiplier
    hardcoded via config.CRYPTO_FEE_MULTIPLIER. Mirrors
    kalshi_backtest.core.kalshi_fee shape -- `price * (1 - price)` is
    the quadratic kernel, with ceil-to-hundredth-of-a-cent."""
    if contracts <= 0:
        return 0.0
    raw = fee_multiplier * contracts * price * (1.0 - price)
    return math.ceil(raw * 10000.0) / 10000.0


@dataclasses.dataclass
class EdgeDecomposition:
    """Everything that went into the quoted level, both so the
    dashboard can show the stack and so test cases can diff individual
    terms against hand calculations.

    There are TWO edge totals per side, because a crossing (taker) order
    and a resting (maker) quote face different costs on this series:

      * `bid_edge_total` / `ask_edge_total` -- the TAKER edge. Includes
        the taker fee (`fee_bid`/`fee_ask`); used for the crossing test.
      * `maker_bid_edge_total` / `maker_ask_edge_total` -- the MAKER edge
        for a resting quote. Uses the MAKER fee (0 on KXCRYPTOLEAD15M --
        a resting quote pays nothing to be filled) in place of the taker
        fee. This is what the resting-quote reservation must use;
        charging a resting quote a taker fee it will never pay pushed
        quotes needlessly far from fair.

    LIP credit is subtracted from BOTH, floored at `adverse_buffer`.
    """
    fair: float
    constant_edge: float
    ci_edge_bid: float
    ci_edge_ask: float
    lean_bid: float
    lean_ask: float
    fee_bid: float                    # TAKER fee at the touch
    fee_ask: float
    maker_fee_bid: float              # MAKER fee for a resting quote (0 on this series)
    maker_fee_ask: float
    lip_credit_bid: float             # dollars we'd expect to earn in LIP from one filled contract
    lip_credit_ask: float
    adverse_buffer_bid: float         # the max_bid / min_ask hard floor (section 6.2)
    adverse_buffer_ask: float
    bid_reservation: float            # TAKER reservation (crossing test)
    ask_reservation: float
    bid_edge_total: float             # TAKER bid-side required edge net of LIP credit
    ask_edge_total: float
    maker_bid_edge_total: float       # MAKER (resting-quote) bid-side edge net of LIP credit
    maker_ask_edge_total: float
    maker_bid_reservation: float
    maker_ask_reservation: float
    theo_vol: float


@dataclasses.dataclass
class QuoteDecision:
    """What the strategy should actually do for one (ticker, side)
    pair. `price` is in that SIDE'S own terms (yes bids in yes prices;
    no bids in no prices). None means "don't quote". `taker_price`
    non-None means "the reservation also cleared the touch, so take
    it" -- strategy.py handles the actual send."""
    quote_bid_price: float | None     # YES bid, yes-side price
    quote_ask_price: float | None     # NO bid, no-side price (= 1 - yes-ask-side price)
    taker_bid_price: float | None     # ask you'd cross to buy yes
    taker_ask_price: float | None     # bid you'd cross to sell yes (as no-buy)
    decomposition: EdgeDecomposition
    reasons: dict[str, str]           # per-side human-readable why / why-not


# ---------------------------------------------------------------------------
# Edge arithmetic for one market, one side
# ---------------------------------------------------------------------------

def decompose_edge(
    fair: float,
    theo_vol: float,
    *,
    net_position: float,              # yes_qty - no_qty
    eff_yes: float,
    eff_no: float,
    max_pos: float,
    market_bid: float | None,         # yes-side best bid
    market_ask: float | None,         # yes-side best ask
    lip_credit_bid: float,
    lip_credit_ask: float,
    config: CoinRaceConfig,
) -> EdgeDecomposition:
    """One market's edge stack. Does not know anything about Kalshi's
    book-aggregated state; `lip_credit_*` is handed in already in
    dollars-per-contract-filled (converted from marginal_reward_rate
    outside, where the book is known)."""

    cfg = config

    # CI (adverse-selection) edge: scaled by theo_vol, equal on both
    # sides. The SPX template's prob_hi/prob_lo asymmetry has no
    # analogue here; the pricer gives us one number per coin.
    ci_edge_bid = cfg.edge_fraction * cfg.ci_vol_multiplier * theo_vol
    ci_edge_ask = cfg.edge_fraction * cfg.ci_vol_multiplier * theo_vol

    # Inventory lean. Same convex-in-ratio shape as the SPX quoter.
    # Mirrors QuotingConfig.lean_* -- see sp_options_lag_quoting.py:382.
    def _ratio(qty):
        return 0.0 if max_pos <= 0 else max(-1.0, min(1.0, qty / max_pos))
    ratio_bid = _ratio(eff_yes)
    ratio_ask = _ratio(eff_no)
    lean_bid = ((cfg.lean_multiplier * ci_edge_bid + cfg.lean_dollars)
                * max(ratio_bid, 0.0) ** cfg.lean_exponent)
    lean_ask = ((cfg.lean_multiplier * ci_edge_ask + cfg.lean_dollars)
                * max(-ratio_ask, 0.0) ** cfg.lean_exponent)

    # TAKER fee priced at the touch -- what a crossing order pays. If no
    # book yet, zero (we aren't crossing anything).
    fee_bid = crypto_fee(market_ask, 1.0, CRYPTO_FEE_MULTIPLIER) \
              if market_ask is not None else 0.0
    fee_ask = crypto_fee(market_bid, 1.0, CRYPTO_FEE_MULTIPLIER) \
              if market_bid is not None else 0.0
    # MAKER fee for a RESTING quote. On KXCRYPTOLEAD15M this is 0
    # (CRYPTO_MAKER_FEE_MULTIPLIER = 0.0): a resting quote pays nothing to
    # be filled, so the resting-quote edge must NOT carry the taker fee.
    maker_fee_bid = crypto_fee(market_ask, 1.0, CRYPTO_MAKER_FEE_MULTIPLIER) \
                    if market_ask is not None else 0.0
    maker_fee_ask = crypto_fee(market_bid, 1.0, CRYPTO_MAKER_FEE_MULTIPLIER) \
                    if market_bid is not None else 0.0

    # Adverse buffer == the lowest edge the quoter would ever demand
    # even if LIP paid an unlimited credit. constant_edge is that floor
    # (identical role to the SPX quoter's constant_edge).
    adverse_buffer_bid = cfg.constant_edge
    adverse_buffer_ask = cfg.constant_edge

    # Required edge BEFORE LIP credit (dollars we demand). The common
    # part (buffer + CI + lean) is shared; only the fee differs between
    # the taker and maker variants.
    common_bid = adverse_buffer_bid + ci_edge_bid + lean_bid
    common_ask = adverse_buffer_ask + ci_edge_ask + lean_ask

    # LIP credit, floored to NOT eat into the adverse buffer. Spec:
    # "we never bid above max_bid, regardless of LIP". max_bid =
    # fair + LIP_value - adverse_buffer; i.e. the bid-side edge never
    # goes BELOW adverse_buffer_bid even with infinite LIP. Applied to
    # BOTH the taker and maker edges -- LIP is earned on any resting size,
    # which includes a quote we rest.
    bid_edge_total = max(common_bid + fee_bid - lip_credit_bid, adverse_buffer_bid)
    ask_edge_total = max(common_ask + fee_ask - lip_credit_ask, adverse_buffer_ask)
    maker_bid_edge_total = max(common_bid + maker_fee_bid - lip_credit_bid, adverse_buffer_bid)
    maker_ask_edge_total = max(common_ask + maker_fee_ask - lip_credit_ask, adverse_buffer_ask)

    bid_reservation = fair - bid_edge_total
    ask_reservation = fair + ask_edge_total
    maker_bid_reservation = fair - maker_bid_edge_total
    maker_ask_reservation = fair + maker_ask_edge_total

    return EdgeDecomposition(
        fair=fair,
        constant_edge=cfg.constant_edge,
        ci_edge_bid=ci_edge_bid, ci_edge_ask=ci_edge_ask,
        lean_bid=lean_bid, lean_ask=lean_ask,
        fee_bid=fee_bid, fee_ask=fee_ask,
        maker_fee_bid=maker_fee_bid, maker_fee_ask=maker_fee_ask,
        lip_credit_bid=lip_credit_bid, lip_credit_ask=lip_credit_ask,
        adverse_buffer_bid=adverse_buffer_bid,
        adverse_buffer_ask=adverse_buffer_ask,
        bid_reservation=bid_reservation,
        ask_reservation=ask_reservation,
        bid_edge_total=bid_edge_total,
        ask_edge_total=ask_edge_total,
        maker_bid_edge_total=maker_bid_edge_total,
        maker_ask_edge_total=maker_ask_edge_total,
        maker_bid_reservation=maker_bid_reservation,
        maker_ask_reservation=maker_ask_reservation,
        theo_vol=theo_vol,
    )


def desired_quote_level(
    side: str, reservation: float,
    market_bid: float | None, market_ask: float | None,
    *, allowed: bool,
    max_improve_ticks: int | None,
) -> float | None:
    """First tick OUTSIDE the reservation, capped a tick inside the
    opposite touch so a quote never crosses. Mirrors
    sp_options_lag_quoting._desired_quote. Returns yes-side price for
    `side == 'yes'`, no-side price for `side == 'no'`.
    """
    if not allowed:
        return None
    if side == "yes":
        yes_price = math.floor(reservation / TICK + 1e-9) * TICK
        if max_improve_ticks is not None and market_bid is not None:
            yes_price = min(yes_price, market_bid + max_improve_ticks * TICK)
        if market_ask is not None:
            yes_price = min(yes_price, market_ask - TICK)
    else:
        yes_price = math.ceil(reservation / TICK - 1e-9) * TICK
        if max_improve_ticks is not None and market_ask is not None:
            yes_price = max(yes_price, market_ask - max_improve_ticks * TICK)
        if market_bid is not None:
            yes_price = max(yes_price, market_bid + TICK)
    yes_price = round(yes_price, 2)
    if not (MIN_QUOTE_PRICE <= yes_price <= MAX_QUOTE_PRICE):
        return None
    return yes_price if side == "yes" else round(1.0 - yes_price, 2)


# ---------------------------------------------------------------------------
# Level selection: pick the resting price that maximises expected value
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LevelChoice:
    price: float | None          # side's own price terms; None = don't quote
    ev: float                    # expected $ per contract over the horizon
    lip_usd: float               # expected LIP $ per contract over the horizon
    fill_ev: float               # P(fill) * (fair_side - price - adverse)
    p_fill: float
    reference_price: float | None
    candidates: list             # [(price, ev, lip_usd, fill_ev, p_fill)] for the log


def choose_quote_level(
    side: str,
    fair_side: float,
    levels: list[LipLevel],
    yes_levels: list[LipLevel],
    no_levels: list[LipLevel],
    params: LipParams | None,
    *,
    max_price: float,
    adverse_edge: float,
    size: float,
    tau: float,
    fill_model=None,
    max_ticks_scan: int = 12,
) -> LevelChoice:
    """Best resting price for one side, by expected value per contract.

    `side` is "yes" (a YES bid) or "no" (a NO bid); `fair_side` is that
    side's fair value (p for yes, 1-p for no). `levels` are the existing
    bids on this side, highest first (our own size marked as `mine`).
    `max_price` is the highest price we may rest at: one tick inside the
    opposite touch, and no better than best bid + max_improve_ticks.

    For each candidate price P from `max_price` down `max_ticks_scan` ticks,
    scored as a RATE ($/s while the quote is up):

        EV(P) = LIP $/s our order adds at P
              + lambda(P) x (fair_side - P - adverse_edge)

    lambda(P) is our fill intensity at P's queue position. A rate, not a
    one-shot P(fill before tau) x edge: the quote is replenished after every
    fill, so profit accrues per fill. Over a 400s+ horizon P(fill) saturates
    near 1 even 3-4 ticks back, which made the deepest price look best and
    parked quotes far behind the touch below the LIP reference (2026-10-08).

    `adverse_edge` is the edge we demand on a fill (constant + CI + lean).
    LIP is paid on any qualifying size, so it is not netted against it.
    `ev`, `lip_usd`, `fill_ev` on the result are $/s; `p_fill` is P(fill
    within the horizon), for the log only.

    Returns price=None if no candidate has EV > 0.
    """
    lo = round(max_price - max_ticks_scan * TICK, 2)
    cands = []
    ref = None
    if params is not None and params.period_reward_usd > 0:
        from .lip_scorer import score_book
        sc = score_book(yes_levels, no_levels, params)
        ref = (sc.yes if side == "yes" else sc.no).reference_price
    p = round(max_price, 2)
    while p >= max(lo, MIN_QUOTE_PRICE) - 1e-9:
        # Queue position at P.
        ahead = 0.0
        best = levels[0].price if levels else p
        for lv in levels:
            if abs(lv.price - p) < 1e-9:
                ahead = max(0.0, lv.size - lv.mine)
        ticks_from_best = max(0.0, round((best - p) / TICK))
        if fill_model is not None and tau is not None:
            # Base rate looked up at the TOUCH, not at P: depth behind the
            # touch is g()'s job, and the coarse price buckets otherwise let
            # a cheaper bucket outrank a closer level (ETH 2026-10-08: 0.11
            # bucket 0.0155/s vs 0.09 bucket 0.0296/s, so it sat a tick back).
            lam = fill_model.lambda_you(side=side, tau=tau, price=best,
                                        ticks_from_best=ticks_from_best,
                                        queue_ahead=ahead)
            horizon = max(min(tau, float(params.snapshots_per_period) if params else tau), 0.0)
            p_fill = 1.0 - math.exp(-lam * horizon) if lam > 0 else 0.0
        else:
            lam, p_fill = 0.0, 0.0
        lip = 0.0
        if params is not None and params.period_reward_usd > 0:
            lip = marginal_reward_rate(yes_levels, no_levels, params, side=side,
                                       candidate_price=p, candidate_size=size) / size
        fill_ev = lam * (fair_side - p - adverse_edge)
        cands.append((p, lip + fill_ev, lip, fill_ev, p_fill))
        p = round(p - TICK, 2)
    if not cands:
        return LevelChoice(None, 0.0, 0.0, 0.0, 0.0, ref, [])
    # Prefer the LOWEST price whose EV is within `ev_tol` of the best: LIP is
    # flat at/above the reference, so above it the only difference is fill
    # EV off a fill model that knows nothing about who is lifting us. The
    # absolute floor is in $/s: 1e-6 is ~$0.0009 over a 15-min event.
    best_ev = max(c[1] for c in cands)
    ev_tol = max(0.02 * abs(best_ev), 1e-6)
    best_c = min((c for c in cands if c[1] >= best_ev - ev_tol), key=lambda c: c[0])
    if best_c[1] <= 0:
        return LevelChoice(None, best_c[1], best_c[2], best_c[3], best_c[4], ref, cands)
    return LevelChoice(best_c[0], best_c[1], best_c[2], best_c[3], best_c[4], ref, cands)


def max_rest_price(side: str, market_bid: float | None, market_ask: float | None,
                   max_improve_ticks: int | None) -> float | None:
    """Highest price (side's own terms) a resting order may sit at without
    crossing, and no better than best bid + max_improve_ticks."""
    if side == "yes":
        own_best, opp = market_bid, market_ask
    else:
        own_best = None if market_ask is None else round(1.0 - market_ask, 2)
        opp = None if market_bid is None else round(1.0 - market_bid, 2)
    cap = MAX_QUOTE_PRICE
    if opp is not None:
        cap = min(cap, round(opp - TICK, 2))
    if max_improve_ticks is not None and own_best is not None:
        cap = min(cap, round(own_best + max_improve_ticks * TICK, 2))
    return cap if cap >= MIN_QUOTE_PRICE else None


# ---------------------------------------------------------------------------
# LIP credit per filled contract
# ---------------------------------------------------------------------------

def lip_credit_per_contract(
    yes_levels: list[LipLevel],
    no_levels: list[LipLevel],
    params: LipParams,
    *,
    side: str,
    candidate_price: float,
    candidate_size: float,
    tau: float | None = None,
    fill_model=None,                   # FillIntensityModel | None
    ticks_from_best: float = 0.0,
    queue_ahead: float = 0.0,
) -> float:
    """Expected LIP dollars per contract from resting `candidate_size`
    contracts at `candidate_price` on `side`, over the time the order is
    expected to stay unfilled.

    `marginal_reward_rate` gives $/second while the order rests. The order
    does NOT rest the whole remaining period -- it fills at some point --
    so the credit is `rate_per_s * E[time resting before fill]`:

        E[rest] = integral_0^horizon exp(-lambda t) dt = (1 - e^{-lambda*horizon})/lambda

    where `lambda = fill_model.lambda_you(side, tau, price, ticks_from_best,
    queue_ahead)` is our order's fill intensity (fills/sec). The horizon is
    `min(tau, remaining period)`.

    Backward-compatible: if `fill_model` or `tau` is None, falls back to the
    full-period UPPER BOUND (rate_per_s * period_s), the old behaviour. That
    bound over-credits LIP and biases toward aggressive inside quotes, so a
    fitted `fill_model` should be supplied in production (see
    fill_intensity.py / rv_modeling/fit_fill_intensity.py).
    """
    if candidate_size <= 0:
        return 0.0
    per_s = marginal_reward_rate(
        yes_levels, no_levels, params,
        side=side, candidate_price=candidate_price, candidate_size=candidate_size)
    period_s = float(params.snapshots_per_period)

    if fill_model is None or tau is None:
        # Legacy upper bound: assume the order rests the whole period.
        return (per_s * period_s) / candidate_size

    horizon = min(float(tau), period_s)
    lam = fill_model.lambda_you(
        side=side, tau=float(tau), price=candidate_price,
        ticks_from_best=ticks_from_best, queue_ahead=queue_ahead)
    expected_rest_s = fill_model.expected_rest_time_s(lam, horizon)
    return (per_s * expected_rest_s) / candidate_size


# ---------------------------------------------------------------------------
# Event-level consistency (section 6.3)
# ---------------------------------------------------------------------------

def apply_event_constraints(
    quote_bids: dict[str, float | None],   # coin -> yes-side bid, None if unquoted
    quote_asks: dict[str, float | None],   # coin -> no-side bid, None if unquoted
) -> tuple[dict[str, float | None], dict[str, float | None], dict[str, str]]:
    """Make sure the 5 YES bids sum to < 1 and the 5 YES asks sum to
    > 1 (both strict). Returns possibly-adjusted-to-None dicts plus a
    reasons map for the viz.

    Policy: pull the ONE highest-fair bid (or lowest-fair ask) that
    would need the biggest adjustment, until the constraint holds.
    Dropping a quote entirely is safer than silently narrowing it, and
    the dropped market will have its chance when the next composite
    tick arrives.
    """
    reasons: dict[str, str] = {}
    bids = dict(quote_bids)
    asks = dict(quote_asks)

    # yes_asks_yes = 1 - no_bids_no
    def _yes_ask_from_no_bid(no_bid):
        return None if no_bid is None else (1.0 - no_bid)

    while True:
        s = sum(v for v in bids.values() if v is not None)
        if s < 1.0 - 1e-9:
            break
        # Drop the highest bid.
        best_coin = max((c for c, v in bids.items() if v is not None),
                        key=lambda c: bids[c], default=None)
        if best_coin is None:
            break
        reasons[best_coin] = f"event-sum-bids({s:.3f})>=1 -- yes bid pulled"
        bids[best_coin] = None

    while True:
        yes_asks = {c: _yes_ask_from_no_bid(v) for c, v in asks.items()}
        s = sum(v for v in yes_asks.values() if v is not None)
        unquoted = sum(1 for v in asks.values() if v is None)
        if s > 1.0 + 1e-9 or unquoted == len(asks):
            break
        # Drop the lowest yes-ask (= highest no-bid).
        best_coin = max((c for c, v in asks.items() if v is not None),
                        key=lambda c: asks[c], default=None)
        if best_coin is None:
            break
        reasons[best_coin] = f"event-sum-asks({s:.3f})<=1 -- no bid pulled"
        asks[best_coin] = None

    return bids, asks, reasons


__all__ = [
    "crypto_fee", "EdgeDecomposition", "QuoteDecision",
    "decompose_edge", "desired_quote_level",
    "lip_credit_per_contract", "apply_event_constraints",
]
