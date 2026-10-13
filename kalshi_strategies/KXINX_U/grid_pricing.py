"""
Loads a KXINX event's cached SPXW option quotes (databento_functions) and
cached Kalshi BBO (predexon_functions), and turns them into an
option-implied probability time series for every Kalshi bucket in the
event, directly comparable to Kalshi's own bid/ask on that bucket --
model-free (Breeden-Litzenberger: P(S_T>K) = -dC/dK), not a Black-76/vol
model, so there's no forward or time-to-expiry anywhere in this pipeline --
see bucket_probabilities and pricing.prob_above_finite_diff.

All the actual data (option quotes, Kalshi ticks) is read from disk caches
already populated by kalshi_infra/databento_functions.py and
kalshi_infra/predexon_functions.py -- nothing here makes a network call.
"""
import datetime
import os

import numpy as np
import pandas as pd

from kalshi_infra import databento_functions as dbf
from kalshi_infra import predexon_functions as pf
from kalshi_strategies.KXINX_U import bl_pricing as pricing
from kalshi_strategies.KXINX_U import ib_emulation as ibe


class ComputationAborted(Exception):
    """Raised by bucket_probabilities_ci (via its optional `should_abort`
    callback) when a caller has signaled this computation is no longer
    wanted partway through -- e.g. kalshi_viz's dashboard, where
    switching to a different day mid-load left the OLD day's ~6-fit
    pricing pass (confirmed directly: tens of seconds to low minutes)
    running to completion in the background for a result nobody would
    ever see, since Streamlit can't preempt a long, non-Streamlit-aware
    Python call mid-flight the way it can its own reruns. Callers that
    never pass should_abort (the default, None) never see this."""

ET = dbf.ET


def load_event(event_ticker: str, underlying: str = "SPXW", tickers: list[str] = None,
                include_kalshi_ticks: bool = True,
                conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS,
                transport_ms: float = 0.0) -> dict:
    """
    conflate_ms/transport_ms degrade the option quotes to IB's snapshot
    cadence before anything downstream sees them (see ib_emulation) --
    the backtest then measures the edge on the feed we intend to trade on.
    Pass conflate_ms=0 for the raw Databento-fidelity baseline.

    Everything needed for one event, all from local cache:
      markets       -- raw Kalshi market dicts for *every* bucket in the event (ticker, floor_strike,
                       cap_strike, subtitle, ...), regardless of `tickers` -- only the SPXW pull is scoped
      expiry        -- the event's close_time (also the SPXW 0DTE expiry)
      spxw_quotes   -- long-form cmbp-1 quotes (raw_symbol, strike_price, right, time, bid, ask, ...)
      kalshi_ticks  -- raw Predexon snapshots for every market in the event, restricted to the
                       same session window as spxw_quotes (data outside that window exists --
                       Kalshi opens KXINX events up to a week ahead of close -- but SPXW 0DTE
                       quotes don't exist yet for those earlier days, so there's nothing to compare).
                       Empty DataFrame when include_kalshi_ticks=False.

    Pass `tickers` (a subset of this event's own Kalshi bucket tickers) to only pull SPXW strikes
    bracketing those buckets' boundaries, not every boundary in the event.

    Pass include_kalshi_ticks=False to skip the Predexon orderbook fetch/read
    entirely (no live call, no cache read) -- for a caller (e.g.
    signal_grid.precompute_atm_signal) that never actually reads
    `kalshi_ticks` and wants zero Predexon involvement, not just a cached
    one.
    """
    markets = dbf._kxinx_event_markets(event_ticker)
    if not markets:
        raise ValueError(f"no Kalshi markets found for {event_ticker}")
    expiry = datetime.datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))

    # Conflate INSIDE the loader, per symbol batch, rather than after the full
    # load: it keeps ~8% of rows, so applying it per batch is what keeps the
    # biggest (least gappy) events inside memory at all -- see
    # load_option_quotes_long_df_chunked.
    _conflate = (None if conflate_ms <= 0
                 else lambda df: ibe.conflate_to_ib(df, interval_ms=conflate_ms,
                                                    transport_ms=transport_ms))
    spxw_quotes = dbf.load_kxinx_event_option_quotes(
        event_ticker, underlying=underlying, tickers=tickers, transform=_conflate)
    if spxw_quotes.empty:
        raise ValueError(f"no cached SPXW quotes for {event_ticker} -- "
                         f"run databento_functions first")
    session_start = spxw_quotes["time"].min()

    if include_kalshi_ticks:
        # fetch_event_orderbooks (unlike load_predexon_long_df) doesn't check
        # its own cache before hitting the live Predexon API -- confirmed
        # directly (a second call here re-fetched all 30 markets instead of
        # reading the parquet already on disk from the first). Read the cache
        # file directly so a re-run of this notebook is a free local read,
        # not a ~2min re-pull.
        cache_path = os.path.join(pf.predexon_orderbooks_dir(event_ticker), f"{event_ticker}.parquet")
        if os.path.exists(cache_path):
            raw = pd.read_parquet(cache_path)
        else:
            raw = pf.fetch_event_orderbooks(event_ticker)
        raw["time"] = pd.to_datetime(raw["time"], utc=True)
        kalshi_ticks = raw[(raw["time"] >= session_start) & (raw["time"] <= expiry)].copy()
        kalshi_ticks["bid"] = kalshi_ticks["best_bid"] / 100.0
        kalshi_ticks["ask"] = kalshi_ticks["best_ask"] / 100.0
    else:
        kalshi_ticks = pd.DataFrame()

    return {
        "markets": markets,
        "expiry": expiry,
        "spxw_quotes": spxw_quotes,
        "kalshi_ticks": kalshi_ticks,
        "session_start": session_start,
    }


