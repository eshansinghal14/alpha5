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
from kalshi_viz import config_form, perf_panel  # noqa: E402
from kalshi_viz.data import load_day, read_summary, stream_url, summary_age_s  # noqa: E402
from kalshi_viz.live_chart import IFRAME_HEIGHT, build_live_html  # noqa: E402

st.set_page_config(page_title="Kalshi strategies", layout="wide")
# Tighter than Streamlit's default 1rem element gap and 2.25rem metric
# values, so each market's stats + chart take less height.
st.markdown("<style>[data-testid='stVerticalBlock'] { gap: 0.4rem; } "
            "[data-testid='stMetricValue'] { font-size: 1.5rem; } "
            "h4 { padding: 0 0 0.25rem 0; }</style>", unsafe_allow_html=True)

# A summary older than this means the process stopped writing.
STALE_SUMMARY_S = 15.0

BLOCK_TEXT = {
    "stale_signal": "The pricing inputs are older than risk.max_signal_age_s -- a dead feed or a "
                    "reprice that keeps failing. A frozen fair against a live book is not edge.",
    "order_error_breaker": "Consecutive real-order errors -- auth, balance or market state. Fix and restart.",
}


SERIES_KEY = "dash_series"


@st.cache_resource
def _registry():
    return registry()


def _stale(series: str) -> bool:
    """True if a timed fragment is drawing a series that is no longer
    selected. Fragment ids hash the function and page position, not its
    args, so every series' fragments share ids; a 2s rerun in flight when
    the sidebar changed drew KXINX's charts into the KXINXDUD page."""
    if st.session_state.get(SERIES_KEY, series) != series:
        st.rerun(scope="app")
        return True
    return False


@st.cache_data(max_entries=64, show_spinner=False)
def _chart_html(series, market, t0_ms, max_position, quoting):
    """~0.7s of Bokeh per build; shared across browser tabs. The x end is
    t0 + 1s: the chart's own poller moves it to now on its first fetch."""
    return build_live_html(series, market, stream_url(series, market), t0_ms, t0_ms + 1000,
                           max_position, quoting)


def _ms(v):
    return "n/a" if v is None else f"{v * 1000:,.0f} ms"


def _render_toggle(series: str, s: dict):
    active = bool(s.get("live_trading"))
    requested = bool(s.get("arm_requested", active))
    if active:
        r = s.get("real") or {}
        st.error(f"🟢 **REAL TRADING IS LIVE** -- {r.get('n_fills', 0)} real fill(s) from "
                 f"{r.get('n_orders', 0)} order(s).")
    else:
        st.info("🔵 SHADOW -- live data, simulated fills, no real orders.")
    key = f"arm_{series}"
    if key not in st.session_state:
        st.session_state[key] = requested
    st.checkbox("Send REAL orders to Kalshi (real money)", key=key,
                on_change=lambda: request_trading_mode(series, st.session_state[key]),
                help="Arms the running process (no restart). Arming starts a fresh session seeded from "
                     "Kalshi's positions; disarming pulls every resting quote. Positions are never flattened.")
    if st.session_state[key] != requested:
        st.warning("Waiting for the process to pick up the request.")
    elif requested and not active:
        st.warning("Armed, but not sending real orders yet -- outside trading_time (the delay after open "
                   "or the cutoff before close), halted, or the arm failed (retried; see the process log).")
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

    problem = (s.get("extras") or {}).get("feed_problem")
    if problem:
        st.error(f"📡 Signal feed: {problem}")

    c = st.columns(3)
    age, lim = s.get("signal_age_s"), s.get("signal_age_limit_s")
    c[0].metric("Signal age", _ms(age), delta="STALE" if age is not None and lim and age > lim else None,
                delta_color="inverse", help=f"Limit {_ms(lim)}.")
    pnl = s.get("pnl") or {}
    c[1].metric("PnL (fair / touch)", f"${pnl.get('fair', 0):+.2f}", f"touch ${pnl.get('touch', 0):+.2f}",
                delta_color="off")
    c[2].metric("LIP accrued", f"${pnl.get('lip_accrued', 0):.2f}",
                help="Expected LIP reward from the scoring replica (program terms from Kalshi).")

    qb, eb = s.get("quote_block_counts") or {}, s.get("blocked_counts") or {}
    if qb or eb:
        st.caption("Blocked this session -- decisions: "
                   + (", ".join(f"{k} x{v}" for k, v in sorted(qb.items(), key=lambda kv: -kv[1])) or "none")
                   + " | orders: " + (", ".join(f"{k} x{v}" for k, v in eb.items()) or "none"))


