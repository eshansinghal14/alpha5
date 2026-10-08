"""
Shared SPXW-implied signal computation: option-implied probability +
confidence band + call/put parity divergence for whichever market in a
Kalshi event is currently closest to 50/50, sampled across the whole
session. This is the "pricing computation" layer both quoting strategies
(spxw_kxinx/sp_options_lag_quoting.py, for KXINX's two-sided range buckets,
and spxw_kxinxu/kxinxu_lag_quoting.py, for KXINXU's single-sided
"S&P >= threshold" ladder) build on -- neither the model
(latency_arbs_functions.bucket_probabilities_ci, Breeden-Litzenberger,
model-free) nor this "pick whichever market is closest to ATM" logic cares
which market shape it's fed: bucket_probabilities_ci already handles a
floor-only market (KXINXU's shape) exactly the same way it handles KXINX's
own tail "greater than" buckets -- a single P(S_T > floor) evaluation, no
floor/cap difference needed. What differs between the two strategies is
what they DO with the signal (position sizing, leaning, take-only
resolution), not how the signal itself gets computed -- that split is the
whole reason this lives in its own file rather than inside either
strategy's module.
"""
import datetime
import os
import sys
from collections import deque

import pandas as pd

_INFRA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kalshi_infra")
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)

import databento_functions as dbf
import latency_arbs_functions as laf
import ib_emulation as ibe


# A Kalshi event may only be priced off options expiring at the same moment.
# 5 minutes of slack absorbs listing-time rounding, nothing more.
EXPIRY_MATCH_TOLERANCE_S = 300

# SPXW dailies are PM-settled on the 16:00 ET close. OPRA's definition schema
# carries only the expiry DATE (midnight UTC, e.g. 2026-08-17T00:00Z for the
# Aug-17 chain -- see databento_functions' same-day filter), so the settlement
# instant has to be reconstructed from it rather than read off: comparing the
# raw midnight-UTC value directly against a 16:00 ET event lands 4h out and
# picks the NEXT day's chain, not the correct same-day one.
SPXW_SETTLEMENT_ET = datetime.time(16, 0)


class ExpiryMismatch(ValueError):
    """Raised when a Kalshi event settles at a different time than the
    options being used to price it."""


def _require_matching_expiry(event_ticker: str, expiry, defs) -> None:
    """Refuse to price a Kalshi event whose settlement time doesn't match the
    option expiry.

    Breeden-Litzenberger on a chain expiring at time T yields P(S_T > K) --
    a statement about the underlying AT T AND NOWHERE ELSE. Applying it to a
    market settling earlier silently substitutes a longer horizon, and since
    a longer horizon is a wider distribution it systematically OVERSTATES
    the odds of reaching distant strikes.

    This is not hypothetical. KXINXU lists intraday hourlies (11:00, 12:00,
    14:00, 15:00 ET) alongside the 16:00 ET daily close, while SPXW 0DTE
    options all settle at 16:00 ET. Pricing the 12:00 event off them applied
    a 4-hour-wider distribution to a 1-hour market, which made deep strikes
    look badly mispriced -- and the strategy responded by loading 50 lots on
    26 markets at once, buying the cheap tail all the way up and down the
    ladder.

    Only the matching-expiry event is tradeable; everything else raises.
    """
    if defs is None or len(defs) == 0 or "expiration" not in getattr(defs, "columns", []):
        return
    raw = pd.to_datetime(pd.Series(defs["expiration"].unique()), utc=True)
    if raw.empty:
        return
    # Date-only -> the instant that chain actually settles (see SPXW_SETTLEMENT_ET).
    exps = pd.Series([
        pd.Timestamp(datetime.datetime.combine(ts.date(), SPXW_SETTLEMENT_ET),
                     tz=dbf.ET).tz_convert("UTC")
        for ts in raw
    ])
    want = pd.Timestamp(expiry)
    gap = (exps - want).abs().min().total_seconds()
    if gap > EXPIRY_MATCH_TOLERANCE_S:
        nearest = exps.iloc[(exps - want).abs().argmin()]
        raise ExpiryMismatch(
            f"{event_ticker} settles {want.isoformat()} but the options price to "
            f"{nearest.isoformat()} ({gap / 3600:.1f}h apart). Breeden-Litzenberger "
            f"gives P(S at the OPTION expiry > K), so this would price the market "
            f"against the wrong horizon. Trade only the event matching the option "
            f"expiry (the 16:00 ET daily close)."
        )


