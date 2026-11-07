"""LIP farm: the markets LIPFARM quotes, the LIP we expect from them, and any
fills, with the shared arm button and the config form in the sidebar. Reads
data/live/LIPFARM/status.json (farm.report)."""
from __future__ import annotations

import json
import os
import time
import types

import altair as alt
import pandas as pd
import streamlit as st

from kalshi_strategies.LIPFARM.farm import CONFIG_PATH, HISTORY_FILE, NAME, ROOT, LipFarmConfig
from kalshi_viz import config_form
from kalshi_viz.arm import arm_control
from kalshi_viz.perf_panel import time_axis

STALE_S = 120.0
# config_form takes a Series; LIPFARM isn't one, but has the fields it reads.
_FORM = types.SimpleNamespace(ticker=NAME, config_cls=LipFarmConfig, config_path=CONFIG_PATH, config_key=None)


def _compact_bids(bids: str) -> str:
    """'yes@0.981x1000 + yes@0.982x1.1' -> 'Y 98.1¢×1000 ⚑' (the 1.1 is the sentinel)."""
    sides: dict[str, list] = {}
    for part in (bids or "").split(" + "):
        try:
            side, rest = part.split("@")
            px, size = rest.split("x")
            sides.setdefault(side, []).append((float(px), float(size)))
        except ValueError:
            continue
    out = []
    for side, orders in sides.items():
        main = [o for o in orders if abs(o[1] - 1.1) > 1e-9]
        sent = len(main) < len(orders)
        for px, size in main or orders:
            out.append(f"{side[0].upper()} {px * 100:g}¢×{size:g}" + (" ⚑" if sent and main else ""))
    return " · ".join(out)


# The ticker is the URL's fragment (farm.kalshi_url); links open in a new tab.
_LINK = st.column_config.LinkColumn(width="medium", display_text=r"#(.+)$")


def _by_market(p: pd.DataFrame) -> pd.DataFrame:
    """One row per market from status.json's per-side positions, which split each
    yes+no pair's $1 half to each side (so a side alone reads as a big win or loss)."""
    rows = []
    for t, g in p.groupby("ticker", sort=False):
        lots = dict(zip(g["side"], g["lots"]))
        marks = dict(zip(g["side"], g["mark"]))
        paired = min(lots.get("yes", 0.0), lots.get("no", 0.0))
        open_ = [f"{s} {lots[s] - paired:g} @ {marks[s] * 100:g}¢" for s in ("yes", "no")
                 if lots.get(s, 0.0) - paired >= 0.01]
        cost = float((g["cost"] + g["fees"]).sum())
        value = float(g["value"].sum())
        first = pd.to_datetime(g["first"], utc=True).min() if "first" in g else pd.NaT
        last = pd.to_datetime(g["last"], utc=True).max() if "last" in g else pd.NaT
        rows.append({"market": g["url"].iloc[0] if "url" in g else t,
                     "last fill (ET)": _et(last), "first fill (ET)": _et(first), "_last": last,
                     "paired": paired, "open": ", ".join(open_) or "-",
                     "cost $": cost, "value $": value, "P&L $": value - cost})
    d = pd.DataFrame(rows).sort_values("_last", ascending=False, na_position="last")
    return d.drop(columns="_last")


def _et(ts) -> str:
    return "" if pd.isna(ts) else ts.tz_convert("US/Eastern").strftime("%m-%d %H:%M:%S")


