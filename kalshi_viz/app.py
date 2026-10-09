"""
Live dashboard for every kalshi_strategies series.

    streamlit run kalshi_viz/app.py          (from the repo root)

Reads only what the runtime writes; never launches a session. Per series:
the real-money switch, guard state, PnL/LIP, a config editor generated from
the series' config dataclass, and a self-updating chart per market.
"""
from __future__ import annotations

import os
import sys

# `streamlit run` puts this file's directory on sys.path, not the repo root.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import streamlit.components.v1 as components  # noqa: E402

from kalshi_infra.runtime.control import request_trading_mode  # noqa: E402
from kalshi_strategies.run import registry  # noqa: E402
from kalshi_viz import config_form  # noqa: E402
from kalshi_viz.data import load_day, read_summary, stream_url, summary_age_s  # noqa: E402
from kalshi_viz.live_chart import POLL_MS, build_live_html  # noqa: E402

st.set_page_config(page_title="Kalshi strategies", layout="wide")

# A summary older than this means the process stopped writing.
STALE_SUMMARY_S = 15.0
# Market dropdown: hide markets evaluated less than this share of the busiest one's decisions.
ACTIVE_MIN_FRACTION = 0.05

BLOCK_TEXT = {
    "stale_signal": "The pricing inputs are older than risk.max_signal_age_s -- a dead feed or a "
                    "reprice that keeps failing. A frozen fair against a live book is not edge.",
    "order_error_breaker": "Consecutive real-order errors -- auth, balance or market state. Fix and restart.",
}


@st.cache_resource
def _registry():
    return registry()


def _ms(v):
    return "n/a" if v is None else f"{v * 1000:,.0f} ms"


def _render_toggle(series: str, s: dict):
    active = bool(s.get("live_trading"))
    if active:
        r = s.get("real") or {}
        st.error(f"🟢 **REAL TRADING IS LIVE** -- {r.get('n_fills', 0)} real fill(s) from "
                 f"{r.get('n_orders', 0)} order(s).")
    else:
        st.info("🔵 SHADOW -- live data, simulated fills, no real orders.")
    key = f"arm_{series}"
    if key not in st.session_state:
        st.session_state[key] = active
    st.checkbox("Send REAL orders to Kalshi (real money)", key=key,
                on_change=lambda: request_trading_mode(series, st.session_state[key]),
                help="Arms the running process (no restart). Arming starts a fresh session seeded from "
                     "Kalshi's positions; disarming pulls every resting quote. Positions are never flattened.")
    if st.session_state[key] != active:
        st.warning("Waiting for the process to confirm -- it refuses to arm if it can't read Kalshi "
                   "positions, or inside close.real_trading_cutoff_s.")
    if (s.get("real") or {}).get("n_errors"):
        st.warning(f"{s['real']['n_errors']} real order error(s) -- see the process log.")


def _render_guards(s: dict):
    blocked = s.get("blocked_reason")
    if blocked == "halted":
        st.warning(f"⛔ **Halted -- {s.get('halt_reason') or 'reason not recorded'}.** Restart to resume.")
    elif blocked:
        st.warning(f"⛔ **Not trading -- {blocked}.** {BLOCK_TEXT.get(blocked, '')}")
    if s.get("over_limit"):
        st.info("📌 Past the position cap (can only reduce): "
                + ", ".join(f"{t.split('-')[-1]} {q:+g}" for t, q in s["over_limit"].items()))
    cs = s.get("config_status") or {}
    if cs.get("error"):
        st.error(f"config.json change REJECTED by the running process: {cs['error']}")
    if cs.get("pending"):
        st.caption("Pending next event: " + ", ".join(f"{k}={v}" for k, v in cs["pending"].items()))

    c = st.columns(4)
    age, lim = s.get("signal_age_s"), s.get("signal_age_limit_s")
    c[0].metric("Signal age", _ms(age), delta="STALE" if age is not None and lim and age > lim else None,
                delta_color="inverse", help=f"Limit {_ms(lim)}.")
    c[1].metric("Feed age", _ms(s.get("feed_age_s")))
    pnl = s.get("pnl") or {}
    c[2].metric("PnL (fair / touch)", f"${pnl.get('fair', 0):+.2f}", f"touch ${pnl.get('touch', 0):+.2f}",
                delta_color="off")
    c[3].metric("LIP accrued", f"${pnl.get('lip_accrued', 0):.2f}",
                help="Expected LIP reward from the scoring replica (program terms from Kalshi).")
    fees, wb = s.get("fees") or {}, s.get("write_budget_orders_per_s")
    if fees:
        st.caption(f"Kalshi fees: {fees.get('type')} x{fees.get('multiplier')} "
                   f"(taker {fees.get('taker_rate')}, maker {fees.get('maker_rate')} x p(1-p))"
                   + (f" · write budget ~{wb:g} orders/s" if wb else ""))

    qb, eb = s.get("quote_block_counts") or {}, s.get("blocked_counts") or {}
    if qb or eb:
        st.caption("Blocked this session -- decisions: "
                   + (", ".join(f"{k} x{v}" for k, v in sorted(qb.items(), key=lambda kv: -kv[1])) or "none")
                   + " | orders: " + (", ".join(f"{k} x{v}" for k, v in eb.items()) or "none"))


