"""
Periodic snapshot writer for the live trading engine (shadow or real --
see run_live.py) -- the only hand-off between run_live.py (a standalone, long-running process) and
viz/data_loader.py's load_live_day (read by the separate Streamlit
process). No message queue/DB in this repo to build on, and both
processes share a filesystem (the trading EC2), so a small set of
overwrite-in-place files under data/live/{series}/{date}/ is the
simplest robust hand-off -- see this project's live-trading plan's
Deferred section for what this deliberately does NOT do (S3 archival,
surviving a mid-day restart).

Every write is atomic (tmp file + os.replace) so load_live_day never
reads a half-written file.
"""
import json
import os
import time
import traceback

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
LIVE_ROOT = os.path.join(_PROJECT_ROOT, "data", "live")


def live_day_dir(series: str, date: str) -> str:
    return os.path.join(LIVE_ROOT, series, date)


def _replace_with_retry(tmp: str, path: str, attempts: int = 5, delay_s: float = 0.05):
    """os.replace, retried on PermissionError.

    On Windows a replace fails outright (WinError 5) if any other process has
    the destination open -- and the dashboard polls these files every 2s in
    Live mode, so collisions are routine rather than exotic. One unlucky
    collision used to raise straight out of flush_loop and kill the whole
    live session; a few short retries clear it. If it still fails the write
    is skipped rather than propagated: a missed snapshot costs one frame of
    dashboard staleness, while a dead process costs the session."""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                print(f"[live] WARN: could not replace {os.path.basename(path)} "
                      f"after {attempts} attempts (reader holding it open); skipping this snapshot")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            time.sleep(delay_s)


def _atomic_write_parquet(df: pd.DataFrame, path: str):
    tmp = path + ".tmp"
    df.to_parquet(tmp)
    _replace_with_retry(tmp, path)


