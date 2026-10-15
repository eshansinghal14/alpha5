"""
Snapshot writer: the hand-off from a running session to kalshi_viz.

Layout (shared by every series; the dashboard knows no series specifics):

    data/live/{series}/{date}/summary.json        every session.FLUSH_INTERVAL_S
    data/live/{series}/{date}/quote_df.parquet    decision log      } every
    data/live/{series}/{date}/fills_df.parquet                      } writer.HEAVY_INTERVAL_S
    data/live/{series}/{date}/pnl_df.parquet      session PnL curve } flushes
    data/live/{series}/{date}/fair_df.parquet     fair history      }
    kalshi_viz/static/{series}/{ticker}.json      per-market chart feed

Every write is atomic (tmp + replace, retried on Windows sharing errors).
`write` runs in the writer process (runtime.writer), never the trading one.
"""
from __future__ import annotations

import json
import os
import time
import traceback

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT, REPO_ROOT

# Env overrides let a test session run beside the live ones without sharing
# files (the writer is a separate process, so module patching can't reach it).
LIVE_ROOT = os.environ.get("ALPHA5_LIVE_ROOT") or os.path.join(DATA_ROOT, "live")
_STREAM_ROOT = os.environ.get("ALPHA5_STREAM_ROOT") or os.path.join(REPO_ROOT, "kalshi_viz", "static")
STREAM_POINTS = 400            # chart points per market after decimation
# Plus a denser tail for the dashboard's 1m/15m chart windows: 400 points over
# a 7h S&P session is one per ~60s, too coarse to read a 1-minute window.
STREAM_RECENT_S = 900.0
STREAM_RECENT_POINTS = 600


def day_dir(series: str, date: str) -> str:
    return os.path.join(LIVE_ROOT, series, date)