def _markets(day) -> list[str]:
    q = day.quote_df
    traded = set(day.fills_df["ticker"].unique()) if not day.fills_df.empty else set()
    if q.empty:
        return sorted(traded)
    sess = day.summary.get("session_started")
    if sess and "time" in q.columns:
        q = q[pd.to_datetime(q["time"], utc=True) >= pd.Timestamp(sess)]
    counts = q.groupby("ticker").size()
    active = set(counts[counts >= ACTIVE_MIN_FRACTION * counts.max()].index) if len(counts) else set()
    if "atm_tradeable" in q.columns:
        active &= set(q.loc[q["atm_tradeable"].fillna(True).astype(bool), "ticker"])
    keep = active | traded
    probs = q.dropna(subset=["prob"]).groupby("ticker")["prob"].mean()
    return sorted(keep, key=lambda t: (abs(probs.get(t, 0.5) - 0.5), t))


def _render_series(series: str, spec):
    @st.fragment(run_every="2s")
    def status():
        s = read_summary(series)
        if s is None:
            st.info(f"No {series} session today. Start one with "
                    f"`python -m kalshi_strategies.run --series {series}`.")
            return
        age = summary_age_s(s)
        if age is not None and age > STALE_SUMMARY_S:
            st.warning(f"Process stopped writing {age:.0f}s ago (last event {s.get('event_ticker')}).")
        st.subheader(f"{series} · {s.get('event_ticker')}")
        _render_toggle(series, s)
        _render_guards(s)
        # Rebuild the chart when the event rolls or a session restarts.
        ident = (s.get("event_ticker"), s.get("session_started"))
        k = f"_ident_{series}"
        if st.session_state.get(k) not in (None, ident):
            st.session_state[k] = ident
            st.rerun(scope="app")
        st.session_state[k] = ident

    status()

    day = load_day(series)
    if day is None:
        return
    markets = _markets(day)
    if not markets:
        st.caption("No decisions recorded yet this session.")
        return
    size = day.fills_df.groupby("ticker")["size"].sum().to_dict() if not day.fills_df.empty else {}
    market = st.selectbox("Market", markets, key=f"mkt_{series}",
                          format_func=lambda t: f"{t}  ({size.get(t, 0.0):.4g} contracts)")
    s = day.summary
    q = (s.get("config") or {}).get("quoting") or {}
    sess = s.get("session_started") or ""
    key = f"_html_{series}_{market}_{sess}_{bool(q.get('enable_quoting'))}"
    for k in [k for k in st.session_state if k.startswith(f"_html_{series}_") and k != key]:
        del st.session_state[k]
    if key not in st.session_state:
        t0 = pd.Timestamp(sess) if sess else pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=15)
        st.session_state[key] = build_live_html(
            series, market, stream_url(series, market), t0.value // 10**6,
            pd.Timestamp.now(tz="UTC").value // 10**6, q.get("max_position"), bool(q.get("enable_quoting")))
    st.caption(f"Chart updates itself every {POLL_MS / 1000:.1f}s -- pan/zoom freely.")
    components.html(st.session_state[key], height=900, scrolling=True)
    with st.expander("Fills (this market)"):
        st.dataframe(day.fills_df[day.fills_df["ticker"] == market] if not day.fills_df.empty
                     else day.fills_df, use_container_width=True)


def main():
    reg = _registry()
    with st.sidebar:
        st.header("Series")
        live = [t for t in sorted(reg) if read_summary(t) is not None]
        series = st.radio("Series", sorted(reg), label_visibility="collapsed",
                          format_func=lambda t: t + ("  ●" if t in live else ""))
        st.header("Config")
        config_form.render(reg[series])
    _render_series(series, reg[series])


main()
