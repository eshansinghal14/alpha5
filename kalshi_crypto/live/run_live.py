"""
Live / paper runner for KXCRYPTOLEAD15M.

Wires:
  * Coinbase level2 WS adapter (one connection, all 5 coins)
  * Kalshi WS for the current event's 5 markets (reuses the
    pattern in kalshi_latency_arbs/live/live_kalshi_ws.py)
  * LiveEngine from kalshi_latency_arbs/live/live_context.py in
    shadow mode -- no real orders unless `--live-trading` AND the
    dashboard arms it
  * CompositeBus + CompositeFeed -- 1s samples feed the strategy
    which fires `COMPOSITE_EVENT_KIND` dispatches
  * Snapshot flusher writing to data/live/KXCRYPTOLEAD15M/{date}/
    in the same shape viz/data_loader expects
  * Per-market stream JSONs to viz/static/

Default: PAPER MODE. The `--live-trading` flag arms real orders at
start; the dashboard can arm/disarm it mid-session via
`data/live/KXCRYPTOLEAD15M/trading_mode.json` (same convention as the
SPX runner). We default `live_trading=False` on the LiveEngine
regardless, so the normal operator flow is "run this script, open the
dashboard, verify, arm from dashboard".

Usage:
    python -m kalshi_crypto.live.run_live
    python -m kalshi_crypto.live.run_live --event-ticker KXCRYPTOLEAD15M-26OCT061445
    python -m kalshi_crypto.live.run_live --live-trading   # REAL money, use with care
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import functools
import json
import os
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

# sys.path shims so we can be run from the project root as
# `python -m kalshi_crypto.live.run_live` without an installed package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_KC = os.path.abspath(os.path.join(_HERE, ".."))
_LIVE = os.path.join(_PROJECT, "kalshi_latency_arbs", "live")
_BACKTEST = os.path.join(_PROJECT, "kalshi_backtest")
_INFRA = os.path.join(_PROJECT, "kalshi_infra")
for _p in (_PROJECT, _KC, _LIVE, _BACKTEST, _INFRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kalshi_crypto.config import (CoinRaceConfig,
                                   CRYPTO_FEE_MULTIPLIER,
                                   CRYPTO_MAKER_FEE_MULTIPLIER,
                                   EDITABLE_FIELDS)
from kalshi_crypto.composite import CompositeBus
from kalshi_crypto.cov_forecaster import ConstantCovForecaster
from kalshi_crypto.robust_har import RobustHarForecaster
from kalshi_crypto.event_state import (build_event_state,
                                        resolve_live_event_ticker,
                                        discover_open_events)
from kalshi_crypto.pricer import BaseRateTracker
from kalshi_crypto.backtest.run_backtest import load_composite_bars
from kalshi_crypto.spot_feed.coinbase import CoinbaseAdapter
from kalshi_crypto.strategy import CoinRaceStrategy, COMPOSITE_EVENT_KIND
from kalshi_crypto.series import SERIES_TICKER, COINS
from kalshi_crypto.live.kalshi_feed import LiveCryptoKalshiFeed
from kalshi_crypto.live.persist import (
    write_summary, write_quote_df, write_fills_df, write_pnl_df,
    write_prob_grid, write_stream_json_per_market, reset_session_files,
)

from live_context import LiveEngine
from live_orders import KalshiOrderClient, DEFAULT_MAX_ORDERS_PER_S


FLUSH_INTERVAL_S = 3.0
# Trailing history replayed into the forecaster at session start: 1h RV
# window + 1h correlation seed + slack.
FORECASTER_WARMUP_S = 4 * 3600.0
HEAVY_FLUSH_EVERY = 10
COMPOSITE_FLUSH_INTERVAL_S = 30.0

# Cadence of composite -> strategy dispatch. CF publishes 1/s so
# anything below that is wasted CPU; 500ms matches a comfortable viz
# refresh.
COMPOSITE_TICK_INTERVAL_S = 0.5

# Config override polling (dashboard writes this file; we read it).
CONFIG_POLL_INTERVAL_S = 1.0
TRADING_MODE_POLL_INTERVAL_S = 1.0

# Same cadences as the SPX runner (kalshi_latency_arbs/live/run_live.py).
# The fill poll must stay within audit_positions' one-interval tolerance for
# resting size, or a quote fill the poll hasn't booked yet reads as a
# ledger mismatch and halts.
AUDIT_INTERVAL_S = 5.0
QUOTE_FILL_POLL_S = 1.0


def _live_dir_for(date: str) -> Path:
    return Path("data") / "live" / SERIES_TICKER / date


def _config_override_path() -> Path:
    return Path("data") / "live" / SERIES_TICKER / "config_override.json"


def _trading_mode_path() -> Path:
    return Path("data") / "live" / SERIES_TICKER / "trading_mode.json"


async def composite_dispatch_loop(bus: CompositeBus, engine: LiveEngine):
    """Fire a composite_tick dispatch at COMPOSITE_TICK_INTERVAL_S.
    This is what drives the strategy's main reprice loop."""
    while True:
        now = pd.Timestamp.now(tz="UTC")
        payload = {"kind": COMPOSITE_EVENT_KIND, "ts": now.timestamp()}
        engine.dispatch(payload, now)
        await asyncio.sleep(COMPOSITE_TICK_INTERVAL_S)


