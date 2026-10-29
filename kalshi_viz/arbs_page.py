"""ARBS dashboard page: arm switch, guards and alerts, PnL (settled, locked
in on held bundles), bundles held, resting quotes, the opportunity ranking
and the settled history. Reads only data/live/ARBS/<date>/summary.json."""
from __future__ import annotations

import json
import os

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_viz import arbs_pnl
from kalshi_viz.arm import arm_control
from kalshi_viz.data import latest_summary, read_summary, summary_age_s

STALE_S = 15.0
LOCAL_TZ = "America/New_York"
TIME_COL = st.column_config.DatetimeColumn(format="MM-DD HH:mm:ss")


def _local(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    """UTC times (ISO strings or timestamps) shown in local time."""
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)
    return df


def _usd(v) -> str:
    return "n/a" if v is None else f"${v:+,.2f}"


def _toggle(series: str, s: dict, idle: bool):
    live = bool(s.get("live_trading")) and not idle
    if live:
        st.error("🟢 **REAL TRADING IS LIVE**")
    else:
        st.info("🔵 SHADOW -- live books, simulated fills (separate shadow ledger), no real orders.")
    shown = arm_control(series, bool(s.get("arm_requested")),
                        help="Arming sweeps our resting orders, polls fills and refuses if the ledger disagrees "
                             "with Kalshi's positions. Disarming pulls every quote. Positions are never flattened.")
    if not idle and shown and not live:
        st.warning("Armed, not live yet: arming, halted, or the arm was refused (see alerts).")


def _cleared_path(series: str) -> str:
    return os.path.join(LIVE_ROOT, series, "alerts_cleared.json")


def _cleared(series: str) -> str:
    """ISO time up to which alerts were cleared as handled ("" if never)."""
    try:
        with open(_cleared_path(series), encoding="utf-8") as fh:
            return json.load(fh).get("before") or ""
    except (OSError, ValueError):
        return ""


def _alerts(series: str, s: dict):
    if s.get("halt_reason"):
        st.error(f"⛔ **Halted until restart -- {s['halt_reason']}.** Completions of broken baskets still run.")
    elif s.get("blocked_reason"):
        st.warning(f"⛔ No new takes/quotes -- {s['blocked_reason']}. Completions still run.")
    cs = s.get("config_status") or {}
    if cs.get("error"):
        st.error(f"config.json change REJECTED by the running process: {cs['error']}")
    if s.get("audit_diff"):
        st.error("📒 Ledger vs Kalshi position mismatch: "
                 + ", ".join(f"{t} kalshi {d['kalshi']:+g} / ours {d['ledger']:+g}" for t, d in s["audit_diff"].items()))
    broken = [h for h in s.get("holdings") or [] if h.get("unhedged")]
    for h in broken:
        c = h.get("completion") or {}
        st.error(f"🧩 **Incomplete basket {h['basket_id']}** for {h.get('broken_s') or 0}s -- unhedged "
                 + ", ".join(f"{t.split('-', 1)[-1]} {q:g}" for t, q in h["unhedged"].items())
                 + (f"; exits now: complete {_usd(c.get('complete_loss') and -c['complete_loss'])}, "
                    f"unwind {_usd(c.get('unwind_loss') and -c['unwind_loss'])} -- {c.get('action')} "
                    f"{c.get('path') or ''}" + (f" ({c['why']})" if c.get("why") else "") if c else ""))
    f = s.get("feed") or {}
    if not f.get("up"):
        st.error("📡 Kalshi websocket down (quotes are pulled after runtime.feed_down_pull_s).")
    # Times are all UTC isoformat from the runner, so they compare as strings.
    cut = _cleared(series)
    al = [a for a in (s.get("alerts") or []) if a["level"] in ("error", "warn") and a["time"] > cut]
    if al:
        with st.expander(f"Alerts ({len(al)} new)", expanded=any(a["level"] == "error" for a in al[-10:])):
            st.dataframe(_local(pd.DataFrame(al[::-1]), "time")[["time", "level", "basket_id", "msg", "live"]],
                         use_container_width=True, hide_index=True, column_config={"time": TIME_COL})
            if st.button("Clear (handled)", key="arbs_clear_alerts"):
                with open(_cleared_path(series), "w", encoding="utf-8") as fh:
                    json.dump({"before": al[-1]["time"]}, fh)
                st.rerun(scope="fragment")


def _metrics(s: dict):
    p = s.get("pnl") or {}
    c = st.columns(4)
    broken = p.get("broken") or 0.0
    n_broken = sum(1 for h in s.get("holdings") or [] if h.get("unhedged"))
    total = (p.get("settled") or 0.0) + (p.get("locked") or 0.0) + broken
    c[0].metric("Total PnL", _usd(total), f"{_usd(broken)} on {n_broken} unfinished" if n_broken else None,
                help="Settled (payouts and unwound contracts, minus everything paid incl. fees) plus what complete "
                     "bundles held lock in at settlement, plus unfinished baskets' extra legs at what they'd sell "
                     "for now.")
    c[1].metric("Capital used", f"${p.get('committed', 0):,.2f}", f"of ${p.get('max_dollars', 0):,.0f}",
                delta_color="off", help="Cost of held baskets plus what resting quotes commit if filled.")
    c[2].metric("Baskets tradeable", f"{s.get('n_tradeable')}/{s.get('n_baskets')}",
                f"{s.get('n_tickers')} markets", delta_color="off")
    n = s.get("counts") or {}
    c[3].metric("Quote fills / takes", f"{n.get('quote_fills', 0)} / {n.get('takes', 0)}",
                f"{n.get('completions', 0)} completions", delta_color="off")


@st.cache_data(ttl=5, show_spinner=False)
def _bundle_log(series: str) -> pd.DataFrame:
    return arbs_pnl.bundle_log(series)


