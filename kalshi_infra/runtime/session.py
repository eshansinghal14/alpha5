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
import time
import traceback

import pandas as pd

from kalshi_infra.config import ConfigStore
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.kalshi import markets as km
from kalshi_infra.kalshi.orders import KalshiOrderClient
from kalshi_infra.kalshi.ws_feed import LiveKalshiFeed
from kalshi_infra.runtime import persist
from kalshi_infra.runtime.control import TradingModeWatcher
from kalshi_infra.runtime.series import Series
from kalshi_infra.runtime.writer import DecisionLog, SnapshotWriter
from kalshi_infra.trading import lip as lipmod
from kalshi_infra.trading.engine import LiveEngine
from kalshi_infra.trading.fair import FairUpdate
from kalshi_infra.trading.params import check_strategy_config
from kalshi_infra.trading.quoter import Quoter


FLUSH_INTERVAL_S = 1.0      # rows shipped to the writer process; serialising happens there
AUDIT_INTERVAL_S = 5.0
REPRICE_IDLE_S = 1.0        # reprice even without new inputs, so time decay and withhold states advance
CLOSE_GRACE_S = 1.0
PREFETCH_BEFORE_CLOSE_S = 30.0
NO_EVENT_RETRY_S = 300.0    # nothing listed yet (e.g. KXINX lists the next day's event after the close)
RETRY_S = 30.0              # failed lookup or failed event


def _log(series: str, msg: str):
    print(f"[{series}] {msg}", flush=True)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


_DECISION_FIELDS = ("market_bid", "market_ask", "prob_raw", "prob_lo", "prob_hi", "net_position",
                    "eff_yes", "eff_no", "bid_final", "ask_final", "quote_bid", "quote_ask",
                    "lip_bid", "lip_ask", "block_bid", "block_ask", "atm_tradeable")


def _decision_key(row: dict) -> tuple:
    return tuple(row.get(k) for k in _DECISION_FIELDS)


async def session_loop(series: Series, *, live_trading: bool, carry_arm: bool,
                       event_ticker: str | None = None):
    """`event_ticker` names a one-off event (no rollover)."""
    writer = SnapshotWriter(series.ticker)
    try:
        await _session_loop(series, writer, live_trading=live_trading, carry_arm=carry_arm,
                            event_ticker=event_ticker)
    finally:
        writer.stop()


async def _session_loop(series: Series, writer: SnapshotWriter, *, live_trading: bool, carry_arm: bool,
                        event_ticker: str | None):
    store = ConfigStore(series.config_cls, series.config_path, series.config_key)
    check_strategy_config(store.config)
    _log(series.ticker, f"config loaded from {series.config_path}"
         + (f" [{series.config_key}]" if series.config_key else ""))
    trading = TradingModeWatcher(series.ticker)
    armed = live_trading
    done: set[str] = set()
    rolled = False
    nxt: dict = {}     # filled by run_event's prefetch with the next event's lookups
    while True:
        for c in store.poll():
            _log(series.ticker, f"config: {c}")
        for c in store.apply_pending():
            _log(series.ticker, f"config (next-event change applied): {c}")
        loop = asyncio.get_running_loop()
        try:
            # Off the loop: a session-lifetime feed (crypto spot) keeps
            # running between events.
            if event_ticker:
                ev = await loop.run_in_executor(None, km.event, event_ticker)
            elif nxt.get("event") is not None and nxt["event"].event_ticker not in done:
                ev = nxt["event"]
            else:
                listed = await loop.run_in_executor(None, km.upcoming_events, series.ticker)
                ev = series.pick_event([e for e in listed if e.event_ticker not in done], store.config)
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
            if wait_s > NO_EVENT_RETRY_S:
                nxt = {}
            continue
        done.add(ev.event_ticker)
        _log(series.ticker, f"trading {ev.event_ticker}"
             + ("  (carrying REAL TRADING over)" if armed and carry_arm and rolled else ""))
        prep, nxt = nxt, {}
        try:
            armed = await run_event(series, ev, store, trading, writer, live_trading=armed,
                                    prep=prep.get("prep"),
                                    next_slot=None if event_ticker else nxt, done=done)
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


