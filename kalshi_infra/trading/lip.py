"""
LIP (Liquidity Incentive Program) scoring replica.

Sources:
  * https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
  * CFTC filing: https://www.cftc.gov/filings/orgrules/rules07152610358.pdf

Mechanics:
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

Scoring functions are pure: the caller provides the book (qualifying
levels per side) and its own resting size. Hand-example from the Kalshi
help page: 20% share * $100 * 8000/10000 = $16.00; tests/test_lip.py checks
it exactly. `LipTracker` at the bottom is the stateful per-session accrual
used by the generic quoter.
"""
from __future__ import annotations

import dataclasses
import math

from kalshi_infra.kalshi.venue import TICK


@dataclasses.dataclass(frozen=True)
class LipParams:
    """Program-level params for one market. `period_reward_usd` is dollars;
    the conversion from the API's raw `period_reward` happens at ingestion
    (params_from_program), NOT here."""
    target_size: float            # Target in contracts
    discount_factor: float        # DF, e.g. 0.5 for 5000 bps
    period_reward_usd: float      # per-period, DOLLARS
    snapshots_per_period: int
    max_reward_usd: float | None = None   # per-account cap for the period


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


# ---------------------------------------------------------------------------
# Session accrual
# ---------------------------------------------------------------------------

CENTI_CENTS_PER_USD = 10_000.0   # period_reward / max_reward_per_account unit (Kalshi API docs)


def params_from_program(program: dict) -> LipParams:
    """LipParams from one /incentive_programs record -- everything comes
    from Kalshi: target, discount, reward (centi-cents), period length
    (one snapshot per second between start_date and end_date), and the
    optional per-account cap."""
    import pandas as pd
    secs = (pd.Timestamp(program["end_date"]) - pd.Timestamp(program["start_date"])).total_seconds()
    cap = program.get("max_reward_per_account")
    return LipParams(
        target_size=float(program["target_size_fp"]),
        discount_factor=float(program["discount_factor_bps"]) / 10_000.0,
        period_reward_usd=float(program.get("period_reward") or 0.0) / CENTI_CENTS_PER_USD,
        snapshots_per_period=max(int(round(secs)), 1),
        max_reward_usd=None if cap is None else float(cap) / CENTI_CENTS_PER_USD,
    )


def levels_from_ladder(bids: dict[float, float], ours: list[tuple[float, float]] = ()) -> list[LipLevel]:
    """One side's levels (own-side prices, best first) with our resting
    orders `ours` [(price, size)] marked as `mine`. At a level Kalshi
    already shows, our size is assumed to be inside the displayed size (the
    ws book includes our own orders); at a level it doesn't show yet, our
    size is added."""
    acc = {p: [s, 0.0] for p, s in bids.items()}
    for p, s in ours:
        if s <= 0:
            continue
        lv = acc.setdefault(p, [0.0, 0.0])
        lv[1] += s
        lv[0] = max(lv[0], lv[1])
    return sorted((LipLevel(price=p, size=v[0], mine=v[1]) for p, v in acc.items()), key=lambda lv: -lv.price)


def others_ladder(bids: dict[float, float], ours: list[tuple[float, float]]) -> dict[float, float]:
    """The visible ladder with our own resting size removed."""
    out = dict(bids)
    for p, s in ours:
        if p in out:
            out[p] = max(out[p] - s, 0.0)
    return {p: s for p, s in out.items() if s > 1e-9}


def reward_rate(yes: list[LipLevel], no: list[LipLevel], params: LipParams) -> float:
    """Our expected $ this snapshot (= $/s), capped at the per-account limit
    spread evenly over the period."""
    r = score_book(yes, no, params).expected_reward_this_snapshot_usd
    if params.max_reward_usd is not None:
        r = min(r, params.max_reward_usd / params.snapshots_per_period)
    return r


@dataclasses.dataclass(frozen=True)
class LipChoice:
    price: float | None      # own-side price; None = no LIP quote
    size: float
    rate: float              # $/s our LIP quote adds at (price, size)
    max_rate: float          # best $/s available outside the edge at full size
    depth_ahead: float


