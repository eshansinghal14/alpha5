"""ARBS dashboard page: arm switch, guards and alerts, PnL (settled, locked
in on held bundles), bundles held, resting quotes, the opportunity ranking
and the settled history. The ARBS page shows every ARBS process together
(ARBS on shard 0, ARBS3 on shard 3: one strategy split for CPU), each with
its own arm switch. Reads only data/live/<series>/<date>/summary.json."""
from __future__ import annotations

import json
import os

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_viz import arbs_pnl
from kalshi_viz.arm import arm_control
from kalshi_viz.perf_panel import time_axis
from kalshi_viz.data import latest_summary, read_summary, summary_age_s

STALE_S = 15.0
SHARD_OF = {"ARBS": "shard 0", "ARBS3": "shard 3"}


def parts(series: str) -> list[str]:
    """The processes one page shows."""
    from kalshi_strategies.ARBS import SERIES
    return list(SERIES) if series == "ARBS" else [series]


def _num_sum(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vals) if vals else None


def merge(ss: dict[str, dict]) -> dict:
    """Summaries of several processes as one. Positions' value is Kalshi's
    for the whole account, so it is taken once, not summed."""
    xs = list(ss.values())
    if len(xs) == 1:
        return xs[0]
    pn = [x.get("pnl") or {} for x in xs]
    pnl = {k: _num_sum(p.get(k) for p in pn)
           for k in ("settled", "locked", "broken", "upside", "held_cost", "committed", "cash", "cash_room")}
    pnl["positions_value"] = next((p["positions_value"] for p in pn if p.get("positions_value") is not None), None)
    pnl["max_dollars"] = next((p["max_dollars"] for p in pn if p.get("max_dollars") is not None), None)

    def tagged(key):
        return [{**r, "shard": SHARD_OF.get(n, n)} for n, x in ss.items() for r in (x.get(key) or [])]

    def why(key):
        return "; ".join(f"{SHARD_OF.get(n, n)}: {x[key]}" for n, x in ss.items() if x.get(key)) or None

    counts: dict = {}
    for x in xs:
        for k, v in (x.get("counts") or {}).items():
            if isinstance(v, (int, float)):
                counts[k] = counts.get(k, 0) + v
    feeds = [x.get("feed") or {} for x in xs]
    cfg_err = "; ".join(f"{SHARD_OF.get(n, n)}: {(x.get('config_status') or {}).get('error')}"
                        for n, x in ss.items() if (x.get("config_status") or {}).get("error"))
    return {
        "live_trading": any(x.get("live_trading") for x in xs), "ledger": xs[0].get("ledger"),
        "halt_reason": why("halt_reason"), "blocked_reason": why("blocked_reason"),
        "config_status": {"error": cfg_err or None},
        "audit_diff": {k: v for x in xs for k, v in (x.get("audit_diff") or {}).items()} or None,
        "feed": {"up": all(f.get("up") for f in feeds), "by_process": dict(zip(ss, feeds))},
        "pnl": pnl, "counts": counts,
        "n_tradeable": _num_sum(x.get("n_tradeable") for x in xs),
        "n_baskets": _num_sum(x.get("n_baskets") for x in xs),
        "n_tickers": _num_sum(x.get("n_tickers") for x in xs),
        "holdings": tagged("holdings"), "quotes": tagged("quotes"),
        "alerts": sorted(tagged("alerts"), key=lambda a: a.get("time") or ""),
        "settled": sorted(tagged("settled"), key=lambda a: a.get("settled_at") or ""),
        "ranking": sorted(tagged("ranking"), key=lambda r: -(r.get("quote_usd_h") or 0.0)),
        "missed": tagged("missed"), "hot": [h for x in xs for h in (x.get("hot") or [])],
        "write_usage": {k: v for x in xs for k, v in (x.get("write_usage") or {}).items()},
        "market_status": {n: x.get("market_status") for n, x in ss.items()},
        "order_errors": {n: x.get("order_errors") for n, x in ss.items()},
    }
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
    cuts = {SHARD_OF.get(p, p): _cleared(p) for p in parts(series)}
    al = [a for a in (s.get("alerts") or []) if a["level"] in ("error", "warn")
          and a["time"] > cuts.get(a.get("shard"), _cleared(series))]
    if al:
        with st.expander(f"Alerts ({len(al)} new)", expanded=any(a["level"] == "error" for a in al[-10:])):
            df = _local(pd.DataFrame(al[::-1]), "time")
            st.dataframe(df[[c for c in ("time", "shard", "level", "basket_id", "msg", "live") if c in df]],
                         use_container_width=True, hide_index=True, column_config={"time": TIME_COL})
            if st.button("Clear (handled)", key="arbs_clear_alerts"):
                for p in parts(series):
                    with open(_cleared_path(p), "w", encoding="utf-8") as fh:
                        json.dump({"before": al[-1]["time"]}, fh)
                st.rerun(scope="fragment")


