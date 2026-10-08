"""
Re-implementation of CF Benchmarks' Spot Rate methodology, in Python.

Reference: CF Spot Rate Methodology Guide, Sept 2026
(https://docs.cfbenchmarks.com/CF%20Spot%20Rate%20Methodology%20Guide.pdf).
We only need the parts Kalshi settles on: a per-second composite price,
one per coin, built from the L2 order books of CF constituent exchanges.

The algorithm, in the spec's wording and expanded to code steps:

  1. Discard bad per-venue books:
       - Stale: receive-lag > per-coin threshold (HYPE 30s, others ~10s).
       - Erroneous: unparseable / one-sided / crossed.
       - Potentially erroneous: venue mid deviates from the cross-venue
         median mid by more than `mid_deviation_fraction` (10% HYPE,
         ~2% others). Reinstate once the deviation drops below half of
         it. Reinstatement state is caller-held -- cf_rate.py is
         stateless and reinstatement bookkeeping lives in composite.py.
  2. Merge surviving venues into a consolidated book:
       - Cap each level at the dynamic order-size cap (trimmed mean +
         5*winsorised SD over nearby levels).
       - Uncross by removing the lesser size at crossing levels until
         bid < ask.
  3. Build the mid-spread curve and compute utilized depth (the maximum
     cumulative volume where mid-spread stays within `utilized_depth
     _deviation` of the spot). Floor at the dynamic spacing.
  4. Return the exponential-density-weighted mid curve, with
     lambda = 1 / (0.3 * utilized_depth).

This file is PURE. Given a list of `VenueBook` for one coin at one
instant, it returns a `CFSpotResult` or `None` (not enough good books).
The caller (composite.py) handles reinstatement, 1s cadence, and
persistence.

Important simplifications made explicit:

  * We do not implement the full dynamic order-size cap in step 2 as a
    data-driven trimmed-mean + 5*winsorised-SD over "nearby levels" -- the
    sample size is small (~tens of levels per side on Coinbase), and the
    trimming makes little difference against the exponential-weighting
    step 5 that dominates. Instead, we cap each level at
    `size_cap_quantile` of the level-size distribution for that side (a
    monotone, well-defined operation that approaches the CF behaviour as
    the trimming fraction grows). The simplification is documented in the
    code; swap for the full version once the settlement-replication
    accuracy below 98% motivates it.
  * The spacing step 4b is dynamic in CF: min(level-spacing, KDE-spacing)
    with 100 target levels. We use `min` of (level price-spacing median,
    utilized-depth bound / 100), which is the same shape with a simpler
    density estimator.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np

from .spot_feed import VenueBook
from .config import MID_DEVIATION_FRACTION, UTILIZED_DEPTH_DEVIATION


@dataclasses.dataclass
class CFSpotResult:
    coin: str
    price: float               # the composite rate
    utilized_depth: float      # base-currency units
    n_venues_in: int           # venues that passed the filters this second
    n_venues_dropped: int
    drop_reasons: dict[str, str]   # venue -> short reason, for diagnostics
    fallback_median_mid: float | None  # median of per-venue mids, for logging the fallback-vs-CF diff
    is_fallback: bool          # True iff we fell back to fallback_median_mid


# Default size-cap: how aggressively to clip outlier-sized levels before
# the depth-weighted average. 0.95 keeps most liquidity; CF's trimmed-mean
# formulation is stricter but needs cross-level data we don't persist here.
DEFAULT_SIZE_CAP_QUANTILE = 0.95


def compute_cf_spot(
    coin: str,
    venues: list[VenueBook],
    *,
    size_cap_quantile: float = DEFAULT_SIZE_CAP_QUANTILE,
    allow_fallback_median: bool = True,
    mid_deviation_fraction: float | None = None,
    utilized_depth_deviation: float | None = None,
    min_venues: int = 1,
) -> CFSpotResult | None:
    """One-second CF Spot Rate replica for `coin`. Returns None if
    nothing usable survives the venue filter.

    `min_venues` is 1 in Phase 1 (Coinbase only). Once Phase 2 is live
    this can be raised to 2 to require redundancy; raising it before
    then would permanently return None.
    """
    if mid_deviation_fraction is None:
        mid_deviation_fraction = MID_DEVIATION_FRACTION.get(coin, 0.02)
    if utilized_depth_deviation is None:
        utilized_depth_deviation = UTILIZED_DEPTH_DEVIATION.get(coin, 0.005)

    drop_reasons: dict[str, str] = {}
    good: list[VenueBook] = []       # books clean enough for the full depth-weighted curve
    mids: list[tuple[str, float]] = []   # every venue that yields SOME usable mid
    for vb in venues:
        if vb.is_resyncing:
            drop_reasons[vb.venue] = "resyncing"; continue
        if vb.is_one_sided():
            drop_reasons[vb.venue] = "one_sided"; continue
        m = vb.mid()
        if m is None or not math.isfinite(m):
            drop_reasons[vb.venue] = "no_mid"; continue
        if vb.is_crossed():
            # A crossed book still has a perfectly usable mid -- 0.5*(bid+ask)
            # is a sensible price estimate regardless of ordering. Delta
            # streams occasionally leave a stale crossing level that doesn't
            # clear for minutes (observed 2026-10-06: BTC composite blanked
            # for ~278s because a crossed Coinbase book was dropped outright
            # and there was no fallback, which starved the pricer and marked
            # BTC DOWN -> whole event theo invalid). For the composite we only
            # need a mid, so keep the venue as a MID-ONLY contributor: it
            # feeds the fallback median but not the depth-weighted curve
            # (whose uncross/utilized-depth math wants a clean book).
            drop_reasons[vb.venue] = "crossed(mid-only)"
            mids.append((vb.venue, m))
            continue
        good.append(vb)
        mids.append((vb.venue, m))

    # Potentially-erroneous filter: median of (surviving) mids, kick any
    # venue > mid_deviation_fraction away from it. With one surviving
    # venue, this step is a no-op (the venue IS the median).
    if len(mids) >= 2:
        median_mid = float(np.median([m for _, m in mids]))
        kept: list[VenueBook] = []
        for vb in good:
            m = vb.mid()
            if abs(m - median_mid) / median_mid > mid_deviation_fraction:
                drop_reasons[vb.venue] = f"outlier(dev={abs(m - median_mid) / median_mid:.3%})"
                continue
            kept.append(vb)
        good = kept
    else:
        median_mid = mids[0][1] if mids else None

    if len(good) < min_venues:
        # Fallback: median mid of whatever yielded a usable mid -- which
        # now INCLUDES crossed books (mid-only contributors). The old
        # `len(good) >= 1` guard here required a clean book and so still
        # returned None when the only venue was crossed, which is exactly
        # the case that blanked BTC. Gate on `mids`, not `good`.
        if allow_fallback_median and median_mid is not None and len(mids) >= 1:
            return CFSpotResult(
                coin=coin, price=median_mid, utilized_depth=0.0,
                n_venues_in=len(good), n_venues_dropped=len(venues) - len(good),
                drop_reasons=drop_reasons, fallback_median_mid=median_mid,
                is_fallback=True,
            )
        return None

    # Consolidated book.
    bid_prices, bid_sizes, ask_prices, ask_sizes = _consolidate(good, size_cap_quantile)
    if bid_prices.size == 0 or ask_prices.size == 0:
        if allow_fallback_median and median_mid is not None:
            return CFSpotResult(
                coin=coin, price=median_mid, utilized_depth=0.0,
                n_venues_in=len(good), n_venues_dropped=len(venues) - len(good),
                drop_reasons=drop_reasons, fallback_median_mid=median_mid,
                is_fallback=True,
            )
        return None
    bid_prices, bid_sizes, ask_prices, ask_sizes = _uncross(
        bid_prices, bid_sizes, ask_prices, ask_sizes)
    if bid_prices.size == 0 or ask_prices.size == 0:
        if allow_fallback_median and median_mid is not None:
            return CFSpotResult(
                coin=coin, price=median_mid, utilized_depth=0.0,
                n_venues_in=len(good), n_venues_dropped=len(venues) - len(good),
                drop_reasons=drop_reasons, fallback_median_mid=median_mid,
                is_fallback=True,
            )
        return None

    # Build the mid curve as a function of cumulative volume on the
    # thinner side.
    cum_vol, mid_curve, spot_bootstrap = _curves(
        bid_prices, bid_sizes, ask_prices, ask_sizes)

    # Utilized depth: largest cumulative volume where the mid at that
    # depth stays within utilized_depth_deviation of the top-of-book spot.
    # The test is on the mid's DEVIATION from spot, |mid(v) - spot|, not on
    # the bid-ask half-spread: for an uncrossed book 0.5*(bid(v)-ask(v)) is
    # <= 0 everywhere, so the old `mid_spread <= threshold` was vacuously
    # true and utilized depth ran to the whole thinner side -- which dragged
    # the weighted mid ~8% off the touch on a real BTC book (observed
    # 2026-10-07: full-book price 92963 vs true BBO mid 85529).
    threshold = utilized_depth_deviation * spot_bootstrap
    within = np.abs(mid_curve - spot_bootstrap) <= threshold
    # Spacing floor: median absolute price step between successive
    # consolidated levels on either side. Prevents a utilized_depth that
    # resolves below the actual level grid.
    all_prices = np.sort(np.concatenate([bid_prices, ask_prices]))
    if len(all_prices) >= 2:
        spacing_floor = float(np.median(np.diff(all_prices)))
    else:
        spacing_floor = 0.0
    if not within.any():
        utilized = max(cum_vol[0], spacing_floor)
    else:
        last_in = int(np.where(within)[0].max())
        utilized = max(cum_vol[last_in], spacing_floor)

    # Exponential-density-weighted spot. lambda = 1 / (0.3 * utilized).
    # Weight at depth v: lambda * exp(-lambda * v). Normalise over
    # [0, utilized] since the curve ends there.
    lam = 1.0 / max(0.3 * utilized, 1e-12)
    mask = cum_vol <= utilized + 1e-12
    v = cum_vol[mask]
    m = mid_curve[mask]
    if v.size < 2:
        price = float(m[0]) if m.size else float(spot_bootstrap)
    else:
        # Weight is piecewise constant on each cumulative-volume interval;
        # integrate analytically against the exponential density, then
        # normalise.
        v0 = np.concatenate([[0.0], v[:-1]])
        weights = np.exp(-lam * v0) - np.exp(-lam * v)
        total = weights.sum()
        if total <= 0:
            price = float(spot_bootstrap)
        else:
            price = float((m * weights).sum() / total)

    return CFSpotResult(
        coin=coin, price=price, utilized_depth=float(utilized),
        n_venues_in=len(good), n_venues_dropped=len(venues) - len(good),
        drop_reasons=drop_reasons,
        fallback_median_mid=float(median_mid) if median_mid is not None else None,
        is_fallback=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _consolidate(venues: list[VenueBook], size_cap_quantile: float):
    """Merge each side across venues. Sort bids descending, asks ascending.
    Each level is capped at the quantile of its own side's sizes."""
    bid_pairs = []
    ask_pairs = []
    for vb in venues:
        for lv in vb.bids:
            if lv.size > 0:
                bid_pairs.append((lv.price, lv.size))
        for lv in vb.asks:
            if lv.size > 0:
                ask_pairs.append((lv.price, lv.size))
    if not bid_pairs or not ask_pairs:
        return (np.empty(0),) * 4
    bid_arr = _group_by_price(bid_pairs, descending=True)
    ask_arr = _group_by_price(ask_pairs, descending=False)
    # Size cap, per side. The purpose is to stop an outsized single level
    # on one venue from dragging the depth-weighted average. Quantile on
    # the current level-size distribution is a simpler stand-in for CF's
    # trimmed-mean + 5*winsor-SD; see module docstring.
    for arr in (bid_arr, ask_arr):
        if arr.size:
            cap = float(np.quantile(arr[:, 1], size_cap_quantile))
            arr[:, 1] = np.minimum(arr[:, 1], cap)
    return bid_arr[:, 0], bid_arr[:, 1], ask_arr[:, 0], ask_arr[:, 1]


