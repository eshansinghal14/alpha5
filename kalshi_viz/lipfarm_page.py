"""LIP farm: the markets LIPFARM quotes, the LIP we expect from them, and any
fills. Reads data/live/LIPFARM/{shadow,live}/status.json (farm.report)."""
from __future__ import annotations

import json
import os
import time

import pandas as pd
import streamlit as st

from kalshi_infra import DATA_ROOT

ROOT = os.path.join(DATA_ROOT, "live", "LIPFARM")
STALE_S = 120.0


def _status(mode: str) -> dict | None:
    try:
        with open(os.path.join(ROOT, mode, "status.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def render(page: str, stale_fn):
    st.title("LIP farm")
    st.caption("One post-only bid of `lots` at the cheap side's touch (best bid <= `max_price`) on the "
               "`num_markets` LIP markets paying us most. Expected LIP is our modelled share over time, "
               "not Kalshi's payout. Skips every event ARBS holds or has quoted; live claims make ARBS's "
               "scan drop the rest. Params: kalshi_strategies/LIPFARM/config.json (read each cycle).")
    modes = [m for m in ("live", "shadow") if _status(m) is not None] or ["shadow"]
    mode = st.radio("Run", modes, horizontal=True, key="lipfarm_mode")

    @st.fragment(run_every="10s")
    def body():
        if stale_fn(page):
            return
        doc = _status(mode)
        if doc is None:
            st.info("No run yet: `python -m kalshi_strategies.LIPFARM.farm` (add `--live` for real orders).")
            return
        s = doc["summary"]
        age = time.time() - (s.get("updated_at") or 0)
        if age > STALE_S:
            st.warning(f"Farm not running (last update {age / 60:.0f} min ago). Figures are as of then.")
        c = st.columns(6)
        c[0].metric("Quoting", s["quoting"])
        c[1].metric("LIP rate now", f"${s['our_usd_day_now']:,.0f}/day")
        c[2].metric("Expected LIP", f"${s['exp_lip_usd']:,.2f}")
        c[3].metric("Fill P&L", f"${s['fill_pnl_usd']:+,.2f}", f"{s['fills']} fills, {s['fill_lots']:,.0f} lots",
                    delta_color="off")
        c[4].metric("Net", f"${s['net_usd']:+,.2f}")
        c[5].metric("Resting cost", f"${s['resting_cost_usd']:,.0f}")
        st.caption(f"{s['mode']} · running {s['hours']:.2f} h · {s.get('waiting_on_arbs', 0)} picks waiting "
                   f"for ARBS to drop them · {s.get('arbs_events_skipped', 0)} ARBS events skipped")

        q = pd.DataFrame(doc["quotes"])
        st.subheader("Quotes")
        if q.empty:
            st.caption("None resting.")
        else:
            st.dataframe(q.rename(columns={"our_usd_day": "our $/day", "pool_usd_day": "pool $/day",
                                           "exp_lip_usd": "expected LIP $"}),
                         hide_index=True, use_container_width=True,
                         column_config={"price": st.column_config.NumberColumn(format="%.3f"),
                                        "our $/day": st.column_config.NumberColumn(format="%.1f"),
                                        "pool $/day": st.column_config.NumberColumn(format="%.1f"),
                                        "expected LIP $": st.column_config.NumberColumn(format="%.2f")})
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
