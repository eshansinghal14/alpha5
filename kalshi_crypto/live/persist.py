"""
Persistence helpers for live sessions. Writes:

  * data/live/KXCRYPTOLEAD15M/{date}/summary.json
  * data/live/KXCRYPTOLEAD15M/{date}/quote_df.parquet
  * data/live/KXCRYPTOLEAD15M/{date}/fills_df.parquet
  * data/live/KXCRYPTOLEAD15M/{date}/pnl_df.parquet
  * viz/static/live_KXCRYPTOLEAD15M__{ticker-with-dashes-only}.json  (per market)

Shape of the stream JSONs matches what live_chart.py expects for the
SPX series -- the viz dropdown exposes each of the 5 crypto markets
individually.

Atomic writes via `.tmp` + `os.replace` with a Windows-friendly retry
helper, matching kalshi_latency_arbs/live/live_persist.py:
_replace_with_retry.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

import pandas as pd


STREAM_POINTS = 400   # same decimation target as live_persist.py


def _replace_with_retry(src: Path, dst: Path, retries: int = 10, delay_s: float = 0.05):
    """Windows can transiently refuse an os.replace while a reader has
    the file mapped (streamlit's live chart polls these files every
    1.5s). Mirrors live_persist._replace_with_retry."""
    for _ in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay_s)
    os.replace(src, dst)   # final attempt; raise if still blocked


def _atomic_write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    _replace_with_retry(tmp, path)


def _atomic_write_parquet(path: Path, df: pd.DataFrame):
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    _replace_with_retry(tmp, path)


def write_summary(path: Path, summary: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, summary)


# Column schemas for the four live parquet files the viz expects. All
# four files MUST exist on disk from the first flush -- the viz's
# load_live_day reads all four and raises FileNotFoundError if any one
# is missing, which the live-mode render treats as "no session running"
# (observed 2026-10-06: only summary.json was written on cold start, so
# the dashboard showed the no-session message against a live process).
_QUOTE_DF_COLUMNS = [
    "time", "ticker", "prob", "prob_lo", "prob_hi", "ci_half",
    "ci_edge", "ci_edge_ask", "constant_edge", "bid_edge", "ask_edge",
    "lean_bid", "lean_ask", "taker_fee_bid", "taker_fee_ask",
    "net_position", "yes_qty", "no_qty", "eff_yes", "eff_no", "ratio",
    "lean_dollars", "event_kind", "event_ticker", "market_bid",
    "market_ask", "bid_reservation", "ask_reservation", "bid_final",
    "ask_final", "inflight_yes", "inflight_no", "cap_allows_yes",
    "cap_allows_no",
]
_FILLS_DF_COLUMNS = ["time", "ticker", "side", "price", "size", "fee",
                     "liquidity", "order_id", "yes_equiv_price"]
_PNL_DF_COLUMNS = ["time", "pnl"]
_PROB_GRID_COLUMNS = ["time", "ticker", "prob", "prob_lo", "prob_hi"]


def write_quote_df(path: Path, debug_log: list[dict]):
    df = pd.DataFrame(debug_log) if debug_log else pd.DataFrame(columns=_QUOTE_DF_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(path, df)


def write_fills_df(path: Path, fills: list):
    if fills:
        rows = [{
            "time": f.time, "ticker": f.ticker, "side": f.side,
            "price": f.price, "size": f.size, "fee": f.fee,
            "liquidity": f.liquidity, "order_id": f.order_id,
            "yes_equiv_price": f.price if f.side == "yes" else (1.0 - f.price),
        } for f in fills]
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(columns=_FILLS_DF_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(path, df)


def write_pnl_df(path: Path, pnl_history: list[tuple]):
    df = (pd.DataFrame(pnl_history, columns=_PNL_DF_COLUMNS)
          if pnl_history else pd.DataFrame(columns=_PNL_DF_COLUMNS))
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(path, df)


def write_prob_grid(path: Path, debug_log: list[dict]):
    """Long-form (time, ticker, prob, prob_lo, prob_hi) history, pivoted
    by the viz into a wide (grid x ticker) frame for the per-market
    probability chart. Reconstructed from debug_log because the crypto
    strategy doesn't keep a separate prob_history (unlike the SPX
    runner, which has one as an input to Breeden-Litzenberger)."""
    if debug_log:
        rows = [{"time": r["time"], "ticker": r["ticker"],
                 "prob": r.get("prob"), "prob_lo": r.get("prob_lo"),
                 "prob_hi": r.get("prob_hi")}
                for r in debug_log]
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(columns=_PROB_GRID_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(path, df)


def reset_session_files(live_dir: Path):
    """Clear the session's parquet / JSON files so a new session isn't
    rendered on top of the previous one. Does NOT clear per-market
    stream JSONs here (viz/static/) because the stream writer rewrites
    them atomically every heavy flush anyway."""
    live_dir.mkdir(parents=True, exist_ok=True)
    for name in ("quote_df.parquet", "fills_df.parquet",
                 "pnl_df.parquet", "prob_grid.parquet"):
        p = live_dir / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Per-market stream JSON -- the viz polls these for the live dropdown.
# ---------------------------------------------------------------------------

def stream_filename(series: str, ticker: str) -> str:
    """Mirrors viz/live_persist.stream_filename convention: dots in the
    ticker become dashes so the filename is Streamlit-safe."""
    safe = ticker.replace(".", "-")
    return f"live_{series}__{safe}.json"


def write_stream_json_per_market(static_dir: Path, series: str,
                                 market_log: dict[str, list[dict]],
                                 fills: list):
    """One JSON per market, in the viz/static/live_chart.py shape.
    Decimates to STREAM_POINTS; always preserves crossings
    (`bid_final` or `ask_final` is not None).
    """
    static_dir.mkdir(parents=True, exist_ok=True)
    fills_by_ticker: dict[str, list] = {}
    for f in fills:
        fills_by_ticker.setdefault(f.ticker, []).append(f)

    for ticker, rows in market_log.items():
        if not rows:
            continue
        decimated = _decimate_rows(rows, STREAM_POINTS)
        tf = fills_by_ticker.get(ticker, [])
        payload = _rows_to_stream_payload(ticker, decimated, tf)
        wrapper = {
            "series": series,
            "ticker": ticker,
            "markets": {ticker: payload},
        }
        path = static_dir / stream_filename(series, ticker)
        try:
            _atomic_write_json(path, wrapper)
        except Exception as e:
            print(f"[kalshi_crypto] stream write failed for {ticker}: {e!r}")


def _decimate_rows(rows: list[dict], target: int) -> list[dict]:
    if len(rows) <= target:
        return rows
    # Always keep "crossings" (bid_final / ask_final non-null) and
    # downsample the rest to roughly target - n_crossings points. Same
    # policy as live_persist._decimate.
    keep_idx = {i for i, r in enumerate(rows)
                if r.get("bid_final") is not None or r.get("ask_final") is not None}
    others = [i for i in range(len(rows)) if i not in keep_idx]
    remaining = max(target - len(keep_idx), 10)
    if remaining < len(others):
        step = len(others) / remaining
        picked = {others[int(i * step)] for i in range(remaining)}
        keep_idx |= picked
    else:
        keep_idx |= set(others)
    return [rows[i] for i in sorted(keep_idx)]


def _ts_ms(ts):
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    return int(float(ts) * 1000)


def _rows_to_stream_payload(ticker: str, rows: list[dict], fills: list) -> dict:
    """Same key shape live_chart.py reads: q_t/bid/ask/prob_on_q/br/ar/
    etc. See `viz/static/live_KXINX__*.json` for a reference."""
    q_t = [_ts_ms(r["time"]) for r in rows]
    bid = [r.get("market_bid") for r in rows]
    ask = [r.get("market_ask") for r in rows]
    prob = [r.get("prob") for r in rows]
    br = [r.get("bid_reservation") for r in rows]
    ar = [r.get("ask_reservation") for r in rows]
    c_bid = [None if r.get("prob") is None else r["prob"] - r.get("constant_edge", 0.0)
             for r in rows]
    c_ask = [None if r.get("prob") is None else r["prob"] + r.get("constant_edge", 0.0)
             for r in rows]
    l_bid = [None if r.get("prob") is None else r["prob"] - (r.get("constant_edge", 0.0) + r.get("lean_bid", 0.0))
             for r in rows]
    l_ask = [None if r.get("prob") is None else r["prob"] + (r.get("constant_edge", 0.0) + r.get("lean_ask", 0.0))
             for r in rows]
    ci_bid = [None if r.get("prob") is None else r["prob"] - (r.get("constant_edge", 0.0) + r.get("lean_bid", 0.0) + r.get("ci_edge", 0.0))
              for r in rows]
    ci_ask = [None if r.get("prob") is None else r["prob"] + (r.get("constant_edge", 0.0) + r.get("lean_ask", 0.0) + r.get("ci_edge_ask", 0.0))
              for r in rows]
    qb = [r.get("quote_bid") for r in rows]
    qa = [r.get("quote_ask") for r in rows]
    # Crypto: every band is stacked on theo. The outer band is the MAKER
    # reservation our resting quotes are bounded by (no taker fee, LIP
    # credit subtracted); taker fee and LIP reference ride separately.
    # The old outer band was the TAKER reservation, which moves with the
    # Kalshi touch (fee = ask*(1-ask)) and the LIP credit every tick --
    # that is why it moved while theo was flat.
    mbr = [r.get("maker_bid_reservation", r.get("bid_reservation")) for r in rows]
    mar = [r.get("maker_ask_reservation", r.get("ask_reservation")) for r in rows]
    lip_rb = [r.get("lip_ref_bid") for r in rows]
    lip_ra = [r.get("lip_ref_ask") for r in rows]

    # Fills, bucketed by (side, liquidity).
    fy_mt, fy_mp, fy_tt, fy_tp = [], [], [], []
    fn_mt, fn_mp, fn_tt, fn_tp = [], [], [], []
    for f in fills:
        yes_eq = f.price if f.side == "yes" else (1.0 - f.price)
        t = _ts_ms(f.time)
        if f.side == "yes":
            if f.liquidity == "maker":
                fy_mt.append(t); fy_mp.append(yes_eq)
            else:
                fy_tt.append(t); fy_tp.append(yes_eq)
        else:
            if f.liquidity == "maker":
                fn_mt.append(t); fn_mp.append(yes_eq)
            else:
                fn_tt.append(t); fn_tp.append(yes_eq)

    pos_t = []
    pos_v = []
    cum = 0.0
    for f in sorted(fills, key=lambda x: x.time):
        delta = f.size if f.side == "yes" else -f.size
        cum += delta
        pos_t.append(_ts_ms(f.time))
        pos_v.append(cum)

    pnl_t = []
    pnl_v = []
    cum_pnl = 0.0
    for f in sorted(fills, key=lambda x: x.time):
        cum_pnl -= f.fee
        pnl_t.append(_ts_ms(f.time))
        pnl_v.append(cum_pnl)

    return {
        "q_t": q_t,
        "bid": bid, "ask": ask, "prob_on_q": prob,
        "br": mbr, "ar": mar,
        "tbr": br, "tar": ar,
        "lip_rb": lip_rb, "lip_ra": lip_ra,
        "c_bid": c_bid, "c_ask": c_ask,
        "l_bid": l_bid, "l_ask": l_ask,
        "ci_bid": ci_bid, "ci_ask": ci_ask,
        "qb": qb, "qa": qa,
        "fy_mt": fy_mt, "fy_mp": fy_mp,
        "fy_tt": fy_tt, "fy_tp": fy_tp,
        "fn_mt": fn_mt, "fn_mp": fn_mp,
        "fn_tt": fn_tt, "fn_tp": fn_tp,
        "pos_t": pos_t, "pos_v": pos_v,
        "pnl_t": pnl_t, "pnl_v": pnl_v,
    }


__all__ = [
    "write_summary", "write_quote_df", "write_fills_df", "write_pnl_df",
    "write_prob_grid",
    "write_stream_json_per_market", "reset_session_files",
    "stream_filename", "STREAM_POINTS",
]