def choose_lip_level(*, side: str, others_yes: dict, others_no: dict,
                     ours_yes: list, ours_no: list, params: LipParams,
                     top_price: float | None, max_size: float, prop_max: float,
                     min_depth_ahead: float, depth_ahead_fn) -> LipChoice:
    """Where to rest the LIP-farming quote on `side` (own-side prices).

    Candidates are every tick at or below `top_price` (the first level
    outside the edge). The best rate over them, at `max_size`, is the
    reference; the DEEPEST candidate that earns at least prop_max of it and
    has at least min_depth_ahead resting ahead wins. The size is then cut to
    the smallest whole number of lots earning the same rate (smaller is
    strictly safer). `ours_*` are our OTHER resting orders, which stay in
    the book while scoring."""
    none = LipChoice(None, 0.0, 0.0, 0.0, 0.0)
    if top_price is None or max_size < 1 or params.period_reward_usd <= 0:
        return none
    others = others_yes if side == "yes" else others_no

    def levels(price, size):
        y = ours_yes + ([(price, size)] if side == "yes" else [])
        n = ours_no + ([(price, size)] if side == "no" else [])
        return levels_from_ladder(others_yes, y), levels_from_ladder(others_no, n)

    base = reward_rate(*levels(None, 0.0), params)

    def rate(price, size):
        return reward_rate(*levels(price, size), params) - base

    cands = []
    p = round(top_price, 2)
    cum_above = sum(s for q, s in others.items() if q > p + 1e-9)
    while p >= TICK - 1e-9:
        # Below the level where the book already reaches target, an order
        # no longer "helps reach the Target Size" and scores 0 -- as do all
        # deeper ones.
        if cum_above >= params.target_size:
            break
        cands.append((p, rate(p, max_size)))
        cum_above += others.get(p, 0.0)
        p = round(p - TICK, 2)
    if not cands:
        return none
    best = max(r for _, r in cands)
    if best <= 0:
        return LipChoice(None, 0.0, 0.0, best, 0.0)
    for price, r in reversed(cands):          # deepest first
        if r + 1e-15 < prop_max * best:
            continue
        ahead = depth_ahead_fn(price)
        if ahead + 1e-9 < min_depth_ahead:
            continue
        lo, hi = 1, int(max_size)
        full = rate(price, hi)
        while lo < hi:
            mid = (lo + hi) // 2
            if rate(price, mid) >= full - 1e-15:
                hi = mid
            else:
                lo = mid + 1
        return LipChoice(price, float(lo), rate(price, lo), best, ahead)
    return LipChoice(None, 0.0, 0.0, best, 0.0)


class LipTracker:
    """Per-market expected LIP accrual for one event. Markets with no active
    program accrue nothing. Measurement only -- nothing here feeds pricing."""

    def __init__(self, params: dict[str, LipParams]):
        self.params = params
        self.accrued_usd: dict[str, float] = {t: 0.0 for t in params}
        self.snapshots_qualified: dict[str, int] = {t: 0 for t in params}
        self.snapshots_total: dict[str, int] = {t: 0 for t in params}
        self._last_snapshot_s: float | None = None

    def due(self, now_s: float) -> bool:
        """Kalshi snapshots once per second."""
        return self._last_snapshot_s is None or now_s - self._last_snapshot_s >= 1.0

    def snapshot(self, now_s: float, books: dict, ours: dict[tuple[str, str], list]):
        """`books`: ticker -> BookView (with .ladder). `ours`: (ticker, side)
        -> [(price, size)] of our resting orders, own-side terms."""
        self._last_snapshot_s = now_s
        for ticker, params in self.params.items():
            if params.period_reward_usd <= 0:
                continue
            book = books.get(ticker)
            ladder = getattr(book, "ladder", None) if book is not None else None
            if ladder is None:
                continue
            yes = levels_from_ladder(ladder.yes_bids, ours.get((ticker, "yes"), []))
            no = levels_from_ladder(ladder.no_bids, ours.get((ticker, "no"), []))
            score = score_book(yes, no, params)
            self.snapshots_total[ticker] += 1
            if score.qualifies:
                self.snapshots_qualified[ticker] += 1
                self.accrued_usd[ticker] += reward_rate(yes, no, params)

    def total_usd(self) -> float:
        return float(sum(self.accrued_usd.values()))

    def qualifying_fraction(self, ticker: str) -> float:
        n = self.snapshots_total.get(ticker, 0)
        return self.snapshots_qualified[ticker] / n if n else 0.0

    def summary(self) -> dict:
        return {
            "total_usd": self.total_usd(),
            "accrued_usd": dict(self.accrued_usd),
            "qualifying_fraction": {t: self.qualifying_fraction(t) for t in self.params},
            "period_reward_usd": {t: p.period_reward_usd for t, p in self.params.items()},
        }


__all__ = [
    "LipParams", "LipLevel", "LipSideScore", "LipBookScore",
    "score_book", "marginal_reward_rate", "period_payout",
    "params_from_program", "levels_from_ladder", "others_ladder", "reward_rate",
    "LipChoice", "choose_lip_level", "LipTracker",
]
