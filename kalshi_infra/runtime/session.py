"""
Live session for ONE series: trade the current event, then the next,
forever. Series-agnostic -- the series contributes only its config and an
EventModel (see runtime.series).

Per event:
    model feeds  ->  wakeup  ->  model.reprice  ->  FairUpdate dispatch  ->  Quoter  ->  LiveEngine
    Kalshi ws    ->  book/trade dispatch (same Quoter, re-evaluates that market)
plus background loops: config hot-reload, arm/disarm, position audit,
quote-fill poll, snapshot flush.

Rollover is scheduled, not polled: Kalshi lists upcoming events with their
open_time/close_time, so the session sleeps to the next event's start and
runs each event until its close_time.

Modes: shadow (default) simulates fills; real sends orders. Arming
reconciles positions against Kalshi first; disarming pulls every quote.
"""
from __future__ import annotations

import asyncio
import collections
import functools
import sys
import time
import traceback

import pandas as pd

from kalshi_infra.config import ConfigStore
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.kalshi import markets as km
from kalshi_infra.kalshi.orders import KalshiOrderClient
from kalshi_infra.kalshi.ws_feed import LiveKalshiFeed
from kalshi_infra.runtime import journal as jr
from kalshi_infra.runtime import persist
from kalshi_infra.runtime.control import TradingModeWatcher
from kalshi_infra.runtime.series import Series
from kalshi_infra.trading import lip as lipmod
from kalshi_infra.trading.engine import LiveEngine
from kalshi_infra.trading.fair import FairUpdate
from kalshi_infra.trading.params import check_strategy_config
from kalshi_infra.trading.quoter import Quoter


FLUSH_INTERVAL_S = 3.0      # summary.json; a heavy flush measured p95 ~640ms (2026-10-09), so not per update
HEAVY_FLUSH_EVERY = 10      # every Nth flush also rewrites the parquets and chart streams
MAX_DEBUG_ROWS = 20_000     # heavy-flush cost scales with this; the oldest half is thinned past it
AUDIT_INTERVAL_S = 5.0
REPRICE_IDLE_S = 1.0        # reprice even without new inputs, so time decay and withhold states advance
CLOSE_GRACE_S = 1.0
NO_EVENT_RETRY_S = 300.0    # nothing listed yet (e.g. KXINX lists the next day's event after the close)
RETRY_S = 30.0              # failed lookup or failed event


def _log(series: str, msg: str):
    print(f"[{series}] {msg}", flush=True)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _prune(seq: list, cap: int | None):
    """Halve the oldest half past `cap`: keeps the full span at reduced
    resolution (the chart decimates anyway)."""
    if cap is None or len(seq) <= cap:
        return
    half = len(seq) // 2
    seq[:half] = seq[:half:2]


async def session_loop(series: Series, *, live_trading: bool, carry_arm: bool,
                       event_ticker: str | None = None):
    """`event_ticker` names a one-off event (no rollover)."""
    store = ConfigStore(series.config_cls, series.config_path, series.config_key)
    check_strategy_config(store.config)
    _log(series.ticker, f"config loaded from {series.config_path}"
         + (f" [{series.config_key}]" if series.config_key else ""))
    trading = TradingModeWatcher(series.ticker)
    armed = live_trading
    done: set[str] = set()
    rolled = False
    while True:
        for c in store.poll():
            _log(series.ticker, f"config: {c}")
        for c in store.apply_pending():
            _log(series.ticker, f"config (next-event change applied): {c}")
        try:
            if event_ticker:
                ev = km.event(event_ticker)
            else:
                listed = [e for e in km.upcoming_events(series.ticker) if e.event_ticker not in done]
                ev = series.pick_event(listed, store.config)
        except Exception as e:
            _log(series.ticker, f"event lookup failed ({e!r}); retrying in {RETRY_S:.0f}s")
            await asyncio.sleep(RETRY_S)
            continue
        if ev is None:
            _log(series.ticker, f"no upcoming event listed; checking again in {NO_EVENT_RETRY_S:.0f}s")
            await asyncio.sleep(NO_EVENT_RETRY_S)
            continue
        start = series.trade_from(ev, store.config)
        wait_s = 0.0 if start is None else (start - _now()).total_seconds()
        if wait_s > 0:
            _log(series.ticker, f"next: {ev.event_ticker}, trading from {start.isoformat()} "
                                f"(in {wait_s:.0f}s)")
            # Capped so a config edit or a rescheduled event is picked up on
            # long waits (KXINX overnight). The re-lookup after the sleep also
            # refreshes the market list, which can grow before the open.
            await asyncio.sleep(min(wait_s, NO_EVENT_RETRY_S))
            continue
        done.add(ev.event_ticker)
        _log(series.ticker, f"trading {ev.event_ticker}"
             + ("  (carrying REAL TRADING over)" if armed and carry_arm and rolled else ""))
        try:
            armed = await run_event(series, ev, store, trading, live_trading=armed)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log(series.ticker, f"{ev.event_ticker} failed; retrying after {RETRY_S:.0f}s:")
            traceback.print_exc()
            done.discard(ev.event_ticker)
            await asyncio.sleep(RETRY_S)
            continue
        if event_ticker:
            return
        armed = armed and carry_arm
        rolled = True