def _tables(series: str, s: dict):
    st.markdown("##### Bundles bought (newest first)")
    log = _bundle_log(series)
    if len(log):
        small = st.column_config.Column(width="small")
        st.dataframe(_local(log.head(200).copy(), "time"), use_container_width=True, hide_index=True,
                     column_order=["time", "series", "basket_id", "how", "bundles", "cost", "pnl", "return", "fills"],
                     column_config={"time": st.column_config.DatetimeColumn(format="MM-DD HH:mm:ss", width="small"),
                                    "series": small, "basket_id": st.column_config.Column(width="medium"),
                                    "how": small, "bundles": small, "cost": small, "pnl": small, "return": small,
                                    "fills": st.column_config.Column(width="medium")})
    else:
        st.caption("None.")
    st.markdown("##### Resting quotes")
    q = s.get("quotes") or []
    if q:
        df = pd.DataFrame(q)
        if "ret" in df:
            df["return"] = df["ret"].map(lambda v: f"{v:.2%}")
        cols = ["basket_id", "ticker", "side", "price", "max_price", "size", "filled", "edge", "return", "age_s",
                "pending"]
        st.dataframe(df[[c for c in cols if c in df]], use_container_width=True, hide_index=True)
    else:
        st.caption("None.")
    with st.expander("Opportunity ranking (return on bundle cost of taking every leg now, fees incl.)"):
        r = s.get("ranking") or []
        if r:
            df = pd.DataFrame(r)
            df["hot"] = df["basket_id"].isin(set(s.get("hot") or []))
            if "ret" in df:
                df["ret"] = df["ret"].map(lambda v: f"{v:.2%}")
            st.dataframe(df, use_container_width=True, hide_index=True)
    with st.expander("Missed fills (prints through our quote price on baskets outside the quoted cap)"):
        m = s.get("missed") or []
        if m:
            st.dataframe(_local(pd.DataFrame(m), "last"), use_container_width=True, hide_index=True,
                         column_config={"last": TIME_COL})
        else:
            st.caption("None since the last restart.")
    with st.expander("Settled baskets"):
        st_ = s.get("settled") or []
        if st_:
            st.dataframe(_local(pd.DataFrame(st_[::-1]), "settled_at"), use_container_width=True, hide_index=True,
                         column_config={"settled_at": TIME_COL})
    with st.expander("Feed / write budget / market status"):
        st.json({"feed": s.get("feed"), "write_usage": s.get("write_usage"), "market_status": s.get("market_status"),
                 "counts": s.get("counts"), "order_errors": s.get("order_errors")})


@st.cache_data(ttl=15, show_spinner=False)
def _pnl_frame(series: str) -> pd.DataFrame:
    return arbs_pnl.load(series)


PNL_WINDOWS = {"6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24), "7d": pd.Timedelta(days=7), "All": None}
def _pnl_chart(series: str):
    """Settled + open PnL, real money only. History is replayed from the
    journal (settled plus what complete bundles lock in); the last point adds
    unfinished baskets at what their extra legs would sell for now, so it
    matches the header. Starts at 0 at the window's start."""
    df = _pnl_frame(series)
    s = read_summary(series)
    if len(df) and s and s.get("ledger") == "real":
        df = pd.concat([df, pd.DataFrame({"time": [pd.Timestamp.now(tz="UTC")], "settled": [df.settled.iloc[-1]],
                                          "total": [df.total.iloc[-1] + ((s.get("pnl") or {}).get("broken") or 0.0)]})],
                       ignore_index=True)
    if df.empty:
        st.caption("No live fills yet.")
        return
    win = st.radio("Window", list(PNL_WINDOWS), index=1, horizontal=True, key=f"arbs_pnl_win_{series}",
                   label_visibility="collapsed")
    since = None if PNL_WINDOWS[win] is None else pd.Timestamp.now(tz="UTC") - PNL_WINDOWS[win]
    before = df[df.time < since] if since is not None else df.iloc[:0]
    d = df[df.time >= since] if since is not None else df
    b0 = float(before.total.iloc[-1]) if len(before) else 0.0
    c = pd.DataFrame({"time": [since if since is not None else d.time.min()] + list(d.time),
                      "usd": [0.0] + [float(v) - b0 for v in d.total]})
    chart = alt.Chart(c).mark_line(interpolate="step-after", strokeWidth=2, color="#1e88e5").encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%m-%d %H:%M")),
        y=alt.Y("usd:Q", title="Settled + open PnL $"),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M:%S"), alt.Tooltip("usd:Q", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#888", strokeWidth=0.7).encode(y="y:Q")
    st.altair_chart((chart + zero).properties(height=260), use_container_width=True)


def render(series: str, stale_fn):
    st.title("Arbitrage · risk-free")
    st.caption("Whole baskets only: take every leg, or quote legs and complete the rest on a fill. "
               f"Series {series}.")

    @st.fragment(run_every="15s")
    def chart():
        if stale_fn(series):
            return
        _pnl_chart(series)

    chart()

    @st.fragment(run_every="2s")
    def body():
        if stale_fn(series):
            return
        s = read_summary(series)
        if s is None:
            prev = latest_summary(series)
            s = prev[1] if prev else None
        if s is None:
            st.caption(f"No {series} run has written yet: `python -m kalshi_strategies.run --series {series}`.")
            _toggle(series, {}, idle=True)
            return
        age = summary_age_s(s)
        idle = age is None or age > STALE_S
        if idle:
            st.warning(f"Process not running (last summary {age or 0:.0f}s ago). Figures are as of then.")
        _toggle(series, s, idle)
        _alerts(series, s)
        _metrics(s)
        _tables(series, s)

    body()