async def composite_flush_loop(bus: CompositeBus):
    while True:
        try:
            n = bus.flush_all()
            if n > 0:
                print(f"[kalshi_crypto] composite flush: {n} rows")
        except Exception as e:
            print(f"[kalshi_crypto] composite flush failed: {e!r}")
        await asyncio.sleep(COMPOSITE_FLUSH_INTERVAL_S)


async def snapshot_loop(engine: LiveEngine, strategy: CoinRaceStrategy,
                        bus: CompositeBus, date: str):
    """Flush summary / parquet / per-market stream JSON on a timer."""
    live_dir = _live_dir_for(date)
    live_dir.mkdir(parents=True, exist_ok=True)
    # Heavy flush on the FIRST cycle, not the 10th: the viz's load_live_day
    # requires all four parquets to exist and raises FileNotFoundError
    # otherwise, which the live-mode render treats as "no session running"
    # (observed 2026-10-06). Writing them up front even when empty keeps
    # the dashboard pointed at the right process from the first poll.
    cycle = 0
    heavy: asyncio.Future | None = None
    # The viz shows "no decisions yet" until quote_df has a row. The cycle-1
    # flush is almost always empty (the strategy hasn't dispatched yet), so
    # keep flushing every cycle until a non-empty one lands; otherwise the
    # dashboard waits out a full HEAVY_FLUSH_EVERY interval (~30s) per event.
    flushed_rows = False
    while True:
        try:
            cycle += 1
            summary = _build_summary(engine, strategy, bus)
            write_summary(live_dir / "summary.json", summary)
            due = cycle == 1 or not flushed_rows or cycle % HEAVY_FLUSH_EVERY == 0
            if due and (heavy is None or heavy.done()):
                flushed_rows = flushed_rows or bool(strategy.debug_log)
                # Off the event loop. With a full event's decision log this
                # is ~4.7s of pandas/JSON work (measured 2026-10-08, 78k
                # rows); run inline it froze Coinbase and Kalshi processing
                # for 5-9s at a time, worst near expiry when the log is
                # largest. Snapshot the lists first so the worker never sees
                # them mutate. Skipped if the previous flush is still running.
                snap = (list(strategy.debug_log),
                        {k: list(v) for k, v in strategy._market_log.items()},
                        list(engine.portfolio.fills),
                        _derive_pnl_history(engine, strategy))
                heavy = asyncio.get_running_loop().run_in_executor(
                    None, _heavy_flush_snapshot, snap, live_dir)
        except Exception as e:
            print(f"[kalshi_crypto] snapshot failed: {e!r}")
            traceback.print_exc()
        await asyncio.sleep(FLUSH_INTERVAL_S)