# Spike-and-revert detection. Calibrated against a real session's own
# per-bucket rates: mid-moneyness buckets topped out at 4.0% (median 1.4%),
# while deep-ITM and far-OTM ran 17-25% (median ~11%). 5% sits just above
# every clean bucket observed, so it separates "genuinely noisy" from
# "ordinary tick movement" on measured data rather than a guessed number.
SPIKE_JUMP = 0.02          # a move this large is a candidate spike
SPIKE_REVERT_FRAC = 0.5    # ...if the next tick retraces at least half of it
MAX_SPIKE_RATE = 0.05      # drop a market whose spike rate exceeds this
NOISE_WINDOW = 200         # ticks of history the rate is measured over
NOISE_MIN_SAMPLES = 40     # a market is untradeable until it has this many ticks


class NoiseTracker:
    """Flags markets whose implied probability spikes and immediately
    reverts, so they can be dropped instead of traded.

    Deep-ITM buckets are the main offenders. Breeden-Litzenberger divides a
    difference of large option premiums by a small strike gap, so on a deep
    strike a single tick of quote noise moves the estimate far more than the
    CI (built from bid/ask slopes at one instant) can express -- measured on
    real data, one bucket reported a 1.2c band while routinely jumping 9c,
    on a third of all ticks. The CI is measuring cross-sectional
    disagreement; this measures temporal instability, which is the thing
    that actually goes wrong there.

    Deliberately CAUSAL and rolling: it only ever looks at ticks already
    seen. A batch pass over the whole day would be look-ahead bias in the
    backtest and, worse, would not match what live can know -- the two paths
    have to make the same decision at the same instant. A market that calms
    down re-enters on its own once the bad ticks age out of the window.

    A market with too little history is untradeable, not clean. It used to
    be the other way round, which left the filter off for the first ~30s of
    every session -- long enough to take three lots on a 22ms band collapse
    the armed filter would have caught (KXINXU T7754.9999, 15:56:42 on
    2026-08-28: 35 ticks seen against a 40-tick minimum; the same ticker was
    being suppressed by 15:57:44).
    """

    def __init__(self, jump=SPIKE_JUMP, revert_frac=SPIKE_REVERT_FRAC,
                 max_rate=MAX_SPIKE_RATE, window=NOISE_WINDOW, min_samples=NOISE_MIN_SAMPLES):
        self.jump = jump
        self.revert_frac = revert_frac
        self.max_rate = max_rate
        self.window = window
        self.min_samples = min_samples
        self._hist: dict[str, deque] = {}
        self._spikes: dict[str, deque] = {}
        self.dropped: dict[str, int] = {}   # ticker -> ticks suppressed as noisy, for reporting
        self.warming: dict[str, int] = {}   # ticker -> ticks suppressed for lack of history

    def accept(self, ticker: str, prob: float) -> bool:
        """Record `prob` and return whether `ticker` is currently tradeable."""
        h = self._hist.setdefault(ticker, deque(maxlen=self.window))
        s = self._spikes.setdefault(ticker, deque(maxlen=self.window))
        h.append(float(prob))
        if len(h) >= 3:
            prev, cur, nxt = h[-3], h[-2], h[-1]
            d_prev, d_next = cur - prev, nxt - cur
            spiked = (abs(d_prev) > self.jump
                      and (d_next < 0) == (d_prev > 0)      # retraced
                      and abs(d_next) > self.revert_frac * abs(d_prev))
            s.append(bool(spiked))
        if len(s) < self.min_samples:
            # Counted apart from `dropped`: "not judged yet" and "judged
            # noisy" are different states and the log conflates them otherwise.
            self.warming[ticker] = self.warming.get(ticker, 0) + 1
            return False
        if (sum(s) / len(s)) > self.max_rate:
            self.dropped[ticker] = self.dropped.get(ticker, 0) + 1
            return False
        return True


