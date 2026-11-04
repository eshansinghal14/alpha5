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
               "(expensive bid in [`decided_min`, `decided_max`]): a bid on the expensive side only. Every "
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
        c[5].metric("Capital resting", f"${s['resting_cost_usd']:,.0f}",
                    f"of ${s.get('max_capital_usd', 0):,.0f}" if s.get("max_capital_usd") else None,
                    delta_color="off")
        st.caption(f"{s['mode']} · this mode's journal covers {s['hours']:.2f} h · "
                   f"{s.get('waiting_on_arbs', 0)} picks waiting for ARBS to drop them · "
                   f"{s.get('arbs_events_skipped', 0)} ARBS events skipped")

        q = pd.DataFrame(doc["quotes"])
        st.subheader("Quotes")
        if q.empty:
            st.caption("None resting.")
        else:
            st.dataframe(q.rename(columns={"our_usd_day": "our $/day", "pool_usd_day": "pool $/day",
                                           "capital_usd": "capital $",
                                           "exp_lip_usd": "expected LIP $", "period_lip_usd": "this period $"}),
                         hide_index=True, use_container_width=True,
                         column_config={"capital $": st.column_config.NumberColumn(format="%.0f"),
                                        "our $/day": st.column_config.NumberColumn(format="%.1f"),
                                        "pool $/day": st.column_config.NumberColumn(format="%.1f"),
                                        "expected LIP $": st.column_config.NumberColumn(format="%.2f"),
                                        "this period $": st.column_config.NumberColumn(
                                            format="%.2f", help="Kalshi pays a market-period only from $1.")})
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