async def _prepare(S: str, ev: km.EventInfo) -> dict:
    """The event's Kalshi REST lookups, concurrently and off the loop: run
    in sequence on the loop they were ~2.3s (LIP alone 1.5s, 2026-10-09)
    during which no feed was read."""
    loop = asyncio.get_running_loop()
    tickers = {m["ticker"] for m in ev.markets}
    fees, progs, client = await asyncio.gather(
        loop.run_in_executor(None, _guarded, kfees.for_event, S, ev.event_ticker),
        loop.run_in_executor(None, _guarded, km.incentive_programs, tickers),
        loop.run_in_executor(None, _guarded, KalshiOrderClient.from_account))
    return {"event_ticker": ev.event_ticker, "fees": fees, "lip": progs, "order_client": client}


def _guarded(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:
        return None, e


async def run_event(series: Series, ev: km.EventInfo, store: ConfigStore,
                    trading: TradingModeWatcher, writer: SnapshotWriter, *, live_trading: bool,
                    prep: dict | None = None, next_slot: dict | None = None,
                    done: set | None = None) -> bool:
    """Trade one event until it closes. Returns whether real trading was
    armed at the end (carried into the next event). `prep` is this event's
    _prepare() result if it was fetched ahead of time; `next_slot` receives
    the next event and its _prepare() shortly before this one closes."""
    S = series.ticker
    cfg = store.config
    if prep is None or prep["event_ticker"] != ev.event_ticker:
        prep_task = asyncio.create_task(_prepare(S, ev))
        model = await series.build_model(ev, cfg)
        prep = await prep_task
    else:
        model = await series.build_model(ev, cfg)
    tickers = list(model.tickers)
    writer.start_event(pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
    lip_pending = "lip" not in prep

    fee_sched, err = prep["fees"]
    if err is not None:
        raise RuntimeError(f"fee lookup failed: {err!r}") from err
    _log(S, f"fees from Kalshi: {fee_sched.fee_type} x{fee_sched.multiplier:g} "
            f"(taker {fee_sched.taker_rate:g}, maker {fee_sched.maker_rate:g} x p(1-p))")

    def _lip_from(res):
        progs, err = res
        if err is not None:
            _log(S, f"WARN: LIP lookup failed ({err!r}); no LIP quoting or accrual this event")
            return None
        progs = {t: p for t, p in progs.items() if t in set(tickers)}
        _log(S, f"LIP programs on {len(progs)}/{len(tickers)} markets")
        return lipmod.LipTracker({t: lipmod.params_from_program(p) for t, p in progs.items()})

    lip = None if lip_pending else _lip_from(prep["lip"])
    quoter = Quoter(cfg, model, fee_sched, close_time=ev.close_time, lip=lip, record_debug=True)
    order_client, err = prep["order_client"]
    if err is None:
        _log(S, f"write budget {order_client.tokens_per_s:g} tokens/s "
                f"(~{order_client.max_orders_per_s:g} orders/s) from Kalshi account limits")
    else:
        _log(S, f"WARN: no order client ({err!r}); real trading cannot be armed")
    engine = LiveEngine(quoter, risk=cfg.risk, quoting=cfg.quoting, fees=fee_sched,
                        order_client=order_client, event_ticker=ev.event_ticker, close_time=ev.close_time)
    engine.set_clocks(signal_age_fn=model.signal_age_s, feed_age_fn=model.feed_age_s,
                      applied_age_fn=model.applied_age_s)

    quoter.debug_log = DecisionLog(writer, _decision_key)
    engine.add_session_reset_hook(writer.reset_session)

    def remaining_s() -> float | None:
        return None if ev.close_time is None else (ev.close_time - pd.Timestamp.now(tz="UTC")).total_seconds()

    def inside_real_cutoff() -> bool:
        cut, rem = cfg.close.real_trading_cutoff_s, remaining_s()
        return cut > 0 and rem is not None and rem <= cut

    if live_trading and not inside_real_cutoff():
        engine.set_live_trading(True)

    timings = {k: collections.deque(maxlen=400) for k in ("reprice", "dispatch", "loop_lag")}

    def _stat(d):
        if not d:
            return None
        v = sorted(d)
        return {"median_ms": round(v[len(v) // 2] * 1000, 2), "p95_ms": round(v[int(len(v) * .95)] * 1000, 2),
                "max_ms": round(v[-1] * 1000, 2), "n": len(v)}

    # --- Kalshi ---
    def on_quote(book):
        writer.bbo(book)
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
                        writer.fair({"time": now, "ticker": t, "prob": f.prob, "prob_lo": f.lo, "prob_hi": f.hi})
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

    def _flush(now):
        writer.pnl(now, engine.current_pnl(mark="prob") + (lip.total_usd() if lip is not None else 0.0))
        for t, pos in engine.portfolio.positions.items():
            f = quoter.fairs.get(t)
            if f is not None:
                writer.pnl_ticker(t, now, pos.unrealized_pnl(f.prob, f.prob))
        summary = persist.build_summary(series=S, event_ticker=ev.event_ticker, engine=engine,
                                        quoter=quoter, store=store, model=model, now=now,
                                        timings={k: _stat(v) for k, v in timings.items()})
        writer.flush(summary, engine.portfolio.fills, engine.live_trading)

    async def flush_loop():
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            try:
                engine.check_failsafe()
                blocked = engine.blocked_reason()
                # Resting quotes keep taking risk through a halt until withdrawn.
                if blocked is not None and engine.has_quotes():
                    _log(S, f"pulling quotes -- {blocked}")
                    engine.pull_quotes()
                _flush(pd.Timestamp.now(tz="UTC"))
            except Exception:
                _log(S, "WARN: flush failed:")
                traceback.print_exc()

    async def prefetch_next():
        """The next event is already listed with its schedule; fetching it and
        its fees/LIP/limits now means the open isn't spent on REST."""
        if next_slot is None or ev.close_time is None:
            return
        await asyncio.sleep(max(0.0, remaining_s() - PREFETCH_BEFORE_CLOSE_S))
        try:
            listed = await asyncio.get_running_loop().run_in_executor(None, km.upcoming_events, S)
            nxt_ev = series.pick_event([e for e in listed if e.event_ticker not in (done or set())
                                        and e.event_ticker != ev.event_ticker], store.config)
            if nxt_ev is None:
                return
            start = series.trade_from(nxt_ev, store.config)
            if start is not None and (start - ev.close_time).total_seconds() > PREFETCH_BEFORE_CLOSE_S:
                return   # not back-to-back (KXINX overnight): limits/LIP could change by then
            next_slot["event"] = nxt_ev
            prep = await _prepare(S, nxt_ev)
            # A series' LIP programs are created when the event opens, so an
            # early lookup finds none (2026-10-09: 0/5 markets); it's redone
            # at the start, concurrently with everything else.
            prep.pop("lip")
            next_slot["prep"] = prep
        except Exception as e:
            _log(S, f"WARN: next-event prefetch failed ({e!r}); it will be looked up at rollover")

    async def until_close():
        if ev.close_time is None:
            raise RuntimeError(f"{ev.event_ticker}: Kalshi lists no close_time")
        # Re-armed each pass in case the clock jumps (sleep is monotonic).
        while (rem := remaining_s()) > -CLOSE_GRACE_S:
            await asyncio.sleep(rem + CLOSE_GRACE_S)

    async def lip_lookup():
        """Trading starts without LIP rather than waiting ~1.5s on it; the
        LIP quote and accrual begin once the programs are known."""
        if not lip_pending:
            return
        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _guarded(km.incentive_programs, set(tickers)))
        quoter.lip = _lip_from(res)

    _log(S, f"starting feeds for {ev.event_ticker} ({len(tickers)} markets, "
            f"{'REAL ORDERS' if engine.live_trading else 'shadow'})")
    coros = [kalshi.run(), *model.feed_tasks(), reprice_loop(), control_loop(), audit_loop(),
             quote_fill_loop(), lag_probe(), flush_loop(), prefetch_next(), lip_lookup()]
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
            _flush(pd.Timestamp.now(tz="UTC"))
            writer.end_event()
        except Exception:
            _log(S, "WARN: final snapshot failed:")
            traceback.print_exc()
    return engine.live_trading
