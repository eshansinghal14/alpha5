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
from kalshi_infra.trading.lip import payable_usd  # noqa: E402
from kalshi_strategies.run import folders, registry  # noqa: E402
from kalshi_viz import config_form, perf_panel  # noqa: E402
from kalshi_viz.data import (has_lip, latest_summary, load_day, read_summary, read_trading_mode,  # noqa: E402
                             stream_url, summary_age_s)
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


OTHER_GROUP = "Other"


@st.cache_resource
def _groups() -> dict[str, list[str]]:
    """Sidebar heading -> its series, one page each. A folder's
    DASHBOARD_GROUP names its heading (folders sharing one are listed by
    DASHBOARD_RANK, lower first); a folder without one goes under Other."""
    out: dict[str, list[str]] = {}
    for mod in sorted(folders().values(), key=lambda m: getattr(m, "DASHBOARD_RANK", 0)):
        g = getattr(mod, "DASHBOARD_GROUP", None) or OTHER_GROUP
        out.setdefault(g, []).extend(mod.SERIES)
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == OTHER_GROUP, kv[0])))


def _stale(page: str) -> bool:
    """True if a timed fragment is drawing a page that is no longer
    selected. Fragment ids hash the function and page position, not its
    args, so every page's fragments share ids; a 2s rerun in flight when
    the sidebar changed drew KXINX's charts into the KXINXDUD page."""
    if st.session_state.get(SERIES_KEY, page) != page:
        st.rerun(scope="app")
        return True
    return False


@st.cache_data(max_entries=64, show_spinner=False)
def _chart_html(series, market, t0_ms, max_position, quoting, lip):
    """~0.2s of Bokeh per build; shared across browser tabs. The x end is
    t0 + 1s: the chart's own poller moves it to now on its first fetch."""
    return build_live_html(series, market, stream_url(series, market), t0_ms, t0_ms + 1000,
                           max_position, quoting, lip)


def _ms(v):
    return "n/a" if v is None else f"{v * 1000:,.0f} ms"


def _idle_request(series: str, s: dict | None) -> bool:
    """Arm state to show while no event is running. A click made since the
    last summary was written is only in trading_mode.json; anything older is
    what the process last reported."""
    last = pd.Timestamp(s["last_update"]) if s and s.get("last_update") else None
    tm = read_trading_mode(series)
    if tm is not None and (last is None or tm[1] > last):
        return tm[0]
    return bool(s.get("arm_requested")) if s else False


def _render_toggle(series: str, s: dict, idle: bool = False):
    active = bool(s.get("live_trading")) and not idle
    requested = _idle_request(series, s) if idle else bool(s.get("arm_requested", active))
    if idle:
        st.info("⏸ No event running. "
                + ("**REAL TRADING is requested** -- the next event arms when it starts trading."
                   if requested else "The next event starts in SHADOW unless you arm it here.")
                + " A process restarted after the click starts disarmed.")
    elif active:
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
    if idle:
        pass
    elif st.session_state[key] != requested:
        st.warning("Waiting for the process to pick up the request.")
    elif requested and not active:
        st.warning("Armed, but not sending real orders yet -- outside trading_time (the delay after open "
                   "or the cutoff before close), halted, or the arm failed (retried; see the process log).")
    if (s.get("real") or {}).get("n_errors"):
        st.warning(f"{s['real']['n_errors']} real order error(s) -- see the process log.")


def _render_vol_sources(s: dict):
    """Coin Race on implied vol: which coins' Kalshi 15M markets aren't giving
    a vol right now, so the race prices them on a held estimate or the HAR
    model. Shown only for summaries that carry vol_src_by_coin."""
    e = s.get("extras") or {}
    src = e.get("vol_src_by_coin")
    if not src or e.get("vol_source") != "implied":
        return
    iv = e.get("implied_vol") or {}
    vol = e.get("vol_bp") or {}

    def why(c):
        v = iv.get(c) or {}
        mid = v.get("mid")
        n = v.get("n_eff")
        bits = []
        if mid is not None:
            bits.append(f"15M mid {mid:.3f}")
        if n is not None:
            bits.append(f"{n:g} readings")
        return ", ".join(bits)

    held = [c for c, x in src.items() if x == "held"]
    realized = [c for c, x in src.items() if x == "realized"]
    if realized:
        st.warning("📉 **Realized vol (no Kalshi implied):** "
                   + "; ".join(f"{c} {vol.get(c, 0):.2f}bp ({why(c)})" for c in realized)
                   + ". Their 15M market is near 0.5, pinned, or just opened.")
    if held:
        st.info("⏳ **Held implied vol (Kalshi readings stopped):** "
                + "; ".join(f"{c} {vol.get(c, 0):.2f}bp, {((iv.get(c) or {}).get('held_s') or 0):.0f}s old ({why(c)})"
                            for c in held)
                + ". Falls back to realized after the hold.")
    live = [c for c, x in src.items() if x == "implied"]
    if live and (held or realized):
        st.caption("Implied from Kalshi: " + ", ".join(f"{c} {vol.get(c, 0):.2f}bp" for c in live))