def _grid(session_start: pd.Timestamp, expiry: pd.Timestamp, freq: str = "2s") -> pd.DatetimeIndex:
    """Sampling lattice the whole option chain is read onto -- NOT a window:
    build_mids does a backward as-of join, so each point is the last known
    quote per contract at that instant, never an aggregate over the interval.
    A shared lattice is what makes the cross-section coherent, which
    Breeden-Litzenberger needs (differencing strikes observed at different
    instants folds the underlying's own move into the curvature).

    2s is a resolution choice, not just a compute one. Measured on
    KXINX-26AUG17H1600 (2026-08-29), NoiseTracker's peak rolling spike rate
    roughly DOUBLES at 500ms -- p25 2.6%->10.0%, median 5.1%->12.5%, max
    17.5%->35.0% -- because Breeden-Litzenberger divides a difference of
    option premiums by a small strike gap, so a less-settled cross-section
    amplifies straight into the estimate. Finer sampling buys ~1.5s of
    reaction time and pays about 2x the estimate noise, and the edge does not
    live at that timescale: markout is insignificant at 30s (t=1.09) and
    strongest at 600s (t=3.10), and conflating the option feed to 100ms/250ms
    changed PnL by $0 across 7 event-days."""
    session_start, expiry = pd.Timestamp(session_start), pd.Timestamp(expiry)
    return pd.date_range(session_start.ceil(freq), expiry.floor(freq), freq=freq)