def _build_summary(engine: LiveEngine, strategy: CoinRaceStrategy,
                   bus: CompositeBus) -> dict:
    """Scalar session state. Published every FLUSH_INTERVAL_S.

    Schema matches kalshi_latency_arbs/live/live_persist._build_summary
    key-for-key: the dashboard (viz/app.py) reads the same fields across
    both strategies, and a missing key here crashes _render_metrics
    with a KeyError rather than falling back to a default (confirmed
    2026-10-06 on `n_fills`). Keep the superset flat; crypto-specific
    extras (fairs, lip, phase) ride alongside the shared schema."""
    es = strategy.event_state
    now = pd.Timestamp.now(tz="UTC")
    snap_settle = es.tracker.snapshot(now.timestamp())
    fr = strategy._last_fair_result
    fills = engine.portfolio.fills
    tickers_traded = {f.ticker for f in fills}
    cfg = strategy.config
    out = {
        # --- shared SPX schema (used by _render_metrics / _render_live) ---
        "event_ticker": es.event_ticker,
        "series": SERIES_TICKER,
        "n_fills": len(fills),
        "total_realized_pnl": float(engine.portfolio.total_realized_pnl()),
        "total_fees": float(engine.portfolio.total_fees()),
        "n_tickers_traded": len(tickers_traded),
        "max_abs_net_position": _max_abs_net_position(fills),
        "last_update": now.isoformat(),
        "session_started": engine.session_started,
        # Config fields the viz reads directly for chart y-ranges and
        # edge-band reconstruction. Same keys the SPX summary provides.
        "edge_fraction": cfg.edge_fraction,
        "max_position": cfg.max_position,
        "constant_edge": cfg.constant_edge,
        "lean_multiplier": cfg.lean_multiplier,
        "lean_dollars": cfg.lean_dollars,
        "enable_quoting": cfg.enable_quoting,
        "quoting_edge": cfg.quoting_edge,
        "quote_size": cfg.quote_size,
        "requote_delay_s": cfg.requote_delay_s,
        "max_improve_ticks": cfg.max_improve_ticks,
        # Trading / guard state.
        "halted": bool(engine.halted),
        "halt_reason": engine.halt_reason,
        "max_loss_usd": float(engine.max_loss_usd),
        "max_signal_age_s": float(engine.max_signal_age_s),
        "blocked_reason": engine.blocked_reason(),
        "blocked_counts": dict(engine.blocked_counts),
        "signal_age_s": engine.signal_age_s() if callable(getattr(engine, "signal_age_s", None)) else None,
        "feed_age_s": engine.feed_age_s() if callable(getattr(engine, "feed_age_s", None)) else None,
        "applied_age_s": engine.applied_age_s() if callable(getattr(engine, "applied_age_s", None)) else None,
        "bias": dict(engine.bias),
        "over_limit": dict(engine.over_limit),
        "consecutive_order_errors": engine.consecutive_order_errors,
        "close_time": engine.close_time.isoformat() if engine.close_time is not None else None,
        "live_trading": bool(engine.live_trading),
        "n_real_orders": engine.n_real_orders,
        "n_real_fills": engine.n_real_fills,
        "n_real_order_errors": engine.real_order_errors,
        "last_reconcile": engine.last_reconcile,
        "balance_at_arm": engine.balance_at_arm,
        "last_audit": engine.last_audit,
        # --- Coin Race-specific extras (safe to add; viz reads via .get) ---
        "now_utc": now.isoformat(),
        "tau_s": snap_settle.tau,
        "phase": snap_settle.phase,
        "start_quality": snap_settle.start_quality,
        "fairs": None if fr is None else {c: float(fr.probs[i]) for i, c in enumerate(COINS)},
        "theo_vol": None if fr is None else {c: float(fr.theo_vol[i]) for i, c in enumerate(COINS)},
        "feed_health": strategy.feed_health.snapshot(),
        "vol_model": (strategy.forecaster.diagnostics()
                      if hasattr(strategy.forecaster, "diagnostics") else None),
        "pricer_sd": (None if fr is None else
                      {c: float(v) for c, v in zip(COINS, fr.diagnostics.get("sd", []))}),
        "tau_eff": None if fr is None else float(fr.tau_eff),
        "theo_stale": bool(strategy.theo_stale()),
        "real_trading_cutoff_min": float(cfg.real_trading_cutoff_min),
        "real_cutoff_hit": bool(getattr(strategy, "real_cutoff_hit", False)),
        "pnl": {
            "realized": float(engine.portfolio.total_realized_pnl()),
            "fees": float(engine.portfolio.total_fees()),
            # LIP is the strategy's primary revenue; surface it in the pnl
            # block AND in the total so the headline figure isn't just the
            # trading residual.
            "lip_accrued": _total_lip_accrued(strategy),
            "total_incl_lip": (float(engine.portfolio.total_realized_pnl())
                               - float(engine.portfolio.total_fees())
                               + _total_lip_accrued(strategy)),
        },
        "total_lip_accrued_usd": _total_lip_accrued(strategy),
        "lip_accrued_usd": dict(strategy._lip_accrued_usd),
        "lip_qualifying_fraction": {
            t: (strategy._lip_snapshots_qualified[t] / strategy._lip_snapshots_total[t]
                if strategy._lip_snapshots_total[t] > 0 else 0.0)
            for t in strategy._lip_snapshots_total
        },
        "composite_bars_in_memory": sum(len(f._bars) for f in bus.feeds.values()) if hasattr(bus, "feeds") else 0,
        "editable_config": {k: getattr(strategy.config, k) for k in EDITABLE_FIELDS},
    }
    return out


def _max_abs_net_position(fills) -> float:
    """Peak |yes_qty - no_qty| across all markets touched this session.
    Mirrors live_persist._max_abs_net_position but computed off the fill
    list rather than a DataFrame -- same semantics."""
    if not fills:
        return 0.0
    per_ticker: dict[str, float] = {}
    peak = 0.0
    for f in sorted(fills, key=lambda x: x.time):
        delta = f.size if f.side == "yes" else -f.size
        per_ticker[f.ticker] = per_ticker.get(f.ticker, 0.0) + delta
        peak = max(peak, abs(per_ticker[f.ticker]))
    return peak


