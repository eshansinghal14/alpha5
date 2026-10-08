"""
SPXW/KXINX latency-arb dashboard: pick a backtest day, inspect Kalshi bid/
ask, the SPXW-implied probability + CI, our own edge-demanded reservation
prices, every fill we took, net position by ticker, and PnL over the
session -- all on one shared, zoomable timeline (see charts.py).

Run from the project root (or anywhere, it's self-locating):
    streamlit run viz/app.py

Mode: Historical (a full backtest replay, picked from a Day selector) or
Live (kalshi_latency_arbs/live/run_live.py's current session -- see that
module's docstring). This page never launches or manages run_live.py; it
reads whatever that process last wrote to data/live/{series}/{today}/
(see data_loader.load_live_day) and, for the two live controls, writes
small request files it picks up: config_override.json for strategy
tuning, and trading_mode.json for the real-money switch (see
_render_trading_toggle). Start the process separately either way.

Both modes build a DayData and hand it to the exact same rendering code
below; charts.py has no idea which mode produced its input.
"""
import dataclasses
import json
import os
import sys

# `streamlit run app.py` adds the script's own directory to sys.path
# automatically, but other launchers (notably streamlit.testing.v1.AppTest,
# used by this project's smoke test) do NOT -- confirmed directly (AppTest
# raised ModuleNotFoundError on this exact sibling import). Do it explicitly
# so this page works the same regardless of how/from-where it's launched.
_VIZ_DIR = os.path.dirname(os.path.abspath(__file__))
if _VIZ_DIR not in sys.path:
    sys.path.insert(0, _VIZ_DIR)
_LIVE_DIR = os.path.join(_VIZ_DIR, "..", "kalshi_latency_arbs", "live")
if _LIVE_DIR not in sys.path:
    sys.path.insert(0, _LIVE_DIR)

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from data_loader import DayData, SERIES, discover_days, load_backtest_day, load_live_day, strategy_module
from charts import build_dashboard_figure, render_dashboard_html
from live_chart import POLL_MS, build_live_html
from live_persist import stream_filename

_PROJECT_ROOT_DATA = os.path.join(_VIZ_DIR, "..", "data")

st.set_page_config(page_title="SPXW latency-arb dashboard", layout="wide")


def _strategy_config_inputs(series: str, key_prefix: str, current: dict | None = None) -> dict:
    """The historical and live sidebars configure the SAME strategy, so both
    render from here -- two sidebars that look different read as a strategy
    difference that doesn't exist.

    Defaults come from QuotingConfig itself rather than literals, so changing
    the dataclass can't leave the dashboard opening on a config nobody set.
    `current` (live only) overrides them with what the running process
    reports it is actually using.
    """
    d = dataclasses.asdict(strategy_module(series).QuotingConfig())
    if current:
        d.update({k: v for k, v in current.items() if v is not None})
    extra = {}
    if "real_trading_cutoff_min" in d:
        # Coin Race only: other strategies' configs don't carry the field,
        # and run_live drops unknown keys anyway.
        extra["real_trading_cutoff_min"] = st.number_input(
            "stop REAL trading X min before expiry (0 = off)", 0.0, 15.0,
            float(d["real_trading_cutoff_min"] or 0.0), 0.5, key=f"cfg_rtc_{key_prefix}",
            help="Disarms real orders (and cancels resting ones) once the event has this "
                 "many minutes left. Paper trading keeps running to the close. The next "
                 "event re-arms automatically if real trading is still switched on.")
    return extra | {
        "edge_fraction": st.number_input(
            "edge_fraction (x CI half-width)", 0.0, 5.0, float(d["edge_fraction"]), 0.05,
            key=f"cfg_ef_{key_prefix}"),
        "constant_edge": st.number_input(
            "constant_edge ($, flat)", 0.0, 0.20, float(d["constant_edge"]), 0.005,
            format="%.3f", key=f"cfg_ce_{key_prefix}",
            help="Flat edge floor charged on BOTH sides of every decision, on top of "
                 "the CI, fee and lean terms. Unlike those three it doesn't scale with "
                 "anything, so it's the only term that still bites when the model is "
                 "confident and the touch is cheap. Every cent trades fill count for "
                 "edge per fill -- tune it against markouts, not by feel."),
        "lean_multiplier": st.number_input(
            "lean_multiplier", 0.0, 5.0, float(d["lean_multiplier"]), 0.1,
            key=f"cfg_lm_{key_prefix}"),
        "lean_dollars": st.number_input(
            "lean_dollars ($ at full position)", 0.0, 0.20, float(d["lean_dollars"]), 0.005,
            format="%.3f", key=f"cfg_ld_{key_prefix}",
            help="Flat inventory brake, charged as this much edge at the position cap and "
                 "pro-rata below it. Unlike lean_multiplier it doesn't scale with the CI "
                 "width, so it still bites when the model is confident -- which is exactly "
                 "when a market runs to the cap in seconds. 0 disables it."),
        "max_position": st.number_input(
            "max_position", 1.0, 1000.0, float(d["max_position"]), 1.0,
            key=f"cfg_mp_{key_prefix}"),
        "max_improve_ticks": st.number_input(
            "max_improve_ticks (0 = uncapped)", 0, 50,
            int(d.get("max_improve_ticks") or 0), 1, key=f"cfg_mit_{key_prefix}",
            help="Cap on how far INSIDE the near touch a resting quote may sit, in ticks. "
                 "floor(reservation) is the most we would ever pay, so using it as the "
                 "target hands edge over in a wide market -- measured on KXINX 2026-08-17, "
                 "6% of quotes overshot best_bid+1tick, p90 by 24c. 1 improves the touch by "
                 "exactly one tick and no more. 0 disables the cap. Higher = more fill "
                 "probability, less edge per fill; tune against quote markouts."),
        "enable_quoting": st.checkbox(
            "enable_quoting (rest orders)", value=bool(d["enable_quoting"]),
            key=f"cfg_eq_{key_prefix}",
            help="Rest quote_size on each side at the first tick outside the taking edge plus "
                 "quoting_edge, on top of taking. Unticking stops NEW quotes and withdraws the "
                 "ones already resting on each market's next decision; unticking REAL trading "
                 "pulls them all immediately."),
        "quoting_edge": st.number_input(
            "quoting_edge ($, the quote's own flat floor)", 0.0, 0.50,
            float(d["quoting_edge"]), 0.005, format="%.3f", key=f"cfg_qe_{key_prefix}",
            help="Extra flat edge a RESTING quote must clear, ON TOP of the whole taking edge "
                 "(constant + CI + fee + lean). A take crosses a price that exists right now; a "
                 "quote is only ever hit by someone who thinks it's wrong."),
        "quote_size": st.number_input(
            "quote_size (contracts per side)", 1.0, 100.0, float(d["quote_size"]), 1.0,
            key=f"cfg_qs_{key_prefix}"),
        "requote_delay_s": st.number_input(
            "requote_delay_s", 0.0, 60.0, float(d["requote_delay_s"]), 0.5,
            key=f"cfg_rd_{key_prefix}",
            help="How long a market/side stays unquoted after our own quote there was hit. "
                 "Requoting instantly into the level someone just crossed is how a quoter gets "
                 "run over by one informed order."),
    }


