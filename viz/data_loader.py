"""
Data loading for the SPXW latency-arb dashboard: turns a backtest run (and
eventually a live trading session -- see DayData's docstring) into one
common shape the chart builders in charts.py consume, so neither app.py nor
charts.py needs to know whether the data came from a backtest replay or a
live feed, or which Kalshi series (KXINX or KXINXU) it's for.

Series support: both kalshi_latency_arbs/spxw_kxinx/run_backtest.py and
spxw_kxinxu/run_backtest.py already expose the same shape
(discover_runnable_events(), run_one(), and a module-level `strat` alias
for their respective strategy module) -- see this module's _load_series()
for how both get loaded here despite sharing the filename "run_backtest.py"
across their two folders (a plain `import run_backtest` for the second one
would silently resolve to whichever is already in sys.modules under that
name; same collision spxw_kxinxu/kxinxu_lag_quoting.py already had to work
around to reach sp_options_lag_quoting.py, see its own docstring).
"""
import dataclasses
import importlib.util
import os
import sys

import pandas as pd
import streamlit as st

import backtest_cache

_VIZ_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.join(_VIZ_DIR, "..")
_ARBS_DIR = os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs")
_KXINX_DIR = os.path.join(_ARBS_DIR, "spxw_kxinx")
_KXINXU_DIR = os.path.join(_ARBS_DIR, "spxw_kxinxu")
_ANALYSIS_DIR = os.path.join(_KXINX_DIR, "analysis")  # shared by both series -- see spxw_kxinxu/run_backtest.py's docstring
_BACKTEST_DIR = os.path.join(_PROJECT_ROOT, "kalshi_backtest")
_LIVE_DIR = os.path.join(_ARBS_DIR, "live")
for _p in (_ARBS_DIR, _KXINX_DIR, _KXINXU_DIR, _ANALYSIS_DIR, _BACKTEST_DIR, _LIVE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import data as bt_data
import engine
import results
import markout_analysis as ma
import live_journal


def _load_module_from_path(module_name: str, file_path: str):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


SERIES = ("KXINX", "KXINXU", "KXCRYPTOLEAD15M")

# KXCRYPTOLEAD15M lives in kalshi_crypto/. Its shim exposes a
# run_backtest-shaped surface (discover_runnable_events / run_one / strat)
# so it slots into _RB without a separate branch in the dashboard. Live
# mode reads data/live/KXCRYPTOLEAD15M/{date}/ -- the same shape run_live
# writes for KXINX/KXINXU. Historical mode is not yet fully wired for
# this series; the shim's precompute_signal returns empty grids so viz
# panels that read them render as blank rather than crash.
_KALSHI_CRYPTO_DIR = os.path.join(_PROJECT_ROOT, "kalshi_crypto")
if _KALSHI_CRYPTO_DIR not in sys.path:
    sys.path.insert(0, _KALSHI_CRYPTO_DIR)

_RB = {
    "KXINX": _load_module_from_path("_kxinx_run_backtest", os.path.join(_KXINX_DIR, "run_backtest.py")),
    "KXINXU": _load_module_from_path("_kxinxu_run_backtest", os.path.join(_KXINXU_DIR, "run_backtest.py")),
    "KXCRYPTOLEAD15M": _load_module_from_path(
        "_kalshi_crypto_viz_shim",
        os.path.join(_PROJECT_ROOT, "kalshi_crypto", "viz_shim.py")),
}


def strategy_module(series: str):
    """The `strat` module (sp_options_lag_quoting or kxinxu_lag_quoting)
    backing `series` -- e.g. `strategy_module("KXINX").QuotingConfig()`.
    Public accessor so callers (app.py) don't need to reach into _RB."""
    return _RB[series].strat


@dataclasses.dataclass
class DayData:
    """Everything one dashboard page needs for one session -- backtest
    today, but shaped so a future load_live_day() (streaming from the real
    feed once it's connected) could return the exact same fields and reuse
    every chart in charts.py unmodified: quote_df's columns are already
    keyed off SPOptionsLagQuotingStrategy.debug_log, which the strategy
    itself produces identically regardless of series or where its data
    came from."""
    event_ticker: str
    series: str
    source: str
    fidelity: str
    config: object  # the matching series' QuotingConfig
    quote_df: pd.DataFrame   # one row per decision: time, ticker, market_bid, market_ask, prob, prob_lo, prob_hi, center, edge, bid_reservation, ask_reservation, net_position, ...
    fills_df: pd.DataFrame   # time, ticker, side, price, size, fee, liquidity, order_id, yes_equiv_price
    pnl_df: pd.DataFrame     # time, pnl (mark-to-market throughout, incl. the real settled total -- see markout_analysis.compute_pnl_over_day)
    summary: dict
    pnl_by_ticker: dict      # ticker -> that market's OWN mark-to-market pnl_df (same shape as pnl_df)
    prob_grid: dict          # {"grid": DatetimeIndex, "probs_mid"/"probs_lo"/"probs_hi": DataFrame (grid x ticker)} -- the FULL, continuous option-implied probability for every market, independent of ATM history (see spxw_signal.compute_prob_grid). quote_df's own prob/prob_lo/prob_hi are ATM-scoped (debug_log only records the strategy's current ticker) and can show a real multi-minute gap whenever ATM moves elsewhere -- use this instead for a market's own implied-prob line so it doesn't visually flatline across those gaps.
    bbo_df: pd.DataFrame = dataclasses.field(default_factory=pd.DataFrame)   # live only: time, ticker, bid, ask, bid_depth, ask_depth -- every Kalshi touch change of the day, recorded whether or not we were trading (see live_journal). Empty for backtests, which read their book from the quote source directly.


def discover_days(series: str = "KXINX") -> list[dict]:
    """Every event in `series` with both a cached Kalshi quote source and a
    full Databento SPXW cache (see the matching run_backtest module's own
    discover_runnable_events), tagged with its quote fidelity -- L2 days
    sorted first, since those are the only ones without predexon
    fidelity-mixing baked in (see kalshi_backtest/data.py's allow_predexon)."""
    rb = _RB[series]
    out = []
    for et in rb.discover_runnable_events():
        avail = bt_data.available_sources(et)
        if avail.get("kalshi_ws"):
            fidelity = "l2"
        elif avail.get("predexon"):
            fidelity = "top_of_book"
        else:
            fidelity = "candle"
        out.append({"event_ticker": et, "fidelity": fidelity})
    return sorted(out, key=lambda d: (d["fidelity"] != "l2", d["event_ticker"]))


def _build_quote_df(strategy) -> pd.DataFrame:
    """The master timeline for the dashboard's main chart, straight from
    strategy.debug_log: one row per _requote call (every decision, both
    signal-driven and same-ticker quote/trade-driven -- see
    SPOptionsLagQuotingStrategy.__call__), already scoped to whichever
    ticker is currently the ATM bucket -- no separate cross-ticker
    stitching needed, since debug_log only ever records the strategy's OWN
    current_ticker."""
    df = pd.DataFrame(strategy.debug_log)
    if df.empty:
        return df
    return df.sort_values("time").reset_index(drop=True)


def _mask_stale_bbo(quote_df: pd.DataFrame, event_data) -> pd.DataFrame:
    """NaNs out market_bid/market_ask wherever the underlying Kalshi feed
    for that row's ticker hadn't had a real update within
    bt_data.STALE_THRESHOLD_S -- the SAME definition/threshold
    engine.Backtest itself uses to refuse trading against a stale book
    (see its own docstring), applied here to visualization instead of
    execution. A naive step-line straight through a stale stretch draws a
    misleading flat "the market didn't move" line when what actually
    happened is the feed simply stopped -- confirmed directly on a real
    day: a Kalshi WS collector outage froze a book at its pre-market state
    and the engine (before this guard existed) kept "trading" against it
    for the entire regular session.

    "Real update" is whichever stream engine.py itself replays into
    true_books for that ticker (raw_ws rows for l2 fidelity, else quotes)
    -- see engine.py's own dual-path book-maintenance handling; a ticker
    with no real ticks at all (shouldn't happen in practice, since
    debug_log only ever records tickers the strategy actually saw
    quotes for) is conservatively treated as always-stale rather than
    always-fresh."""
    if quote_df.empty:
        return quote_df
    quote_df = quote_df.copy()
    stale = pd.Series(True, index=quote_df.index)
    for ticker, sub in quote_df.groupby("ticker"):
        if ticker in event_data.raw_ws:
            real_times = pd.Series(sorted(row.time for row in event_data.raw_ws[ticker]))
        else:
            real_times = pd.Series(sorted(q.time for q in event_data.quotes.get(ticker, [])))
        if real_times.empty:
            continue
        sub_sorted = sub[["time"]].sort_values("time")
        merged = pd.merge_asof(
            sub_sorted, real_times.rename("real_time").to_frame(),
            left_on="time", right_on="real_time", direction="backward",
        )
        age_s = (merged["time"] - merged["real_time"]).dt.total_seconds()
        fresh = (age_s <= bt_data.STALE_THRESHOLD_S).to_numpy()
        stale.loc[sub_sorted.index] = ~fresh
    quote_df.loc[stale, ["market_bid", "market_ask"]] = float("nan")
    return quote_df


def _add_yes_equiv_price(fills: pd.DataFrame) -> pd.DataFrame:
    """Fill prices are in each fill's OWN side's terms (a 'no' fill's price
    is a NO-dollar price -- see execution.resolve_marketable's touch_price)
    -- convert to yes-equivalent (1 - price for 'no') so every fill can be
    plotted on the same yes-probability y-axis as bid/ask/prob. Confirmed
    this matters: an earlier version of the markout plots used a fill's raw
    price directly regardless of side, which only ever looked right by
    coincidence (a crossed book happened to put the NO price near where the
    YES bid visually sat) -- see this project's crossed-book investigation."""
    if fills.empty:
        return fills.assign(yes_equiv_price=[])
    fills = fills.copy()
    fills["yes_equiv_price"] = fills["price"].where(fills["side"] == "yes", 1.0 - fills["price"])
    return fills


def load_backtest_day(event_ticker: str, series: str = "KXINX", config=None,
                       allow_predexon: bool = True, use_cache: bool = True) -> DayData:
    """Runs the real backtest (record_debug=True -- needed for the
    dashboard's edge/reservation lines, see _build_quote_df) and shapes the
    result into DayData. allow_predexon defaults True, matching run_one, so
    the dashboard and the backtest sweeps answer the same question -- they
    disagreed by $2.88 on KXINXU-26AUG27H1600 while these defaults differed.
    Days with no kalshi_ws capture at all (2026-08-24, KXINXU 2026-08-21)
    are only viewable this way; the resulting DayData carries the real
    `source`/`fidelity`, which the dashboard surfaces so a predexon day is
    never mistaken for an L2 one."""
    rb = _RB[series]
    strat = rb.strat
    config = config or strat.QuotingConfig()
    # Cache is keyed on config + module CONTENT + input-data signature (see
    # backtest_cache), so a hit is only ever a result this exact code would
    # produce now. Changing an edge parameter or editing the strategy misses
    # and recomputes rather than serving the old numbers.
    if use_cache:
        cached = backtest_cache.load(event_ticker, series, config)
        if cached is not None:
            return cached
    event_data = bt_data.load_event_data(event_ticker, allow_predexon=allow_predexon)
    # Cooperative cancellation: st.session_state["_dashboard_request_key"]
    # is set by app.py's main() on EVERY rerun, right before calling into
    # this (indirectly, via the st.cache_data-wrapped _load_cached). If the
    # user switches days/series while THIS call is still deep in its own
    # multi-minute pricing pass, that key will have already moved on to a
    # DIFFERENT value by the time should_abort() is next checked -- confirmed
    # directly this was a real problem: Streamlit reruns the whole script on
    # a widget change, but can't preempt a long, non-Streamlit-aware Python
    # call mid-flight (no st.* calls inside it to interrupt at), so the OLD
    # day's computation just kept running to completion in the background,
    # burning CPU nobody would ever see the result of and visibly slowing
    # down the NEW day's own computation via contention. Not wired into
    # kalshi_backtest.engine.Backtest.run itself (the fill-simulation loop
    # below) -- that's typically fast relative to the pricing pass this
    # guards; only the ~6-spline-fit pricing computation is worth guarding.
    request_key = (series, event_ticker)

    _MISSING = object()

    def should_abort():
        # Abort only when the key is PRESENT and has moved on. app.py sets it
        # on every rerun before calling here, so present-and-different means
        # "the user switched away". Absent means there is no dashboard at all
        # -- a headless caller like warm_backtest_cache -- and the old
        # `.get() != request_key` read None as a mismatch and aborted the
        # pricing pass on its first check, making every non-Streamlit use of
        # this function impossible.
        cur = st.session_state.get("_dashboard_request_key", _MISSING)
        return cur is not _MISSING and cur != request_key

    # return_grid=True: this ALSO gets the full continuous per-market grid
    # (see precompute_signal's own docstring) in the same pass, rather
    # than a second, separate compute_prob_grid call redoing the same ~6
    # spline fits -- confirmed directly that redundant second pass was
    # what made loading a single dashboard day take ~2x as long as needed.
    markets, signal_events, grid, probs_lo, probs_mid, probs_hi = strat.precompute_signal(
        event_ticker, return_grid=True, should_abort=should_abort)
    strategy = strat.SPOptionsLagQuotingStrategy(config, record_debug=True)
    # wire_s measured directly from this event's own Kalshi WS ticks --
    # see kalshi_backtest/data.py's empirical_wire_s docstring.
    bt = engine.Backtest(latency=engine.LatencyModel(wire_s=bt_data.empirical_wire_s(event_ticker)),
                         max_position=config.max_position)
    result = bt.run(strategy.__call__, event_ticker, event_data=event_data, external_events=signal_events)

    quote_df = _mask_stale_bbo(_build_quote_df(strategy), event_data)
    fills = _add_yes_equiv_price(results.fills_df(result))
    # Marked against the option-implied probability, not Kalshi's own touch
    # -- see markout_analysis._mark_series for why (wide BBO understates, and
    # a missing quote used to mark a leg to zero outright).
    pnl_df = ma.compute_pnl_over_day(result, event_data, prob_grid=probs_mid)
    # Per-market curves too, so the PnL panel can show the SELECTED market's
    # own line next to the combined one. Cheap -- the pricing pass is already
    # done, this just re-walks the fills.
    pnl_by_ticker = {
        t: ma.compute_pnl_over_day(result, event_data, prob_grid=probs_mid, tickers=[t])
        for t in fills["ticker"].unique()
    } if not fills.empty else {}

    prob_grid = {"grid": grid, "probs_mid": probs_mid, "probs_lo": probs_lo, "probs_hi": probs_hi}

    day = DayData(
        event_ticker=event_ticker, series=series, source=event_data.source, fidelity=event_data.fidelity,
        config=config, quote_df=quote_df, fills_df=fills, pnl_df=pnl_df,
        summary=results.summary(result), pnl_by_ticker=pnl_by_ticker, prob_grid=prob_grid,
    )
    if use_cache:
        backtest_cache.save(day, event_ticker, series, config)
    return day


def _live_pnl_by_ticker(fills_df: pd.DataFrame, probs_mid: pd.DataFrame,
                         freq: str = "15s") -> dict[str, pd.DataFrame]:
    """Per-market mark-to-market PnL for a LIVE session.

    The backtest gets this from markout_analysis.compute_pnl_over_day, which
    needs a BacktestResult; live has only the flushed fills and probability
    history, so it's rebuilt here from those. Marked at the option-implied
    probability, same convention as the backtest (see _mark_series), so a
    live curve and a backtest curve of the same market mean the same thing.

    Without this, live sessions had pnl_by_ticker={} and the PnL panel fell
    back to the all-markets total for every market -- exactly the reading
    that made a market settling +$0.16 look like a loss.
    """
    if fills_df.empty or probs_mid is None or probs_mid.empty:
        return {}
    f = fills_df.copy()
    f["time"] = pd.to_datetime(f["time"], utc=True)
    out: dict[str, pd.DataFrame] = {}
    for ticker, sub in f.groupby("ticker"):
        if ticker not in probs_mid.columns:
            continue
        sub = sub.sort_values("time")
        grid = pd.date_range(sub["time"].min().floor(freq),
                             max(sub["time"].max(), probs_mid.index.max()).ceil(freq), freq=freq)
        mark = probs_mid[ticker].reindex(probs_mid.index.union(grid)).ffill().reindex(grid).ffill().bfill()
        signed = sub["size"].where(sub["side"] == "yes", -sub["size"])
        # cumulative qty/cost per leg at each grid point
        yes = sub[sub["side"] == "yes"]
        no = sub[sub["side"] == "no"]
        def _cum(g, col):
            if g.empty:
                return pd.Series(0.0, index=grid)
            s = g.set_index("time")[col].groupby(level=0).sum().cumsum()
            return s.reindex(s.index.union(grid)).ffill().reindex(grid).fillna(0.0)
        yq = _cum(yes, "size")
        yc = _cum(yes.assign(c=yes["size"] * yes["price"]), "c")
        nq = _cum(no, "size")
        nc = _cum(no.assign(c=no["size"] * no["price"]), "c")
        fee = _cum(sub, "fee")
        pnl = yq * mark - yc + nq * (1.0 - mark) - nc - fee
        out[ticker] = pd.DataFrame({"time": grid, "pnl": pnl.values}).dropna()
    return out


def load_live_day(series: str) -> DayData:
    """Reads whatever kalshi_latency_arbs/live/run_live.py's own
    flush_snapshot last wrote to data/live/{series}/{today}/ -- that
    process is a separate, long-running standalone script (started
    independently, e.g. on the trading EC2), never launched from here;
    this function is a pure reader. Raises FileNotFoundError if no live
    session has written anything yet today for `series` (e.g. run_live.py
    isn't running, or hasn't been started for today).

    Deliberately NOT @st.cache_data-wrapped (unlike load_backtest_day) --
    live data must be re-read on every call, not memoized.

    Returns the exact same DayData shape load_backtest_day does (per this
    module's own long-standing design intent -- see DayData's docstring),
    so app.py/charts.py need no live-specific branch beyond choosing which
    loader to call."""
    date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    day_dir = os.path.join(_PROJECT_ROOT, "data", "live", series, date)
    summary_path = os.path.join(day_dir, "summary.json")
    if not os.path.exists(summary_path):
        raise FileNotFoundError(f"no live session found for {series} today ({day_dir})")

    import json
    with open(summary_path) as f:
        summary = json.load(f)

    quote_df = pd.read_parquet(os.path.join(day_dir, "quote_df.parquet"))
    fills_df = pd.read_parquet(os.path.join(day_dir, "fills_df.parquet"))
    pnl_df = pd.read_parquet(os.path.join(day_dir, "pnl_df.parquet"))
    prob_long = pd.read_parquet(os.path.join(day_dir, "prob_grid.parquet"))

    # The snapshot above is only ever the CURRENT session: a restart, or
    # arming real trading (which resets the ledger), starts it over. The
    # journal is the day's append-only record, so decisions and fills come
    # from it when it has more -- otherwise the dashboard silently forgets
    # everything before the last restart. PnL deliberately stays
    # session-scoped: it is a curve of one ledger's marks, and splicing two
    # would produce a line that means neither.
    j_dec = live_journal.read_journal(series, date, "decisions")
    j_fills = live_journal.read_journal(series, date, "fills")
    bbo_df = live_journal.read_journal(series, date, "bbo")
    if len(j_dec) > len(quote_df):
        quote_df = j_dec
    if len(j_fills) > len(fills_df):
        fills_df = j_fills
    if not fills_df.empty and "yes_equiv_price" not in fills_df.columns:
        fills_df["yes_equiv_price"] = fills_df["price"].where(fills_df["side"] == "yes",
                                                              1.0 - fills_df["price"])

    if prob_long.empty:
        probs_mid = probs_lo = probs_hi = pd.DataFrame()
        grid = pd.DatetimeIndex([])
    else:
        probs_mid = prob_long.pivot_table(index="time", columns="ticker", values="prob")
        probs_lo = prob_long.pivot_table(index="time", columns="ticker", values="prob_lo")
        probs_hi = prob_long.pivot_table(index="time", columns="ticker", values="prob_hi")
        grid = probs_mid.index

    prob_grid = {"grid": grid, "probs_mid": probs_mid, "probs_lo": probs_lo, "probs_hi": probs_hi}
    pnl_by_ticker = _live_pnl_by_ticker(fills_df, probs_mid)

    strat = strategy_module(series)
    # Reconstruct the ACTUAL config run_live.py is using (only the two
    # fields charts.py reads: max_position for the position panel's
    # y_range, edge_fraction for the edge-decomposition bands) rather than
    # a fresh default, which could silently disagree with what's actually
    # running.
    config = strat.QuotingConfig(
        edge_fraction=summary.get("edge_fraction", strat.QuotingConfig().edge_fraction),
        max_position=summary.get("max_position", strat.QuotingConfig().max_position),
        constant_edge=summary.get("constant_edge", strat.QuotingConfig().constant_edge),
    )
    return DayData(
        event_ticker=summary["event_ticker"], series=series, source="live", fidelity="live",
        config=config, quote_df=quote_df, fills_df=fills_df, pnl_df=pnl_df,
        summary=summary, pnl_by_ticker=pnl_by_ticker, prob_grid=prob_grid,
        bbo_df=bbo_df,
    )