def _heavy_flush_snapshot(snap, live_dir: Path):
    """Worker-thread heavy flush from copied state (see snapshot_loop)."""
    debug_log, market_log, fills, pnl = snap
    try:
        write_quote_df(live_dir / "quote_df.parquet", debug_log)
        write_fills_df(live_dir / "fills_df.parquet", fills)
        write_pnl_df(live_dir / "pnl_df.parquet", pnl)
        write_prob_grid(live_dir / "prob_grid.parquet", debug_log)
        static_dir = Path(_PROJECT) / "viz" / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        write_stream_json_per_market(static_dir, SERIES_TICKER, market_log, fills)
    except Exception as e:
        print(f"[kalshi_crypto] heavy flush failed: {e!r}")


def _heavy_flush(engine: LiveEngine, strategy: CoinRaceStrategy, live_dir: Path):
    write_quote_df(live_dir / "quote_df.parquet", strategy.debug_log)
    write_fills_df(live_dir / "fills_df.parquet", engine.portfolio.fills)
    # pnl_df and prob_grid are populated from debug_log + portfolio
    # fees; no separate pnl_history list exists on this strategy yet, so
    # the pnl curve is derived from cumulative fees + unrealized PnL.
    # Good enough for the dashboard to render SOMETHING; richer MTM
    # curve is a followup.
    write_pnl_df(live_dir / "pnl_df.parquet", _derive_pnl_history(engine, strategy))
    write_prob_grid(live_dir / "prob_grid.parquet", strategy.debug_log)
    # Per-market stream JSONs for viz/static/.
    static_dir = Path(_PROJECT) / "viz" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    write_stream_json_per_market(static_dir, SERIES_TICKER,
                                  strategy._market_log,
                                  engine.portfolio.fills)


def _total_lip_accrued(strategy: CoinRaceStrategy) -> float:
    """Sum of expected LIP reward accrued across all 5 markets this event
    (dollars). This is a real, earned component of the strategy's P&L --
    the whole point of the strategy is the LIP program -- so it belongs in
    the headline PnL, not just as a side metric."""
    return float(sum(strategy._lip_accrued_usd.values()))


def _derive_pnl_history(engine: LiveEngine,
                        strategy: CoinRaceStrategy) -> list[tuple]:
    """Running PnL curve = trading PnL (fills marked to the current fair,
    net of fees) PLUS cumulative LIP accrual.

    LIP is the dominant intended revenue source, so it is folded directly
    into the curve rather than tracked beside it. The fill mark uses the
    strategy's latest per-coin fair (`_last_fair_result`): a filled YES at
    price p is worth (fair - p) per contract right now; a NO fill is worth
    ((1-fair) - p_no). Positions still open at event end converge to the
    settlement outcome, which the fair already estimates.

    LIP is accrued per-second in `strategy._lip_accrued_usd` (cumulative);
    the per-row `lip_accrued_usd` in debug_log carries its time path, so
    the curve steps up with it. Here we attach the FINAL cumulative LIP to
    the last point and interpolate the trading part at fill times -- good
    enough for the live dashboard; a fully time-resolved LIP curve would
    read debug_log, which _heavy_flush already persists."""
    fr = strategy._last_fair_result
    fair_by_coin = None
    if fr is not None:
        fair_by_coin = {c: float(fr.probs[i]) for i, c in enumerate(COINS)}

    pts = []
    cum = 0.0
    for f in sorted(engine.portfolio.fills, key=lambda x: x.time):
        cum -= f.fee
        # Mark the fill to the current fair, if we have one.
        if fair_by_coin is not None:
            from kalshi_crypto.series import coin_from_market_ticker
            coin = coin_from_market_ticker(f.ticker)
            if coin is not None:
                fair = fair_by_coin[coin]
                if f.side == "yes":
                    cum += (fair - f.price) * f.size
                else:   # no-side fill: pays off (1 - fair)
                    cum += ((1.0 - fair) - f.price) * f.size
        pts.append((f.time, cum))

    # Fold in cumulative LIP accrual. Attach it to a final point at the
    # latest known time so the headline PnL reflects it even before any
    # fill (LIP accrues from resting size, independent of fills).
    lip = _total_lip_accrued(strategy)
    now = pd.Timestamp.now(tz="UTC")
    if lip != 0.0 or not pts:
        pts.append((now, cum + lip))
    else:
        pts[-1] = (pts[-1][0], pts[-1][1] + lip)
    return pts


_SAVED_CONFIG_MTIME = 0.0


def load_saved_config() -> CoinRaceConfig:
    """CoinRaceConfig with the dashboard's last-applied values as defaults.

    config_override.json is the persisted store: every dashboard Apply
    writes it, and a restart starts from it instead of the dataclass
    defaults. Only EDITABLE_FIELDS are taken. Records the file's mtime so
    config_override_loop applies only LATER edits (CLI flags given at
    startup then survive until the next dashboard change)."""
    global _SAVED_CONFIG_MTIME
    cfg = CoinRaceConfig()
    path = _config_override_path()
    if path.exists():
        saved = _read_request_json(path) or {}
        applied = {}
        for k, v in saved.items():
            if k in EDITABLE_FIELDS and hasattr(cfg, k):
                v = _normalise_field(k, v)
                setattr(cfg, k, v)
                applied[k] = v
        _SAVED_CONFIG_MTIME = path.stat().st_mtime
        if applied:
            print(f"[kalshi_crypto] loaded saved dashboard config: {applied}")
    return cfg


