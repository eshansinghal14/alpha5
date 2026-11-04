"""LIP farm: the markets LIPFARM quotes, the LIP we expect from them, and any
fills, with the shared arm button and the config form in the sidebar. Reads
data/live/LIPFARM/status.json (farm.report)."""
from __future__ import annotations

import json
import os
import time
import types

import pandas as pd
import streamlit as st

from kalshi_strategies.LIPFARM.farm import CONFIG_PATH, NAME, ROOT, LipFarmConfig
from kalshi_viz import config_form
from kalshi_viz.arm import arm_control

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


def _status() -> dict | None:
    try:
        with open(os.path.join(ROOT, "status.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


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
    st.caption("Wide markets (spread >= `wide_spread`): a bid of `lots` at both touches. Decided markets "
               "(expensive bid >= `decided_min`, below the market's top price): a bid on the expensive side only. Every "
               "side must reach Target, so the snapshot pays. Best LIP per dollar first, up to `max_capital`. "
               "Expected LIP is our modelled share over time, not Kalshi's payout (nothing under $1 per "
               "market-period). Skips events ARBS holds; when live, ARBS stays off our claimed markets.")

    @st.fragment(run_every="5s")
    def body():
        if stale_fn(page):
            return
        doc = _status()
        if doc is None:
            st.info("No run yet: `python -m kalshi_strategies.LIPFARM.farm` (EC2: alpha5-lipfarm).")
            _toggle({}, idle=True)
            return
        s = doc["summary"]
        age = time.time() - (s.get("updated_at") or 0)
        idle = age > STALE_S
        if idle:
            st.warning(f"Farm not running (last update {age / 60:.0f} min ago). Figures are as of then.")
        _toggle(s, idle)
        c = st.columns(6)
        c[0].metric("Quoting", s["quoting"])
        c[1].metric("LIP rate now", f"${s['our_usd_day_now']:,.0f}/day")
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
                "market": q["ticker"],
                "m": q["mode"].map({"wide": "W", "decided": "D", "hedge": "X"}).fillna(""),
                "quote": q["bids"].map(_compact_bids),
                "$/day": q["our_usd_day"], "pool/d": q["pool_usd_day"],
                "period $": q.get("period_lip_usd", 0.0), "LIP $": q["exp_lip_usd"], "cap $": q["capital_usd"]})
            num = st.column_config.NumberColumn
            st.dataframe(view, hide_index=True, use_container_width=True, column_config={
                "market": st.column_config.TextColumn(width="medium"),
                "m": st.column_config.TextColumn(width="small", help="W wide, D decided, X exit only"),
                "quote": st.column_config.TextColumn(width="medium", help="⚑ = 1.1-lot sentinel one tick above, "
                                                                          "in the bid's order group"),
                "$/day": num(format="%.0f", width="small"), "pool/d": num(format="%.0f", width="small"),
                "period $": num(format="%.2f", width="small", help="Kalshi pays a market-period only from $1."),
                "LIP $": num(format="%.2f", width="small"), "cap $": num(format="%.0f", width="small")})
        st.subheader("Fills")
        p = pd.DataFrame(doc["positions"])
        if p.empty:
            st.caption("No fills.")
        else:
            st.dataframe(p, hide_index=True, use_container_width=True)
        if doc.get("waiting"):
            with st.expander(f"Waiting for ARBS ({len(doc['waiting'])})"):
                st.write(doc["waiting"])

    body()