def compute_prob_grid(
    event_ticker: str,
    underlying: str = "SPXW",
    conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS,
    transport_ms: float = 0.0,
    freq: str = "2s",
    tolerance: str = "30s",
    hold_baseline_window: str = "3min",
    hold_multiplier: float = 3.0,
    hold_settle_window: str = "300ms",
    should_abort=None,
):
    """
    `should_abort`, if given, is checked periodically during the expensive
    part of this computation and raises ComputationAborted if it returns
    True -- see bucket_probabilities_ci's own docstring. None (default):
    no checks, no behavior change for any existing caller.

    The FULL per-market probability grid (every market in the event, every
    grid tick) -- precompute_atm_signal below wraps this and reduces it
    down to just whichever ticker is closest to 0.5 at each instant (what
    the live strategy actually needs to trade), but a single market's own
    option-implied probability is a genuine, continuous quantity computed
    straight from the option chain regardless of whether that market
    happened to be ATM at any given moment. Split out specifically for the
    dashboard (viz/data_loader.py) to plot ONE selected market's implied
    probability continuously across the whole session -- confirmed
    directly that precompute_atm_signal's own reduced `events` output is
    NOT suitable for that: it only contains entries for whichever ticker
    was ATM at each instant, so a market's own debug_log-derived quote_df
    (also ATM-scoped, one level up) can have real multi-minute-plus gaps
    whenever ATM moves elsewhere, which a naive step-line chart then
    visually bridges into a misleading "flat" stretch even though the
    option-implied probability for that market was being computed the
    whole time (confirmed on a real day: KXINX-26AUG25H1600's B7637 has a
    75-minute gap in its debug_log around 16:00-17:00 UTC purely because
    ATM moved to a different bucket, not because of any real staleness).

    Returns (markets, grid, probs_lo, probs_mid, probs_hi) -- the last
    three are None if the grid is empty (see `_grid`), or normal (possibly
    all-NaN-for-some-columns) DataFrames otherwise, grid index x one
    column per market ticker. probs_lo/probs_hi are the UNION of the
    call-side and put-side bounds (see bucket_probabilities_ci's own
    docstring) -- wide enough on their own to absorb call/put disagreement
    directly, no separate divergence signal needed.
    """
    # Checked before EACH potentially-slow stage below, not just inside
    # bucket_probabilities_ci's own 6 fits -- confirmed directly that
    # load_event/build_mids/hold_through_wide_spread (parsing/replaying
    # the raw cached ticks into a grid, potentially tens of millions of
    # rows for a busy SPXW day) can take AS LONG AS the fits themselves,
    # so a check only inside bucket_probabilities_ci still let an
    # already-stale request run through this whole earlier stage
    # pointlessly first. See bucket_probabilities_ci's own docstring for
    # why this exists at all.
    def _check_abort():
        if should_abort is not None and should_abort():
            raise laf.ComputationAborted("aborted before compute_prob_grid could start/continue")

    _check_abort()
    event = laf.load_event(event_ticker, underlying=underlying, include_kalshi_ticks=False,
                           conflate_ms=conflate_ms, transport_ms=transport_ms)
    markets = event["markets"]
    spxw_quotes = event["spxw_quotes"]
    expiry = event["expiry"]
    session_start = event["session_start"]

    grid = laf._grid(session_start, expiry, freq=freq)
    if len(grid) == 0:
        return markets, grid, None, None, None

    _check_abort()

    mids, spreads = laf.build_mids(spxw_quotes, grid, tolerance=tolerance)
    held_mids, held_spreads = laf.hold_through_wide_spread(
        mids, spreads, baseline_window=hold_baseline_window,
        multiplier=hold_multiplier, settle_window=hold_settle_window,
    )

    _check_abort()
    date = expiry.astimezone(dbf.ET).date()
    defs = dbf.get_option_definitions(underlying, date)
    _require_matching_expiry(event_ticker, expiry, defs)

    call_mid, put_mid = laf.build_price_curves(spxw_quotes, held_mids, defs, grid)
    call_spread, put_spread = laf.build_price_curves(spxw_quotes, held_spreads, defs, grid)
    _check_abort()

    probs_lo, probs_mid, probs_hi = laf.bucket_probabilities_ci(
        markets, call_mid, call_spread, put_mid, put_spread, grid, should_abort=should_abort)

    return markets, grid, probs_lo, probs_mid, probs_hi


