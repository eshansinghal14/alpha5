"""Strategy performance panel: cumulative PnL with 1s/10s/60s markout over a
chosen window, and the summary numbers beside it. Data is what
kalshi_viz.perf_worker last wrote; a page render never computes it."""
from __future__ import annotations

import math

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_viz import perf

WINDOWS = {"1h": pd.Timedelta(hours=1), "6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24),
           "7d": pd.Timedelta(days=7), "All": None}
COLORS = {"PnL (settled; open at mid)": "#1e88e5", "1s markout": "#fb8c00", "10s markout": "#e53935",
          "60s markout": "#6d4c41", "LIP reward": "#43a047", "PnL incl. LIP": "#8e24aa"}
SOLID = ("PnL (settled; open at mid)", "PnL incl. LIP")


def _usd(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"${v:+.2f}"


def _c(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.2f}c"


def _chart(df: pd.DataFrame, since: pd.Timestamp | None, lip: pd.DataFrame | None = None):
    c = perf.curves(df, since, lip)
    if c.empty:
        st.caption("No real fills in this window.")
        return
    shown = [k for k in COLORS if k in set(c.line)]
    base = alt.Chart(c).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M")),
        y=alt.Y("usd:Q", title="$ (cumulative)"),
        color=alt.Color("line:N", title=None, scale=alt.Scale(domain=shown, range=[COLORS[k] for k in shown]),
                        legend=alt.Legend(orient="top-left")),
        strokeDash=alt.condition(alt.FieldOneOfPredicate(field="line", oneOf=list(SOLID)),
                                 alt.value([1, 0]), alt.value([5, 3])),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M:%S"), "line:N", alt.Tooltip("usd:Q", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#888", strokeWidth=0.7).encode(y="y:Q")
    st.altair_chart((base.mark_line(interpolate="step-after", strokeWidth=2) + zero).properties(height=280),
                    use_container_width=True)


PNL_HELP = "Settled fills at their result; fills on open markets at the latest Kalshi mid."
MO_HELP = ("Per contract vs Kalshi mid 1s after the fill (10s and 60s below), fee included. Negative = "
           "picked off. 60s catches drift a 10s mark misses; wide books at LIP prices make 1s noisy.")
SPREAD_HELP = "Mid 1ms before the fill minus our price, fee included: the half-spread we got at the fill."


def _common(g: dict):
    """The rows every column shows, so the three line up."""
    st.metric("1s markout", _c(g["mo1"]), f"10s {_c(g['mo10'])} · 60s {_c(g['mo60'])}", delta_color="off",
              help=MO_HELP)
    st.metric("Spread at fill", _c(g["mo0"]), delta_color="off", help=SPREAD_HELP)
    st.metric("Lots filled", f"{g['contracts']:,.0f}",
              f"{g['fills']} fills" + (f" · {g['taker']} taker" if g["taker"] else ""), delta_color="off")


def _overall(g: dict, lip: bool):
    st.markdown("**Overall**")
    if not g["fills"] and not lip:
        st.caption("No real fills in this window.")
        return
    if lip:
        st.metric("PnL incl. LIP", _usd(g["pnl_incl_lip"]), f"fills only {_usd(g['pnl'])}", delta_color="off",
                  help=PNL_HELP + " Plus the estimated LIP reward.")
    else:
        st.metric("PnL", _usd(g["pnl"]), f"{_usd(g['per_h'])}/h", delta_color="off", help=PNL_HELP)
    _common(g)


def _regular(g: dict):
    st.markdown("**Regular quoting**")
    if not g["fills"]:
        st.caption("No regular fills in this window.")
        return
    st.metric("PnL", _usd(g["pnl"]), f"{_usd(g['per_h'])}/h", delta_color="off",
              help=PNL_HELP + " Edge quotes and takes; everything not on the LIP quote.")
    _common(g)


