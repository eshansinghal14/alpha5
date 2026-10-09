"""
Snapshot writer: the hand-off from a running session to kalshi_viz.

Layout (shared by every series; the dashboard knows no series specifics):

    data/live/{series}/{date}/summary.json        every session.FLUSH_INTERVAL_S
    data/live/{series}/{date}/quote_df.parquet    decision log      } every
    data/live/{series}/{date}/fills_df.parquet                      } HEAVY_FLUSH_EVERY
    data/live/{series}/{date}/pnl_df.parquet      session PnL curve } flushes
    data/live/{series}/{date}/fair_df.parquet     fair history      }
    kalshi_viz/static/{series}/{ticker}.json      per-market chart feed

Every write is atomic (tmp + replace, retried on Windows sharing errors).
`capture` runs on the event loop and only copies references; `write` does the
pandas/IO on a worker thread.
"""
from __future__ import annotations

import json
import os
import time
import traceback

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT, REPO_ROOT

LIVE_ROOT = os.path.join(DATA_ROOT, "live")
STREAM_POINTS = 400            # chart points per market after decimation
STREAM_ACTIVE_FRACTION = 0.05  # markets with < this share of the busiest market's decisions get no stream


def day_dir(series: str, date: str) -> str:
    return os.path.join(LIVE_ROOT, series, date)


def stream_dir(series: str) -> str:
    """Under kalshi_viz/static so Streamlit serves it same-origin at
    /app/static/{series}/ -- the live chart polls these without CORS or a
    second server."""
    return os.path.join(REPO_ROOT, "kalshi_viz", "static", series)


def stream_filename(ticker: str) -> str:
    # Dots would read as a file extension in the served URL.
    return ticker.replace(".", "_") + ".json"


def _replace(tmp: str, path: str, attempts: int = 5, delay_s: float = 0.05):
    # A dashboard poll holding the target open makes os.replace fail on
    # Windows; a dropped frame is cheap, a crashed session is not.
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                print(f"[persist] WARN: could not replace {os.path.basename(path)}; skipping")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            time.sleep(delay_s)


def _write_json(obj, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, default=_json_default)
    _replace(tmp, path)


def _write_parquet(df: pd.DataFrame, path: str):
    tmp = path + ".tmp"
    df.to_parquet(tmp)
    _replace(tmp, path)


def _json_default(o):
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary(*, series: str, event_ticker: str, engine, quoter, store, model,
                  now: pd.Timestamp, timings: dict | None = None) -> dict:
    """Scalar session state. The schema is the same for every series;
    series-specific values go under "extras"."""
    fills = engine.portfolio.fills
    pnl_touch, pnl_fair = engine.current_pnl(mark="touch"), engine.current_pnl(mark="prob")
    lip = quoter.lip.summary() if quoter.lip is not None else None
    lip_total = lip["total_usd"] if lip else 0.0
    from kalshi_infra.config import to_dict
    return {
        "series": series,
        "event_ticker": event_ticker,
        "last_update": now.isoformat(),
        "session_started": engine.session_started,
        "close_time": engine.close_time.isoformat() if engine.close_time is not None else None,
        # What the process is ACTUALLY doing, as opposed to what the dashboard
        # asked for in trading_mode.json.
        "live_trading": engine.live_trading,
        "halted": engine.halted, "halt_reason": engine.halt_reason,
        "blocked_reason": engine.blocked_reason(),
        "blocked_counts": dict(engine.blocked_counts),
        "quote_block_counts": dict(quoter.block_counts),
        "signal_age_s": engine.signal_age_s(), "feed_age_s": engine.feed_age_s(),
        "applied_age_s": engine.applied_age_s(), "signal_age_limit_s": engine.signal_age_limit(),
        "fees": {"type": quoter.fees.fee_type, "multiplier": quoter.fees.multiplier,
                 "taker_rate": quoter.fees.taker_rate, "maker_rate": quoter.fees.maker_rate},
        "write_budget_orders_per_s": (engine.order_client.max_orders_per_s
                                      if engine.order_client is not None else None),
        "consecutive_order_errors": engine.consecutive_order_errors,
        "pnl": {"touch": pnl_touch, "fair": pnl_fair,
                "realized": engine.portfolio.total_realized_pnl(), "fees": engine.portfolio.total_fees(),
                "lip_accrued": lip_total, "fair_incl_lip": pnl_fair + lip_total},
        "lip": lip,
        "n_fills": len(fills),
        "n_tickers_traded": len({f.ticker for f in fills}),
        "position_limit": engine.position_limit(),
        "over_limit": {t: round(q, 4) for t, q in engine.over_limit.items()},
        "positions": {t: p.yes_qty - p.no_qty for t, p in engine.portfolio.positions.items()
                      if abs(p.yes_qty - p.no_qty) > 1e-9},
        "real": {"n_orders": engine.n_real_orders, "n_fills": engine.n_real_fills,
                 "n_errors": engine.real_order_errors, "n_quotes": engine.n_real_quotes,
                 "n_resting": len(engine.real_resting), "n_cancelled": engine.n_real_cancelled,
                 "n_failed_cancels": engine.failed_cancels, "balance_at_arm": engine.balance_at_arm,
                 "last_reconcile": engine.last_reconcile, "reconcile_mismatches": engine.reconcile_mismatches,
                 "last_audit": engine.last_audit, "last_audit_diffs": engine.last_audit_diffs},
        "orders": {"n": len(engine.orders), "n_unresolved": len(engine.unresolved_orders),
                   "unresolved_reasons": dict(engine.unresolved_reasons),
                   "n_resolution_errors": engine.resolution_errors},
        "config": to_dict(store.config),
        "config_status": store.status(),
        "timings": timings or {},
        "extras": model.summary_extras(),
    }