def _atomic_write_json(obj: dict, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    _replace_with_retry(tmp, path)


def compute_live_pnl(engine, now: pd.Timestamp, mark: str = "prob") -> float:
    """Whole-session mark-to-market PnL -- thin wrapper around
    LiveEngine.current_pnl(). `now` is unused, kept for call-site symmetry.

    Defaults to mark="prob" so the persisted curve matches
    markout_analysis.compute_pnl_over_day and a live session can be compared
    against a backtest of the same day. The failsafe deliberately keeps using
    touch marking -- see LiveEngine.current_pnl."""
    return engine.current_pnl(mark=mark)


def capture_snapshot(series: str, event_ticker: str, strategy, engine, config,
                     pnl_history: list[tuple], prob_history: list[dict], now: pd.Timestamp,
                     out_root: str = None, feed_rate: float | None = None,
                     timings: dict | None = None, heavy: bool = True) -> dict:
    """Take a consistent, plain-data copy of everything write_snapshot needs.

    MUST run on the event loop, and is deliberately cheap: it copies list
    references and reads scalars, nothing more. Everything expensive --
    DataFrame construction, sorting, parquet, JSON -- happens in
    write_snapshot on a worker thread.

    Split out because the old single-pass version ran on the loop and
    rebuilt the full-session DataFrame per flush. Measured 2026-08-28:
    531ms median at 10.7k decisions on KXINXU, directly matched to the
    loop-lag probe's worst stall.
    """
    summary = _build_summary(event_ticker, engine, config, now, feed_rate, timings)
    summary["max_abs_net_position"] = _peak_net_position(engine.portfolio.fills)
    return {
        "series": series,
        "date": now.strftime("%Y-%m-%d"),
        "out_root": out_root,
        # False -> summary.json only. The parquets and the chart's per-market
        # stream files are what cost real time (they were 90%+ of a flush),
        # and nothing in the TRADING path reads any of it -- it is all for the
        # dashboard. So the guard/PnL numbers stay fresh every cycle and the
        # heavy rebuild happens on a slow cadence. See run_live's flush_loop.
        "heavy": heavy,
        # Shallow copies: the lists are appended to from the loop, so a
        # snapshot of the references is enough to iterate them safely off it.
        # The dicts inside are never mutated after being appended.
        "debug_log": list(strategy.debug_log),
        "fills": list(engine.portfolio.fills),
        "pnl_history": list(pnl_history),
        "prob_history": list(prob_history),
        "pnl_by_ticker": _pnl_by_ticker(engine, strategy, series) if heavy else {},
        "summary": summary,
    }


def _peak_net_position(fills) -> float:
    """Peak |net position| across the session, straight off the fill list --
    the same number _max_abs_net_position derives from a DataFrame, computed
    without building one so it can go in every summary."""
    net, peak = {}, 0.0
    for f in fills:
        net[f.ticker] = net.get(f.ticker, 0.0) + (f.size if f.side == "yes" else -f.size)
        peak = max(peak, abs(net[f.ticker]))
    return peak


WRITE_PHASES: dict = {}


def write_snapshot(state: dict):
    """The expensive half of a flush. Runs on a worker thread; touches only
    `state`, never live engine/strategy objects.

    Phase timings land in WRITE_PHASES so the cost can be attributed --
    building DataFrames vs serialising parquet vs the chart's stream JSON
    are three very different things to fix."""
    _ph = {}
    _t = time.perf_counter()
    series, date = state["series"], state["date"]
    out_root = state["out_root"]
    out_dir = live_day_dir(series, date) if out_root is None else os.path.join(out_root, series, date)
    os.makedirs(out_dir, exist_ok=True)
    pnl_history, prob_history = state["pnl_history"], state["prob_history"]
    fills = state["fills"]

    if not state.get("heavy", True):
        _atomic_write_json(state["summary"], os.path.join(out_dir, "summary.json"))
        _ph["summary_json"] = time.perf_counter() - _t
        WRITE_PHASES.clear()
        WRITE_PHASES.update({k: round(v * 1000, 1) for k, v in _ph.items()})
        return

    quote_df = pd.DataFrame(state["debug_log"])
    if not quote_df.empty:
        quote_df = quote_df.sort_values("time").reset_index(drop=True)
    _ph["build_quote_df"] = time.perf_counter() - _t; _t = time.perf_counter()
    _atomic_write_parquet(quote_df, os.path.join(out_dir, "quote_df.parquet"))
    _ph["write_quote_parquet"] = time.perf_counter() - _t; _t = time.perf_counter()

    if fills:
        fills_df = pd.DataFrame([
            {"time": f.time, "ticker": f.ticker, "side": f.side, "price": f.price,
             "size": f.size, "fee": f.fee, "liquidity": f.liquidity, "order_id": f.order_id}
            for f in fills
        ]).sort_values("time").reset_index(drop=True)
        fills_df["yes_equiv_price"] = fills_df["price"].where(fills_df["side"] == "yes", 1.0 - fills_df["price"])
    else:
        fills_df = pd.DataFrame(columns=["time", "ticker", "side", "price", "size", "fee", "liquidity",
                                          "order_id", "yes_equiv_price"])
    _atomic_write_parquet(fills_df, os.path.join(out_dir, "fills_df.parquet"))

    pnl_df = pd.DataFrame(pnl_history, columns=["time", "pnl"]) if pnl_history else pd.DataFrame(columns=["time", "pnl"])
    _atomic_write_parquet(pnl_df, os.path.join(out_dir, "pnl_df.parquet"))

    prob_df = pd.DataFrame(prob_history) if prob_history else pd.DataFrame(columns=["time", "ticker", "prob", "prob_lo", "prob_hi"])
    _atomic_write_parquet(prob_df, os.path.join(out_dir, "prob_grid.parquet"))
    _ph["other_parquets"] = time.perf_counter() - _t; _t = time.perf_counter()

    summary = dict(state["summary"])
    _atomic_write_json(summary, os.path.join(out_dir, "summary.json"))
    _ph["summary_json"] = time.perf_counter() - _t; _t = time.perf_counter()

    # Streaming snapshot for the live chart (viz/live_chart.py). Best-effort:
    # a failure here must never take down the trading session, which is what
    # this whole process exists to do.
    try:
        write_stream_json(series, quote_df, fills_df, state["pnl_by_ticker"])
    except Exception:
        print("[live] WARN: stream json write failed:")
        traceback.print_exc()
    _ph["stream_json"] = time.perf_counter() - _t
    WRITE_PHASES.clear()
    WRITE_PHASES.update({k: round(v * 1000, 1) for k, v in _ph.items()})


def _build_summary(event_ticker, engine, config, now, feed_rate, timings) -> dict:
    """Scalar session state, read on the event loop. max_abs_net_position is
    filled in by write_snapshot, which has the fills DataFrame."""
    fills = engine.portfolio.fills
    tickers_traded = {f.ticker for f in fills}
    return {
        "event_ticker": event_ticker,
        "n_fills": len(fills),
        "total_realized_pnl": engine.portfolio.total_realized_pnl(),
        "total_fees": engine.portfolio.total_fees(),
        "n_tickers_traded": len(tickers_traded),
        "last_update": now.isoformat(),
        # Bumped when arming real trading clears the ledger. The dashboard
        # keys its cached market ordering and chart on this, so a reset
        # rebuilds them instead of showing the discarded session's shape.
        "session_started": engine.session_started,
        # So load_live_day can reconstruct the ACTUAL QuotingConfig this
        # session is running with, not a fresh default -- charts.py needs
        # these two exact values (max_position for the position panel's
        # y_range, edge_fraction for the edge-decomposition bands).
        "edge_fraction": config.edge_fraction,
        "max_position": config.max_position,
        "constant_edge": config.constant_edge,
        "lean_multiplier": config.lean_multiplier,
        "lean_dollars": config.lean_dollars,
        # Read by the dashboard to decide whether the chart should carry the
        # resting-quote lines at all -- see live_chart.build_live_html.
        "enable_quoting": getattr(config, "enable_quoting", False),
        "quoting_edge": getattr(config, "quoting_edge", None),
        "enable_quoting": config.enable_quoting,
        "quoting_edge": config.quoting_edge,
        "quote_size": config.quote_size,
        "requote_delay_s": config.requote_delay_s,
        "halted": engine.halted,
        "halt_reason": engine.halt_reason,
        "max_loss_usd": engine.max_loss_usd,
        # Why orders are (or aren't) being sent right now, and how often each
        # guard has fired this session. Without the counts a guard that
        # silently blocks everything looks identical to a strategy that
        # simply found no edge all day.
        "blocked_reason": engine.blocked_reason(),
        "blocked_counts": dict(engine.blocked_counts),
        "signal_age_s": engine.signal_age_s(),
        "feed_age_s": engine.feed_age_s(),
        "applied_age_s": engine.applied_age_s(),
        "feed_records_per_s": feed_rate,
        "timings": timings or {},
        "max_signal_age_s": engine.max_signal_age_s,
        "bias": dict(engine.bias),
        "consecutive_order_errors": engine.consecutive_order_errors,
        "close_time": engine.close_time.isoformat() if engine.close_time is not None else None,
        # Whether orders are actually reaching Kalshi. The dashboard's
        # checkbox writes a request file; THIS is the running process
        # confirming it took effect, so the two can never silently disagree
        # about whether real money is moving.
        "live_trading": engine.live_trading,
        "n_real_orders": engine.n_real_orders,
        "n_real_fills": engine.n_real_fills,
        "n_real_order_errors": engine.real_order_errors,
        "last_reconcile": engine.last_reconcile,
        "balance_at_arm": engine.balance_at_arm,
        # Non-empty means replaying Kalshi's own fills disagreed with its own
        # position record, i.e. we read the fills endpoint's side convention
        # wrong. Kalshi's positions win, but it should never be non-empty.
        "reconcile_mismatches": engine.reconcile_mismatches,
        # Ground-truth check against Kalshi's own positions. A stale
        # last_audit while armed means the check itself is failing, which is
        # not the same as passing.
        "last_audit": engine.last_audit,
        "last_audit_diffs": engine.last_audit_diffs,
        "position_limit": engine.position_limit(),
        # Reported, not halted on -- an over-cap market can only be reduced
        # from here (see LiveEngine.check_position_limit).
        "over_limit": {t: round(q, 4) for t, q in engine.over_limit.items()},
        "max_abs_position_now": max((abs(p.yes_qty - p.no_qty)
                                     for p in engine.portfolio.positions.values()), default=0.0),
        # Both conventions, because the gap between them is large and neither
        # is "the" answer -- see LiveEngine.current_pnl. pnl_prob matches the
        # backtest; pnl_touch is the liquidation value the failsafe watches.
        "pnl_prob": engine.current_pnl(mark="prob"),
        "pnl_touch": engine.current_pnl(mark="touch"),
        # Order-outcome accounting, for comparing live behaviour against what
        # the backtest engine would have done with the same decisions. Without
        # these, an order that never filled live was indistinguishable from one
        # the strategy never placed -- so "the backtest trades but live
        # doesn't" wasn't measurable at all.
        # Every order must land in exactly one bucket; n_unaccounted is the
        # residual and should always be 0. It isn't decoration -- a nonzero
        # value means an order reached none of _resolve_arrival's outcomes
        # (or never arrived at all), which is exactly the failure this
        # accounting exists to catch.
        "n_orders": len(engine.orders),
        "n_unresolved": len(engine.unresolved_orders),
        "n_resting": sum(1 for t in engine.queue_trackers if not t.done),
        "n_trackers": len(engine.queue_trackers),
        "n_cancelled": len(engine.cancelled_ids),
        "n_resolution_errors": engine.resolution_errors,
        "unresolved_reasons": dict(engine.unresolved_reasons),
        "n_in_flight": len(engine._pending_submissions) + engine.n_scheduled + engine.n_real_in_flight,
        # Real resting quotes: placed, currently on the book, and pulled back
        # off it. n_failed_cancels is the one to watch -- a cancel that errors
        # leaves a live order we no longer want, and it is retried rather than
        # forgotten (see LiveEngine._cancel_real).
        "n_real_quotes": engine.n_real_quotes,
        "n_real_resting": len(engine.real_resting),
        "n_real_cancelled": engine.n_real_cancelled,
        "n_failed_cancels": engine.failed_cancels,
        # Uses n_resting (trackers NOT done), not len(queue_trackers): a
        # resting order that later fills via on_trade is recorded in `fills`
        # while its tracker stays in queue_trackers, so counting every
        # tracker double-counts it and drives this negative. Real quotes are
        # subtracted the same way -- live on the book, or cancelled off it.
        # A quote that PARTIALLY filled and was then cancelled counts as both
        # a fill and a cancel and so drives this to -1; quote_size is 1, where
        # that cannot happen.
        # Shadow cancels are their own outcome: a cancelled shadow quote has
        # no fill, isn't unresolved and isn't resting, so leaving it out made
        # this count every cancel -- tens of thousands per session -- and the
        # invariant meant nothing.
        "n_unaccounted": (len(engine.orders) - len(fills) - len(engine.unresolved_orders)
                          - sum(1 for t in engine.queue_trackers if not t.done)
                          - len(engine._pending_submissions) - engine.n_scheduled
                          - engine.n_real_in_flight
                          - len(engine.real_resting) - engine.n_real_cancelled
                          - _n_shadow_cancelled(engine)),
    }


def _n_shadow_cancelled(engine) -> int:
    """Shadow orders whose only outcome was being cancelled: cancelled and
    never filled, never marked unresolved, not a real order."""
    if engine.live_trading:
        return 0
    filled = {f.order_id for f in engine.portfolio.fills}
    unresolved = {o.order_id for o in engine.unresolved_orders}
    real = set(engine.exchange_to_order.values()) if engine.exchange_to_order else set()
    return sum(1 for oid in engine.cancelled_ids
               if oid not in filled and oid not in unresolved and oid not in real)


_PNL_HIST: dict[tuple, list] = {}   # (series, ticker) -> [(time, pnl), ...]
_PNL_HIST_MAX = 4000


def append_bias_row(series: str, date: str, row: dict, out_root: str = None):
    """Append one pricing-health sample to bias_history.csv.

    summary.json is overwritten every flush, so it says how we are pricing
    RIGHT NOW and nothing about how we priced an hour ago. Validating the
    basis needs the history: whether our fair value sits systematically to
    one side of the market, and whether that drifts over a session, is a
    question about the series, not the instant. CSV and append-only so a
    restart adds to it rather than replacing it -- unlike the parquets,
    which a restart rewrites.
    """
    out_dir = live_day_dir(series, date) if out_root is None else os.path.join(out_root, series, date)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "bias_history.csv")
    cols = ["time", "basis", "basis_method", "n", "n_outside", "median_vs_mid",
            "median_excess", "same_side", "systematic"]
    new = not os.path.exists(path)
    try:
        with open(path, "a") as fh:
            if new:
                fh.write(",".join(cols) + "\n")
            fh.write(",".join("" if row.get(c) is None else str(row.get(c)) for c in cols) + "\n")
    except OSError:
        pass