def _build_mids_chunk(
    quotes: pd.DataFrame, symbols: np.ndarray, grid: pd.DatetimeIndex, tolerance: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    q = quotes[quotes["raw_symbol"].isin(symbols)][["raw_symbol", "time", "bid", "ask"]].copy()
    q["mid"] = (q["bid"] + q["ask"]) / 2.0
    q["spread"] = q["ask"] - q["bid"]
    # merge_asof's `by=` still requires the 'on' column globally sorted, not
    # just sorted within each by-group -- confirmed directly (sorting by
    # ["raw_symbol", "time"], the natural-looking choice, raised "left keys
    # must be sorted" because that leaves 'time' non-monotonic across group
    # boundaries). Sort by time alone; `by` handles the grouping.
    q = q.sort_values("time")

    left = pd.DataFrame({
        # grid.values strips tz (numpy has no tz-aware dtype), so tiling it
        # directly gives a naive datetime64[ns] column that merge_asof then
        # rejects against q['time']'s tz-aware dtype (confirmed directly --
        # MergeError: incompatible merge keys). pd.to_datetime(..., utc=True)
        # re-attaches UTC to match.
        "time": pd.to_datetime(np.tile(grid.values, len(symbols)), utc=True),
        "raw_symbol": np.repeat(symbols, len(grid)),
    }).sort_values("time")
    # Match dtypes by casting the *small* side (left: grid x chunk size, a
    # few hundred thousand rows at most) to q's raw_symbol dtype, not the
    # other way around -- an earlier version cast q's raw_symbol (category,
    # a real memory optimization from databento_functions) up to plain
    # Python str to fix a merge_asof dtype-mismatch error, which worked but
    # confirmed directly it silently reintroduced the exact cost category
    # dtype exists to avoid: on a ~62M-row quote set, materializing every
    # row's ~20-char symbol as its own Python str object was enough on its
    # own to get the whole process OOM-killed.
    if isinstance(q["raw_symbol"].dtype, pd.CategoricalDtype):
        left["raw_symbol"] = left["raw_symbol"].astype(q["raw_symbol"].dtype)

    merged = pd.merge_asof(
        left, q[["raw_symbol", "time", "mid", "spread"]],
        on="time", by="raw_symbol", direction="backward", tolerance=pd.Timedelta(tolerance),
    )
    mids = merged.pivot(index="time", columns="raw_symbol", values="mid")
    spreads = merged.pivot(index="time", columns="raw_symbol", values="spread")
    return mids, spreads


def build_mids(
    quotes: pd.DataFrame, grid: pd.DatetimeIndex, tolerance: str = "30s", symbol_chunk_size: int = 40,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """For each raw_symbol, last-known mid price *and* bid-ask spread on
    `grid` (merge_asof, backward). Returns (mids, spreads), each a wide
    DataFrame indexed by grid time, one column per raw_symbol. NaN where
    the most recent quote is older than `tolerance` or there's no quote
    yet. Spread is tracked alongside mid specifically so
    hold_through_wide_spread can detect and hold through momentary
    liquidity pulls -- see its docstring.

    Processed in chunks of `symbol_chunk_size` symbols -- each chunk filters
    `quotes` down to just its own symbols *before* the copy/sort/merge_asof,
    not after, so peak memory scales with chunk size rather than the total
    event's symbol count.
    """
    symbols = np.asarray(pd.unique(quotes["raw_symbol"]))
    chunks = [
        _build_mids_chunk(quotes, symbols[i:i + symbol_chunk_size], grid, tolerance)
        for i in range(0, len(symbols), symbol_chunk_size)
    ]
    mids = pd.concat([c[0] for c in chunks], axis=1).reindex(grid)
    spreads = pd.concat([c[1] for c in chunks], axis=1).reindex(grid)
    return mids, spreads


def hold_through_wide_spread(
    mids: pd.DataFrame, spreads: pd.DataFrame, baseline_window: str = "3min", multiplier: float = 3.0,
    min_periods: int = 5, settle_window: str = "300ms",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (held_mids, held_spreads) -- both masked/held together with the
    same anomaly mask, so a CI built from held_spreads (see
    bucket_probabilities_ci) stays anchored to the *pre-pull* spread while
    a hold is in effect, rather than pairing a frozen mid with whatever the
    (possibly still-wide) live spread happens to be right now.

    held_mids: per raw_symbol, any grid point whose
    spread is abnormally wide relative to that symbol's own recent
    (trailing, causal) baseline spread is replaced by the last
    *normal*-spread mid instead of the fresh (wide-spread) one -- i.e. the
    probability computed downstream from this "held" mid doesn't move
    during a momentary liquidity pull, only once quotes return to
    something resembling their recent normal width.

    Confirmed directly (raw orderbook inspection around a real spurious
    probability spike) this is a real, recurring pattern on thinner
    strikes: every market maker briefly pulls their offer, leaving the
    NBBO ask as whichever stale, far-away quote still happens to be
    resting -- for anywhere from tens of milliseconds to a couple of
    seconds -- before a competitive quote reappears. Not a real
    repricing, but (via bucket_probabilities' finite difference, dividing
    by a small strike gap) it produces a large, spurious, single-instant
    probability spike that reverts on its own a few seconds later.

    baseline = trailing median spread over `baseline_window`, using only
    *prior* grid points (`.shift(1)` -- so the anomalous point itself
    never counts toward its own baseline). Median, not mean, so it isn't
    dragged up by the tail end of the same anomaly if one lands inside the
    window. `multiplier` x baseline is the "abnormally wide" threshold --
    tune against known spikes, see notebook. Points before enough history
    exists for a baseline (`min_periods`) are left as their original mid,
    not held -- there's nothing to compare them against yet.

    The hold doesn't release the instant the spread first looks normal
    again -- it requires `settle_window` of *continuously* normal spread
    first. Confirmed directly this matters, on a real case: a genuine
    multi-second liquidity pull passed through one single ~260ms window,
    mid-recovery, where the spread happened to look completely normal (one
    market maker's fresh repricing, immediately followed by the spread
    blowing back out again for several more seconds) -- releasing the hold
    right there accepted that momentary, not-yet-stable quote as "settled"
    and fed its (meaningfully different) mid straight into the probability,
    when the market was in fact still unsettled for several seconds
    afterward. Requiring a full settle_window of calm quotes before
    releasing means a blip that short can't get through.
    """
    baseline = spreads.shift(1).rolling(baseline_window, min_periods=min_periods).median()
    anomalous = (spreads > (multiplier * baseline)).fillna(False)
    unsettled = anomalous.rolling(settle_window, min_periods=1).max().astype(bool)
    held_mids = mids.where(~unsettled).ffill().where(baseline.notna(), mids)
    held_spreads = spreads.where(~unsettled).ffill().where(baseline.notna(), spreads)
    return held_mids, held_spreads


def _fetched_meta(quotes: pd.DataFrame, defs: pd.DataFrame) -> pd.DataFrame:
    """defs, restricted to exactly the raw_symbols present in `quotes`.

    Passing the *full* defs (every expiry, from get_option_definitions)
    straight through is a footgun: many strike levels are listed under
    several expiries at once, so meta["strike_price"] == k can match more
    than one raw_symbol at that price and silently pick the wrong expiry's
    contract. Restricting to quotes' own symbols makes the (strike, right)
    -> raw_symbol mapping unambiguous by construction, since
    load_kxinx_event_option_quotes already fetched only one expiry's
    contracts."""
    fetched = set(quotes["raw_symbol"].unique())
    return defs[defs["raw_symbol"].isin(fetched)].drop_duplicates(subset=["raw_symbol"])


def build_price_curves(quotes: pd.DataFrame, mids: pd.DataFrame, defs: pd.DataFrame, grid: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Wide call and put mid-price DataFrames, indexed by grid time, one
    column per listed strike (ascending) -- the input bucket_probabilities'
    Breeden-Litzenberger estimate needs. `mids` is build_mids(quotes, grid).
    Returns (call_curve, put_curve); either can have fewer strikes than the
    other if, at some strike, only one side ever had a valid quote.
    """
    meta = _fetched_meta(quotes, defs).set_index("raw_symbol")[["strike_price", "instrument_class"]]
    strikes = sorted(meta["strike_price"].unique())
    call_cols, put_cols = {}, {}
    for k in strikes:
        syms = meta[meta["strike_price"] == k]
        c = syms[syms["instrument_class"] == "C"].index
        p = syms[syms["instrument_class"] == "P"].index
        if len(c) and c[0] in mids.columns:
            call_cols[k] = mids[c[0]]
        if len(p) and p[0] in mids.columns:
            put_cols[k] = mids[p[0]]
    return pd.DataFrame(call_cols, index=grid), pd.DataFrame(put_cols, index=grid)


def bucket_probabilities(
    markets: list[dict], call_curve: pd.DataFrame, put_curve: pd.DataFrame, grid: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Model-free (Breeden-Litzenberger) option-implied probability of each
    Kalshi bucket, on `grid`: P(S_T > K) = -dC/dK (equivalently 1 - dP/dK
    from puts), via pricing.prob_above_finite_diff -- no volatility, no
    time-to-expiry, no forward, no Black-76 anywhere. Averages the call-
    and put-based estimates when both sides have data at a given instant.
    Range buckets: P(floor < S_T <= cap) = P(S_T>floor) - P(S_T>cap).
    'greater' (cap missing): P(S_T > floor). 'less' (floor missing):
    1 - P(S_T > cap).
    """
    call_strikes = np.array(call_curve.columns, dtype=float)
    put_strikes = np.array(put_curve.columns, dtype=float)
    call_arr = call_curve.to_numpy()
    put_arr = put_curve.to_numpy()

    def prob_above_series(level: float) -> np.ndarray:
        estimates = []
        if call_arr.shape[1] >= 2:
            estimates.append(pricing.prob_above_finite_diff(level, call_strikes, call_arr, is_call=True))
        if put_arr.shape[1] >= 2:
            estimates.append(pricing.prob_above_finite_diff(level, put_strikes, put_arr, is_call=False))
        if not estimates:
            return np.full(len(grid), np.nan)
        return np.nanmean(np.vstack(estimates), axis=0)

    cols = {}
    level_cache: dict[float, np.ndarray] = {}

    def cached_prob_above(level):
        if level not in level_cache:
            level_cache[level] = prob_above_series(level)
        return level_cache[level]

    for m in markets:
        floor = float(m["floor_strike"]) if m.get("floor_strike") is not None else None
        cap = float(m["cap_strike"]) if m.get("cap_strike") is not None else None
        if floor is not None and cap is not None:
            prob = cached_prob_above(floor) - cached_prob_above(cap)
        elif floor is not None:
            prob = cached_prob_above(floor)
        elif cap is not None:
            prob = 1.0 - cached_prob_above(cap)
        else:
            continue
        # Clip to a valid probability range. Small negative/>1 excursions
        # are a real, expected artifact of a discrete finite difference on
        # noisy bid-ask-driven quotes, not a bug.
        cols[m["ticker"]] = np.clip(prob, 0.0, 1.0)

    return pd.DataFrame(cols, index=grid)


def bucket_probabilities_ci(
    markets: list[dict],
    call_mid: pd.DataFrame, call_spread: pd.DataFrame,
    put_mid: pd.DataFrame, put_spread: pd.DataFrame,
    grid: pd.DatetimeIndex,
    should_abort=None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    `should_abort`, if given, is a zero-arg callable checked between each
    of the 6 spline-fitting passes below (the overwhelming majority of
    this function's runtime) -- raises ComputationAborted the moment it
    returns True, instead of running the remaining (wasted) fits to
    completion. None (the default) skips these checks entirely, so every
    existing caller that doesn't pass it is completely unaffected. See
    kalshi_viz for the one caller that does, and
    ComputationAborted's own docstring for why.

    Returns (probs_lo, probs, probs_hi). `probs` matches
    bucket_probabilities exactly (the mid-based point estimate); probs_lo/
    probs_hi are the WIDEST achievable band across BOTH sides of the
    chain -- min(call_lo, put_lo) to max(call_hi, put_hi), not an average
    -- at each instant (see pricing.prob_above_finite_diff_bounds for how
    one side's own [lo, hi] comes from its quoted [bid, ask]).

    Union, not average, specifically so this ALSO absorbs call/put
    disagreement into the same band, with no separate divergence signal
    needed: two sides that each quote a narrow, but mutually
    CONTRADICTORY, range (a stale/thin/mispriced side) produce a wide
    union band on their own, since the union has to stretch to cover both
    sides' ranges -- an average of two disagreeing narrow ranges would
    instead land somewhere in between and stay narrow, hiding exactly the
    disagreement that matters. An EARLIER version of this function
    computed that disagreement (|call-only estimate - put-only estimate|)
    as its own separate `probs_divergence` return value, additively folded
    into the strategy's own edge calculation
    (QuotingConfig.parity_divergence_weight) -- retired in favor of this,
    since the union band captures the same information as part of one
    coherent confidence interval instead of a second, separately-tuned term.

    `call_spread`/`put_spread`: wide DataFrames shaped like `call_mid`/
    `put_mid` (e.g. build_price_curves(quotes, spreads, defs, grid) --
    that function is generic over whatever per-symbol series it's given,
    mid or spread).
    """
    call_strikes = np.array(call_mid.columns, dtype=float)
    put_strikes = np.array(put_mid.columns, dtype=float)
    call_mid_arr, call_spread_arr = call_mid.to_numpy(), call_spread.to_numpy()
    put_mid_arr, put_spread_arr = put_mid.to_numpy(), put_spread.to_numpy()
    call_bid_arr, call_ask_arr = call_mid_arr - call_spread_arr / 2, call_mid_arr + call_spread_arr / 2
    put_bid_arr, put_ask_arr = put_mid_arr - put_spread_arr / 2, put_mid_arr + put_spread_arr / 2

    # Fit each price array's per-row spline ONCE up front (see
    # pricing.fit_price_splines's own docstring) -- this function queries
    # one level per market boundary (~50+ for a wide event) against these
    # SAME 6 arrays, and fitting fresh per level (rather than once here,
    # shared across every level below) was confirmed directly to turn a
    # ~7s-per-array cost into many minutes for a whole event.
    has_calls, has_puts = call_mid_arr.shape[1] >= 2, put_mid_arr.shape[1] >= 2
    print(f"  [bucket_probabilities_ci] fitting splines (has_calls={has_calls}, has_puts={has_puts})...")

    def _check_abort(label: str):
        print(f"    {label}")
        if should_abort is not None and should_abort():
            raise ComputationAborted(f"aborted before {label}")

    _check_abort("fit 1/6: call mid")
    call_mid_splines = pricing.fit_price_splines(call_strikes, call_mid_arr) if has_calls else None
    _check_abort("fit 2/6: call bid")
    call_bid_splines = pricing.fit_price_splines(call_strikes, call_bid_arr) if has_calls else None
    _check_abort("fit 3/6: call ask")
    call_ask_splines = pricing.fit_price_splines(call_strikes, call_ask_arr) if has_calls else None
    _check_abort("fit 4/6: put mid")
    put_mid_splines = pricing.fit_price_splines(put_strikes, put_mid_arr) if has_puts else None
    _check_abort("fit 5/6: put bid")
    put_bid_splines = pricing.fit_price_splines(put_strikes, put_bid_arr) if has_puts else None
    _check_abort("fit 6/6: put ask")
    put_ask_splines = pricing.fit_price_splines(put_strikes, put_ask_arr) if has_puts else None
    print("  [bucket_probabilities_ci] splines ready, evaluating boundary levels...")

    def call_above(level: float) -> np.ndarray:
        if not has_calls:
            return np.full(len(grid), np.nan)
        return pricing.prob_above_from_splines(call_mid_splines, level, is_call=True)

    def put_above(level: float) -> np.ndarray:
        if not has_puts:
            return np.full(len(grid), np.nan)
        return pricing.prob_above_from_splines(put_mid_splines, level, is_call=False)

    def prob_above_bounds(level: float) -> tuple[np.ndarray, np.ndarray]:
        los, his = [], []
        if has_calls:
            lo, hi = pricing.prob_above_bounds_from_splines(call_bid_splines, call_ask_splines, level, is_call=True)
            los.append(lo)
            his.append(hi)
        if has_puts:
            lo, hi = pricing.prob_above_bounds_from_splines(put_bid_splines, put_ask_splines, level, is_call=False)
            los.append(lo)
            his.append(hi)
        if not los:
            nan_arr = np.full(len(grid), np.nan)
            return nan_arr, nan_arr
        # nan_safe_min_max, not bare np.nanmin/np.nanmax: a grid instant
        # where NEITHER side has a usable slope is all-NaN, which those warn
        # about ("All-NaN slice encountered") once per level per event. NaN
        # is the correct value there and downstream already treats it as
        # "no estimate" -- see pricing.nan_safe_min_max.
        lo_arr = pricing.nan_safe_min_max(np.vstack(los))[0]   # tightest lower bound across sides
        hi_arr = pricing.nan_safe_min_max(np.vstack(his))[1]   # widest upper bound across sides
        return lo_arr, hi_arr

    call_cache: dict[float, np.ndarray] = {}
    put_cache: dict[float, np.ndarray] = {}
    bounds_cache: dict[float, tuple[np.ndarray, np.ndarray]] = {}

    def cached_call(level):
        if level not in call_cache:
            call_cache[level] = call_above(level)
        return call_cache[level]

    def cached_put(level):
        if level not in put_cache:
            put_cache[level] = put_above(level)
        return put_cache[level]

    def cached_mid(level):
        # nanmean over [call, put] reproduces the old "average whichever
        # side(s) are actually available" behavior for free: a side with
        # <2 strikes is all-NaN here, and nanmean skips NaNs elementwise.
        return np.nanmean(np.vstack([cached_call(level), cached_put(level)]), axis=0)

    def cached_bounds(level):
        if level not in bounds_cache:
            bounds_cache[level] = prob_above_bounds(level)
        return bounds_cache[level]

    cols_lo, cols_mid, cols_hi = {}, {}, {}
    for m in markets:
        floor = float(m["floor_strike"]) if m.get("floor_strike") is not None else None
        cap = float(m["cap_strike"]) if m.get("cap_strike") is not None else None
        if floor is not None and cap is not None:
            prob_mid = cached_mid(floor) - cached_mid(cap)
            floor_lo, floor_hi = cached_bounds(floor)
            cap_lo, cap_hi = cached_bounds(cap)
            # widest/narrowest achievable DIFFERENCE, not "everything at its
            # own hi" -- to maximize floor-cap, take floor as high as
            # possible and cap as low as possible, and vice versa.
            prob_hi = floor_hi - cap_lo
            prob_lo = floor_lo - cap_hi
        elif floor is not None:
            prob_mid = cached_mid(floor)
            prob_lo, prob_hi = cached_bounds(floor)
        elif cap is not None:
            prob_mid = 1.0 - cached_mid(cap)
            cap_lo, cap_hi = cached_bounds(cap)
            prob_lo, prob_hi = 1.0 - cap_hi, 1.0 - cap_lo
        else:
            continue
        # Translate into [0, 1] rather than clipping each bound separately --
        # see pricing.clip_band_preserving_width on why a plain clip reported
        # false certainty on most grid points.
        lo_c, mid_c, hi_c = pricing.clip_band_preserving_width(prob_lo, prob_mid, prob_hi)
        cols_mid[m["ticker"]] = mid_c
        cols_lo[m["ticker"]] = lo_c
        cols_hi[m["ticker"]] = hi_c

    return (pd.DataFrame(cols_lo, index=grid), pd.DataFrame(cols_mid, index=grid),
            pd.DataFrame(cols_hi, index=grid))


def filter_confident_buckets(
    probs_lo: pd.DataFrame, probs_hi: pd.DataFrame, tickers: list[str] = None,
    max_median_width: float = 0.05, keep_idx=None,
) -> list[str]:
    """
    Tickers whose median (probs_hi - probs_lo) confidence-band width, over
    `keep_idx` (or the whole grid if not given), is at or below
    `max_median_width` -- i.e. buckets whose bracketing strikes are
    liquid/tight enough that the finite-difference estimate is usually
    genuinely confident, not just usually close by luck.

    Buckets that don't clear this bar are better left out of any "how well
    does this track Kalshi" analysis entirely than chased with yet another
    special-cased noise filter -- confirmed directly this is what was
    actually driving a deep, essentially-impossible-to-happen bucket's
    probability to occasionally read 0.15-0.23 instead of ~0: its
    bracketing strikes carry large dollar premiums (deep ITM puts), so
    even everyday, non-anomalous bid-ask noise on them is large enough in
    *absolute* terms, relative to the small strike gap being divided by,
    to swing the estimate a lot -- no single spread-widening or settle-time
    threshold fixes that, because nothing ever looked anomalous on either
    strike individually.
    """
    if tickers is None:
        tickers = list(probs_lo.columns)
    width = probs_hi - probs_lo
    if keep_idx is not None:
        width = width[keep_idx]
    medians = width[tickers].median()
    return medians[medians <= max_median_width].index.tolist()


def kalshi_fair_series(kalshi_ticks: pd.DataFrame, grid: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Per-ticker bid/ask/mid on `grid` (last-known, forward-filled --
    Kalshi quotes are much sparser than SPXW's, so no tolerance cutoff here
    the way build_mids uses one for the fast-moving option book)."""
    out = {}
    for ticker, g in kalshi_ticks.groupby("ticker"):
        g = g.sort_values("time")
        merged = pd.merge_asof(
            pd.DataFrame({"time": grid}), g[["time", "bid", "ask"]],
            on="time", direction="backward",
        )
        merged["mid"] = (merged["bid"] + merged["ask"]) / 2.0
        out[ticker] = merged.set_index("time")
    return out


def find_worst_fit_moments(
    probs: pd.DataFrame, kalshi_fair: dict[str, pd.DataFrame], tickers: list[str] = None,
    n: int = 10, min_gap: str = "15min", session_start: pd.Timestamp = None, exclude_after_open: str = None,
    expiry: pd.Timestamp = None, exclude_before_expiry: str = None,
    probs_lo: pd.DataFrame = None, probs_hi: pd.DataFrame = None, max_ci_width: float = None,
    sustain_window: str = None,
) -> list[dict]:
    """
    Model-fit diagnostic, pooled across every ticker in `tickers` (default:
    every bucket in `probs`): how far outside Kalshi's *own quoted band*
    does the option-implied probability sit, and when. Distance is 0
    whenever probs[ticker] falls inside [kalshi_fair[ticker]['bid'], 'ask']
    (the model agrees with the market to within its own quoted width);
    positive when the option-implied probability is above Kalshi's own
    ask; negative when it's below Kalshi's own bid.

    At most one candidate per (ticker, min_gap) window (so one bucket's
    persistent stale patch can't fill up the whole top-n), but not deduped
    *across* tickers -- several buckets sitting far outside Kalshi's band
    at the same moment are distinct, real instances, not one event.

    Pass `session_start` + `exclude_after_open` (e.g. "5min") to drop thin,
    price-discovery-y liquidity right after open, and/or `expiry` +
    `exclude_before_expiry` (e.g. "10min") to drop the window right before
    settlement -- confirmed directly that's a real noise source distinct
    from the crossed-quote guard below: even a *valid* (non-crossed) Kalshi
    quote right at close can reflect the book thinning out/one side pulling
    orders as the market's about to settle, not a genuine intraday
    disagreement worth surfacing.

    Pass `probs_lo`/`probs_hi` (from bucket_probabilities_ci) + `max_ci_width`
    to also require *our own* confidence band be reasonably tight at that
    specific instant, not just that the bucket is confident on average --
    filter_confident_buckets screens out buckets that are unreliable
    overall, but a normally-reliable bucket can still pass through one
    genuinely wide-CI moment (e.g. mid-recovery from a severe, sustained
    market-wide liquidity event, longer than hold_through_wide_spread's
    settle_window is worth chasing with an ever-longer hold -- confirmed
    directly that doing so trades away real responsiveness elsewhere for
    diminishing returns on one severe episode). A wide CI at that moment
    already means "we don't know," which is a different, better answer
    than either trusting the point estimate or holding through it -- so
    exclude it from "confident disagreement" instead of either of those.

    Pass `sustain_window` (e.g. "1s") to additionally require the distance
    be *sustained*, not a single flickering sample -- ranks (and reports)
    a centered rolling-median-smoothed distance instead of the raw
    instantaneous one, so an isolated blip that's gone again a couple of
    grid points later can't win just by being a one-sample outlier, the
    same "sustained, not single-step" reasoning used elsewhere in this
    pipeline for jump detection. `prob`/`kalshi_bid`/`kalshi_ask` in the
    result are still the *raw* values at that exact instant, only the
    ranking distance is smoothed.

    Returns [{"ticker", "time", "distance", "prob", "kalshi_bid",
    "kalshi_ask"}, ...], largest |distance| first, across the whole pool.
    """
    if tickers is None:
        tickers = list(probs.columns)

    candidates = []
    for ticker in tickers:
        if ticker not in probs.columns or ticker not in kalshi_fair:
            continue
        p = probs[ticker]
        kf = kalshi_fair[ticker].reindex(p.index)
        # kf["bid"] <= kf["ask"] excludes crossed Kalshi quotes -- confirmed
        # directly these are real, not rare: a stale/empty book right at
        # settlement showed up as e.g. bid=0.99, ask=0.00, which would
        # otherwise win "most outside Kalshi's band" by construction (a
        # trivially crossed band) rather than reflecting any genuine
        # options-vs-Kalshi disagreement.
        valid = p.notna() & kf["bid"].notna() & kf["ask"].notna() & (kf["bid"] <= kf["ask"])
        if probs_lo is not None and probs_hi is not None and max_ci_width is not None and ticker in probs_lo:
            ci_width = (probs_hi[ticker] - probs_lo[ticker]).reindex(p.index)
            valid = valid & (ci_width <= max_ci_width)
        p, kf = p[valid], kf[valid]
        if session_start is not None and exclude_after_open is not None:
            cutoff = pd.Timestamp(session_start) + pd.Timedelta(exclude_after_open)
            keep = p.index >= cutoff
            p, kf = p[keep], kf[keep]
        if expiry is not None and exclude_before_expiry is not None:
            cutoff = pd.Timestamp(expiry) - pd.Timedelta(exclude_before_expiry)
            keep = p.index <= cutoff
            p, kf = p[keep], kf[keep]
        if p.empty:
            continue

        dist = pd.Series(0.0, index=p.index)
        above = p > kf["ask"]
        below = p < kf["bid"]
        dist[above] = (p - kf["ask"])[above]
        dist[below] = (p - kf["bid"])[below]
        if sustain_window is not None:
            dist = dist.rolling(sustain_window, center=True, min_periods=1).median()
        dist = dist[dist != 0]
        for t, d in dist.items():
            candidates.append({
                "ticker": ticker, "time": t, "distance": d, "prob": p.loc[t],
                "kalshi_bid": kf["bid"].loc[t], "kalshi_ask": kf["ask"].loc[t],
            })

    candidates.sort(key=lambda c: abs(c["distance"]), reverse=True)
    selected = []
    for c in candidates:
        if all(
            not (c["ticker"] == s["ticker"] and abs(c["time"] - s["time"]) <= pd.Timedelta(min_gap))
            for s in selected
        ):
            selected.append(c)
        if len(selected) == n:
            break
    return selected