def _normalise_field(k: str, v):
    # The dashboard widget spells "uncapped" as 0; the strategy spells it
    # None (0 would mean "may only join the best bid, never improve").
    if k == "max_improve_ticks" and not v:
        return None
    return v


def _read_request_json(path: Path) -> dict | None:
    """Read a dashboard request file. Returns None for an empty/partial file
    (caught mid-write by a writer that isn't atomic) so the caller retries
    on the next poll WITHOUT advancing last_mtime -- otherwise the request
    would be skipped for good."""
    try:
        txt = path.read_text()
    except OSError:
        return None
    if not txt.strip():
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return None


async def config_override_loop(strategy: CoinRaceStrategy):
    path = _config_override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The saved file was already applied at startup (load_saved_config), and
    # the config object is shared across events, so only newer edits apply.
    last_mtime = _SAVED_CONFIG_MTIME
    while True:
        try:
            if path.exists():
                m = path.stat().st_mtime
                overrides = _read_request_json(path) if m > last_mtime else None
                if overrides is not None:
                    strategy.apply_config_override(
                        {k: _normalise_field(k, v) for k, v in overrides.items()})
                    last_mtime = m
        except Exception as e:
            print(f"[kalshi_crypto] config override read failed: {e!r}")
        await asyncio.sleep(CONFIG_POLL_INTERVAL_S)


async def trading_mode_loop(engine: LiveEngine, strategy: CoinRaceStrategy | None = None):
    path = _trading_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    last_mtime = 0.0
    while True:
        try:
            # Real-money cutoff before expiry. Disarms REAL orders only
            # (set_live_trading(False) cancels resting real orders); the
            # shadow engine keeps simulating to the close. The dashboard
            # toggle is left as-is, so the next event re-arms from it.
            if strategy is not None and engine.live_trading:
                cut = float(getattr(strategy.config, "real_trading_cutoff_min", 0.0) or 0.0)
                tau = strategy.event_state.window.T - time.time()
                if cut > 0 and tau <= cut * 60.0:
                    engine.set_live_trading(False)
                    strategy.real_cutoff_hit = True
                    print(f"[kalshi_crypto] real trading OFF: {tau:.0f}s to expiry <= "
                          f"cutoff {cut:g} min; paper trading continues")
            if path.exists():
                m = path.stat().st_mtime
                state = _read_request_json(path) if m > last_mtime else None
                if state is not None:
                    armed = bool(state.get("live_trading", False))
                    if armed and strategy is not None and getattr(strategy, "real_cutoff_hit", False):
                        # Inside the cutoff window: honour the cutoff over a
                        # re-arm until the next event.
                        armed = False
                    if hasattr(engine, "set_live_trading"):
                        try:
                            actual = engine.set_live_trading(armed)
                            actual = bool(engine.live_trading) if actual is None else bool(actual)
                            print(f"[kalshi_crypto] trading mode requested armed={armed}, "
                                  f"in effect live_trading={actual}"
                                  + ("" if actual == armed else
                                     "  (NOT armed: see the order-client / reconcile messages above)"))
                        except Exception as e:
                            print(f"[kalshi_crypto] set_live_trading failed: {e!r}")
                    last_mtime = m
        except Exception as e:
            print(f"[kalshi_crypto] trading mode read failed: {e!r}")
        await asyncio.sleep(TRADING_MODE_POLL_INTERVAL_S)


async def quote_fill_loop(engine: LiveEngine, event_ticker: str):
    """Book fills on RESTING real quotes. Unlike a take, a resting fill
    has no create-order response to read it from, so without this poll
    it never reaches the ledger and the position cap runs blind."""
    while True:
        await asyncio.sleep(QUOTE_FILL_POLL_S)
        if not engine.live_trading or not engine.exchange_to_order:
            continue
        try:
            fills = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(engine.order_client.fills, event_ticker,
                                        min_ts=engine.fill_poll_min_ts()))
            engine.apply_real_fills(fills)
        except Exception:
            print("[kalshi_crypto] WARN: quote fill poll failed:")
            traceback.print_exc()


async def position_audit_loop(engine: LiveEngine, event_ticker: str):
    """Reconcile the ledger against Kalshi's positions; halts on mismatch.
    The one guard that doesn't trust our own fill accounting."""
    while True:
        await asyncio.sleep(AUDIT_INTERVAL_S)
        if not engine.live_trading:
            continue
        try:
            real = await asyncio.get_running_loop().run_in_executor(
                None, engine.order_client.positions, event_ticker)
            engine.audit_positions(real)
        except Exception:
            print("[kalshi_crypto] WARN: position audit failed:")
            traceback.print_exc()