# ---------------------------------------------------------------------------
# Capture / write
# ---------------------------------------------------------------------------

def capture(*, series: str, summary: dict, quoter, engine, pnl_history: list, fair_history: list,
            pnl_by_ticker: dict, now: pd.Timestamp, heavy: bool) -> dict:
    """Cheap, on-loop: copy references only. Lists are append-only from the
    loop, so a shallow copy is safe to iterate off it."""
    return {
        "series": series, "date": now.strftime("%Y-%m-%d"), "heavy": heavy, "summary": summary,
        "debug_log": list(quoter.debug_log) if heavy else None,
        "fills": list(engine.portfolio.fills) if heavy else None,
        "pnl_history": list(pnl_history) if heavy else None,
        "fair_history": list(fair_history) if heavy else None,
        "pnl_by_ticker": {k: list(v) for k, v in pnl_by_ticker.items()} if heavy else None,
    }


WRITE_PHASES: dict = {}


def write(state: dict):
    """Off-loop half of a flush. Touches only `state`."""
    t = time.perf_counter()
    ph = {}
    d = day_dir(state["series"], state["date"])
    os.makedirs(d, exist_ok=True)
    if state["heavy"]:
        quote_df = pd.DataFrame(state["debug_log"])
        if not quote_df.empty:
            quote_df = quote_df.sort_values("time", kind="stable").reset_index(drop=True)
        _write_parquet(quote_df, os.path.join(d, "quote_df.parquet"))
        ph["quote_df"] = time.perf_counter() - t; t = time.perf_counter()
        fills_df = fills_frame(state["fills"])
        _write_parquet(fills_df, os.path.join(d, "fills_df.parquet"))
        _write_parquet(pd.DataFrame(state["pnl_history"], columns=["time", "pnl"]),
                       os.path.join(d, "pnl_df.parquet"))
        fh = state["fair_history"]
        _write_parquet(pd.DataFrame(fh) if fh else
                       pd.DataFrame(columns=["time", "ticker", "prob", "prob_lo", "prob_hi"]),
                       os.path.join(d, "fair_df.parquet"))
        ph["other_parquets"] = time.perf_counter() - t; t = time.perf_counter()
        try:
            write_streams(state["series"], quote_df, fills_df, state["pnl_by_ticker"])
        except Exception:
            print("[persist] WARN: stream write failed:")
            traceback.print_exc()
        ph["streams"] = time.perf_counter() - t; t = time.perf_counter()
    _write_json(state["summary"], os.path.join(d, "summary.json"))
    ph["summary"] = time.perf_counter() - t
    WRITE_PHASES.clear()
    WRITE_PHASES.update({k: round(v * 1000, 1) for k, v in ph.items()})


def fills_frame(fills) -> pd.DataFrame:
    cols = ["time", "ticker", "side", "price", "size", "fee", "liquidity", "order_id", "yes_equiv_price"]
    if not fills:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([{"time": f.time, "ticker": f.ticker, "side": f.side, "price": f.price,
                        "size": f.size, "fee": f.fee, "liquidity": f.liquidity, "order_id": f.order_id}
                       for f in fills]).sort_values("time").reset_index(drop=True)
    df["yes_equiv_price"] = df["price"].where(df["side"] == "yes", 1.0 - df["price"])
    return df


def reset_session_files(series: str):
    """Drop per-market stream files. A market that no longer qualifies is
    never rewritten, so without this the chart keeps serving the discarded
    session's (e.g. shadow) fills after an arm or a rollover."""
    d = stream_dir(series)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Per-market chart streams
# ---------------------------------------------------------------------------

def _decimate(df: pd.DataFrame, n: int = STREAM_POINTS) -> pd.DataFrame:
    """Thin across the whole session (a tail would show only the last
    minute of an active market), always keeping the last row and every row
    that produced a take -- those are what the chart is read to check."""
    if len(df) <= n:
        return df
    step = len(df) // n + 1
    keep = np.zeros(len(df), dtype=bool)
    keep[::step] = True
    keep[-1] = True
    for col in ("bid_final", "ask_final"):
        if col in df.columns:
            keep |= df[col].notna().to_numpy()
    return df[keep]


