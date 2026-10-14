"""Strategy performance panel: cumulative PnL with 1s/10s markout over a
chosen window, and the summary numbers beside it. Data from kalshi_viz.perf,
refreshed in a background thread so a page render never waits on it."""
from __future__ import annotations

import math

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_viz import perf

WINDOWS = {"1h": pd.Timedelta(hours=1), "6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24),
           "7d": pd.Timedelta(days=7), "All": None}
COLORS = {"PnL (settled; open at mid)": "#1e88e5", "1s markout": "#fb8c00", "10s markout": "#e53935"}


def _usd(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"${v:+.2f}"


def _c(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.2f}c"


def _chart(df: pd.DataFrame, since: pd.Timestamp | None):
    c = perf.curves(df, since)
    if c.empty:
        st.caption("No real fills in this window.")
        return
    base = alt.Chart(c).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M")),
        y=alt.Y("usd:Q", title="$ (cumulative)"),
        color=alt.Color("line:N", title=None, scale=alt.Scale(domain=list(COLORS), range=list(COLORS.values())),
                        legend=alt.Legend(orient="top-left")),
        strokeDash=alt.condition(alt.datum.line == "PnL (settled; open at mid)", alt.value([1, 0]), alt.value([5, 3])),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M:%S"), "line:N", alt.Tooltip("usd:Q", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#888", strokeWidth=0.7).encode(y="y:Q")
    st.altair_chart((base.mark_line(interpolate="step-after", strokeWidth=2) + zero).properties(height=280),
                    use_container_width=True)


def _side(s: dict):
    if not s.get("fills"):
        st.caption("No real fills in this window.")
        return
    a, b = st.columns(2)
    a.metric("PnL", _usd(s["pnl"]), f"{_usd(s['per_h'])}/h", delta_color="off",
             help="Settled fills at their result; fills on open markets at the latest Kalshi mid.")
    b.metric("Settled / open", _usd(s["settled_usd"]), f"open {_usd(s['open_usd'])} on {s['unsettled']} fills",
             delta_color="off")
    a, b = st.columns(2)
    m = s["markouts"].get("all", {})
    a.metric("1s markout", _c(m.get("mo1")), f"10s {_c(m.get('mo10'))}", delta_color="off",
             help="Per contract vs Kalshi mid, fee included.")
    a2 = s["markouts"].get("maker", {})
    b.metric("Spread captured", _c(m.get("mo0")), f"maker 10s {_c(a2.get('mo10'))}", delta_color="off",
             help="Mid just before the fill minus our price: the half-spread we got at the fill.")
    a, b = st.columns(2)
    t = s.get("t_event")
    a.metric("Lots filled", f"{s['contracts']:,.0f}", f"{s['fills']} fills · {s['taker']} taker", delta_color="off",
             help="Contracts traded in the window, maker and taker.")
    b.metric("Max drawdown", _usd(s["max_dd"]), f"fees ${s['fees']:.2f}", delta_color="off")
    st.caption(f"{s['events']} events · {s['won']:.0%} won"
               + (f" · t={t:+.2f} (under ~2 is within noise)" if not math.isnan(t) else ""))


def _table(s: dict):
    rows = s.get("markouts") or {}
    if not rows:
        return
    t = pd.DataFrame(rows).T[["fills", "mo0", "mo1", "mo10", "mo60", "pnl_c"]]
    t.columns = ["fills", "at fill", "+1s", "+10s", "+60s", "PnL"]
    t["fills"] = t["fills"].astype(int)
    st.dataframe(t.style.format({c: "{:+.2f}" for c in t.columns if c != "fills"}), use_container_width=True)
    st.caption("Cents per contract vs Kalshi mid, fee included. 'at fill' uses the mid 1ms before the fill; "
               "PnL is settled (or open at mid).")


def render(series: str):
    p = perf.get(series)
    p.ensure_background()

    @st.fragment(run_every="15s")
    def body():
        # Same shared-fragment-id race as app._stale: don't draw a series
        # that is no longer selected.
        if st.session_state.get("dash_series", series) != series:
            st.rerun(scope="app")
            return
        df = p.latest
        if df is None:
            st.caption("Loading performance from the day journals… (first pass takes ~30s)")
            return
        if p.error:
            st.caption(f"Last performance refresh failed: {p.error}")
        win = st.radio("Window", list(WINDOWS), index=2, horizontal=True, key=f"perf_win_{series}",
                       label_visibility="collapsed")
        since = None if WINDOWS[win] is None else pd.Timestamp.now(tz="UTC") - WINDOWS[win]
        s = perf.summarize(df, since)
        left, right = st.columns([3, 2])
        with left:
            _chart(df, since)
        with right:
            _side(s)
        with st.expander("Markout by fill type"):
            _table(s)
        if p.updated is not None:
            st.caption(f"Updated {p.updated:%H:%M:%S} UTC. Fills are marked once 60s of book follows them.")

    with st.container(border=True):
        st.markdown("##### Strategy performance (real fills)")
        body()