async def composite_sampling_loop(bus: CompositeBus):
    """Pin the per-second CF Spot Rate for every coin. Independent of
    the dispatch loop; just advances the second-boundary clock."""
    while True:
        now_s = time.time()
        try:
            bus.sample_all(now_s)
        except Exception as e:
            print(f"[kalshi_crypto] composite sampling error: {e!r}")
        # Target: aligned to one-second boundaries so bars line up with
        # Kalshi's own second-aligned snapshots.
        next_s = int(now_s) + 1
        await asyncio.sleep(max(0.0, next_s - now_s + 0.02))


def _backfill_tracker(es, forecaster) -> dict[str, str]:
    """Seed the SettlementTracker (and the forecaster's RV windows) from
    PERSISTED 1s composite bars when we adopt an event mid-window.

    Without this, a session started after the event's start TWAP window
    `[S-60, S)` has no bars for it, so `start_quality` is "missing" for
    every coin, `start_locked` is never True, the strategy feeds the pricer
    x = I = 0, and the fair collapses to a pure variance-ranking prior that
    ignores where each coin actually is -- which showed up live as a theo
    13-19c off the Kalshi mid, e.g. BTC priced 0.17 while the market said
    0.035 (observed 2026-10-07). The composite collector persists bars
    continuously, so the start window is almost always already on disk; we
    just have to load it. Returns the per-coin start_quality after seeding,
    for the startup log."""
    now_s = pd.Timestamp.now(tz="UTC").timestamp()
    bars_by_coin = {}
    for coin in COINS:
        bars_by_coin[coin] = load_composite_bars(
            coin, now_s - FORECASTER_WARMUP_S, es.window.T)
    es.tracker.backfill({c: [b for b in bl if b.ts_s >= es.window.S - 120]
                         for c, bl in bars_by_coin.items()})
    # Warm the forecaster on the trailing FORECASTER_WARMUP_S of mids: it
    # needs a full hour for the 1h RV feature and another for the
    # correlation EWMA seed, else it prices off training-mean vol and the
    # prior correlation.
    all_bars = sorted(
        ((b.ts_s, c, b.mid) for c, bars in bars_by_coin.items() for b in bars),
        key=lambda t: t[0])
    # Group by second so one update() carries all coins present that second.
    by_s: dict[int, dict[str, float]] = {}
    for ts_s, coin, price in all_bars:
        by_s.setdefault(int(ts_s), {})[coin] = price
    for ts_s in sorted(by_s):
        try:
            forecaster.update(float(ts_s), by_s[ts_s])
        except NotImplementedError:
            break
    snap = es.tracker.snapshot(pd.Timestamp.now(tz="UTC").timestamp())
    return dict(snap.start_quality)