async def run_event(series: Series, ev: km.EventInfo, store: ConfigStore,
                    trading: TradingModeWatcher, *, live_trading: bool) -> bool:
    """Trade one event until it closes. Returns whether real trading was
    armed at the end (carried into the next event)."""
    S = series.ticker
    cfg = store.config
    model = await series.build_model(ev, cfg)
    tickers = list(model.tickers)
    date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    persist.reset_session_files(S)

    fee_sched = kfees.for_event(S, ev.event_ticker)
    _log(S, f"fees from Kalshi: {fee_sched.fee_type} x{fee_sched.multiplier:g} "
            f"(taker {fee_sched.taker_rate:g}, maker {fee_sched.maker_rate:g} x p(1-p))")

    lip = None
    try:
        progs = km.incentive_programs(set(tickers))
        lip = lipmod.LipTracker({t: lipmod.params_from_program(p) for t, p in progs.items()})
        _log(S, f"LIP programs on {len(progs)}/{len(tickers)} markets")
    except Exception as e:
        _log(S, f"WARN: LIP lookup failed ({e!r}); no LIP quoting or accrual this event")

    quoter = Quoter(cfg, model, fee_sched, close_time=ev.close_time, lip=lip, record_debug=True)
    try:
        order_client = KalshiOrderClient.from_account()
        _log(S, f"write budget {order_client.tokens_per_s:g} tokens/s "
                f"(~{order_client.max_orders_per_s:g} orders/s) from Kalshi account limits")
    except Exception as e:
        order_client = None
        _log(S, f"WARN: no order client ({e!r}); real trading cannot be armed")
    engine = LiveEngine(quoter, risk=cfg.risk, quoting=cfg.quoting, fees=fee_sched,
                        order_client=order_client, event_ticker=ev.event_ticker, close_time=ev.close_time)
    engine.set_clocks(signal_age_fn=model.signal_age_s, feed_age_fn=model.feed_age_s,
                      applied_age_fn=model.applied_age_s)

    journal = jr.DayJournal(S, date)
    quoter.debug_log = jr.JournalingList(journal.record_decision)
    pnl_history: list[tuple] = []
    fair_history: list[dict] = []
    pnl_by_ticker: dict[str, collections.deque] = {}

    def on_reset():
        pnl_history.clear()
        fair_history.clear()
        pnl_by_ticker.clear()
        persist.reset_session_files(S)
    engine.add_session_reset_hook(on_reset)

    def remaining_s() -> float | None:
        return None if ev.close_time is None else (ev.close_time - pd.Timestamp.now(tz="UTC")).total_seconds()

    def inside_real_cutoff() -> bool:
        cut, rem = cfg.close.real_trading_cutoff_s, remaining_s()
        return cut > 0 and rem is not None and rem <= cut

    if live_trading and not inside_real_cutoff():
        engine.set_live_trading(True)

    timings = {k: collections.deque(maxlen=400) for k in ("reprice", "dispatch", "flush", "loop_lag")}

    def _stat(d):
        if not d:
            return None
        v = sorted(d)
        return {"median_ms": round(v[len(v) // 2] * 1000, 2), "p95_ms": round(v[int(len(v) * .95)] * 1000, 2),
                "max_ms": round(v[-1] * 1000, 2), "n": len(v)}

    # --- Kalshi ---
    def on_quote(book):
        journal.record_bbo(book)
        engine.update_book(book)
        engine.dispatch(book, pd.Timestamp.now(tz="UTC"))

    def on_trade(ticker, trade):
        now = pd.Timestamp.now(tz="UTC")
        engine.on_trade(ticker, trade, now)
        engine.dispatch(trade, now)

    kalshi = LiveKalshiFeed(tickers, on_quote, on_trade, label=S)

    # --- loops ---
    wake = asyncio.Event()
    model.set_wakeup(wake.set)

    async def reprice_loop():
        """Reprices as soon as the model has new inputs; a burst that lands
        during one reprice is coalesced into the next."""
        while True:
            try:
                await asyncio.wait_for(wake.wait(), REPRICE_IDLE_S)
            except asyncio.TimeoutError:
                pass
            wake.clear()
            try:
                t0 = time.perf_counter()
                fairs = model.reprice(time.time())
                timings["reprice"].append(time.perf_counter() - t0)
                if not fairs:
                    continue
                now = pd.Timestamp.now(tz="UTC")
                for t, f in fairs.items():
                    if f is not None:
                        fair_history.append({"time": now, "ticker": t, "prob": f.prob,
                                             "prob_lo": f.lo, "prob_hi": f.hi})
                t1 = time.perf_counter()
                engine.dispatch(FairUpdate(fairs), now)
                timings["dispatch"].append(time.perf_counter() - t1)
            except Exception:
                # One bad reprice must not kill the producer; the freshness
                # guard stops trading if it keeps failing.
                _log(S, "ERROR in reprice:")
                traceback.print_exc()

    async def control_loop():
        while True:
            await asyncio.sleep(cfg.runtime.control_poll_s)
            try:
                for c in store.poll():
                    _log(S, f"config: {c}")
                want = trading.poll()
                if want is not None and want and inside_real_cutoff():
                    _log(S, "arm request ignored: inside real_trading_cutoff_s")
                    want = None
                if want is not None and want != engine.live_trading:
                    got = engine.set_live_trading(want)
                    _log(S, f"trading mode requested live={want}, in effect live={got}")
                if engine.live_trading and inside_real_cutoff():
                    _log(S, f"real trading OFF: within {cfg.close.real_trading_cutoff_s:g}s of close; "
                            f"paper continues")
                    engine.set_live_trading(False)
            except Exception:
                _log(S, "WARN: control poll failed:")
                traceback.print_exc()

    async def audit_loop():
        """The one guard that uses Kalshi's own positions instead of our ledger."""
        while True:
            await asyncio.sleep(AUDIT_INTERVAL_S)
            if not engine.live_trading:
                continue
            try:
                real = await asyncio.get_running_loop().run_in_executor(
                    None, engine.order_client.positions, ev.event_ticker)
                engine.audit_positions(real)
            except Exception:
                _log(S, "WARN: position audit failed:")
                traceback.print_exc()

    async def quote_fill_loop():
        """Resting-quote fills have no response to piggyback on; this poll is
        their only path into the ledger."""
        while True:
            await asyncio.sleep(cfg.runtime.quote_fill_poll_s)
            if not engine.live_trading or not engine.exchange_to_order:
                continue
            try:
                fills = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(engine.order_client.fills, ev.event_ticker,
                                            min_ts=engine.fill_poll_min_ts()))
                engine.apply_real_fills(fills)
            except Exception:
                _log(S, "WARN: quote fill poll failed:")
                traceback.print_exc()

    async def lag_probe():
        probe = 0.02
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(probe)
            timings["loop_lag"].append(max(0.0, time.perf_counter() - t0 - probe))

    flush_busy = [False]
    flush_n = [0]

    def _write(state):
        t0 = time.perf_counter()
        try:
            persist.write(state)
        except Exception:
            _log(S, f"WARN: snapshot write failed (heavy={state['heavy']}):")
            traceback.print_exc()
            sys.stdout.flush()
        try:
            jr.write(state.get("journal"))
        except Exception:
            _log(S, "WARN: journal write failed:")
            traceback.print_exc()
        return time.perf_counter() - t0

    def _done(fut):
        flush_busy[0] = False
        try:
            timings["flush"].append(fut.result())
        except Exception:
            _log(S, "WARN: snapshot write failed:")
            traceback.print_exc()

    def _snapshot(heavy: bool, now):
        summary = persist.build_summary(series=S, event_ticker=ev.event_ticker, engine=engine,
                                        quoter=quoter, store=store, model=model, now=now,
                                        timings={**{k: _stat(v) for k, v in timings.items()},
                                                 "write_phases_ms": dict(persist.WRITE_PHASES)})
        state = persist.capture(series=S, summary=summary, quoter=quoter, engine=engine,
                                pnl_history=pnl_history, fair_history=fair_history,
                                pnl_by_ticker=pnl_by_ticker, now=now, heavy=heavy)
        if heavy:
            journal.record_fills(engine.portfolio.fills, engine.live_trading)
            state["journal"] = journal.capture()
        return state

    async def flush_loop():
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            now = pd.Timestamp.now(tz="UTC")
            engine.check_failsafe()
            blocked = engine.blocked_reason()
            # Resting quotes keep taking risk through a halt until withdrawn.
            if blocked is not None and engine.has_quotes():
                _log(S, f"pulling quotes -- {blocked}")
                engine.pull_quotes()
            pnl_history.append((now, engine.current_pnl(mark="prob")
                                + (lip.total_usd() if lip is not None else 0.0)))
            for t, pos in engine.portfolio.positions.items():
                f = quoter.fairs.get(t)
                if f is not None:
                    pnl_by_ticker.setdefault(t, collections.deque(maxlen=4000)).append(
                        (now, pos.unrealized_pnl(f.prob, f.prob)))
            _prune(quoter.debug_log, MAX_DEBUG_ROWS)
            _prune(fair_history, MAX_DEBUG_ROWS)
            if flush_busy[0]:
                continue   # overwrite-in-place: skipping beats queueing
            flush_n[0] += 1
            # Heavy on the first flush so the dashboard finds every file at once.
            state = _snapshot(heavy=flush_n[0] % HEAVY_FLUSH_EVERY == 1, now=now)
            flush_busy[0] = True
            asyncio.get_running_loop().run_in_executor(None, _write, state).add_done_callback(_done)

    async def until_close():
        if ev.close_time is None:
            raise RuntimeError(f"{ev.event_ticker}: Kalshi lists no close_time")
        # Re-armed each pass in case the clock jumps (sleep is monotonic).
        while (rem := remaining_s()) > -CLOSE_GRACE_S:
            await asyncio.sleep(rem + CLOSE_GRACE_S)

    _log(S, f"starting feeds for {ev.event_ticker} ({len(tickers)} markets, "
            f"{'REAL ORDERS' if engine.live_trading else 'shadow'})")
    coros = [kalshi.run(), *model.feed_tasks(), reprice_loop(), control_loop(), audit_loop(),
             quote_fill_loop(), lag_probe(), flush_loop()]
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await until_close()
        _log(S, f"{ev.event_ticker} closed")
    finally:
        for t in tasks:
            t.cancel()
        # Pull quotes BEFORE awaiting cancellation: a quote resting after we
        # stop pricing keeps filling unwatched.
        if engine.live_trading:
            try:
                engine.pull_quotes()
                engine.cancel_resting()
                if engine.exchange_to_order:
                    engine.apply_real_fills(engine.order_client.fills(
                        ev.event_ticker, min_ts=engine.fill_poll_min_ts()))
            except Exception:
                _log(S, f"WARN: shutdown cleanup failed -- CHECK KALSHI BY HAND for resting "
                        f"orders on {ev.event_ticker}:")
                traceback.print_exc()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await model.close()
        except Exception:
            traceback.print_exc()
        try:
            _write(_snapshot(heavy=True, now=pd.Timestamp.now(tz="UTC")))
        except Exception:
            _log(S, "WARN: final snapshot failed:")
            traceback.print_exc()
    return engine.live_trading