def _lip(g: dict):
    st.markdown("**LIP quoting**")
    if not g.get("markets") and not g["fills"]:
        st.caption("No LIP payable while armed in this window.")
        return
    st.metric("Net (reward + 10s markout)", _usd(g["net_mo10"]),
              f"60s {_usd(g['net_mo60'])} · settled {_usd(g['net'])}", delta_color="off",
              help="Estimated LIP reward from the scoring replica while armed (not money received), only "
                   "markets at or over Kalshi's $1.00 minimum payout, plus the LIP fills' markout $ vs Kalshi "
                   "mid 10s (60s) later. Settled is the same with settlement PnL: far noisier.")
    _common(g)
    st.metric("Reward per lot filled", _c(g["reward_c"]), f"{_usd(g['reward_per_h'])}/h", delta_color="off",
              help="LIP reward over LIP lots filled. Above |markout| means the program pays for the "
                   "adverse selection.")


def _pct(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.1%}"


def _edge_share(E: dict):
    """Our edge vs theo as a share of all the edge vs theo that traded."""
    if not E.get("prints"):
        st.caption("Edge market share: no public prints journaled while armed in this window "
                   "(the trade tape is journaled from 2026-10-15).")
        return
    a, b, c = st.columns(3)
    a.metric("Edge market share", _pct(E["share"]), f"{_usd(E['ours_usd'])} of ${E['market_usd']:.2f}",
             delta_color="off",
             help="Edge our fills took vs our theo, over the edge every trade in these markets gave up vs "
                  "the same theo (size x |theo - price| per print). Before fees, armed only. Negative = "
                  "on net we bought above our own theo.")
    b.metric("Volume share", _pct(E["vol_share"]),
             f"{E['our_lots']:,.0f} of {E['market_lots']:,.0f} lots", delta_color="off",
             help="Our contracts over all contracts printed. Edge share above volume share = we take the "
                  "better-priced part of the flow.")
    c.metric("Edge per lot (c)", _c(E["ours_c"]), f"market {_c(E['market_c'])}", delta_color="off",
             help="Cents of edge vs theo per contract: ours vs the average print.")
    if "maker_usd" in E:
        st.caption(f"Our edge: maker {_usd(E['maker_usd'])} · taker {_usd(E['taker_usd'])} · "
                   f"{E['prints']:,} prints priced")


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


def render(series: str, lip: bool = False):
    """`lip`: the series' events carry LIP programs; adds the LIP column."""

    @st.fragment(run_every="15s")
    def body():
        # Same shared-fragment-id race as app._stale: don't draw a series
        # that is no longer selected.
        if st.session_state.get("dash_series", series) != series:
            st.rerun(scope="app")
            return
        r = perf.read_result(series)
        if r is None:
            st.caption("Waiting for the performance worker's first pass over the journals "
                       "(`python -m kalshi_viz.perf_worker`; on EC2 the alpha5-perf unit).")
            return
        df = r.fills
        if r.error:
            st.caption(f"Last performance refresh failed: {r.error}")
        win = st.radio("Window", list(WINDOWS), index=2, horizontal=True, key=f"perf_win_{series}",
                       label_visibility="collapsed")
        since = None if WINDOWS[win] is None else pd.Timestamp.now(tz="UTC") - WINDOWS[win]
        s = perf.summarize(df, since)
        lf = r.lip if lip else None
        sp = perf.summarize_split(df, lf, since)
        cols = st.columns(3 if lip else 2)
        with cols[0]:
            _overall(sp["all"], lip)
        with cols[1]:
            _regular(sp["regular"])
        if lip:
            with cols[2]:
                _lip(sp["lip"])
        _chart(df, since, lf)
        _edge_share(perf.summarize_edge(df, r.prints, since))
        if s.get("fills"):
            st.caption(f"Settled {_usd(s['settled_usd'])} · open {_usd(s['open_usd'])} on {s['unsettled']} fills · "
                       f"fees ${s['fees']:.2f} · max drawdown {_usd(s['max_dd'])} · {s['events']} events")
        with st.expander("Markout by fill type"):
            _table(s)
        if r.updated is not None:
            st.caption(f"Updated {r.updated:%H:%M:%S} UTC. Fills are marked once 60s of book follows them.")

    with st.container(border=True):
        st.markdown("##### Strategy performance (real fills)")
        body()