@st.cache_data(show_spinner="Running backtest...")
def _load_cached(event_ticker: str, series: str, edge_fraction: float,
                  max_position: float,
                  constant_edge: float = 0.0, lean_multiplier: float = 1.0,
                  lean_dollars: float = 0.0, enable_quoting: bool = False,
                  quoting_edge: float = 0.02, quote_size: float = 1.0,
                  requote_delay_s: float = 1.0,
                  max_improve_ticks: int = 1) -> DayData:
    # Cached on every arg that changes the result -- switching back to an
    # already-viewed combination is instant; only a genuinely new one
    # re-runs the backtest. QuotingConfig itself isn't hashable-friendly for
    # st.cache_data, so it's rebuilt from these plain scalar args instead of
    # passed in directly. lot_size isn't exposed here -- left at
    # QuotingConfig's own default (see that field's docstring).
    strat = strategy_module(series)
    config = strat.QuotingConfig(edge_fraction=edge_fraction, max_position=max_position,
                                  constant_edge=constant_edge, lean_multiplier=lean_multiplier,
                                  lean_dollars=lean_dollars, enable_quoting=enable_quoting,
                                  quoting_edge=quoting_edge, quote_size=quote_size,
                                  requote_delay_s=requote_delay_s,
                                  # 0 in the widget means "uncapped", which the
                                  # strategy spells as None.
                                  max_improve_ticks=(max_improve_ticks or None))
    return load_backtest_day(event_ticker, series=series, config=config)


MODES = ("Historical", "Live")

# Dropdown activity filter -- see _ranked_markets.
ACTIVE_MIN_FRACTION = 0.05   # of the busiest market's decision count
ACTIVE_MAX_AGE_S = 300       # seconds since that market's last decision