def _render_guards(s: dict):
    blocked = s.get("blocked_reason")
    if s.get("real_lock"):
        st.error(f"🔒 **Real trading off until restart -- {s['real_lock']}.** Shadow continues.")
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
    _render_vol_sources(s)

    lip = has_lip(s)
    c = st.columns(3 if lip else 2)
    age, lim = s.get("signal_age_s"), s.get("signal_age_limit_s")
    c[0].metric("Signal age", _ms(age), delta="STALE" if age is not None and lim and age > lim else None,
                delta_color="inverse", help=f"Limit {_ms(lim)}.")
    pnl = s.get("pnl") or {}
    c[1].metric("PnL (fair / touch)", f"${pnl.get('fair', 0):+.2f}", f"touch ${pnl.get('touch', 0):+.2f}",
                delta_color="off")
    if lip:
        acc = ((s.get("lip") or {}).get("accrued_usd") or {}).values()
        c[2].metric("LIP payable", f"${sum(payable_usd(v) for v in acc):.2f}",
                    f"accrued ${pnl.get('lip_accrued', 0):.2f}", delta_color="off",
                    help="Estimated LIP reward from the scoring replica this event, counting only markets "
                         "at or over Kalshi's $1.00 minimum payout (under it pays nothing).")

    qb, eb = s.get("quote_block_counts") or {}, s.get("blocked_counts") or {}
    if qb or eb:
        st.caption("Blocked this session -- decisions: "
                   + (", ".join(f"{k} x{v}" for k, v in sorted(qb.items(), key=lambda kv: -kv[1])) or "none")
                   + " | orders: " + (", ".join(f"{k} x{v}" for k, v in eb.items()) or "none"))


def _markets(day, show_all: bool = False) -> list[str]:
    """Markets of the running event to chart, closest-to-0.5 first. With
    max_markets set (and not show_all), only the ones the quoter currently
    trades plus any we have traded or hold: the S&P events list 30-60
    markets and every chart is its own iframe."""
    q, f = day.quote_df, day.fills_df
    traded = set(f["ticker"].unique()) if not f.empty else set()
    traded |= {t for t, v in (day.summary.get("positions") or {}).items() if abs(v) > 1e-9}
    listed = set(day.summary.get("tickers") or []) | traded
    if not q.empty:
        listed |= set(q["ticker"].unique())
    fair = day.fair_df.dropna(subset=["prob"]) if not day.fair_df.empty else day.fair_df
    probs = fair.groupby("ticker")["prob"].last() if not fair.empty else pd.Series(dtype=float)
    excluded = set()
    if not q.empty and "atm_tradeable" in q.columns:
        last = q.groupby("ticker")["atm_tradeable"].last()
        excluded = {t for t, ok in last.items() if ok is False or ok == 0} - traded
        if ((day.summary.get("config") or {}).get("quoting") or {}).get("max_markets") and not show_all:
            # Unquoted markets never appear in quote_df, so "not excluded"
            # kept all of them: 53 charts on KXINXU (2026-10-14).
            active = {t for t, ok in last.items() if ok is True or ok == 1}
            listed = (active | traded) & listed
    # Unpriced markets last: SPX prices only the IB-scoped buckets near the
    # money, and defaulting those to 0.5 sorted dozens of empty charts first.
    return sorted(listed, key=lambda t: (t not in probs.index, t in excluded, abs(probs.get(t, 0.5) - 0.5), t))


def _last_summary(series: str) -> dict | None:
    return read_summary(series) or ((latest_summary(series) or (None, None))[1])


@st.cache_resource
def _custom_pages() -> dict:
    """series -> render(series) for folders that draw their own page
    (DASHBOARD_PAGE = "module:function")."""
    import importlib
    out = {}
    for mod in folders().values():
        spec = getattr(mod, "DASHBOARD_PAGE", None)
        if spec:
            m, fn = spec.split(":")
            for s in mod.SERIES:
                out[s] = getattr(importlib.import_module(m), fn)
    return out


def _render_page(series: str):
    custom = _custom_pages().get(series)
    if custom is not None:
        custom(series, _stale)
        return
    st.title(series)
    perf_panel.render(series, lip=has_lip(_last_summary(series)))
    _render_series(series, series)


