"""ARBS dashboard page: arm switch, guards and alerts, PnL (settled, locked
in on held bundles), bundles held, resting quotes, the opportunity ranking
and the settled history. Reads only data/live/ARBS/<date>/summary.json."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from kalshi_infra.runtime.control import request_trading_mode
from kalshi_viz.data import latest_summary, read_summary, summary_age_s

STALE_S = 15.0


def _usd(v) -> str:
    return "n/a" if v is None else f"${v:+,.2f}"


def _toggle(series: str, s: dict, idle: bool):
    live = bool(s.get("live_trading")) and not idle
    if live:
        st.error("🟢 **REAL TRADING IS LIVE**")
    else:
        st.info("🔵 SHADOW -- live books, simulated fills (separate shadow ledger), no real orders.")
    key = f"arm_{series}"
    if key not in st.session_state:
        st.session_state[key] = bool(s.get("arm_requested"))
    st.checkbox("Send REAL orders to Kalshi (real money)", key=key,
                on_change=lambda: request_trading_mode(series, st.session_state[key]),
                help="Arming sweeps our resting orders, polls fills and refuses if the ledger disagrees with "
                     "Kalshi's positions. Disarming pulls every quote. Positions are never flattened.")
    if not idle and st.session_state[key] and not live:
        st.warning("Armed, not live yet: arming, halted, or the arm was refused (see alerts).")


def _alerts(s: dict):
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
                 + (f"; still needs {c.get('need')}, caps {c.get('caps')}, asks {c.get('asks')}"
                    + (f", **short: {c.get('short')}**" if c.get("short") else "") if c else ""))
    f = s.get("feed") or {}
    if not f.get("up"):
        st.error("📡 Kalshi websocket down (quotes are pulled after runtime.feed_down_pull_s).")
    al = [a for a in (s.get("alerts") or []) if a["level"] in ("error", "warn")]
    if al:
        with st.expander(f"Alerts ({len(al)} recent)", expanded=any(a["level"] == "error" for a in al[-10:])):
            st.dataframe(pd.DataFrame(al[::-1])[["time", "level", "basket_id", "msg", "live"]],
                         use_container_width=True, hide_index=True)


def _metrics(s: dict):
    p = s.get("pnl") or {}
    c = st.columns(5)
    c[0].metric("Settled PnL", _usd(p.get("settled")), f"{len(s.get('settled') or [])} baskets", delta_color="off",
                help="Settled baskets: payout received minus everything paid incl. fees.")
    c[1].metric("Locked-in PnL", _usd(p.get("locked")), delta_color="off",
                help="Complete bundles held: guaranteed payout minus their cost. Paid at settlement.")
    c[2].metric("Capital used", f"${p.get('committed', 0):,.2f}", f"of ${p.get('max_dollars', 0):,.0f}",
                delta_color="off", help="Cost of held baskets plus what resting quotes commit if filled.")
    c[3].metric("Baskets tradeable", f"{s.get('n_tradeable')}/{s.get('n_baskets')}",
                f"{s.get('n_tickers')} markets", delta_color="off")
    n = s.get("counts") or {}
    c[4].metric("Quote fills / takes", f"{n.get('quote_fills', 0)} / {n.get('takes', 0)}",
                f"{n.get('completions', 0)} completions", delta_color="off")


def _tables(s: dict):
    st.markdown("##### Bundles held")
    h = s.get("holdings") or []
    if h:
        df = pd.DataFrame(h)
        df["held"] = df["held"].map(lambda d: ", ".join(f"{t.split('-', 1)[-1]} {q:g}" for t, q in d.items()))
        df["status"] = df["unhedged"].map(lambda u: "INCOMPLETE" if u else "complete")
        df["return"] = (df["locked_pnl"].fillna(0) / df["cost"].where(df["cost"] > 0)).map(
            lambda v: "" if pd.isna(v) else f"{v:.2%}")
        st.dataframe(df[["basket_id", "type", "status", "bundles", "payout", "cost", "locked_pnl", "return", "held"]]
                     .sort_values("status", ascending=False), use_container_width=True, hide_index=True)
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
    with st.expander("Settled baskets"):
        st_ = s.get("settled") or []
        if st_:
            st.dataframe(pd.DataFrame(st_[::-1]), use_container_width=True, hide_index=True)
    with st.expander("Feed / write budget / market status"):
        st.json({"feed": s.get("feed"), "write_usage": s.get("write_usage"), "market_status": s.get("market_status"),
                 "counts": s.get("counts"), "order_errors": s.get("order_errors")})


def render(series: str, stale_fn):
    st.title("Arbitrage · risk-free")
    st.caption("Whole baskets only: take every leg, or quote legs and complete the rest on a fill. "
               f"Series {series}.")

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
        _alerts(s)
        _metrics(s)
        _tables(s)

    body()