def _status() -> dict | None:
    try:
        with open(os.path.join(ROOT, "status.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


WINDOWS = {"6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24), "7d": pd.Timedelta(days=7), "All": None}
_LINES = {"net_usd": ("Net", "#1e88e5"), "exp_lip_usd": ("Expected LIP", "#43a047"),
          "fill_pnl_usd": ("Fill P&L", "#e53935"), "locked_net_usd": ("Locked net", "#0d47a1")}


@st.cache_data(ttl=30, show_spinner=False)
def _history() -> pd.DataFrame:
    """The runner's per-report points (pnl_history.jsonl); empty if none yet."""
    try:
        d = pd.read_json(os.path.join(ROOT, HISTORY_FILE), lines=True)
    except (OSError, ValueError):
        return pd.DataFrame()
    if d.empty:
        return d
    d["time"] = pd.to_datetime(d["t"], unit="s", utc=True)
    # Points rebuilt from the log before 11-07 19:00 UTC have no locked LIP, so no locked net.
    d["locked_net_usd"] = d.get("lip_locked_usd", pd.Series(float("nan"), index=d.index)) + d["fill_pnl_usd"]
    return d.sort_values("time").reset_index(drop=True)


def _window(h: pd.DataFrame, s: dict, span: pd.Timedelta | None) -> pd.DataFrame:
    """History in the window with the live summary appended, each line less its
    value at the window's start (the last point before it), so all start at 0. A line
    with no value there (locked net, before it was recorded) starts at its first point."""
    now = pd.Timestamp(s.get("updated_at") or time.time(), unit="s", tz="UTC")
    live = pd.DataFrame([{"time": now, **{k: s.get(k) for k in _LINES},
                          "locked_net_usd": s.get("lip_locked_usd", 0.0) + s.get("fill_pnl_usd", 0.0)}])
    h = pd.concat([h, live], ignore_index=True) if len(h) else live
    since = None if span is None else now - span
    before = h[h.time <= since] if since is not None else h.iloc[:0]
    base = before.iloc[-1] if len(before) else h.iloc[0]
    d = h[h.time >= since] if since is not None else h
    start = since if len(before) else h.time.iloc[0]
    d = pd.concat([pd.DataFrame([{**base.to_dict(), "time": start}]), d], ignore_index=True)
    for k in _LINES:
        v = d[k].astype(float)
        b0 = base.get(k) if pd.notna(base.get(k)) else (v.dropna().iloc[0] if v.notna().any() else 0.0)
        d[k] = v - float(b0)
    return d


def _chart(d: pd.DataFrame, span: pd.Timedelta | None):
    long = d.melt(id_vars="time", value_vars=list(_LINES), var_name="k", value_name="usd")
    long = long.dropna(subset=["usd"])
    long["what"] = long.k.map(lambda k: _LINES[k][0])
    names = [v[0] for v in _LINES.values()]
    color = alt.Color("what:N", title=None, sort=names, legend=alt.Legend(orient="top-left"),
                      scale=alt.Scale(domain=names, range=[v[1] for v in _LINES.values()]))
    line = alt.Chart(long).mark_line(interpolate="step-after").encode(
        x=alt.X("time:T", title=None, axis=time_axis(span)), y=alt.Y("usd:Q", title="$ since window start"),
        color=color, strokeWidth=alt.condition(alt.datum.what == "Net", alt.value(2.5), alt.value(1.3)),
        strokeDash=alt.condition(alt.datum.what == "Locked net", alt.value([2, 3]), alt.value([1, 0])),
        tooltip=[alt.Tooltip("time:T", format="%m-%d %H:%M"), "what:N", alt.Tooltip("usd:Q", format="+,.2f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#888", strokeWidth=0.7).encode(y="y:Q")
    st.altair_chart((line + zero).properties(height=260), use_container_width=True)


def _toggle(s: dict, idle: bool):
    live = bool(s.get("live_trading")) and not idle
    if live:
        st.error("🟢 **REAL TRADING IS LIVE**")
    else:
        st.info("🔵 SHADOW -- live books, quotes simulated (expected LIP accrues as if resting), no real orders.")
    arm_control(NAME, bool(s.get("arm_requested")),
                help="Arming cancels any lipf- orders left from before and starts quoting for real; ARBS drops "
                     "the claimed events first (~2-3 min). Disarming pulls every quote. Positions are kept.")


def render(page: str, stale_fn):
    with st.sidebar:
        st.header("Config")
        config_form.render(_FORM)
    st.title("LIP farm")
    st.caption("Only low-flow markets (`max_trades_day` over `flow_days`, not `skip_categories`). "
               "Wide markets (spread >= `wide_spread`): a bid of the market's Target (where LIP stops counting) at both touches. Decided markets "
               "(expensive bid >= `decided_min`, below the market's top price): a bid on the expensive side only. "
               "Other markets: both touches, sized by exit liquidity (`exit_depth_frac`). Every "
               "side must reach Target, so the snapshot pays. Best LIP per dollar first, up to `max_capital`. "
               "Expected LIP is our modelled share over time, not Kalshi's payout (nothing under $1 per "
               "market-period). Skips events ARBS holds; when live, ARBS stays off our claimed markets.")

    @st.fragment(run_every="5s")
    def body():
        if stale_fn(page):
            return
        doc = _status()
        if doc is None:
            st.info("No run yet: `python -m kalshi_strategies.LIPFARM.runner` (EC2: alpha5-lipfarm).")
            _toggle({}, idle=True)
            return
        s = doc["summary"]
        age = time.time() - (s.get("updated_at") or 0)
        idle = age > STALE_S
        if idle:
            st.warning(f"Farm not running (last update {age / 60:.0f} min ago). Figures are as of then.")
        _toggle(s, idle)
        win = st.radio("Window", list(WINDOWS), index=1, horizontal=True, key="lipfarm_win",
                       label_visibility="collapsed")
        d = _window(_history(), s, WINDOWS[win])
        if len(d) > 2:
            _chart(d, WINDOWS[win])
        else:
            st.caption("No P&L history yet (the runner adds a point every ~2 min).")
        c = st.columns(6)
        c[0].metric("Quoting", s["quoting"])
        c[1].metric("LIP rate now", f"${s['our_usd_day_now'] / 1440:,.3f}/min", f"${s['our_usd_day_now']:,.0f}/day",
                    delta_color="off")
        c[2].metric("Expected LIP", f"${s['exp_lip_usd']:,.2f}",
                    f"${s.get('lip_locked_usd', 0):,.2f} locked (past $1)", delta_color="off",
                    help="Locked: program periods already past Kalshi's $1 minimum, paid in full. The rest "
                         "is still under $1 for its period and is forfeited if the market drops before $1.")
        c[3].metric("Fill P&L", f"${s['fill_pnl_usd']:+,.2f}", f"{s['fills']} fills, {s['fill_lots']:,.0f} lots",
                    delta_color="off")
        c[4].metric("Net", f"${s['net_usd']:+,.2f}")
        c[5].metric("Exposure", f"${s.get('exposure_usd', 0):,.0f}",
                    f"of ${s.get('max_capital_usd', 0):,.0f} cap · ${s['resting_cost_usd']:,.0f} bids resting",
                    delta_color="off", help="What fills have left us holding, unexited, at cost. Resting bids "
                                            "hold no cash at Kalshi; a bid that could fill us past the cap "
                                            "isn't placed.")
        ws = ("websocket up" if s.get("ws_up") else "websocket down (REST fallback)") if s.get("live_trading") else "REST"
        st.caption(f"writes {s.get('write_tps_10m', 0):.2f} of {s.get('write_tps_cap', 0):.0f} tokens/s (10 min) · "
                   f"{ws} · {s['mode']} · this mode's journal covers {s['hours']:.2f} h · "
                   f"{s.get('waiting_on_arbs', 0)} picks waiting for ARBS to drop them · "
                   f"{s.get('arbs_events_skipped', 0)} ARBS events skipped")

        q = pd.DataFrame(doc["quotes"])
        st.subheader("Quotes")
        if q.empty:
            st.caption("None resting.")
        else:
            view = pd.DataFrame({
                "market": q["url"] if "url" in q else q["ticker"],
                "m": q["mode"].map({"wide": "W", "decided": "D", "narrow": "N", "hedge": "X"}).fillna(""),
                "quote": q["bids"].map(_compact_bids),
                "$/min": q["our_usd_day"] / 1440, "pool/min": q["pool_usd_day"] / 1440,
                "period $": q.get("period_lip_usd", 0.0), "LIP $": q["exp_lip_usd"], "cap $": q["capital_usd"]})
            num = st.column_config.NumberColumn
            st.dataframe(view, hide_index=True, use_container_width=True, column_config={
                "market": _LINK,
                "m": st.column_config.TextColumn(width="small", help="W wide, D decided, N narrow (exit-liquidity sized), X exit only"),
                "quote": st.column_config.TextColumn(width="medium", help="⚑ = 1.1-lot sentinel one tick above, "
                                                                          "in the bid's order group"),
                "$/min": num(format="%.4f", width="small"), "pool/min": num(format="%.3f", width="small"),
                "period $": num(format="%.2f", width="small", help="Kalshi pays a market-period only from $1."),
                "LIP $": num(format="%.2f", width="small"), "cap $": num(format="%.0f", width="small")})
        st.subheader("Fills")
        p = pd.DataFrame(doc["positions"])
        if p.empty:
            st.caption("No fills.")
        else:
            st.dataframe(_by_market(p), hide_index=True, use_container_width=True, column_config={
                "market": _LINK,
                "last fill (ET)": st.column_config.TextColumn(width="small", help="Latest fill in this market, US Eastern"),
                "first fill (ET)": st.column_config.TextColumn(width="small", help="First fill in this market, US Eastern"),
                "paired": st.column_config.NumberColumn(format="%.0f", help="yes+no pairs: worth $1 each at settlement"),
                "open": st.column_config.TextColumn(help="Unpaired contracts left, marked at today's bid"),
                "cost $": st.column_config.NumberColumn(format="%.2f", help="Everything paid, fees included"),
                "value $": st.column_config.NumberColumn(format="%.2f"),
                "P&L $": st.column_config.NumberColumn(format="%+.2f")})
        if doc.get("waiting"):
            with st.expander(f"Waiting for ARBS ({len(doc['waiting'])})"):
                st.write(doc["waiting"])

    body()