async def run(event_ticker: str, config: CoinRaceConfig,
              live_trading: bool, max_orders_per_s: float):
    es = build_event_state(event_ticker, config)
    date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    live_dir = _live_dir_for(date)
    reset_session_files(live_dir)
    print(f"[kalshi_crypto] starting session: event={event_ticker} "
          f"S={es.window.S:.0f} T={es.window.T:.0f} "
          f"live_trading={live_trading}")

    bus = CompositeBus(coins=COINS, persist=True)
    coinbase = CoinbaseAdapter(coins=list(COINS))
    bus.deep_book_fn = coinbase.deep_book

    # Robust HAR on the venue mid (rv_modeling/vol_fix_reference). Warmed
    # from persisted bars in _backfill_tracker below.
    forecaster = RobustHarForecaster()
    base_rate_tracker = BaseRateTracker(config.base_rate_window)
    strategy = CoinRaceStrategy(
        event_state=es, config=config,
        forecaster=forecaster,
        base_rate_tracker=base_rate_tracker,
        record_debug=True,
    )
    strategy.attach_composite(bus)

    # Backfill the start-TWAP window (and the forecaster's RV windows) from
    # persisted composite bars BEFORE the first dispatch, so an event
    # adopted mid-window prices off real returns instead of a variance
    # prior. See _backfill_tracker.
    try:
        sq = _backfill_tracker(es, forecaster)
        n_ok = sum(1 for v in sq.values() if v in ("ok", "partial"))
        print(f"[kalshi_crypto] start-TWAP backfill: {sq} "
              f"({n_ok}/{len(sq)} coins locked)")
    except Exception as e:
        print(f"[kalshi_crypto] start-TWAP backfill failed ({e!r}); "
              f"event will price off the variance prior until S is re-reached")

    # LiveEngine from the SPX live stack. We reuse it unmodified: it
    # expects a `strategy` object with .__call__, .live_quote_order_ids,
    # .clear_quotes, .drop_quote -- all of which CoinRaceStrategy
    # provides.
    # Built unconditionally (no connection is opened on construction), same
    # as the SPX runner, so the dashboard's real-trading checkbox can arm
    # without a restart. Building it only under --live-trading left the
    # checkbox unable to arm ("no order client configured"). Whether real
    # orders are SENT is still decided by engine.live_trading.
    try:
        order_client = KalshiOrderClient(max_orders_per_s=max_orders_per_s)
    except Exception as e:
        order_client = None
        print(f"[kalshi_crypto] WARN: could not build the Kalshi order client ({e!r}); "
              f"real trading cannot be armed this session")
    engine = LiveEngine(
        strategy=strategy,
        fee_multiplier=CRYPTO_FEE_MULTIPLIER,
        maker_fee_multiplier=CRYPTO_MAKER_FEE_MULTIPLIER,
        order_client=order_client,
        event_ticker=event_ticker,
        close_time=pd.Timestamp(es.window.T, unit="s", tz="UTC"),
    )
    cut_s = float(config.real_trading_cutoff_min or 0.0) * 60.0
    inside_cutoff = cut_s > 0 and (es.window.T - time.time()) <= cut_s
    if live_trading and inside_cutoff:
        strategy.real_cutoff_hit = True
        print(f"[kalshi_crypto] adopted inside the {config.real_trading_cutoff_min:g}-min "
              f"real-trading cutoff: paper only for this event")
    elif live_trading and hasattr(engine, "set_live_trading"):
        try:
            engine.set_live_trading(True)
        except Exception as e:
            print(f"[kalshi_crypto] WARN: failed to arm real trading: {e!r}")

    # Kalshi feed for the 5 event markets. LiveEngine.update_book
    # caches the book and `dispatch` fires the strategy for its
    # last_event; same shape as the SPX runner at
    # kalshi_latency_arbs/live/run_live.py:310.
    def on_quote(book):
        now = pd.Timestamp.now(tz="UTC")
        engine.update_book(book)
        engine.dispatch(book, now)

    def on_trade(ticker, trade):
        now = pd.Timestamp.now(tz="UTC")
        engine.on_trade(ticker, trade, now)
        engine.dispatch(trade, now)

    kalshi_feed = LiveCryptoKalshiFeed(
        event_ticker=event_ticker,
        tickers=list(es.tickers.values()),
        on_quote=on_quote,
        on_trade=on_trade,
    )

    # Route Coinbase updates into the composite bus.
    def on_coinbase_update(vb):
        bus.observe(vb)

    tasks = [
        asyncio.create_task(coinbase.run(on_coinbase_update)),
        asyncio.create_task(kalshi_feed.run()),
        asyncio.create_task(composite_sampling_loop(bus)),
        asyncio.create_task(composite_dispatch_loop(bus, engine)),
        asyncio.create_task(composite_flush_loop(bus)),
        asyncio.create_task(snapshot_loop(engine, strategy, bus, date)),
        asyncio.create_task(config_override_loop(strategy)),
        asyncio.create_task(trading_mode_loop(engine, strategy)),
        asyncio.create_task(quote_fill_loop(engine, event_ticker)),
        asyncio.create_task(position_audit_loop(engine, event_ticker)),
    ]

    try:
        # Hand over AT close: the next event opens at this one's T, and
        # settlement needs nothing after T (the end average is [T-60, T)).
        # A 60s grace here was most of the delay before the next event
        # appeared in the viz.
        close_at = es.window.T + CLOSE_GRACE_S
        while time.time() < close_at:
            await asyncio.sleep(1.0)
        print(f"[kalshi_crypto] event {event_ticker} closed; shutting down")
    except KeyboardInterrupt:
        print("[kalshi_crypto] interrupted")
    finally:
        for t in tasks:
            t.cancel()
        # Before awaiting the cancelled tasks, so nothing reprices in between.
        # cancel_resting also sweeps orders this process lost track of.
        if engine.live_trading:
            try:
                engine.pull_quotes()
                engine.cancel_resting()
            except Exception:
                print(f"[kalshi_crypto] WARN: could not pull quotes on shutdown -- CHECK KALSHI "
                      f"BY HAND for resting orders on {event_ticker}:")
                traceback.print_exc()
            # The fill poll was just cancelled; one last read so a quote fill
            # in the final second still reaches the ledger.
            try:
                if engine.exchange_to_order:
                    engine.apply_real_fills(engine.order_client.fills(
                        event_ticker, min_ts=engine.fill_poll_min_ts()))
            except Exception as e:
                print(f"[kalshi_crypto] WARN: final fill poll failed: {e!r}")
        await asyncio.gather(*tasks, return_exceptions=True)
        # Final write of persisted bars + a last summary.
        bus.flush_all()
        try:
            summary = _build_summary(engine, strategy, bus)
            write_summary(live_dir / "summary.json", summary)
            _heavy_flush(engine, strategy, live_dir)
        except Exception as e:
            print(f"[kalshi_crypto] final flush failed: {e!r}")