def stream_dir(series: str) -> str:
    """Under kalshi_viz/static so Streamlit serves it same-origin at
    /app/static/{series}/ -- the live chart polls these without CORS or a
    second server."""
    return os.path.join(_STREAM_ROOT, series)


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
                  now: pd.Timestamp, timings: dict | None = None,
                  arm_requested: bool | None = None) -> dict:
    """Scalar session state. The schema is the same for every series;
    series-specific values go under "extras"."""
    fills = engine.portfolio.fills
    pnl_touch, pnl_fair = engine.current_pnl(mark="touch"), engine.current_pnl(mark="prob")
    lip = quoter.lip.summary() if quoter.lip is not None else None
    lip_total = lip["total_usd"] if lip else 0.0
    by_tk = engine.pnl_by_ticker(mark="prob")
    volume: dict[str, float] = {}
    for f in fills:
        volume[f.ticker] = volume.get(f.ticker, 0.0) + f.size
    positions = engine.portfolio.positions
    # Our live LIP orders per market: [(side, own-side price, remaining)].
    lip_live: dict[str, list] = {}
    for q in engine.om.live(role="lip"):
        if q.state != "cancelling":
            lip_live.setdefault(q.ticker, []).append((q.side, round(q.price, 2), round(q.remaining, 2)))
    markets = {t: {"pnl": by_tk.get(t, 0.0),
                   "contracts": volume.get(t, 0.0),
                   "position": (positions[t].yes_qty - positions[t].no_qty) if t in positions else 0.0,
                   "lip": (lip["accrued_usd"].get(t) if lip else None),
                   "lip_qualifying": (lip["qualifying_fraction"].get(t) if lip else None),
                   "lip_quotes": lip_live.get(t, [])}
               for t in set(model.tickers) | set(by_tk) | set(volume)}
    from kalshi_infra.config import to_dict
    return {
        "series": series,
        "event_ticker": event_ticker,
        "tickers": list(model.tickers),
        "last_update": now.isoformat(),
        "session_started": engine.session_started,
        "close_time": engine.close_time.isoformat() if engine.close_time is not None else None,
        # What the process is ACTUALLY doing, as opposed to what the dashboard
        # asked for in trading_mode.json.
        "live_trading": engine.live_trading,
        "arm_requested": engine.live_trading if arm_requested is None else bool(arm_requested),
        "halted": engine.halted, "halt_reason": engine.halt_reason,
        "real_lock": getattr(engine, "real_lock", None),
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
        "markets": markets,
        "n_fills": len(fills),
        "n_tickers_traded": len({f.ticker for f in fills}),
        "position_limit": engine.position_limit(),
        "over_limit": {t: round(q, 4) for t, q in engine.over_limit.items()},
        "positions": {t: p.yes_qty - p.no_qty for t, p in engine.portfolio.positions.items()
                      if abs(p.yes_qty - p.no_qty) > 1e-9},
        "real": {"n_orders": engine.n_real_orders, "n_fills": engine.n_real_fills,
                 "n_errors": engine.real_order_errors, "n_quotes": engine.n_real_quotes,
                 "n_resting": len(engine.om.quotes) if engine.live_trading else 0,
                 "n_cancelled": engine.n_real_cancelled,
                 "n_failed_cancels": engine.failed_cancels, "balance_at_arm": engine.balance_at_arm,
                 "last_reconcile": engine.last_reconcile, "reconcile_mismatches": engine.reconcile_mismatches,
                 "last_audit": engine.last_audit, "last_audit_diffs": engine.last_audit_diffs},
        # Real write-token spend by "kind:reason" since the client was built
        # (one per event); see KalshiOrderClient.usage.
        "write_usage": engine.order_client.usage() if engine.order_client is not None else None,
        # Quote ladder state: live quotes by state, queued creates, quote
        # budget, and every create/cancel by reason (real and shadow alike).
        "quotes": engine.om.summary(),
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
            bh = state.get("bbo_history") or []
            write_streams(state["series"], quote_df, fills_df, state["pnl_by_ticker"],
                          bbo_df=pd.DataFrame(bh) if bh else None,
                          fair_df=pd.DataFrame(fh) if fh else None, tickers=state.get("tickers"))
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

def _decimate(df: pd.DataFrame, n: int = STREAM_POINTS, recent_s: float = 0.0,
              recent_n: int = 0) -> pd.DataFrame:
    """Thin across the whole session (a tail would show only the last
    minute of an active market), always keeping the last row and every row
    that produced a take -- those are what the chart is read to check.
    With `recent_n`, also keep up to that many rows from the last `recent_s`
    seconds, for the dashboard's short windows."""
    if len(df) <= n + recent_n:
        return df
    step = len(df) // n + 1
    keep = np.zeros(len(df), dtype=bool)
    keep[::step] = True
    keep[-1] = True
    if recent_n and "time" in df.columns:
        t = pd.to_datetime(df["time"], utc=True)
        idx = np.flatnonzero((t >= t.iloc[-1] - pd.Timedelta(seconds=recent_s)).to_numpy())
        if len(idx):
            keep[idx[::len(idx) // recent_n + 1]] = True
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
    # Edges exist only once a decision has priced them; before that the
    # bands are gaps, not a zero-width band on fair.
    prob = q["prob"].where(_col(q, "bid_reservation").notna())
    const = _col(q, "constant_edge").fillna(0.0)
    c_bid, c_ask = prob - const, prob + const
    l_bid, l_ask = c_bid - _col(q, "lean_bid").fillna(0.0), c_ask + _col(q, "lean_ask").fillna(0.0)
    return {"c_bid": _clean(c_bid), "c_ask": _clean(c_ask), "l_bid": _clean(l_bid), "l_ask": _clean(l_ask),
            "ci_bid": _clean(l_bid - _col(q, "ci_edge").fillna(0.0)),
            "ci_ask": _clean(l_ask + _col(q, "ci_edge_ask").fillna(0.0))}


def _hold(values: np.ndarray, sets: np.ndarray) -> np.ndarray:
    """At each row, the value from the most recent row where `sets` is True
    (NaN before the first). Unlike ffill, a setting row's own NaN is kept:
    a withheld fair or a pulled quote must show as a gap, not the last value."""
    idx = np.where(sets, np.arange(len(sets)), -1)
    idx = np.maximum.accumulate(idx) if len(idx) else idx
    out = np.full(len(values), np.nan)
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out


_DECISION_COLS = ("prob", "constant_edge", "lean_bid", "lean_ask", "ci_edge", "ci_edge_ask",
                  "bid_reservation", "ask_reservation", "quote_bid", "quote_ask", "lip_bid", "lip_ask",
                  "bid_final", "ask_final")


def market_timeline(q: pd.DataFrame, bbo: pd.DataFrame, fair: pd.DataFrame) -> pd.DataFrame:
    """One market's chart rows from all three sources, so the touch and our
    fair plot from the session start even before (or without) any decision:
      touch     -- Kalshi BBO changes and decision rows
      fair/band -- fair updates (NaN when withheld) and decision rows
      the rest  -- decision rows only (reservations, edges, our quotes)
    Each value is held from the last row that set it."""
    parts = []
    if len(bbo):
        parts.append(pd.DataFrame({"time": bbo["time"], "_src": "b", "mb": bbo["bid"], "ma": bbo["ask"]}))
    if len(fair):
        parts.append(pd.DataFrame({"time": fair["time"], "_src": "f", "fp": fair["prob"],
                                   "flo": fair["prob_lo"], "fhi": fair["prob_hi"]}))
    if len(q):
        d = pd.DataFrame({"time": q["time"], "_src": "d", "mb": q["market_bid"], "ma": q["market_ask"],
                          "fp": _col(q, "prob_raw"), "flo": _col(q, "prob_lo"), "fhi": _col(q, "prob_hi")})
        for c in _DECISION_COLS:
            d[c] = _col(q, c).to_numpy()
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    t = pd.concat(parts, ignore_index=True)
    t["time"] = pd.to_datetime(t["time"], utc=True)
    t = t.sort_values("time", kind="stable").reset_index(drop=True)
    src = t["_src"].to_numpy()
    out = pd.DataFrame({"time": t["time"]})
    num = lambda c: pd.to_numeric(t[c], errors="coerce").to_numpy(dtype=float) if c in t else np.full(len(t), np.nan)
    for c, setters in (("market_bid", ("b", "d")), ("market_ask", ("b", "d")),
                       ("prob_raw", ("f", "d")), ("prob_lo", ("f", "d")), ("prob_hi", ("f", "d"))):
        col = {"market_bid": "mb", "market_ask": "ma", "prob_raw": "fp", "prob_lo": "flo", "prob_hi": "fhi"}[c]
        out[c] = _hold(num(col), np.isin(src, setters))
    is_d = src == "d"
    for c in _DECISION_COLS:
        # Take prices are events, not levels: only on the row that made them.
        out[c] = num(c) if c in ("bid_final", "ask_final") else _hold(num(c), is_d)
    # Before the first decision there is no clipped fair; the raw one is it.
    out["prob"] = np.where(np.isnan(out["prob"]), out["prob_raw"], out["prob"])
    return out


def write_streams(series: str, quote_df: pd.DataFrame, fills_df: pd.DataFrame, pnl_by_ticker: dict,
                  bbo_df: pd.DataFrame | None = None, fair_df: pd.DataFrame | None = None,
                  tickers: list | None = None):
    """One small JSON per market of the running event, decisions or not. One
    file per market so a poll downloads only the market on screen."""
    out = stream_dir(series)
    os.makedirs(out, exist_ok=True)
    bbo_df = pd.DataFrame() if bbo_df is None else bbo_df
    fair_df = pd.DataFrame() if fair_df is None else fair_df
    wanted = set(tickers or [])
    for df in (fills_df, quote_df, bbo_df, fair_df):
        if not df.empty:
            wanted |= set(df["ticker"].unique())
    groups_of = lambda df: dict(tuple(df.groupby("ticker"))) if not df.empty else {}
    q_groups, f_groups = groups_of(quote_df), groups_of(fills_df)
    b_groups, fa_groups = groups_of(bbo_df), groups_of(fair_df)
    empty = fills_df.iloc[0:0]
    nothing = pd.DataFrame()
    for tk in wanted:
        q = market_timeline(q_groups.get(tk, nothing), b_groups.get(tk, nothing), fa_groups.get(tk, nothing))
        q = _decimate(q, recent_s=STREAM_RECENT_S, recent_n=STREAM_RECENT_POINTS) if len(q) else q
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
            "br": _clean(_col(q, "bid_reservation")) if has else [],
            "ar": _clean(_col(q, "ask_reservation")) if has else [],
            **_edge_bands(q),
            "qb": _clean(_col(q, "quote_bid")) if has else [], "qa": _clean(_col(q, "quote_ask")) if has else [],
            "lb": _clean(_col(q, "lip_bid")) if has else [], "la": _clean(_col(q, "lip_ask")) if has else [],
            **{f"f{s}_{k}t": _ms(g["time"]) for (s, k), g in groups.items()},
            **{f"f{s}_{k}p": _clean(g["yes_equiv_price"]) for (s, k), g in groups.items()},
            "pos_t": _ms(f["time"]), "pos_v": [float(v) for v in sgn],
            "pnl_t": _ms(pnl["time"]), "pnl_v": _clean(pnl["pnl"]),
        }
        _write_json({"series": series, "ticker": tk, "market": entry}, os.path.join(out, stream_filename(tk)))
