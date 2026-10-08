"""
LIP (Liquidity Incentive Program) scoring replica.

Sources:
  * https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
  * CFTC filing: https://www.cftc.gov/filings/orgrules/rules07152610358.pdf

Mechanics (section 6.1 of our spec):
  1. A snapshot is taken ONCE PER SECOND at a random time in the second.
  2. Each side is scored independently: YES side = YES bids; NO side =
     NO bids. A YES ASK at p counts as a NO BID at (1 - p) -- they are
     the same resting liquidity, just priced in different terms.
  3. Walking down from best bid, accumulating size:
       - The REFERENCE PRICE is the first level whose cumulative size
         reaches Target / 5.
       - Stop accumulating once cumulative size reaches Target.
       - If total size on the side is below Target, that side has no
         qualifying orders.
  4. Score of a qualifying order = `size * DF^N`, where N = ticks below
     the Reference Price (0 if at or above it).
  5. Normalize across all users on that side.
  6. User's snapshot score = normalized YES share + normalized NO share
     (max 2.0 across all users on that market).
  7. SNAPSHOT EXCLUSION: if EITHER side fails to reach Target, the
     snapshot is excluded. Payout for the period scales by
     (non_excluded / total).
  8. Payout = (user_total_score / all_users_total_score) *
             period_reward * (non_excluded / total), rounded down to the
             cent, min $1.00.

This file is pure. The caller provides the book (as a list of
qualifying-level pairs per side) and the user's own orders. Hand-example
from the Kalshi help page: 20% share * $100 * 8000/10000 = $16.00; the
tests in tests/test_lip_scorer.py verify this exactly.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np

from .series import TICK


@dataclasses.dataclass(frozen=True)
class LipParams:
    """Program-level params. In Phase 1 these come from
    `GET /incentive_programs?status=active` once per period and are
    cached per market. `period_reward_usd` is the field whose unit this
    file treats as authoritative dollars -- the conversion from the
    API's raw `period_reward` (unverified unit) is applied at ingestion
    (see event_state.py), NOT here."""
    target_size: float            # Target in contracts
    discount_factor: float        # DF, e.g. 0.5 for 5000 bps
    period_reward_usd: float      # per-period, DOLLARS
    snapshots_per_period: int = 900


@dataclasses.dataclass
class LipLevel:
    """One price level on one side after merging contributions from all
    participants. `size` is total contracts resting at that price;
    `mine` is how much of that is ours. Prices are in that SIDE'S own
    terms (yes bids in yes prices; no bids / yes asks in no prices)."""
    price: float
    size: float
    mine: float = 0.0


@dataclasses.dataclass
class LipSideScore:
    reference_price: float | None     # None if no qualifying orders on this side
    our_raw_score: float              # size-weighted, before normalising
    all_raw_score: float
    our_share: float                  # our_raw / all_raw, 0..1
    qualifies: bool                   # at least Target in cumulative size
    levels_in_window: list[LipLevel]  # the levels within [reference_price - K ticks, best]
                                      # that got any score at all; for the viz


@dataclasses.dataclass
class LipBookScore:
    yes: LipSideScore
    no: LipSideScore
    qualifies: bool                   # BOTH sides reach Target
    reward_per_snapshot_usd: float    # period_reward_usd / snapshots_per_period
    expected_reward_this_snapshot_usd: float   # our combined score contribution
                                               # given both sides qualify


def _score_side(levels: list[LipLevel], params: LipParams) -> LipSideScore:
    """`levels` is sorted outside-first (best bid first; best is the
    HIGHEST yes-bid or the HIGHEST no-bid). This matches walking down
    from the touch into the book, which is what the Kalshi rules
    describe.
    """
    if not levels:
        return LipSideScore(reference_price=None, our_raw_score=0.0,
                            all_raw_score=0.0, our_share=0.0, qualifies=False,
                            levels_in_window=[])

    cumulative = 0.0
    threshold_ref = params.target_size / 5.0
    threshold_full = params.target_size
    reference_price: float | None = None
    stop_idx: int | None = None
    for i, lv in enumerate(levels):
        cumulative += lv.size
        if reference_price is None and cumulative >= threshold_ref:
            reference_price = lv.price
        if cumulative >= threshold_full:
            stop_idx = i
            break
    qualifies = reference_price is not None and stop_idx is not None

    if not qualifies:
        return LipSideScore(reference_price=reference_price, our_raw_score=0.0,
                            all_raw_score=0.0, our_share=0.0, qualifies=False,
                            levels_in_window=[])

    # Score each level within [best .. stop_idx] inclusive. N = ticks
    # below the Reference Price (0 if at or ABOVE it).
    DF = params.discount_factor
    scored: list[LipLevel] = []
    our_score = 0.0
    all_score = 0.0
    for lv in levels[:stop_idx + 1]:
        ticks_below = max(0, int(round((reference_price - lv.price) / TICK)))
        weight = DF ** ticks_below
        all_score += lv.size * weight
        our_score += lv.mine * weight
        scored.append(lv)
    share = (our_score / all_score) if all_score > 0 else 0.0
    return LipSideScore(
        reference_price=reference_price,
        our_raw_score=our_score,
        all_raw_score=all_score,
        our_share=share,
        qualifies=True,
        levels_in_window=scored,
    )


def score_book(
    yes_levels: list[LipLevel],
    no_levels: list[LipLevel],
    params: LipParams,
) -> LipBookScore:
    """Full snapshot score for one market, both sides.

    `yes_levels`: YES BIDS, sorted highest-price first.
    `no_levels`: NO BIDS, sorted highest-price first (= equivalent to
        YES ASKS sorted lowest-first, converted to no-side terms).

    Both lists are the AGGREGATED book -- our resting size plus the
    rest of the market. Each level's `mine` is the portion of its
    `size` that is us. Separation at this layer makes the scorer
    agnostic to how the caller discovered its own quotes.
    """
    yes = _score_side(yes_levels, params)
    no = _score_side(no_levels, params)
    qualifies = yes.qualifies and no.qualifies
    reward_per_snapshot = params.period_reward_usd / max(params.snapshots_per_period, 1)
    # Expected reward THIS snapshot (dollars) given both sides qualify:
    #   (yes_share + no_share) * reward_per_snapshot.
    # Spec: max contribution across all users is 2.0, so a single user's
    # expected value is bounded by 2 * reward_per_snapshot.
    expected = (yes.our_share + no.our_share) * reward_per_snapshot if qualifies else 0.0
    return LipBookScore(
        yes=yes, no=no, qualifies=qualifies,
        reward_per_snapshot_usd=reward_per_snapshot,
        expected_reward_this_snapshot_usd=expected,
    )


def marginal_reward_rate(
    yes_levels: list[LipLevel],
    no_levels: list[LipLevel],
    params: LipParams,
    *,
    side: str,                    # "yes" | "no" -- which side gets the new order
    candidate_price: float,       # in that side's own terms
    candidate_size: float,
) -> float:
    """Expected incremental dollars per second from ADDING an order at
    (candidate_price, candidate_size, side).

    Section 6.1 defines this as the rate the quoter trades off against
    adverse-selection cost. Positive values mean "the quote is paying
    us this much per second to sit there".

    Uses `reward_per_snapshot_usd` from the current `period_reward_usd`
    (so a mid-period re-measure updates the signal), and 1 snapshot per
    second -- so dollars/second == dollars/snapshot.
    """
    base = score_book(yes_levels, no_levels, params)
    # Add the hypothetical order and rescore.
    new_yes = list(yes_levels)
    new_no = list(no_levels)
    tgt_list = new_yes if side == "yes" else new_no
    # Merge into an existing level if one is already there, else insert
    # sorted (descending by price for both sides, since both are "bids
    # in their own terms").
    inserted = False
    for i, lv in enumerate(tgt_list):
        if math.isclose(lv.price, candidate_price, abs_tol=1e-9):
            tgt_list[i] = LipLevel(price=lv.price, size=lv.size + candidate_size,
                                   mine=lv.mine + candidate_size)
            inserted = True
            break
    if not inserted:
        new_lv = LipLevel(price=candidate_price, size=candidate_size,
                          mine=candidate_size)
        # Insert sorted descending.
        j = 0
        while j < len(tgt_list) and tgt_list[j].price > candidate_price:
            j += 1
        tgt_list.insert(j, new_lv)

    after = score_book(new_yes, new_no, params)
    # Reward per snapshot = reward per second (one snapshot/sec).
    return after.expected_reward_this_snapshot_usd - base.expected_reward_this_snapshot_usd


def period_payout(user_total_score: float, all_users_total_score: float,
                  non_excluded: int, total_snapshots: int,
                  period_reward_usd: float) -> float:
    """End-of-period dollars paid. Spec:
         payout = user_total / all_users_total * period_reward * (non_excl / total)
         rounded DOWN to the cent, min $1.00 (if > 0).
    """
    if all_users_total_score <= 0 or total_snapshots <= 0:
        return 0.0
    gross = (user_total_score / all_users_total_score) * period_reward_usd \
            * (non_excluded / total_snapshots)
    rounded = math.floor(gross * 100.0) / 100.0
    if rounded > 0 and rounded < 1.00:
        return 1.00
    return rounded


__all__ = [
    "LipParams", "LipLevel", "LipSideScore", "LipBookScore",
    "score_book", "marginal_reward_rate", "period_payout",
]