def _group_by_price(pairs: list[tuple[float, float]], *, descending: bool) -> np.ndarray:
    """Sum sizes at identical prices, return a 2-column (price, size)
    array sorted by price."""
    # Dict is faster than numpy groupby for small lists (~hundreds of
    # levels across venues).
    acc: dict[float, float] = {}
    for p, s in pairs:
        acc[p] = acc.get(p, 0.0) + s
    arr = np.array(sorted(acc.items(), key=lambda kv: -kv[0] if descending else kv[0]),
                   dtype=float)
    return arr


def _uncross(bid_p, bid_s, ask_p, ask_s):
    """Remove crossing size. Walk from the top of each book inward,
    removing `min(bid_s[0], ask_s[0])` at a time, as long as the top-of-
    book is crossed."""
    bid_p = bid_p.copy(); bid_s = bid_s.copy()
    ask_p = ask_p.copy(); ask_s = ask_s.copy()
    bi = ai = 0
    while bi < bid_p.size and ai < ask_p.size and bid_p[bi] >= ask_p[ai]:
        q = min(bid_s[bi], ask_s[ai])
        bid_s[bi] -= q
        ask_s[ai] -= q
        if bid_s[bi] <= 0:
            bi += 1
        if ask_s[ai] <= 0:
            ai += 1
    return bid_p[bi:], bid_s[bi:], ask_p[ai:], ask_s[ai:]