def _ms(ts) -> list:
    return [int(pd.Timestamp(x).value // 10**6) for x in ts]


def _clean(vals) -> list:
    return [None if v is None or pd.isna(v) else round(float(v), 4) for v in vals]


def _col(q, name):
    return q[name] if name in q.columns else pd.Series(np.nan, index=q.index)


def _edge_bands(q: pd.DataFrame) -> dict:
    """Cumulative boundaries outward from fair in the quoter's order --
    constant, lean, CI, fee -- so the outermost pair is the take reservation."""
    if q.empty:
        return {k: [] for k in ("c_bid", "c_ask", "l_bid", "l_ask", "ci_bid", "ci_ask")}
    prob = q["prob"]
    const = _col(q, "constant_edge").fillna(0.0)
    c_bid, c_ask = prob - const, prob + const
    l_bid, l_ask = c_bid - _col(q, "lean_bid").fillna(0.0), c_ask + _col(q, "lean_ask").fillna(0.0)
    return {"c_bid": _clean(c_bid), "c_ask": _clean(c_ask), "l_bid": _clean(l_bid), "l_ask": _clean(l_ask),
            "ci_bid": _clean(l_bid - _col(q, "ci_edge").fillna(0.0)),
            "ci_ask": _clean(l_ask + _col(q, "ci_edge_ask").fillna(0.0))}


def write_streams(series: str, quote_df: pd.DataFrame, fills_df: pd.DataFrame, pnl_by_ticker: dict):
    """One small JSON per market the dashboard can select (traded, or being
    evaluated at meaningful volume and not excluded by max_markets). One file
    per market so a poll downloads only the market on screen."""
    out = stream_dir(series)
    os.makedirs(out, exist_ok=True)
    wanted = set(fills_df["ticker"].unique()) if not fills_df.empty else set()
    if not quote_df.empty:
        counts = quote_df.groupby("ticker").size()
        active = set(counts[counts >= STREAM_ACTIVE_FRACTION * counts.max()].index)
        if "atm_tradeable" in quote_df.columns:
            active &= set(quote_df.loc[quote_df["atm_tradeable"].fillna(True).astype(bool), "ticker"].unique())
        wanted |= active
    q_groups = dict(tuple(quote_df.groupby("ticker"))) if not quote_df.empty else {}
    f_groups = dict(tuple(fills_df.groupby("ticker"))) if not fills_df.empty else {}
    empty = fills_df.iloc[0:0]
    for tk in wanted:
        q = q_groups.get(tk, pd.DataFrame())
        q = _decimate(q[q["prob"].notna()]) if len(q) else q
        f = f_groups.get(tk, empty)
        liq = f["liquidity"] if "liquidity" in f.columns else pd.Series("taker", index=f.index)
        groups = {("y", "m"): f[(f["side"] == "yes") & (liq == "maker")],
                  ("y", "t"): f[(f["side"] == "yes") & (liq != "maker")],
                  ("n", "m"): f[(f["side"] == "no") & (liq == "maker")],
                  ("n", "t"): f[(f["side"] == "no") & (liq != "maker")]}
        sgn = f["size"].where(f["side"] == "yes", -f["size"]).cumsum() if not f.empty else pd.Series(dtype=float)
        pnl = pd.DataFrame(pnl_by_ticker.get(tk, []), columns=["time", "pnl"])
        pnl = _decimate(pnl)
        has = len(q) > 0
        entry = {
            "q_t": _ms(q["time"]) if has else [],
            "bid": _clean(q["market_bid"]) if has else [], "ask": _clean(q["market_ask"]) if has else [],
            "prob": _clean(q["prob"]) if has else [],
            "lo": _clean(_col(q, "prob_lo")) if has else [], "hi": _clean(_col(q, "prob_hi")) if has else [],
            "br": _clean(q["bid_reservation"]) if has else [], "ar": _clean(q["ask_reservation"]) if has else [],
            **_edge_bands(q),
            "qb": _clean(_col(q, "quote_bid")) if has else [], "qa": _clean(_col(q, "quote_ask")) if has else [],
            "lb": _clean(_col(q, "lip_bid")) if has else [], "la": _clean(_col(q, "lip_ask")) if has else [],
            **{f"f{s}_{k}t": _ms(g["time"]) for (s, k), g in groups.items()},
            **{f"f{s}_{k}p": _clean(g["yes_equiv_price"]) for (s, k), g in groups.items()},
            "pos_t": _ms(f["time"]), "pos_v": [float(v) for v in sgn],
            "pnl_t": _ms(pnl["time"]), "pnl_v": _clean(pnl["pnl"]),
        }
        _write_json({"series": series, "ticker": tk, "market": entry}, os.path.join(out, stream_filename(tk)))
