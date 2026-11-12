"""Config editor generated from a series' config dataclass metadata. Writes
config.json (validated as a whole before anything touches disk); the running
session hot-reloads it."""
from __future__ import annotations

import json

import streamlit as st

from infra.config import (APPLY_NEXT_EVENT, ConfigError, describe, get_dotted, load,
                                 update_file)


# Shown in config.json but not in the form: tuning knobs nobody should need
# to touch from the dashboard.
HIDDEN = {"quoting.atm_hysteresis"}


class _BadInput(ValueError):
    pass


def _nullable_number(d: dict, value, label: str, key: str, help_: str):
    """Text box for an optional number: empty = null."""
    raw = st.text_input(label, value="" if value is None else f"{value:g}", key=key,
                        help=help_ + "\n\nLeave empty for none.", placeholder="none")
    raw = raw.strip()
    if not raw:
        return None
    try:
        v = int(raw) if d["type"] == "int" else float(raw)
    except ValueError:
        raise _BadInput(f"{d['name']}: {raw!r} is not a{'n integer' if d['type'] == 'int' else ' number'}")
    return v


def _json_dict(d: dict, value, label: str, key: str, help_: str):
    """JSON text box for a dict field, e.g. {"XRP": 4.0}; empty = null when nullable."""
    raw = st.text_input(label, value="" if value is None else json.dumps(value), key=key,
                        help=help_ + ("\n\nJSON object; leave empty for none." if d["nullable"] else "\n\nJSON object."),
                        placeholder="none" if d["nullable"] else "")
    raw = raw.strip()
    if not raw and d["nullable"]:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        raise _BadInput(f"{d['name']}: {raw!r} is not valid JSON")
    if not isinstance(v, dict):
        raise _BadInput(f"{d['name']}: expected a JSON object like {{\"XRP\": 4.0}}")
    return v


def _market_checkboxes(d: dict, value, key: str, label: str, help_: str, markets: list[str]):
    """One checkbox per market for a list-of-market-names field. All ticked
    is saved as null (= every market, including any added later)."""
    st.markdown(f"**{label}**", help=help_)
    on = set(markets) if value is None else set(value)
    cols = st.columns(len(markets))
    picked = [m for c, m in zip(cols, markets) if c.checkbox(m, value=m in on, key=f"{key}_{m}")]
    if len(picked) == len(markets):
        return None
    return picked


def _widget(d: dict, value, key: str, markets: list[str] | None = None):
    """One input for one field. Returns the new value."""
    name = d["name"].split(".")[-1]
    label = name + ("  ⏭" if d["apply"] == APPLY_NEXT_EVENT else "")
    help_ = d["help"] + ("\n\n⏭ applies at the next event." if d["apply"] == APPLY_NEXT_EVENT else "")
    if d["type"] == "list" and name.endswith("_markets") and markets:
        return _market_checkboxes(d, value, key, label, help_, markets)
    if d["type"] == "dict":
        return _json_dict(d, value, label, key, help_)
    if d["type"] == "bool":
        return st.checkbox(label, value=bool(value), key=key, help=help_)
    if d["type"] == "str":
        return st.text_input(label, value=value or "", key=key, help=help_)
    if d["nullable"]:
        return _nullable_number(d, value, label, key, help_)
    if d["type"] == "int":
        return int(st.number_input(label, value=int(value), step=int(d["step"] or 1),
                                   min_value=None if d["min"] is None else int(d["min"]),
                                   max_value=None if d["max"] is None else int(d["max"]),
                                   key=key, help=help_))
    step = float(d["step"] or 0.01)
    fmt = "%.4f" if step < 0.01 else "%.3f" if step < 0.1 else "%.2f"
    return float(st.number_input(label, value=float(value), step=step, format=fmt,
                                 min_value=None if d["min"] is None else float(d["min"]),
                                 max_value=None if d["max"] is None else float(d["max"]),
                                 key=key, help=help_))


def render(series, markets: list[str] | None = None) -> None:
    """Sidebar form for `series` (a runtime.series.Series). `markets`: the
    series' market names (XRP, BTC, ...), for per-market checkbox fields."""
    try:
        cfg = load(series.config_cls, series.config_path, series.config_key)
    except (ConfigError, OSError, ValueError) as e:
        st.error(f"config.json is invalid -- fix it by hand: {e}")
        return
    fields = [d for d in describe(series.config_cls) if d["editable"] and d["name"] not in HIDDEN]
    sections: dict[str, list] = {}
    for d in fields:
        sections.setdefault(d.get("group") or d["name"].split(".")[0], []).append(d)

    changes, bad = {}, []
    with st.form(f"cfg_{series.ticker}"):
        for sec, ds in sections.items():
            with st.expander(sec, expanded=sec in ("quoting", "pricing") or len(sections) == 1):
                sub = None
                for d in ds:
                    if d.get("sub") and d["sub"] != sub:
                        if sub is not None or d is not ds[0]:
                            st.markdown('<hr style="margin:6px 0;border:none;border-top:1px solid #8886">',
                                        unsafe_allow_html=True)
                        st.caption(d["sub"])
                    sub = d.get("sub") or sub
                    cur = get_dotted(cfg, d["name"])
                    if d["type"] == "list" and not markets:
                        continue            # no market list to draw checkboxes from: leave as is
                    try:
                        new = _widget(d, cur, f"cfg_{series.ticker}_{d['name']}", markets)
                    except _BadInput as e:
                        bad.append(str(e))
                        continue
                    if new != cur:
                        changes[d["name"]] = new
        submitted = st.form_submit_button("Save to config.json")
    if submitted:
        if bad:
            st.error("Rejected, nothing written: " + "; ".join(bad))
            return
        if not changes:
            st.info("No changes.")
            return
        try:
            update_file(series.config_cls, series.config_path, series.config_key, changes)
        except ConfigError as e:
            st.error(f"Rejected, nothing written: {e}")
            return
        nxt = [k for k in changes if next(d for d in fields if d["name"] == k)["apply"] == APPLY_NEXT_EVENT]
        st.success(f"Saved {len(changes)} change(s). Live fields apply within ~1s"
                   + (f"; {', '.join(nxt)} apply at the next event." if nxt else "."))