def _metrics(s: dict):
    p = s.get("pnl") or {}
    c = st.columns(5)
    broken = p.get("broken") or 0.0
    n_broken = sum(1 for h in s.get("holdings") or [] if h.get("unhedged"))
    settled = p.get("settled") or 0.0
    unrealized = (p.get("locked") or 0.0) + broken
    c[0].metric("Total PnL", _usd(settled + unrealized),
                help="Settled (payouts and unwound contracts, minus everything paid incl. fees) plus expected "
                     "unrealized.")
    upside = p.get("upside") or 0.0
    note = [f"{_usd(upside)} upside"] if upside >= 0.005 else []
    if n_broken:
        note.append(f"{_usd(broken)} on {n_broken} unfinished")
    c[1].metric("Expected unrealized", _usd(unrealized), " · ".join(note) or None,
                help="What complete bundles held lock in at settlement, plus unfinished baskets' extra legs at "
                     "what they'd sell for now. Upside (not in the totals): what bundles that can pay more than "
                     "their locked payout (a draw, an unlisted winner, a ladder between rungs) are expected to "
                     "add, at mid prices.")
    if p.get("cash_room") is not None:
        c[2].metric("Shard cash", f"${p['cash']:,.2f}", f"${p['cash_room']:,.0f} free to size on", delta_color="off",
                    help="Kalshi's available balance on the traded shards (net of resting orders); quotes are "
                         "sized against it less cash_reserve (trading.max_dollars_auto). Positions on the shard "
                         f"(Kalshi's value): ${p.get('positions_value') or 0:,.0f}.")
    else:
        c[2].metric("Capital used", f"${p.get('committed', 0):,.2f}", f"of ${p.get('max_dollars', 0):,.0f}",
                    delta_color="off", help="Cost of held baskets (resting quotes aren't counted; each is sized to fit what's left).")
    c[3].metric("Baskets tradeable", f"{s.get('n_tradeable')}/{s.get('n_baskets')}",
                f"{s.get('n_tickers')} markets", delta_color="off")
    n = s.get("counts") or {}
    c[4].metric("Quote fills / takes", f"{n.get('quote_fills', 0)} / {n.get('takes', 0)}",
                f"{n.get('completions', 0)} completions", delta_color="off")


@st.cache_data(ttl=5, show_spinner=False)
def _bundle_log(series: str) -> pd.DataFrame:
    logs = [x for x in (arbs_pnl.bundle_log(p) for p in parts(series)) if len(x)]
    if not logs:
        return pd.DataFrame()
    return pd.concat(logs, ignore_index=True).sort_values("time", ascending=False, kind="stable")