def _render_series(series: str, page: str):
    @st.fragment(run_every="2s")
    def status():
        if _stale(page):
            return
        s = read_summary(series)
        if s is None:
            prev = latest_summary(series)
            s = prev[1] if prev else None
        age = summary_age_s(s) if s else None
        idle = s is None or (age is not None and age > STALE_SUMMARY_S)
        if s is None:
            st.subheader(series)
            st.caption(f"No {series} session has written yet. Start the process with "
                       f"`python -m kalshi_strategies.run --series {series}`.")
            _render_toggle(series, {}, idle=True)
            return
        st.subheader(f"{series} · {s.get('event_ticker')}" + ("  (last event)" if idle else ""))
        if idle:
            st.caption(f"Last summary {pd.Timestamp(s['last_update']).strftime('%Y-%m-%d %H:%M:%S')} UTC "
                       f"({age / 3600:.1f}h ago). Between events this is normal; mid-event it means "
                       "the process stopped.")
        _render_toggle(series, s, idle=idle)
        _render_guards(s)
        # Rebuild the chart when the event rolls or a session restarts.
        ident = (s.get("event_ticker"), s.get("session_started"))
        k = f"_ident_{series}"
        if st.session_state.get(k) not in (None, ident):
            st.session_state[k] = ident
            st.rerun(scope="app")
        st.session_state[k] = ident

    status()
    _render_markets(series, page)


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
def _render_markets(series: str, page: str):
    if _stale(page):
        return
    day = load_day(series)
    if day is None:
        return
    show_all = st.checkbox("Show every market (slow on S&P: one chart each)", key=f"all_mkts_{series}")
    current = _markets(day, show_all)
    markets = [t for t in _stable_order(series, day, current) if t in current]
    if not markets:
        st.caption("Waiting for the session's first snapshot.")
        return
    s = day.summary
    q = (s.get("config") or {}).get("quoting") or {}
    sess = s.get("session_started") or ""
    quoting = bool(q.get("enable_quoting"))
    lip_on = has_lip(s)
    t0 = pd.Timestamp(sess) if sess else pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=15)
    for tk in markets:
        m = _market_stats(day, tk)
        c = st.columns([3, 2, 2, 2, 2] if lip_on else [3, 2, 2, 2], vertical_alignment="bottom")
        label = _market_label(tk, s.get("event_ticker"))
        lq = m.get("lip_quotes") or []
        if lq:
            # e.g. "🏳 LIP yes 200@0.12" -- the market we're farming LIP on right now.
            flag = ", ".join(f"{side} {size:g}@{px:.2f}" for side, px, size in lq)
            c[0].markdown(f"#### {label} :orange-background[🏳 LIP {flag}]")
        else:
            c[0].markdown(f"#### {label}")
        c[1].metric("PnL", "n/a" if m.get("pnl") is None else f"${m['pnl']:+.2f}")
        c[2].metric("Contracts traded", f"{m.get('contracts') or 0:.4g}")
        c[3].metric("Position", f"{m.get('position') or 0:+.4g}")
        if lip_on:
            lip, qf = m.get("lip"), m.get("lip_qualifying")
            note = [] if lip is None or payable_usd(lip) > 0 else ["under $1: pays $0"]
            note += [] if qf is None else [f"{qf:.0%} snapshots qualify"]
            c[4].metric("LIP", "no program" if lip is None else f"${lip:.2f}",
                        " · ".join(note) or None, delta_color="off")
        # Same args -> same html string -> Streamlit keeps the iframe mounted.
        html = _chart_html(series, tk, t0.value // 10**6, q.get("max_position"), quoting, lip_on)
        components.html(html, height=IFRAME_HEIGHT, scrolling=True)
    with st.expander(f"{series} fills"):
        st.dataframe(day.fills_df, use_container_width=True)


def _market_names(series: str) -> list[str]:
    """The running event's market names (BTC, ETH, ...), in the event's own
    order, for the per-market config checkboxes; [] with no live summary."""
    got = latest_summary(series)
    s = got[1] if got else {}
    tickers = s.get("tickers") or list((s.get("markets") or {}))
    return list(dict.fromkeys(_market_label(t, s.get("event_ticker")) for t in tickers))


def _pick(series: str):
    st.session_state[SERIES_KEY] = series


def main():
    reg, groups = _registry(), _groups()
    every = [s for ms in groups.values() for s in ms]
    # A session from before (or a series since removed) can hold a page that's gone.
    if st.session_state.get(SERIES_KEY) not in every:
        st.session_state[SERIES_KEY] = every[0]
    series = st.session_state[SERIES_KEY]
    live = {s for s in reg if read_summary(s) is not None}
    with st.sidebar:
        # One button per series under its heading: one radio can't carry headings,
        # and one radio per group would leave a selection showing in every group.
        for g, ms in groups.items():
            st.subheader(g)
            for s in ms:
                st.button(s + ("  ●" if s in live else ""), key=f"nav_{s}", on_click=_pick, args=(s,),
                          type="primary" if s == series else "secondary", use_container_width=True)
        st.header("Config")
        config_form.render(reg[series], _market_names(series))
    _render_page(series)


main()