def precompute_signal(
    event_ticker: str,
    underlying: str = "SPXW",
    conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS,
    transport_ms: float = 0.0,
    freq: str = "2s",
    tolerance: str = "30s",
    hold_baseline_window: str = "3min",
    hold_multiplier: float = 3.0,
    hold_settle_window: str = "300ms",
    round_decimals: int = 3,
    return_grid: bool = False,
    should_abort=None,
) -> tuple[list[dict], list[tuple]]:
    """
    EVERY market's own option-implied probability + CI, as external-event
    tuples -- the signal stream the taking strategy actually consumes.

    This is precompute_atm_signal without the ATM reduction. Each market is
    an independent bet with its own fair value and its own Kalshi touch, and
    the strategy's edge test is per-market either way, so there is nothing to
    gain by pre-selecting one "current" market -- and pre-selecting one costs
    a rollover state machine in the strategy (see
    sp_options_lag_quoting.py's module docstring on why that was removed).

    Deduped PER TICKER rather than globally: a market emits an event only
    when its OWN (prob, prob_lo, prob_hi), rounded to `round_decimals`,
    actually moves. So a quiet bucket costs nothing even though every bucket
    is now eligible -- the event count scales with how much the option
    surface really moves, not with n_markets * n_grid_ticks.

    Returns (markets, events), or (markets, events, grid, probs_lo,
    probs_mid, probs_hi) with return_grid=True -- same shape as
    precompute_atm_signal, and each payload is the same
    {ticker, prob, prob_lo, prob_hi} dict.
    """
    markets, grid, probs_lo, probs_mid, probs_hi = compute_prob_grid(
        event_ticker, underlying=underlying, conflate_ms=conflate_ms,
        transport_ms=transport_ms, freq=freq, tolerance=tolerance,
        hold_baseline_window=hold_baseline_window, hold_multiplier=hold_multiplier,
        hold_settle_window=hold_settle_window, should_abort=should_abort,
    )
    if probs_mid is None or probs_mid.empty:
        empty = markets, []
        return empty + (grid, probs_lo, probs_mid, probs_hi) if return_grid else empty

    # One tracker across all markets, fed in grid order so each ticker's
    # decision uses only ticks at or before that instant -- same information
    # live has. Feeding it per-ticker in column order would still be causal
    # per ticker, but walking the grid keeps the traversal obviously
    # chronological.
    noise = NoiseTracker()
    events: list[tuple] = []
    keep_by_ticker = {}
    for ticker in probs_mid.columns:
        mid = probs_mid[ticker].round(round_decimals)
        lo = probs_lo[ticker].round(round_decimals)
        hi = probs_hi[ticker].round(round_decimals)
        valid = mid.notna() & lo.notna() & hi.notna()
        if not valid.any():
            continue
        # Keep only rows where this ticker's own rounded triple changed --
        # the per-ticker analogue of precompute_atm_signal's `last_key` dedup.
        trip = pd.concat([mid, lo, hi], axis=1)
        changed = trip.ne(trip.shift()).any(axis=1)
        keep_by_ticker[ticker] = valid & (changed | ~valid.shift(fill_value=False))

    for i, t in enumerate(grid):
        for ticker, keep in keep_by_ticker.items():
            if not keep.iloc[i]:
                continue
            p = float(probs_mid.loc[t, ticker])
            if not noise.accept(ticker, p):
                continue   # spiking and reverting -- don't trade it
            events.append((t, {
                "ticker": ticker,
                "prob": p,
                "prob_lo": float(probs_lo.loc[t, ticker]),
                "prob_hi": float(probs_hi.loc[t, ticker]),
            }))
    if noise.dropped:
        worst = sorted(noise.dropped.items(), key=lambda kv: -kv[1])[:5]
        print(f"  [spxw_signal] suppressed {len(noise.dropped)} noisy market(s) "
              f"(spike-and-revert > {noise.max_rate:.0%}): "
              + ", ".join(f"{k.split('-')[-1]}({v})" for k, v in worst))
    if noise.warming:
        print(f"  [spxw_signal] withheld {sum(noise.warming.values())} tick(s) across "
              f"{len(noise.warming)} market(s) still under {noise.min_samples} ticks of history")

    # The engine pushes these onto a time-ordered heap, but sorting here
    # keeps the returned list itself chronological -- callers that plot or
    # slice it directly (markout_analysis._signal_df) assume that ordering,
    # and building per-ticker means it comes out grouped by ticker otherwise.
    events.sort(key=lambda e: e[0])

    if return_grid:
        return markets, events, grid, probs_lo, probs_mid, probs_hi
    return markets, events