def _tables(series: str, s: dict):
    st.markdown("##### Bundles bought (newest first)")
    log = _bundle_log(series)
    if len(log):
        small = st.column_config.Column(width="small")
        st.dataframe(_local(log.head(200).copy(), "time"), use_container_width=True, hide_index=True,
                     column_order=["time", "tte", "series", "basket_id", "how", "bundles", "cost", "pnl", "return",
                                   "fills"],
                     column_config={"time": st.column_config.DatetimeColumn(format="MM-DD HH:mm:ss", width="small"),
                                    "tte": small, "series": small, "basket_id": st.column_config.Column(width="medium"),
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
        cols = ["shard", "basket_id", "ticker", "side", "price", "max_price", "size", "filled", "edge", "return",
                "age_s", "pending"]
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
    return combine([arbs_pnl.load(p) for p in parts(series)])


def combine(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Several processes' PnL histories as one: settled and total summed at
    every time any of them changed; capital is the account's, from the
    first frame that has it."""
    frames = [f.drop_duplicates("time", keep="last").set_index("time") for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=["time", "settled", "total", "capital"])
    if len(frames) == 1:
        return frames[0].reset_index()
    times = sorted(set().union(*(set(f.index) for f in frames)))
    out = pd.DataFrame({"time": times, "settled": 0.0, "total": 0.0})
    for f in frames:
        for c in ("settled", "total"):
            out[c] += f[c].reindex(times, method="ffill").fillna(0.0).values
    cap = next((f for f in frames if f.capital.notna().any()), frames[0])
    out["capital"] = cap.capital.reindex(times, method="ffill").values
    return out


PNL_WINDOWS = {"6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24), "7d": pd.Timedelta(days=7), "All": None}
def _pnl_chart(series: str):
    """Settled + open PnL, real money only. History is replayed from the
    journal (settled plus what complete bundles lock in); the last point adds
    unfinished baskets at what their extra legs would sell for now, so it
    matches the header. Starts at 0 at the window's start. Capital used
    (right axis): Kalshi's value of our positions on the traded shards,
    logged each minute from 11-11; a level, not rebased to the window."""
    df = _pnl_frame(series)
    found = {p: x for p in parts(series) if (x := read_summary(p)) is not None}
    s = merge(found) if found else None
    if len(df) and s and s.get("ledger") == "real":
        df = pd.concat([df, pd.DataFrame({"time": [pd.Timestamp.now(tz="UTC")], "settled": [df.settled.iloc[-1]],
                                          "total": [df.total.iloc[-1] + ((s.get("pnl") or {}).get("broken") or 0.0)],
                                          "capital": [(s.get("pnl") or {}).get("positions_value")
                                                      or df.capital.iloc[-1]]})],
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
    cap0 = float(before.capital.iloc[-1]) if len(before) else 0.0
    c = pd.DataFrame({"time": [since if since is not None else d.time.min()] + list(d.time),
                      "usd": [0.0] + [float(v) - b0 for v in d.total],
                      "capital": [cap0] + list(d.capital.astype(float))})
    x = alt.X("time:T", title=None, axis=time_axis(PNL_WINDOWS[win]))
    chart = alt.Chart(c).mark_line(interpolate="step-after", strokeWidth=2, color="#1e88e5").encode(
        x=x, y=alt.Y("usd:Q", title="Settled + open PnL $", axis=alt.Axis(titleColor="#1e88e5")),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M:%S"), alt.Tooltip("usd:Q", format="+.2f"),
                 alt.Tooltip("capital:Q", title="capital used", format=",.0f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#888", strokeWidth=0.7).encode(y="y:Q")
    capital = alt.Chart(c).mark_line(interpolate="step-after", strokeWidth=1.5, color="#fb8c00", opacity=0.8).encode(
        x=x, y=alt.Y("capital:Q", title="Capital used $ (shard positions)",
                     axis=alt.Axis(orient="right", titleColor="#fb8c00", format=",.0f")),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M:%S"),
                 alt.Tooltip("capital:Q", title="capital used", format=",.0f")])
    layers = alt.layer(chart + zero, capital).resolve_scale(y="independent")
    st.altair_chart(layers.properties(height=260), use_container_width=True)


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
        ss, idles = {}, {}
        for p in parts(series):
            x = read_summary(p)
            if x is None:
                prev = latest_summary(p)
                x = prev[1] if prev else None
            if x is not None:
                ss[p] = x
                age = summary_age_s(x)
                idles[p] = age is None or age > STALE_S
        if not ss:
            st.caption("No ARBS run has written yet: `python -m kalshi_strategies.run --series ARBS`.")
            _toggle(series, {}, idle=True)
            return
        cols = st.columns(len(parts(series)))
        for col, p in zip(cols, parts(series)):
            with col:
                if len(cols) > 1:
                    st.markdown(f"**{SHARD_OF.get(p, p)}** ({p})")
                if p not in ss:
                    st.caption("Not running yet.")
                    continue
                if idles[p]:
                    st.warning(f"{p} not running (last summary {summary_age_s(ss[p]) or 0:.0f}s ago).")
                _toggle(p, ss[p], idles[p])
        s = merge(ss)
        _alerts(series, s)
        _metrics(s)
        _tables(series, s)

    body()
    _shadow_check(series)


def _shadow_check(series: str, days: int = 7):
    """The daily shadow check (kalshi_strategies/ARBS/shadow_check.py)."""
    d = os.path.join(LIVE_ROOT, series, "shadow_check")
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))[-days:] if os.path.isdir(d) else []
    with st.expander("Shadow check: replay vs real fills, edge policy, print markouts", expanded=False):
        if not files:
            st.caption("No daily shadow check yet (alpha5-arbs-shadow-check.timer, 00:40 UTC).")
            return
        docs = []
        for f in files:
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    docs.append(json.load(fh))
            except (OSError, ValueError):
                continue
        def tot(doc, k):
            return sum(r.get(k) or 0.0 for r in doc["calibration"])
        st.dataframe(pd.DataFrame([{
            "day": x["day"], "quote arbs": x["coverage"]["arbs"],
            "with print %": round(100 * x["coverage"]["matched"] / max(x["coverage"]["arbs"], 1)),
            "real c/lot": round(100 * tot(x, "f_real_usd") / tot(x, "f_lots"), 2) if tot(x, "f_lots") else None,
            "replay c/lot": round(100 * tot(x, "f_shadow_usd") / tot(x, "f_shadow_lots"), 2)
            if tot(x, "f_shadow_lots") else None} for x in reversed(docs)]), hide_index=True)
        x = docs[-1]
        st.caption(f"{x['day']}. Replay of each quote-started arb at its real edge and size vs what it made "
                   "(on the arbs the replay fills):")
        if x["calibration"]:
            st.dataframe(pd.DataFrame(x["calibration"])[["group", "arbs", "matched", "filled", "lots", "real_usd",
                                                        "real_c", "shadow_c"]], hide_index=True)
        else:
            st.caption("No finished quote-started arbs that day.")
        st.caption(f"Every print our quote met, $ for a {x['policy_size']:g}-lot quote at each edge, and at the "
                   "shadow's choice (live events it had seen):")
        st.dataframe(pd.DataFrame([{"group": p["group"], "live": p["live"], "prints": p["prints"],
                                    **{f"{100 * e:g}c": v for e, v in zip(x["edges"], p["edges"])},
                                    "shadow": round(p["shadow_usd"], 2), "shadow prints": p["shadow_prints"]}
                                   for p in x["policy"]]), hide_index=True)
        ru = [y for y in docs if (y.get("rules") or {}).get("narrowest") is not None]
        if ru:
            st.caption("Exit-cost rules vs fixed edges, $ for a 5-lot quote on each print our quote met, the model "
                       "fitted on earlier days only. Narrowest: least edge whose predicted net clears 1c. Best EV: "
                       "edge with the best fill odds x predicted net, no quote when none is positive.")
            tab = []
            for y in ru:
                rr, ed = y["rules"], y["edges"]
                k = int(max(range(len(ed)), key=lambda i: rr["fixed"][i]))
                tab.append({"day": y["day"], "trained days": len(rr["trained_on"]), "filled prints": rr["prints"],
                            "fixed 5c": rr["fixed"][ed.index(0.05)], "fixed 10c": rr["fixed"][ed.index(0.1)],
                            "best fixed (hindsight)": f"{rr['fixed'][k]:.2f} @ {100 * ed[k]:g}c",
                            "narrowest": rr["narrowest"], "best EV": rr["best_ev"],
                            "best EV quoted %": round(100 * rr["best_ev_quoted"])})
            t = pd.DataFrame(tab)
            tot = {c: t[c].sum() for c in ("filled prints", "fixed 5c", "fixed 10c", "narrowest", "best EV")}
            st.dataframe(pd.concat([t, pd.DataFrame([{"day": "total", **tot}])], ignore_index=True), hide_index=True)
        st.caption("Every recorded print (ours or not): edge at the print and how far the other legs' cost "
                   "moved after it, cents per bundle. A filled maker quote pays the move.")
        st.dataframe(pd.DataFrame(x["markout"][:40]), hide_index=True)