def main():
    st.title("SPXW latency-arb -- backtest inspector")

    with st.sidebar:
        st.header("Mode")
        mode = st.radio("Mode", MODES, horizontal=True, label_visibility="collapsed")

        st.header("Series")
        series = st.radio("Series", SERIES, horizontal=True, label_visibility="collapsed")

        if mode == "Live":
            # Backtest controls are meaningless here -- this config edits the
            # process that's already running, not a replay we're about to start.
            try:
                _live_config_sidebar(series, load_live_day(series).summary)
            except FileNotFoundError:
                st.info(f"No live {series} session running.")

        if mode == "Historical":
            days = discover_days(series)
            st.header("Day")
            if not days:
                st.error(f"No runnable {series} days found (need both a cached Kalshi quote source and a "
                          f"full Databento SPXW cache -- see {series.lower()}'s run_backtest.py docstring).")
                return
            labels = [f"{d['event_ticker']}  ({d['fidelity']})"
                      + ("" if d["fidelity"] == "l2" else "  ⚠ not L2")
                      for d in days]
            choice = st.selectbox("Backtest day", labels, index=0)
            event_ticker = days[labels.index(choice)]["event_ticker"]

            st.header("Strategy config")
            cfg_vals = _strategy_config_inputs(series, f"hist_{series}")
            edge_fraction = cfg_vals["edge_fraction"]
            constant_edge = cfg_vals["constant_edge"]
            max_position = cfg_vals["max_position"]

    if mode == "Live":
        _render_live(series)
        return

    # Set on EVERY rerun, before the (possibly multi-minute) load below --
    # see data_loader.load_backtest_day's own should_abort docstring: this
    # is how a still-in-flight load for a day/series we've since switched
    # AWAY from notices and abandons itself instead of running to
    # completion in the background for a result nobody will see.
    st.session_state["_dashboard_request_key"] = (series, event_ticker)
    # Keyword args deliberately: this list has grown and a positional insert
    # in the wrong slot silently shifts every argument after it.
    day = _load_cached(event_ticker, series, edge_fraction, max_position,
                        constant_edge=constant_edge,
                        lean_multiplier=cfg_vals["lean_multiplier"],
                        lean_dollars=cfg_vals["lean_dollars"],
                        enable_quoting=cfg_vals["enable_quoting"],
                        quoting_edge=cfg_vals["quoting_edge"],
                        quote_size=cfg_vals["quote_size"],
                        requote_delay_s=cfg_vals["requote_delay_s"],
                        max_improve_ticks=cfg_vals["max_improve_ticks"])
    _render_day(day)


def _write_trading_mode(series: str):
    """Ask the running run_live process to arm/disarm real order placement.

    Written to its own file, NOT config_override.json: that one is rewritten
    wholesale by the config panel's Apply button, and the control that moves
    real money must not ride along with an edge_fraction tweak. run_live's
    trading_mode_loop picks this up within ~1s, reconciles positions against
    Kalshi, and reports back through summary.json's `live_trading`.
    """
    path = os.path.join(_PROJECT_ROOT_DATA, "live", series, "trading_mode.json")
    _atomic_write_json(path, {"live_trading": bool(st.session_state[f"live_trading_{series}"]),
                              "requested_at": pd.Timestamp.now(tz="UTC").isoformat()})


