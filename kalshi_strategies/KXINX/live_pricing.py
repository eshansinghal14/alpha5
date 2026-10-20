"""
Bucket probabilities from ONE current SPXW quote snapshot (IB): Breeden-
Litzenberger P(S_T > K) per level, combined into floor/cap buckets. Pure
computation, no I/O.

LevelPricer fits the six price curves once per snapshot and answers
P(S_T > K) at any level inside the quoted strikes, so one fit can price
every series' markets (the pricing hub does exactly that).
"""
import numpy as np

from kalshi_strategies.KXINX import bl_pricing as pricing


class LevelPricer:
    """P(S_T > level) and its [lo, hi] band from one option snapshot. Rows
    may be all-NaN for a side with fewer than 2 strikes; that side is then
    skipped, same as the batch path. Results are cached per level."""

    def __init__(self, call_strikes: np.ndarray, call_mid: np.ndarray, call_bid: np.ndarray,
                 call_ask: np.ndarray, put_strikes: np.ndarray, put_mid: np.ndarray,
                 put_bid: np.ndarray, put_ask: np.ndarray):
        self.has_calls = call_strikes.size >= 2
        self.has_puts = put_strikes.size >= 2
        fit = pricing.fit_price_splines
        self._c = [fit(call_strikes, a[None, :]) for a in (call_mid, call_bid, call_ask)] if self.has_calls else None
        self._p = [fit(put_strikes, a[None, :]) for a in (put_mid, put_bid, put_ask)] if self.has_puts else None
        self._mid: dict[float, float] = {}
        self._bounds: dict[float, tuple[float, float]] = {}

    def mid(self, level: float) -> float:
        """Mean of the call- and put-side estimates that exist; NaN if neither."""
        if level not in self._mid:
            vals = []
            if self.has_calls:
                vals.append(float(pricing.prob_above_from_splines(self._c[0], level, is_call=True)[0]))
            if self.has_puts:
                vals.append(float(pricing.prob_above_from_splines(self._p[0], level, is_call=False)[0]))
            vals = [v for v in vals if not np.isnan(v)]
            self._mid[level] = float(np.mean(vals)) if vals else float("nan")
        return self._mid[level]

    def bounds(self, level: float) -> tuple[float, float]:
        """Union of the call-side and put-side bid/ask bounds (see
        bucket_probabilities_ci for why union, not average)."""
        if level not in self._bounds:
            los, his = [], []
            for splines, is_call in ((self._c, True), (self._p, False)):
                if splines is None:
                    continue
                lo, hi = pricing.prob_above_bounds_from_splines(splines[1], splines[2], level, is_call=is_call)
                los.append(float(lo[0]))
                his.append(float(hi[0]))
            los = [v for v in los if not np.isnan(v)]
            his = [v for v in his if not np.isnan(v)]
            self._bounds[level] = (min(los), max(his)) if los and his else (float("nan"), float("nan"))
        return self._bounds[level]


def _levels(markets: list[dict]) -> list[float]:
    return sorted({float(m[k]) for m in markets for k in ("floor_strike", "cap_strike") if m.get(k) is not None})


def bucket_fairs(markets: list[dict], pricer: LevelPricer, monotone: bool = False,
                 ) -> dict[str, tuple[float, float, float]]:
    """{ticker: (prob_lo, prob_mid, prob_hi)} for every market whose levels
    the pricer can reach, clipped into [0, 1] by translation.

    monotone=True makes the mid P(S_T > K) non-increasing across these
    markets' boundary levels before differencing (pricing.monotone_prob_above),
    so no bucket mid goes negative and the buckets sum to ~1. Bands are
    unchanged. The projection only sees `markets`' own levels, so two series
    sharing a pricer can get slightly different mids at a common level."""
    mids: dict[float, float] = {}
    if monotone:
        levels = _levels(markets)
        raw = np.array([pricer.mid(L) for L in levels])
        widths = np.array([b[1] - b[0] for b in (pricer.bounds(L) for L in levels)])
        mids = dict(zip(levels, pricing.monotone_prob_above(raw, widths).tolist()))

    def mid(level):
        return mids[level] if level in mids else pricer.mid(level)

    out: dict[str, tuple[float, float, float]] = {}
    for m in markets:
        floor = float(m["floor_strike"]) if m.get("floor_strike") is not None else None
        cap = float(m["cap_strike"]) if m.get("cap_strike") is not None else None
        if floor is not None and cap is not None:
            prob_mid = mid(floor) - mid(cap)
            floor_lo, floor_hi = pricer.bounds(floor)
            cap_lo, cap_hi = pricer.bounds(cap)
            prob_hi = floor_hi - cap_lo
            prob_lo = floor_lo - cap_hi
        elif floor is not None:
            prob_mid = mid(floor)
            prob_lo, prob_hi = pricer.bounds(floor)
        elif cap is not None:
            prob_mid = 1.0 - mid(cap)
            cap_lo, cap_hi = pricer.bounds(cap)
            prob_lo, prob_hi = 1.0 - cap_hi, 1.0 - cap_lo
        else:
            continue
        if np.isnan(prob_mid) or np.isnan(prob_lo) or np.isnan(prob_hi):
            continue
        lo_c, mid_c, hi_c = pricing.clip_band_preserving_width(prob_lo, prob_mid, prob_hi)
        out[m["ticker"]] = (float(lo_c), float(mid_c), float(hi_c))
    return out


def compute_live_bucket_probs(
    markets: list[dict],
    call_strikes: np.ndarray, call_mid_row: np.ndarray, call_bid_row: np.ndarray, call_ask_row: np.ndarray,
    put_strikes: np.ndarray, put_mid_row: np.ndarray, put_bid_row: np.ndarray, put_ask_row: np.ndarray,
    monotone: bool = False,
) -> dict[str, tuple[float, float, float]]:
    """One snapshot's (prob_lo, prob_mid, prob_hi) per market. `*_row` arrays
    are one row each, aligned to their sorted `*_strikes`."""
    pricer = LevelPricer(call_strikes, call_mid_row, call_bid_row, call_ask_row,
                         put_strikes, put_mid_row, put_bid_row, put_ask_row)
    return bucket_fairs(markets, pricer, monotone=monotone)


def pick_atm_signal(bucket_probs: dict[str, tuple[float, float, float]]) -> dict | None:
    """The bucket whose prob_mid is closest to 0.5, as
    {"ticker","prob","prob_lo","prob_hi"}; None if nothing is priced yet."""
    if not bucket_probs:
        return None
    ticker = min(bucket_probs, key=lambda t: abs(bucket_probs[t][1] - 0.5))
    lo, mid, hi = bucket_probs[ticker]
    return {"ticker": ticker, "prob": mid, "prob_lo": lo, "prob_hi": hi}