# Rollover polling: Coin Race events roll every 15 minutes, so a longer
# idle poll than the SPX runner's would starve the loop. Still bounded
# below by Kalshi's listing cadence -- querying faster than that just
# costs REST round trips.
ROLLOVER_POLL_S = 30.0
# Right after an event ends the next one is due within seconds, so poll
# fast for a while instead of waiting out a full ROLLOVER_POLL_S.
ROLLOVER_FAST_POLL_S = 2.0
ROLLOVER_FAST_WINDOW_S = 120.0
CLOSE_GRACE_S = 1.0


async def session_loop(config: CoinRaceConfig, live_trading: bool,
                       max_orders_per_s: float, carry_arm: bool = True,
                       event_ticker_override: str | None = None):
    """Trade one event, then the next. One fresh engine/strategy per
    event. Mirrors the SPX runner's session_loop so the dispatcher in
    kalshi_main/run_live.py sees the same shape across series."""
    armed = live_trading
    last_event = None
    last_ended = 0.0

    def _poll_s() -> float:
        if time.monotonic() - last_ended < ROLLOVER_FAST_WINDOW_S:
            return ROLLOVER_FAST_POLL_S
        return ROLLOVER_POLL_S

    while True:
        try:
            event_ticker = resolve_live_event_ticker(event_ticker_override)
        except Exception as e:
            print(f"[kalshi_crypto] no open {SERIES_TICKER} event ({e!r}); "
                  f"retrying in {_poll_s():.0f}s")
            await asyncio.sleep(_poll_s())
            continue
        if event_ticker == last_event:
            await asyncio.sleep(_poll_s())
            continue
        last_event = event_ticker
        print(f"[kalshi_crypto] trading event {event_ticker}"
              + ("  (carrying REAL TRADING over from previous event)"
                 if armed and carry_arm else ""))
        try:
            await run(event_ticker, config,
                      live_trading=armed and carry_arm,
                      max_orders_per_s=max_orders_per_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            print(f"[kalshi_crypto] {event_ticker} failed; retrying after "
                  f"{ROLLOVER_POLL_S:.0f}s:")
            traceback.print_exc()
            last_event = None
            await asyncio.sleep(ROLLOVER_POLL_S)
            continue
        # If --event-ticker was explicit, don't roll onto the next one --
        # a named event is a one-off, same semantics as SPX's own
        # session_loop at kalshi_latency_arbs/live/run_live.py:203.
        if event_ticker_override:
            return
        last_ended = time.monotonic()
        armed = armed and carry_arm   # carry across rollover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-ticker", default=None,
                        help="Specific event (else: soonest-closing open event). "
                             "Naming an event disables rollover -- it is a one-off.")
    parser.add_argument("--live-trading", action="store_true",
                        help="Arm REAL orders on start. Without this: paper mode.")
    parser.add_argument("--max-orders-per-s", type=float,
                        default=DEFAULT_MAX_ORDERS_PER_S)
    parser.add_argument("--no-carry-arm", action="store_true",
                        help="At event rollover, drop back to shadow instead of "
                             "carrying armed real-trading forward.")
    # Direct exposes of the most-commonly-tuned config fields.
    parser.add_argument("--constant-edge", type=float, default=None)
    parser.add_argument("--edge-fraction", type=float, default=None)
    parser.add_argument("--ci-vol-multiplier", type=float, default=None)
    parser.add_argument("--quote-size", type=float, default=None)
    parser.add_argument("--enable-quoting", action="store_true")
    parser.add_argument("--max-position", type=float, default=None)
    args = parser.parse_args()
    _run_from_args(args)


def _run_from_args(args):
    """Shared tail used by both main() and the kalshi_main dispatcher,
    which parses a superset of these flags."""
    import traceback  # noqa: F401 -- session_loop uses it; keep import local

    config = load_saved_config()
    for arg, field in [("constant_edge", "constant_edge"),
                       ("edge_fraction", "edge_fraction"),
                       ("ci_vol_multiplier", "ci_vol_multiplier"),
                       ("quote_size", "quote_size"),
                       ("max_position", "max_position")]:
        v = getattr(args, arg, None)
        if v is not None:
            setattr(config, field, v)
    if getattr(args, "enable_quoting", False):
        config.enable_quoting = True

    asyncio.run(session_loop(
        config=config,
        live_trading=args.live_trading,
        max_orders_per_s=args.max_orders_per_s,
        carry_arm=not getattr(args, "no_carry_arm", False),
        event_ticker_override=args.event_ticker,
    ))


if __name__ == "__main__":
    main()