def _atomic_write_json(path: str, obj) -> None:
    """Write to a temp file then os.replace onto `path`. run_live polls these
    request files every second; an in-place open("w") truncates first, so a
    poll landing mid-write read an empty file (JSONDecodeError 'Expecting
    value: line 1 column 1', 2026-10-08)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _render_trading_toggle(series: str, summary: dict):
    """The real-money switch, plus an honest banner of what's ACTUALLY
    happening.

    The banner reads `live_trading` out of summary.json -- what the trading
    process reports it is doing -- while the checkbox holds what we've asked
    for. They differ for a second after a click, and keep differing if the
    process is stopped or refused to arm (e.g. it couldn't verify positions
    against Kalshi), which is exactly when a checkbox alone would lie.
    """
    active = bool(summary.get("live_trading"))
    if active:
        started = summary.get("session_started")
        since = (f" Session started {pd.Timestamp(started).tz_convert('UTC').strftime('%H:%M:%SZ')}."
                 if started else "")
        st.error(f"🟢 **REAL TRADING IS LIVE** — orders are being sent to Kalshi. "
                 f"{summary.get('n_real_fills', 0)} real fill(s) from "
                 f"{summary.get('n_real_orders', 0)} order(s).{since}")
    else:
        st.info("🔵 SHADOW MODE — live market data, simulated fills, NO real orders are being sent.")

    key = f"live_trading_{series}"
    if key not in st.session_state:
        st.session_state[key] = active
    st.checkbox(
        "Send REAL orders to Kalshi (real money)", key=key,
        on_change=_write_trading_mode, args=(series,),
        help="Arms real order placement in the already-running run_live process — no restart, and "
             "the strategy logic is byte-identical either way; only fill resolution changes. "
             "Arming STARTS A FRESH SESSION: the shadow fills, PnL curve and charts are cleared, "
             "and positions are seeded from Kalshi so the per-market cap and lean act on what we "
             "actually hold. Unticking stops NEW orders and PULLS every resting quote off the "
             "book (including any left over from a crashed process); it does not flatten "
             "positions.",
    )
    cutoff = summary.get("real_trading_cutoff_min") or 0
    if st.session_state[key] and not active and summary.get("real_cutoff_hit"):
        st.info(f"Real trading paused for this event: inside the {cutoff:g}-min pre-expiry "
                f"cutoff. Paper trading continues; real orders resume on the next event.")
    elif st.session_state[key] != active:
        st.warning("Waiting for the live process to confirm. if this doesn't clear within a few "
                   "seconds, check the run_live log - it refuses to arm when it can't read "
                   "positions from Kalshi.")
    elif active and cutoff:
        tau = summary.get("tau_s")
        if tau is not None:
            st.caption(f"Real trading auto-stops {cutoff:g} min before expiry "
                       f"(in {max(0.0, tau - cutoff * 60) / 60:.1f} min).")
    if summary.get("n_real_order_errors"):
        st.warning(f"{summary['n_real_order_errors']} real order(s) errored — see the run_live log.")


# Why the engine is refusing to trade, in the operator's words rather than
# the engine's. Keys are LiveEngine.blocked_reason's own return values.
_BLOCK_EXPLANATIONS = {
    # No fixed text for "halted": several different guards set it (PnL,
    # ledger-vs-Kalshi divergence, a fill outside its limit, growth past the
    # cap) and the engine's own halt_reason says which. Hardcoding the PnL
    # wording here once reported a ledger divergence as a PnL breach.
    "halted": "",
    "stale_signal": "The option-implied signal has gone stale (dead Databento feed, or repricing "
                    "is throwing). The edge is a lag on option repricing, so a frozen option side "
                    "means we'd be crossing a live book with an old fair value.",
    "order_error_breaker": "Too many consecutive real-order errors — circuit breaker open. This is "
                           "auth, balance or market state, not a moved touch. Fix and restart.",
    "event_closing": "The event is about to stop accepting orders.",
    "systematic_bias": "Our implied probability is offset from the market's touch in the SAME "
                       "direction across many markets at once. A real edge is localised; a "
                       "uniform ladder-wide offset is a pricing error (basis/spot), so trading "
                       "stops rather than sweeping the ladder.",
}


def _render_guards(summary: dict):
    """Live guard state: what is blocking trading right now, plus the feed
    clocks and the counters the guards act on.

    Shown whether or not real trading is armed -- the guards apply in shadow
    mode too (see LiveEngine.blocked_reason), and a shadow session silently
    blocked for an hour otherwise looks exactly like one that found no edge.
    """
    over = summary.get("over_limit") or {}
    if over:
        st.info("📌 " + ", ".join(f"**{t.split('-')[-1]}** {q:+.4g}" for t, q in list(over.items())[:6])
                + f" past the {summary.get('position_limit', 0):g} cap. Not an error and not blocking: "
                  "the strategy can only reduce these from here, and takes no lean penalty doing so. "
                  "It halts if any of them grows further.")

    blocked = summary.get("blocked_reason")
    if blocked:
        if blocked == "halted":
            reason = summary.get("halt_reason") or "reason not recorded"
            st.warning(f"⛔ **Halted — {reason}.** No further orders this session; restart to "
                       f"resume. Nothing is flattened, and any position already on the book stays "
                       f"there.")
        else:
            st.warning(f"⛔ **Not trading — {blocked}.** {_BLOCK_EXPLANATIONS.get(blocked, '')}")

    # All three latencies in ms: they live in the tens-to-hundreds range now,
    # where seconds-with-one-decimal rounded almost everything to "0.0s".
    def _ms(v):
        return "n/a" if v is None else f"{v * 1000:,.0f} ms"

    signal_age = summary.get("signal_age_s")
    feed_age = summary.get("feed_age_s")
    applied_age = summary.get("applied_age_s")
    max_age = summary.get("max_signal_age_s")
    cols = st.columns(4)
    cols[0].metric("Signal age", _ms(signal_age),
                   delta=("STALE" if max_age and signal_age and signal_age > max_age else None),
                   delta_color="inverse",
                   help=f"Since the last successful option-implied recompute. Trading stops above "
                        f"{(max_age or 0) * 1000:,.0f} ms.")
    cols[1].metric("ES feed age", _ms(feed_age),
                   help="Since a Databento record arrived, stamped on their own callback thread. "
                        "This is feed liveness alone and keeps ticking even if our loop is wedged.")
    backlog = None if (applied_age is None or feed_age is None) else applied_age - feed_age
    cols[2].metric("Loop backlog", _ms(backlog),
                   help="Applied age minus feed age: how long records sit in our asyncio queue "
                        "before being folded into the book. This, not the feed, is what pushed "
                        "signal age into seconds.")
    cols[3].metric("Worst-case PnL halt", f"-${summary.get('max_loss_usd', 80):.0f}",
                   help="Trips on the WORSE of touch marking (liquidation value) and markout "
                        "marking (the backtest's convention).")

    _render_pricing_health(summary)

    counts = summary.get("blocked_counts") or {}
    if counts:
        st.caption("Orders blocked this session: "
                   + ", ".join(f"{k} × {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))


def _render_pricing_health(summary: dict):
    """The ladder-wide bias check -- whether our fair value is trustworthy at
    all, as opposed to whether the plumbing around it is working."""
    bias = summary.get("bias") or {}
    cols = st.columns(2)
    med = bias.get("median")
    cols[0].metric("Ladder bias (median)", "n/a" if med is None else f"{med * 100:+.2f}c",
                   delta=("SYSTEMATIC" if bias.get("systematic") else None), delta_color="inverse",
                   help="Median of (our prob - market mid) across markets quoted two-sided. A real "
                        "edge is localised, so a persistent ladder-wide offset means our pricing "
                        "is shifted, not that the market is wrong.")
    same = bias.get("same_side")
    n = bias.get("n")
    cols[1].metric("Markets biased one way", "n/a" if same is None else f"{same}/{n}",
                   help="How many of the two-sided markets sit on the SAME side of our fair value. "
                        "All of them pointing one way is the signature of a pricing error.")


def _render_live(series: str):
    try:
        day = load_live_day(series)
    except FileNotFoundError:
        st.info(
            f"No live session found for {series} today. Start one separately with "
            f"`python kalshi_main/run_live.py --series {series}` -- "
            f"this page only reads its output, it doesn't launch it."
        )
        return

    # Banner, real-money switch and metrics all refresh together on one 2s
    # fragment -- they're plain widgets, so re-rendering them costs nothing
    # and disturbs nothing (unlike the chart below, which is emitted once).
    # Rendered BEFORE the empty-decisions check: arming/disarming real
    # trading must stay reachable on a session that hasn't traded yet.
    @st.fragment(run_every="2s")
    def _live_status():
        try:
            fresh = load_live_day(series)
        except FileNotFoundError:
            st.info(f"Live session for {series} stopped writing output.")
            return
        _render_trading_toggle(series, fresh.summary)
        _render_guards(fresh.summary)
        _render_metrics(fresh)

    _live_status()

    # Event-rollover watcher. Each KXCRYPTOLEAD15M period is a NEW event
    # (new event_ticker, new session_started) every 15 minutes. _render_live
    # reads `day` once at the top and builds the chart from that snapshot;
    # the 2s metric fragment never triggers a full rerun, so without this
    # the dashboard keeps the PREVIOUS period's chart mounted (still polling
    # the old market's static JSON) until a manual refresh. This fragment
    # notices the event has rolled and reruns the whole live view so the
    # graphs reset to the new period's window, markets and stream files.
    rolled_key = f"_live_rendered_event_{series}"
    st.session_state[rolled_key] = day.event_ticker

    @st.fragment(run_every="3s")
    def _watch_rollover():
        try:
            fresh = load_live_day(series)
        except FileNotFoundError:
            return
        if fresh.event_ticker != st.session_state.get(rolled_key):
            # New 15-minute period -- rebuild everything against it.
            # scope="app": a bare st.rerun() inside a fragment reruns only
            # the fragment, which would NOT recompute the chart key or
            # remount the iframe; the whole _render_live must re-run.
            st.rerun(scope="app")

    _watch_rollover()

    if day.quote_df.empty:
        # No decisions in the CURRENT session's files. This is the normal
        # transient right after an event rollover (run_live resets the
        # stream files, then the feed-health recovery window + the first
        # heavy-flush cycle take ~30s to repopulate) and at cold start.
        # Poll on a fragment so the view recovers on its own once the next
        # heavy flush lands -- WITHOUT this the check short-circuits on the
        # page load that happened to catch the gap and never re-runs, so
        # the warning sticks until a manual rerun even though decisions are
        # already being written again (observed 2026-10-07).
        @st.fragment(run_every="3s")
        def _await_decisions():
            try:
                fresh = load_live_day(series)
            except FileNotFoundError:
                st.info(f"Live session for {series} stopped writing output.")
                return
            if fresh.quote_df.empty:
                st.warning("Live session running but no decisions recorded yet "
                           "(normal for ~30s after an event rollover).")
            else:
                # decisions arrived -- rebuild the FULL live view (scope="app",
                # not the fragment, so the chart below actually gets built).
                st.rerun(scope="app")

        _await_decisions()
        return

    market_tickers = _ranked_markets(day)
    traded_size = (day.fills_df.groupby("ticker")["size"].sum().to_dict()
                   if not day.fills_df.empty else {})
    market = st.selectbox(
        "Market", market_tickers, key=f"live_market_{series}",
        format_func=lambda t: f"{t}  ({traded_size.get(t, 0.0):.4g} contracts)",
    )

    # The chart is emitted ONCE per (series, market) and updates itself by
    # fetching the JSON run_live writes -- see viz/live_chart.py. Streamlit
    # never re-renders it, so pan and zoom survive indefinitely. Caching the
    # html is what makes that true: the string has to be byte-identical
    # across reruns or Streamlit swaps the srcdoc and remounts the iframe.
    #
    # session_started in the key so a reset remounts the chart against the
    # fresh stream files rather than holding the discarded session's view.
    # enable_quoting in the key too: it decides whether the document carries
    # the resting-quote lines at all, so toggling it has to rebuild.
    quoting = bool(day.summary.get("enable_quoting", False))
    session_started = day.summary.get("session_started", "")
    key = f"_live_html_{series}_{market}_{session_started}_{quoting}"
    # Drop cached chart HTML from PRIOR periods/sessions so they don't
    # accumulate in session_state across rollovers (and so a stale chart for
    # this series can never be re-shown). Keep only the current session's.
    _prefix = f"_live_html_{series}_"
    for k in [k for k in st.session_state
              if k.startswith(_prefix) and session_started not in k]:
        del st.session_state[k]
    if key not in st.session_state:
        # Window is [session start, now]. Not quote_df.min(): quote_df comes
        # from the day's journal and reaches back into earlier sessions,
        # while the chart only streams the current one.
        t0 = (pd.Timestamp(session_started).tz_convert("UTC") if session_started
              else pd.to_datetime(day.quote_df["time"], utc=True).min())
        t1 = pd.Timestamp.now(tz="UTC")
        st.session_state[key] = build_live_html(
            series=series, market=market,
            json_url=f"/app/static/{stream_filename(series, market)}",
            x_start_ms=t0.value // 10**6, x_end_ms=t1.value // 10**6,
            max_position=day.config.max_position, quoting=quoting,
        )
    st.caption(f"Live chart streams itself every {POLL_MS / 1000:.1f}s — drag/zoom freely, "
               f"the view is never reset. Switch market to rebuild.")
    components.html(st.session_state[key], height=900, scrolling=True)

    with st.expander("Raw fills (this market)"):
        st.dataframe(day.fills_df[day.fills_df["ticker"] == market], use_container_width=True)


def _live_config_sidebar(series: str, summary: dict):
    """Edit the RUNNING strategy's config from the sidebar, without restarting it.

    Writes data/live/{series}/config_override.json, which run_live's
    config_loop picks up within ~1s and applies to the live QuotingConfig
    (see that loop for which fields are tunable and why). Values shown are
    the ones the process reports it's actually using, not dashboard
    defaults -- so this can't drift from reality the way a fresh
    QuotingConfig() would.
    """
    st.header("Strategy config")
    st.caption("Applies on the next decision — no restart. Lowering max_position "
               "does NOT unwind; it only stops the position growing.")
    vals = _strategy_config_inputs(series, f"live_{series}", current=summary)
    if st.button("Apply to live session", key=f"cfg_apply_{series}"):
        path = os.path.join(_PROJECT_ROOT_DATA, "live", series, "config_override.json")
        # Merge into what's saved, don't replace: this file is also the
        # persisted default run_live loads on restart (Coin Race), so a
        # field not shown in this sidebar must not be dropped from it.
        saved = {}
        try:
            with open(path) as fh:
                saved = json.load(fh) or {}
        except (OSError, ValueError):
            pass
        _atomic_write_json(path, saved | vals)
        st.success("Applied — run_live picks it up within ~1s and keeps it as the default "
                   "after restarts")


def _ranked_markets(day: DayData) -> list[str]:
    """Markets ordered nearest-to-ATM first, computed ONCE per session and
    then held fixed.

    Ranking is mean |prob - 0.5| over each market's own implied series --
    where the action was, rather than where the strategy happened to trade
    (fill count, the old ordering, is an output and reshuffles whenever the
    edge config changes).

    Frozen deliberately. In Live mode the underlying data grows every 2s, so
    a freshly-computed ranking reorders the dropdown continuously, which is
    disorienting on its own and used to change which market you were looking
    at. A rough ordering that stays put is more useful than a precise one
    that moves. Recomputed only when the day/series changes, or while no
    market has a usable signal yet (so an empty first load can't freeze an
    arbitrary order).
    """
    # Markets the strategy is ACTIVELY WORKING: it has fills there, or it is
    # still evaluating them at meaningful volume. Not "traded only" -- the
    # near-money buckets are the most-evaluated on the board and matter even
    # on a session where the edge test never cleared, since that near-miss is
    # exactly what you want to inspect.
    #
    # But not "everything evaluated" either: that includes the deep tails the
    # NoiseTracker suppresses, which stop receiving signal and so go stale
    # with only a few hundred decisions. Requiring recent activity AND at
    # least 5% of the busiest market's decision count separates them cleanly.
    q = day.quote_df
    if "KXCRYPTOLEAD15M" in day.series:
        # Coin Race: always the event's 5 coins, every one priced together,
        # so none is a dead tail. The activity filter hid coins out of the
        # race (Kalshi barely updates them, so few decisions -- SOL had 461
        # vs 13.5k for BTC on 2026-10-08). Fixed canonical order, no ranking.
        from kalshi_crypto.series import COINS
        tickers = list(q["ticker"].unique())
        order = {c: i for i, c in enumerate(COINS)}
        return sorted(tickers, key=lambda t: order.get(t.rsplit("-", 1)[-1], len(order)))
    traded = set(day.fills_df["ticker"].unique()) if not day.fills_df.empty else set()
    counts = q.groupby("ticker").size()
    last_seen = pd.to_datetime(q["time"], utc=True).groupby(q["ticker"]).max()
    t_end = last_seen.max()
    min_count = ACTIVE_MIN_FRACTION * counts.max() if len(counts) else 0
    tickers = [
        t for t in q["ticker"].unique()
        if t in traded
        or (counts.get(t, 0) >= min_count
            and (t_end - last_seen.get(t)).total_seconds() <= ACTIVE_MAX_AGE_S)
    ]
    # ...and that the ATM gate actually permits. max_markets makes most of the
    # ladder STRUCTURALLY untradeable rather than merely un-crossed, so those
    # buckets aren't the near-misses this list exists to surface -- the
    # strategy still evaluates and logs every market it has a signal for, which
    # is why they cleared the activity bar above.
    #
    # Union over the session, not a snapshot of the current 5: the set is
    # re-ranked every decision and follows the market, so more than
    # max_markets buckets legitimately qualify over a full day.
    if "atm_tradeable" in q.columns:
        allowed = set(q.loc[q["atm_tradeable"].fillna(True), "ticker"].unique())
        tickers = [t for t in tickers if t in traded or t in allowed]
    if not tickers:
        tickers = list(q["ticker"].unique())

    # Keyed on session_started too, so arming real trading (which clears the
    # ledger -- see LiveEngine.reset_session) rebuilds the ordering instead of
    # freezing the discarded shadow session's.
    cache_key = f"_market_rank_{day.series}_{day.event_ticker}_{day.summary.get('session_started', '')}"
    cached = st.session_state.get(cache_key)
    if cached is not None:
        # Keep the cached order verbatim (dropping anything gone), and append
        # markets that have appeared since. Deliberately NOT conditional on
        # cached covering every ticker: a new market showing up mid-session
        # would then trigger a full rerank and move every existing entry.
        live = set(tickers)
        known = set(cached)
        return [t for t in cached if t in live] + [t for t in tickers if t not in known]

    probs_mid = day.prob_grid.get("probs_mid")
    size_by_ticker = (day.fills_df.groupby("ticker")["size"].sum().to_dict()
                      if not day.fills_df.empty else {})
    dist = {}
    for t in tickers:
        col = probs_mid[t] if probs_mid is not None and t in probs_mid.columns else None
        dist[t] = float("inf") if col is None or col.notna().sum() == 0 else float((col - 0.5).abs().mean())
    ranked = sorted(tickers, key=lambda t: (dist[t], -size_by_ticker.get(t, 0.0)))

    if any(d != float("inf") for d in dist.values()):
        st.session_state[cache_key] = ranked
    return ranked


def _render_metrics(day: DayData):
    """The metric row. Split out of _render_day so Live mode can refresh it
    on its own cadence without touching the self-updating chart."""
    s = day.summary
    # Predexon is allowed (some days have no kalshi_ws capture at all and are
    # otherwise unviewable), so the fidelity has to be stated rather than
    # assumed -- a top-of-book day priced and filled like an L2 one is the
    # kind of thing that quietly invalidates a comparison between days.
    if day.source != "live" and day.fidelity != "l2":
        st.warning(
            f"**{day.event_ticker} is not L2 data** — source `{day.source}`, "
            f"fidelity `{day.fidelity}`. This day has no full kalshi_ws order-book "
            f"capture, so the book is reconstructed from a thinner source: depth is "
            f"approximate, resting-quote fills are less trustworthy, and PnL is not "
            f"directly comparable with an L2 day.",
            icon="⚠️",
        )
    cols = st.columns(6)
    cols[0].metric("Source / fidelity", f"{day.source} / {day.fidelity}",
                   delta=(None if day.fidelity == "l2" or day.source == "live" else "not L2"),
                   delta_color="inverse",
                   help="l2 = full kalshi_ws order book. Anything else is a fallback "
                        "source (predexon top-of-book, or 1-min candles with no depth "
                        "at all) used because that day has no kalshi_ws capture.")
    # Contracts, not fill count -- thin depth produces many microscopic
    # partial fills (one market showed 25 fills totalling 0.25 contracts), so
    # a count overstates activity wherever liquidity is worst.
    total_contracts = float(day.fills_df["size"].sum()) if not day.fills_df.empty else 0.0
    cols[1].metric("Contracts traded", f"{total_contracts:.4g}",
                   help=f"{s['n_fills']} individual fills")
    # Live sessions have nothing settled yet, so total_realized_pnl is
    # structurally 0 for the whole session (see core.Position.settle -- it
    # only populates realized_pnl once a result is known) and the metric was
    # pinned at $0.00 no matter what the position was actually doing. Show
    # the live mark-to-market instead, which is what pnl_df already carries
    # (live_persist.compute_live_pnl), and label it honestly so it's never
    # confused with a settled number.
    if day.source == "live" and not day.pnl_df.empty:
        # For the crypto (LIP) strategy this curve already includes accrued
        # LIP; surface the LIP component separately so it's legible.
        lip = s.get("total_lip_accrued_usd")
        label = "Live PnL (mkt + LIP)" if lip is not None else "Live PnL (mark-to-mkt)"
        cols[2].metric(label, f"${day.pnl_df['pnl'].iloc[-1]:+.2f}",
                       delta=(None if lip is None else f"LIP ${lip:+.2f}"),
                       help=(None if lip is None else
                             "Mark-to-fair on open positions, net of fees, PLUS "
                             "accrued LIP rewards. LIP is the strategy's primary "
                             "revenue, so it is included in the headline figure."))
    else:
        cols[2].metric("Realized PnL", f"${s['total_realized_pnl']:+.2f}")
    cols[3].metric("Fees", f"${s['total_fees']:.2f}")
    cols[4].metric("Peak |net position|", f"{s['max_abs_net_position']:.0f}")
    cols[5].metric("Tickers traded", s["n_tickers_traded"])


def _render_day(day: DayData, redraw_chart: bool = True):
    _render_metrics(day)
    if day.quote_df.empty:
        st.warning("No decisions recorded for this day (empty debug_log) -- nothing to plot.")
        return

    # One market at a time, never overlaid -- a busy day can trade a dozen-
    # plus buckets, which made the old one-line-per-ticker position panel
    # unreadable.
    #
    # Ordered by how at-the-money the market was, nearest first: mean
    # |prob - 0.5| over its own option-implied series. Fill count (the old
    # ordering) ranks by where the strategy happened to trade, which is an
    # output; ATM-ness ranks by where the action was, which is independent
    # of the strategy and stable across config changes. Markets with no
    # implied series fall to the end rather than jumping to the front on a
    # missing value.
    traded_size = (day.fills_df.groupby("ticker")["size"].sum().to_dict()
                   if not day.fills_df.empty else {})
    market_tickers = _ranked_markets(day)
    market = st.selectbox(
        "Market", market_tickers,
        # Stable key so the selection is remembered BY VALUE across reruns.
        # Without it Streamlit tracks the choice by index, and Live mode's 2s
        # refresh would silently move you to whatever market now sits at that
        # index.
        key=f"market_select_{day.series}_{day.event_ticker}",
        # Contracts, not fill count. A market filled 25 times at 0.01 each is
        # a quarter of one contract, but "25 fills" reads as the busiest
        # market on the board -- thin depth produces a lot of dust.
        format_func=lambda t: f"{t}  ({traded_size.get(t, 0.0):.4g} contracts)",
    )

    quote_df = day.quote_df[day.quote_df["ticker"] == market]
    fills_df = day.fills_df[day.fills_df["ticker"] == market]

    # The FULL, continuous option-implied probability for this one market
    # (see DayData.prob_grid's docstring) -- NOT quote_df's own prob/
    # prob_lo/prob_hi, which only has rows for whenever this market
    # happened to be the strategy's own ATM ticker and can have real
    # multi-minute-plus gaps otherwise (confirmed directly: a naive
    # step-line chart over quote_df's version visually "flatlines" across
    # those gaps even though the option-implied probability itself was
    # being computed the whole time).
    pg = day.prob_grid
    prob_series = pd.DataFrame({
        "time": pg["grid"],
        "prob": pg["probs_mid"].get(market),
        "prob_lo": pg["probs_lo"].get(market),
        "prob_hi": pg["probs_hi"].get(market),
    }).dropna(subset=["prob"])

    st.caption("Drag pans, scroll zooms (both bounded to the data/axis limits); the box-zoom and "
               "reset tools are in the toolbar on the right of each panel.")

    # Re-emit the SAME html string whenever the underlying data hasn't
    # changed, so Streamlit's element diff sees no change and leaves the
    # iframe mounted. Without this the Live fragment (run_every="2s")
    # rebuilt the whole Bokeh document every tick, handing components.html a
    # brand-new ~1.7MB srcdoc each time, which remounts the iframe -- that
    # remount IS the "screen blanks then repaints" flicker, and it also
    # threw away the user's current pan/zoom on every tick.
    #
    # Caching the STRING (not just the figure) is required, not merely an
    # optimization: bokeh.embed.file_html stamps freshly-generated random
    # model ids into every render, so rebuilding from identical data still
    # produces a byte-different document and would still remount.
    fp = (
        day.event_ticker, market, day.source,
        len(quote_df), len(fills_df), len(day.pnl_df),
        str(quote_df["time"].max()) if len(quote_df) else "",
        str(fills_df["time"].max()) if len(fills_df) else "",
        float(day.pnl_df["pnl"].iloc[-1]) if len(day.pnl_df) else 0.0,
        len(prob_series), str(prob_series["time"].max()) if len(prob_series) else "",
        day.config.max_position, day.config.edge_fraction, day.config.constant_edge,
    )
    # `redraw_chart=False` (Live mode's default) additionally holds the LAST
    # rendered chart even when the data has moved on, so the view survives
    # refreshes -- the fingerprint alone can't do that, since live data
    # genuinely changes every tick. Switching market still forces a redraw:
    # holding a chart of a market you're no longer looking at would be wrong,
    # not merely stale.
    stale_ok = (not redraw_chart
                and st.session_state.get("_chart_html") is not None
                and (st.session_state.get("_chart_fp") or (None,))[:2] == fp[:2])
    if not stale_ok and st.session_state.get("_chart_fp") != fp:
        layout = build_dashboard_figure(quote_df, fills_df, day.pnl_df, day.event_ticker, market,
                                         max_position=day.config.max_position, prob_series=prob_series,
                                         market_pnl_df=day.pnl_by_ticker.get(market))
        st.session_state["_chart_fp"] = fp
        st.session_state["_chart_html"] = render_dashboard_html(layout)
    components.html(st.session_state["_chart_html"], height=980, scrolling=True)
    if stale_ok:
        st.caption(f"Chart frozen at last redraw (auto-refresh off) — metrics above are live. "
                   f"Tick 'Auto-refresh chart' or switch market to redraw.")

    with st.expander("Raw fills (this market)"):
        st.dataframe(fills_df, use_container_width=True)


if __name__ == "__main__":
    main()