def _markets(day) -> list[str]:
    """Every market of the running event, closest-to-0.5 first. Not gated on
    decisions: the touch and our fair chart from the session start. Markets
    the quoter has excluded (max_markets) go last unless traded."""
    q, f = day.quote_df, day.fills_df
    traded = set(f["ticker"].unique()) if not f.empty else set()
    listed = set(day.summary.get("tickers") or []) | traded
    if not q.empty:
        listed |= set(q["ticker"].unique())
    fair = day.fair_df.dropna(subset=["prob"]) if not day.fair_df.empty else day.fair_df
    probs = fair.groupby("ticker")["prob"].last() if not fair.empty else pd.Series(dtype=float)
    excluded = set()
    if not q.empty and "atm_tradeable" in q.columns:
        last = q.groupby("ticker")["atm_tradeable"].last()
        excluded = {t for t, ok in last.items() if ok is False or ok == 0} - traded
        # With max_markets set, chart only the markets the quoter trades (plus
        # any we hold fills in); the rest are dozens of empty SPX buckets.
        if ((day.summary.get("config") or {}).get("quoting") or {}).get("max_markets"):
            listed -= excluded
    # Unpriced markets last: SPX prices only the IB-scoped buckets near the
    # money, and defaulting those to 0.5 sorted dozens of empty charts first.
    return sorted(listed, key=lambda t: (t not in probs.index, t in excluded, abs(probs.get(t, 0.5) - 0.5), t))


def _render_series(series: str, spec):
    st.title(series)
    perf_panel.render(series)

    @st.fragment(run_every="2s")
    def status():
        if _stale(series):
            return
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
    _render_markets(series)


def _market_stats(day, ticker: str) -> dict:
    """Per-market numbers from the summary; derived from the fills for a
    process that predates summary["markets"]."""
    m = (day.summary.get("markets") or {}).get(ticker)
    if m is not None:
        return m
    f = day.fills_df[day.fills_df["ticker"] == ticker] if not day.fills_df.empty else day.fills_df
    lip = day.summary.get("lip") or {}
    return {"pnl": None, "contracts": float(f["size"].sum()) if len(f) else 0.0,
            "position": (day.summary.get("positions") or {}).get(ticker, 0.0),
            "lip": (lip.get("accrued_usd") or {}).get(ticker),
            "lip_qualifying": (lip.get("qualifying_fraction") or {}).get(ticker)}


def _market_label(ticker: str, event_ticker: str | None) -> str:
    """The market's own part of the ticker: BTC for KXCRYPTOLEAD15M-26OCT092115-BTC."""
    if event_ticker and ticker.startswith(event_ticker + "-"):
        return ticker[len(event_ticker) + 1:]
    return ticker.rsplit("-", 1)[-1]


def _stable_order(series: str, day, markets: list[str]) -> list[str]:
    """Keep the first ordering for the whole session: re-sorting as fairs move
    would reorder the chart iframes, and a moved iframe remounts (losing
    pan/zoom). New markets go at the end."""
    k = f"_order_{series}"
    ident = (day.summary.get("event_ticker"), day.summary.get("session_started"))
    prev = st.session_state.get(k)
    order = list(prev[1]) if prev and prev[0] == ident else []
    order += [t for t in markets if t not in order]
    st.session_state[k] = (ident, order)
    return order


@st.fragment(run_every="2s")
def _render_markets(series: str):
    if _stale(series):
        return
    day = load_day(series)
    if day is None:
        return
    current = _markets(day)
    markets = [t for t in _stable_order(series, day, current) if t in current]
    if not markets:
        st.caption("Waiting for the session's first snapshot.")
        return
    s = day.summary
    q = (s.get("config") or {}).get("quoting") or {}
    sess = s.get("session_started") or ""
    quoting = bool(q.get("enable_quoting"))
    t0 = pd.Timestamp(sess) if sess else pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=15)
    for tk in markets:
        m = _market_stats(day, tk)
        c = st.columns([3, 2, 2, 2, 2], vertical_alignment="bottom")
        c[0].markdown(f"#### {_market_label(tk, s.get('event_ticker'))}")
        c[1].metric("PnL", "n/a" if m.get("pnl") is None else f"${m['pnl']:+.2f}")
        c[2].metric("Contracts traded", f"{m.get('contracts') or 0:.4g}")
        c[3].metric("Position", f"{m.get('position') or 0:+.4g}")
        lip, qf = m.get("lip"), m.get("lip_qualifying")
        c[4].metric("LIP", "no program" if lip is None else f"${lip:.2f}",
                    None if qf is None else f"{qf:.0%} snapshots qualify", delta_color="off")
        # Same args -> same html string -> Streamlit keeps the iframe mounted.
        html = _chart_html(series, tk, t0.value // 10**6, q.get("max_position"), quoting)
        components.html(html, height=IFRAME_HEIGHT, scrolling=True)
    with st.expander("Fills"):
        st.dataframe(day.fills_df, use_container_width=True)


def main():
    reg = _registry()
    with st.sidebar:
        st.header("Series")
        live = [t for t in sorted(reg) if read_summary(t) is not None]
        series = st.radio("Series", sorted(reg), label_visibility="collapsed", key=SERIES_KEY,
                          format_func=lambda t: t + ("  ●" if t in live else ""))
        st.header("Config")
        config_form.render(reg[series])
    _render_series(series, reg[series])


main()
