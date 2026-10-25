"""Arbitrage · single-leg: backtest results for buying only a basket's
lagging leg (kalshi_strategies/ARBS/single_leg/backtest.py). Reads
data/arbs_single_leg/{result.json, trades_*.parquet}; the Run button starts
the backtest as a detached low-priority process."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_infra import REPO_ROOT
from kalshi_strategies.ARBS.single_leg.backtest import OUT_DIR

LOG = os.path.join(OUT_DIR, "run.log")
PID = os.path.join(OUT_DIR, "run.pid")


def _running() -> bool:
    try:
        with open(PID) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _start(days: float):
    os.makedirs(OUT_DIR, exist_ok=True)
    cmd = [sys.executable, "-u", "-m", "kalshi_strategies.ARBS.single_leg.backtest", "--days", f"{days:g}"]
    if os.name != "nt":
        cmd = ["nice", "-n", "19", *cmd]
    with open(LOG, "w") as log:
        p = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=os.name != "nt")
    with open(PID, "w") as fh:
        fh.write(str(p.pid))


def _pct(v):
    return "n/a" if v is None else f"{v:+.1%}"


def _c(v):
    return "n/a" if v is None else f"{v:+.2f}c"


def _summary(label: str, r: dict):
    st.markdown(f"**{label}**")
    if not r or not r.get("trades"):
        st.caption("No trades.")
        return
    c = st.columns(5)
    c[0].metric("PnL (1 contract/trade)", f"${r['pnl_per_contract']:+.2f}", f"{r['trades']} trades", delta_color="off")
    c[1].metric("Return on entry", _pct(r["return_on_entry"]), f"avg {_c(r['avg_pnl_c'])}/trade", delta_color="off",
                help="Sum of PnL over sum of entry prices: the cash a leg ties up while held.")
    c[2].metric("Win rate", f"{r['win_rate']:.0%}", f"{r['closed']} closed", delta_color="off")
    c[3].metric("Median hold", f"{r['median_hold_min'] / 60:.1f}h", delta_color="off")
    c[4].metric("Worst / best", _c(r["worst_c"]), _c(r["best_c"]), delta_color="off")
    br = r.get("by_reason") or {}
    st.caption("Exits: " + " · ".join(f"{k} {v['n']} (${v['pnl']:+.2f})" for k, v in sorted(br.items())))
    p = r.get("params") or {}
    st.caption("Params: " + ", ".join(f"{k}={v}" for k, v in p.items()))


def _trades(path: str):
    if not os.path.exists(path):
        return
    t = pd.read_parquet(path)
    if t.empty:
        return
    t = t.sort_values("entry_ts")
    t["time"] = pd.to_datetime(t.exit_ts, unit="s", utc=True)
    t["cum_pnl"] = t.pnl.cumsum()
    st.altair_chart(alt.Chart(t).mark_line(interpolate="step-after").encode(
        x=alt.X("time:T", title=None), y=alt.Y("cum_pnl:Q", title="$ cumulative (1 contract/trade)"),
        tooltip=["basket_id", "ticker", "reason", alt.Tooltip("pnl:Q", format="+.4f")]).properties(height=240),
        use_container_width=True)
    t["type"] = t.basket_id.str.split(":").str[0]
    by = t.groupby("type").agg(trades=("pnl", "size"), pnl=("pnl", "sum"), avg_c=("pnl", "mean"),
                               win=("pnl", lambda s: (s > 0).mean()), hold_h=("hold_min", "median"))
    by["avg_c"] *= 100
    by["hold_h"] /= 60
    st.markdown("By relationship type")
    st.dataframe(by.sort_values("pnl").style.format({"pnl": "{:+.2f}", "avg_c": "{:+.2f}", "win": "{:.0%}",
                                                     "hold_h": "{:.1f}"}), use_container_width=True)
    with st.expander(f"All {len(t)} trades"):
        show = t.assign(entry_time=pd.to_datetime(t.entry_ts, unit="s", utc=True))[
            ["entry_time", "basket_id", "ticker", "side", "entry", "target", "exit", "reason", "pnl", "lag",
             "hold_min"]]
        st.dataframe(show, use_container_width=True, hide_index=True)


def render(page: str, stale_fn):
    st.title("Arbitrage · single-leg")
    st.caption("Backtest only. Buy just the basket leg that lags its relationship (fair = payout minus the other "
               "legs' mids), rest a sell at that fair, stop out or time out; optionally complete into the full "
               "basket at the max hold. One contract per trade, 1-minute Kalshi candles: no depth or queue, so "
               "fills are optimistic.")
    path = os.path.join(OUT_DIR, "result.json")
    r = None
    if os.path.exists(path):
        with open(path) as fh:
            r = json.load(fh)
    c = st.columns([1, 1, 3])
    days = c[0].number_input("Days", min_value=1.0, max_value=120.0, value=float((r or {}).get("window", {})
                                                                                .get("days", 30.0)), step=1.0)
    running = _running()
    if c[1].button("Run backtest", disabled=running, use_container_width=True):
        _start(days)
        st.rerun()
    if running:
        c[2].info("Backtest running (fetches new candles, then a parameter sweep; a few minutes).")
        if os.path.exists(LOG):
            with open(LOG, errors="replace") as fh:
                st.code("".join(fh.readlines()[-8:]) or "starting...")
    if r is None:
        st.caption("No backtest has run yet. On the box: `python -m kalshi_strategies.ARBS.single_leg.backtest`.")
        return
    w = r["window"]
    st.caption(f"Ran {pd.Timestamp(r['run_at']):%Y-%m-%d %H:%M} UTC in {r['runtime_s']:.0f}s · window "
               f"{pd.Timestamp(w['start']):%m-%d %H:%M} to {pd.Timestamp(w['end']):%m-%d %H:%M} UTC · "
               f"{r['n_baskets']} baskets, {r['n_markets']} markets")
    with st.container(border=True):
        _summary("Default parameters", r.get("default"))
        _trades(os.path.join(OUT_DIR, "trades_default.parquet"))
    with st.container(border=True):
        _summary("Best of the sweep (by PnL; in-sample, so optimistic)", r.get("best"))
        _trades(os.path.join(OUT_DIR, "trades_best.parquet"))
    with st.expander(f"Sweep ({len(r.get('sweep') or [])} parameter sets)"):
        rows = [{**x["params"], **{k: x.get(k) for k in ("trades", "pnl_per_contract", "return_on_entry",
                                                         "win_rate", "median_hold_min")}} for x in r.get("sweep") or []]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