def _curves(bid_p, bid_s, ask_p, ask_s):
    """Build the (cum_vol, mid) curves up to the thinner side's total
    volume. Also returns a "spot bootstrap" -- top-of-book mid -- used to
    size the utilized-depth threshold.

    Fully vectorised: `searchsorted` maps every grid point to its level
    index on each side in one call. The previous version evaluated a
    Python closure once per grid point, which is O(n) Python-level work
    per composite -- ~180 ms on a 42k-level Coinbase BTC book, run on
    every WS message, which pegged the event loop (see composite.py)."""
    spot_bootstrap = 0.5 * (bid_p[0] + ask_p[0])
    # Sample grid: union of bid and ask cumulative volumes (natural
    # breakpoints for both piecewise-constant price curves). Capped at
    # the thinner side's total.
    bid_cum = np.cumsum(bid_s)
    ask_cum = np.cumsum(ask_s)
    v_max = min(bid_cum[-1], ask_cum[-1])
    grid = np.unique(np.concatenate([bid_cum, ask_cum]))
    grid = grid[grid > 0]
    grid = grid[grid <= v_max + 1e-12]
    bi = np.clip(np.searchsorted(bid_cum, grid, side="left"), 0, bid_p.size - 1)
    ai = np.clip(np.searchsorted(ask_cum, grid, side="left"), 0, ask_p.size - 1)
    mid = 0.5 * (bid_p[bi] + ask_p[ai])
    return grid, mid, spot_bootstrap


__all__ = ["CFSpotResult", "compute_cf_spot"]