def reset_session_files(series: str, out_dir: str = None):
    """Drop every trace of the previous session for `series`: the
    accumulated per-ticker PnL curves and the per-market stream snapshots
    the live chart polls.

    Called when arming real trading (see LiveEngine.reset_session). The
    day-directory parquets are overwritten by the next flush, but these two
    are not: _PNL_HIST accumulates across flushes by design, and a stream
    file for a market that no longer qualifies is never rewritten -- so
    without this the chart would keep serving shadow fills alongside real
    ones indefinitely.
    """
    for key in [k for k in _PNL_HIST if k[0] == series]:
        del _PNL_HIST[key]
    d = out_dir if out_dir is not None else _STATIC_DIR
    prefix = f"live_{series}__"
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith(".json"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


def _pnl_by_ticker(engine, strategy, series: str) -> dict:
    """Per-market mark-to-market PnL HISTORY, marked at each market's own
    latest implied probability -- same convention as the backtest and as
    current_pnl(mark="prob"), so the streamed curve matches the panel the
    dashboard draws from the parquet.

    Accumulated across flushes rather than recomputed: a position's PnL at
    past instants can't be reconstructed from current state (it needs the
    probability as it stood then), so an instantaneous value per flush would
    give the chart a single point and no curve at all -- which is exactly
    what the first version did.
    """
    signals = getattr(strategy, "signals", {}) or {}
    now = pd.Timestamp.now(tz="UTC")
    out = {}
    for ticker, pos in engine.portfolio.positions.items():
        sig = signals.get(ticker)
        if sig is None or sig.get("prob") is None:
            continue
        p = float(sig["prob"])
        hist = _PNL_HIST.setdefault((series, ticker), [])
        hist.append((now, pos.unrealized_pnl(p, p)))
        if len(hist) > _PNL_HIST_MAX:
            del hist[:len(hist) - _PNL_HIST_MAX]
        out[ticker] = pd.DataFrame(hist, columns=["time", "pnl"])
    return out


STREAM_POINTS = 400   # points per series in the streaming snapshot
STREAM_ACTIVE_FRACTION = 0.05   # of the busiest market's decisions -- mirrors app._ranked_markets


def stream_filename(series: str, ticker: str) -> str:
    """Per-market snapshot filename. Ticker dots are replaced because they'd
    read as a file extension in the served URL."""
    return f"live_{series}__{ticker.replace('.', '_')}.json"


def _decimate(df: pd.DataFrame, n: int = STREAM_POINTS) -> pd.DataFrame:
    """Thin `df` to roughly `n` rows, always keeping the last one and every
    row where the strategy crossed.

    Downsampled across the WHOLE session rather than tailed: a tail of 400
    decisions can be under a minute on an active market, so the chart would
    show a sliver of recent history instead of the session's shape. Thinning
    keeps the full span at a resolution a 900px-tall chart can actually
    render.

    Crossing rows are exempt from the stride, so the result can exceed `n` --
    by the number of takes, which is tens per session at most."""
    if len(df) <= n:
        return df
    step = len(df) // n + 1
    keep = np.zeros(len(df), dtype=bool)
    keep[::step] = True
    keep[-1] = True
    # A uniform stride kept 2 of one market's 10 crossings, and the fill
    # markers for the dropped ones landed inside a 7s gap where the step
    # lines held a stale reservation from before the take -- the chart showed
    # three sells at 4.6c of NEGATIVE edge (KXINXU T7754.9999, 15:56:42 on
    # 2026-08-28). The rows that produced a fill are exactly the ones the
    # chart is read to check, so they can't be the ones thinning drops.
    for col in ("bid_final", "ask_final"):
        if col in df.columns:
            keep |= df[col].notna().to_numpy()
    return df[keep]
_STATIC_DIR = os.path.join(_PROJECT_ROOT, "viz", "static")


def _ms(ts) -> list:
    return [int(pd.Timestamp(x).value // 10**6) for x in ts]


def _clean(vals) -> list:
    return [None if pd.isna(v) else round(float(v), 4) for v in vals]


def _edge_bands(q: pd.DataFrame) -> dict:
    """Cumulative edge boundaries for the four nested bands the chart draws.

    Accumulated outward from prob in the strategy's own order -- constant,
    lean, CI, fee -- so each band's outer edge is the running total up to
    that term and the visible ring is that term's contribution. The outermost
    pair is therefore bid_reservation/ask_reservation by construction.

    Per-side, not symmetric: lean applies only to the side that would grow
    the position, and each side's CI edge measures to its own bound.
    """
    if q.empty:
        return {k: [] for k in ("c_bid", "c_ask", "l_bid", "l_ask", "ci_bid", "ci_ask")}

    def col(name, default=0.0):
        return q[name].fillna(default) if name in q.columns else pd.Series(default, index=q.index)

    prob = q["prob"]
    const = col("constant_edge")
    c_bid, c_ask = prob - const, prob + const
    l_bid, l_ask = c_bid - col("lean_bid"), c_ask + col("lean_ask")
    ci_b = col("ci_edge")
    ci_a = col("ci_edge_ask") if "ci_edge_ask" in q.columns else ci_b
    return {
        "c_bid": _clean(c_bid), "c_ask": _clean(c_ask),
        "l_bid": _clean(l_bid), "l_ask": _clean(l_ask),
        "ci_bid": _clean(l_bid - ci_b), "ci_ask": _clean(l_ask + ci_a),
    }


def write_stream_json(series: str, quote_df: pd.DataFrame, fills_df: pd.DataFrame,
                       pnl_by_ticker: dict, out_dir: str = None):
    """Compact snapshot the live chart polls (see viz/live_chart.py).

    Written to viz/static/ so Streamlit serves it at /app/static/, which is
    same-origin with the dashboard -- the chart's iframe can fetch it without
    any CORS setup or a second web server.

    ONE FILE PER MARKET, not one per series: the chart only ever displays a
    single market, so a combined file makes the browser re-download every
    other market's history on every poll. On a 60-market ladder that was
    ~400KB per fetch versus ~15KB for the one being watched.

    Scoped to markets the dashboard can actually select -- traded, OR still
    being evaluated at meaningful volume (the same rule app._ranked_markets
    applies). Writing only traded markets left every other selectable market
    with no data at all, so picking one produced a permanently blank chart.
    """
    if out_dir is None:
        out_dir = _STATIC_DIR
    os.makedirs(out_dir, exist_ok=True)
    traded = set(fills_df["ticker"].unique()) if not fills_df.empty else set()
    wanted = set(traded)
    if not quote_df.empty:
        counts = quote_df.groupby("ticker").size()
        cutoff = STREAM_ACTIVE_FRACTION * counts.max()
        active = set(counts[counts >= cutoff].index)
        # Same ATM gate the dropdown applies (app._ranked_markets): a market
        # max_markets excludes is evaluated and logged but can never be
        # traded, so writing it a stream file is work for a chart nobody can
        # open.
        if "atm_tradeable" in quote_df.columns:
            active &= set(quote_df.loc[quote_df["atm_tradeable"].fillna(True), "ticker"].unique())
        wanted |= active

    # Grouped ONCE, not re-scanned per market. The old form ran
    # quote_df[quote_df["ticker"] == tk] inside this loop -- a full pass over
    # the whole decision frame for every market -- which on a 40-market
    # ladder is 40 scans of ~17k rows per flush. Measured 2026-08-28 that
    # made this function 75% of the entire snapshot cost (6.7s on KXINXU),
    # and the snapshot is what was slowing pricing down by 3-6x.
    q_groups = {k: v for k, v in quote_df.groupby("ticker")} if not quote_df.empty else {}
    f_groups = {k: v for k, v in fills_df.groupby("ticker")} if not fills_df.empty else {}
    empty_fills = fills_df.iloc[0:0]
    for tk in wanted:
        q = _decimate(q_groups[tk]) if tk in q_groups else pd.DataFrame()
        f = f_groups.get(tk, empty_fills)
        # Split by side AND by how the fill happened: the chart draws a
        # circle for a quote of ours that got hit and an x for a take, so it
        # needs the four groups separately rather than deriving side alone.
        liq = (f["liquidity"] if "liquidity" in f.columns
               else pd.Series("taker", index=f.index))
        fym, fyt = f[(f["side"] == "yes") & (liq == "maker")], f[(f["side"] == "yes") & (liq != "maker")]
        fnm, fnt = f[(f["side"] == "no") & (liq == "maker")], f[(f["side"] == "no") & (liq != "maker")]
        # net position is a cumsum over this market's own fills
        sgn = f["size"].where(f["side"] == "yes", -f["size"]).cumsum() if not f.empty else pd.Series(dtype=float)
        pnl = pnl_by_ticker.get(tk)
        entry = {
            "q_t": _ms(q["time"]) if len(q) else [],
            "bid": _clean(q["market_bid"]) if len(q) else [],
            "ask": _clean(q["market_ask"]) if len(q) else [],
            "prob_on_q": _clean(q["prob"]) if len(q) else [],
            "br": _clean(q["bid_reservation"]) if len(q) else [],
            "ar": _clean(q["ask_reservation"]) if len(q) else [],
            # Edge decomposition, as CUMULATIVE boundaries outward from prob:
            # prob -> constant -> lean -> CI -> fee(=reservation). Accumulated
            # here rather than in JS so the browser can't disagree with the
            # strategy's own arithmetic about what the bands mean.
            **_edge_bands(q),
            # Where our own quotes were resting, in YES terms like every
            # other price here (see the strategy's own debug_log comment).
            "qb": _clean(q["quote_bid"]) if "quote_bid" in q.columns else [],
            "qa": _clean(q["quote_ask"]) if "quote_ask" in q.columns else [],
            "fy_mt": _ms(fym["time"]), "fy_mp": _clean(fym["yes_equiv_price"]),
            "fy_tt": _ms(fyt["time"]), "fy_tp": _clean(fyt["yes_equiv_price"]),
            "fn_mt": _ms(fnm["time"]), "fn_mp": _clean(fnm["yes_equiv_price"]),
            "fn_tt": _ms(fnt["time"]), "fn_tp": _clean(fnt["yes_equiv_price"]),
            "pos_t": _ms(f["time"]), "pos_v": [float(v) for v in sgn],
            "pnl_t": _ms(_decimate(pnl)["time"]) if pnl is not None and len(pnl) else [],
            "pnl_v": _clean(_decimate(pnl)["pnl"]) if pnl is not None and len(pnl) else [],
        }
        name = stream_filename(series, tk)
        tmp = os.path.join(out_dir, name + ".tmp")
        with open(tmp, "w") as fh:
            json.dump({"series": series, "ticker": tk, "markets": {tk: entry}}, fh)
        _replace_with_retry(tmp, os.path.join(out_dir, name))


def _max_abs_net_position(fills_df: pd.DataFrame) -> float:
    if fills_df.empty:
        return 0.0
    peak = 0.0
    for _, g in fills_df.groupby("ticker"):
        g = g.sort_values("time")
        signed = g["size"].where(g["side"] == "yes", -g["size"])
        net = signed.cumsum()
        peak = max(peak, float(net.abs().max()))
    return peak