def precompute_atm_signal(
    event_ticker: str,
    underlying: str = "SPXW",
    conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS,
    transport_ms: float = 0.0,
    freq: str = "2s",
    tolerance: str = "30s",
    hold_baseline_window: str = "3min",
    hold_multiplier: float = 3.0,
    hold_settle_window: str = "300ms",
    round_decimals: int = 3,
    return_grid: bool = False,
    should_abort=None,
) -> tuple[list[dict], list[tuple]]:
    """
    NOT what the taking strategy uses any more -- see precompute_signal
    above, which emits every market rather than reducing to whichever is
    closest to 50/50. Kept because the reduced stream is still the right
    input for anything that genuinely wants a single at-the-money series
    (e.g. an ATM-focused diagnostic), and because dropping it would break
    callers for no benefit.

    return_grid=True additionally returns the full (grid, probs_lo,
    probs_mid, probs_hi) this function already computes internally (see
    compute_prob_grid) -- for a caller (viz/data_loader.py) that ALSO
    wants the continuous per-market grid and would otherwise have to call
    compute_prob_grid a second time, redoing the same ~6 spline fits this
    function just did. Confirmed directly that redundant second pass was
    what made loading a single dashboard day take roughly 2x as long as
    necessary -- not literally "running multiple days," just the SAME
    day's pricing computed twice.

    Everything a quoting strategy needs from the option side, precomputed
    off the cached Databento SPXW quotes for `event_ticker` (via
    compute_prob_grid -> latency_arbs_functions.load_event) -- no live
    network calls, matching load_event's own docstring. Works the same for
    any Kalshi event whose markets carry floor_strike/cap_strike (KXINX's
    two-sided range buckets, KXINXU's single-sided floor-only ladder, or
    any future series shaped like either) -- nothing here is specific to
    one market family.

    For each grid instant picks whichever market's own implied probability
    sits closest to 0.5 (the ATM one at that instant) and emits one
    (true_time, payload) external-event tuple -- feed straight into
    kalshi_backtest.engine.Backtest.run(external_events=...), same
    convention as strategies/crossing_arb.find_crossing_signals.

    Deduped like book.py's replay_ws_ticks / bucket_probabilities' touch
    stream: consecutive grid points are collapsed whenever the ATM ticker
    AND its (prob, prob_lo, prob_hi), rounded to `round_decimals`, are
    unchanged -- so a strategy only wakes up on the option side when
    something about its own fair value actually moved, not on every 2s
    grid tick regardless.

    Returns (markets, events) -- `markets` is load_event's own raw Kalshi
    market dicts (floor_strike/cap_strike/ticker/...) for the whole event,
    `events` is the (possibly empty, if no SPXW data survives the CI
    pipeline) list of external-event tuples, each payload a dict with keys
    ticker/prob/prob_lo/prob_hi. With return_grid=True, returns
    (markets, events, grid, probs_lo, probs_mid, probs_hi) instead.
    """
    markets, grid, probs_lo, probs_mid, probs_hi = compute_prob_grid(
        event_ticker, underlying=underlying, conflate_ms=conflate_ms,
        transport_ms=transport_ms, freq=freq, tolerance=tolerance,
        hold_baseline_window=hold_baseline_window, hold_multiplier=hold_multiplier,
        hold_settle_window=hold_settle_window, should_abort=should_abort,
    )
    if probs_mid is None or probs_mid.empty:
        empty = markets, []
        return empty + (grid, probs_lo, probs_mid, probs_hi) if return_grid else empty

    valid_row = probs_mid.notna().any(axis=1)
    dist_from_atm = (probs_mid - 0.5).abs()
    atm_ticker = pd.Series(index=grid, dtype=object)
    atm_ticker.loc[valid_row] = dist_from_atm.loc[valid_row].idxmin(axis=1)

    events: list[tuple] = []
    last_key = None
    for t, ticker, ok in zip(grid, atm_ticker, valid_row):
        if not ok or pd.isna(ticker):
            continue
        p, lo, hi = probs_mid.loc[t, ticker], probs_lo.loc[t, ticker], probs_hi.loc[t, ticker]
        if pd.isna(p) or pd.isna(lo) or pd.isna(hi):
            continue
        key = (ticker, round(float(p), round_decimals), round(float(lo), round_decimals),
               round(float(hi), round_decimals))
        if key == last_key:
            continue
        last_key = key
        events.append((t, {"ticker": ticker, "prob": float(p), "prob_lo": float(lo), "prob_hi": float(hi)}))

    if return_grid:
        return markets, events, grid, probs_lo, probs_mid, probs_hi
    return markets, events
