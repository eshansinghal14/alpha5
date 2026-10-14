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
from kalshi_infra.runtime import persist, write_groups
from kalshi_infra.runtime.control import StopWatcher, TradingModeWatcher, request_stop_all
from kalshi_infra.runtime.series import Series
from kalshi_infra.runtime.writer import DecisionLog, SnapshotWriter
from kalshi_infra.trading import lip as lipmod
from kalshi_infra.trading.engine import LiveEngine
from kalshi_infra.trading.fair import FairUpdate
from kalshi_infra.trading.params import check_strategy_config
from kalshi_infra.trading.quoter import Quoter


FLUSH_INTERVAL_S = 1.0      # rows shipped to the writer process; serialising happens there
PUMP_INTERVAL_S = 0.05      # quote-create queue drain; one create's budget refills in 0.25s at 40/s
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


class StopAll(Exception):
    """The run is over: the loss limit tripped here or another series' did."""


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
    stopper = StopWatcher()
    # Real PnL of finished events, carried so max_loss_usd bounds the run.
    run_state = {"real_pnl": 0.0}
    armed = live_trading
    done: set[str] = set()
    rolled = False
    nxt: dict = {}     # filled by run_event's prefetch with the next event's lookups
    while True:
        if (reason := stopper.poll()) is not None:
            _log(series.ticker, f"STOPPING: {reason}")
            return
        lim = store.config.risk.max_loss_usd
        if run_state["real_pnl"] <= -lim:
            request_stop_all(f"{series.ticker} real PnL this run {run_state['real_pnl']:+.2f} <= -{lim:g}")
            _log(series.ticker, f"STOPPING: real PnL this run {run_state['real_pnl']:+.2f} <= -{lim:g}")
            return
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
                                    next_slot=None if event_ticker else nxt, done=done,
                                    stopper=stopper, run_state=run_state)
        except asyncio.CancelledError:
            raise
        except StopAll as e:
            _log(series.ticker, f"STOPPED: {e}. Real PnL this run {run_state['real_pnl']:+.2f}. "
                                f"Restart the process to trade again.")
            return
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


async def _prepare(S: str, ev: km.EventInfo, write_group: str | None = None) -> dict:
    """The event's Kalshi REST lookups, concurrently and off the loop: run
    in sequence on the loop they were ~2.3s (LIP alone 1.5s, 2026-10-09)
    during which no feed was read."""
    loop = asyncio.get_running_loop()
    tickers = {m["ticker"] for m in ev.markets}
    fees, progs, client = await asyncio.gather(
        loop.run_in_executor(None, _guarded, kfees.for_event, S, ev.event_ticker),
        loop.run_in_executor(None, _guarded, km.incentive_programs, tickers),
        loop.run_in_executor(None, _guarded, _client_for, ev.event_ticker, write_group))
    return {"event_ticker": ev.event_ticker, "fees": fees, "lip": progs, "order_client": client}


def _client_for(event_ticker: str, write_group: str | None = None) -> KalshiOrderClient:
    """An order client pinned to the event's exchange shard, so every write is
    billed to that shard's own write bucket (see KalshiOrderClient.shard).
    In a write group it draws on the group's one shared budget."""
    shard = km.event_shard(event_ticker)
    budget = None if write_group is None else write_groups.get(write_group, shard).budget
    return KalshiOrderClient.from_account(shard=shard, budget=budget)


def _guarded(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:
        return None, e


async def run_event(series: Series, ev: km.EventInfo, store: ConfigStore,
                    trading: TradingModeWatcher, writer: SnapshotWriter, *, live_trading: bool,
                    prep: dict | None = None, next_slot: dict | None = None,
                    done: set | None = None, stopper: StopWatcher | None = None,
                    run_state: dict | None = None) -> bool:
    """Trade one event until it closes. Returns whether real trading is
    requested (carried into the next event), even if the close cutoff
    disarmed the engine. `prep` is this event's
    _prepare() result if it was fetched ahead of time; `next_slot` receives
    the next event and its _prepare() shortly before this one closes."""
    S = series.ticker
    cfg = store.config
    if prep is None or prep["event_ticker"] != ev.event_ticker:
        prep_task = asyncio.create_task(_prepare(S, ev, series.write_group))
        model = await series.build_model(ev, cfg)
        prep = await prep_task
    else:
        model = await series.build_model(ev, cfg)
    tickers = list(model.tickers)
    writer.start_event(pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"), tickers)
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
        _log(S, f"write budget {order_client.tokens_per_s:g} tokens/s on shard {order_client.shard} "
                f"(~{order_client.max_orders_per_s:g} orders/s) from Kalshi account limits")
    else:
        _log(S, f"WARN: no order client ({err!r}); real trading cannot be armed")
    engine = LiveEngine(quoter, risk=cfg.risk, quoting=cfg.quoting, fees=fee_sched,
                        order_client=order_client, event_ticker=ev.event_ticker, close_time=ev.close_time)
    engine.set_clocks(signal_age_fn=model.signal_age_s, feed_age_fn=model.feed_age_s,
                      applied_age_fn=model.applied_age_s)
    attach = getattr(model, "attach_engine", None)   # optional: models that read our positions
    if attach is not None:
        attach(engine)
    group = None
    if series.write_group is not None:
        shard = order_client.shard if order_client is not None else None
        group = write_groups.get(series.write_group, shard).group
        group.attach(S, engine.om)
        _log(S, f"write group {series.write_group}: sharing one bucket and quote queue with "
                f"{', '.join(n for n in group.members if n != S) or 'no other series yet'}")
    run_state = {"real_pnl": 0.0} if run_state is None else run_state
    engine.loss_offset = run_state["real_pnl"]
    # This event's real PnL, worse of touch/fair, as of the last moment it
    # was armed -- after a cutoff disarm the ledger keeps taking paper fills.
    event_real = {"pnl": None}
    stop = {"reason": None}
    stop_evt = asyncio.Event()

    def _stop(reason: str):
        if stop["reason"] is None:
            stop["reason"] = reason
            stop_evt.set()

    quoter.debug_log = DecisionLog(writer, _decision_key)
    engine.add_session_reset_hook(writer.reset_session)

    def remaining_s() -> float | None:
        return None if ev.close_time is None else (ev.close_time - pd.Timestamp.now(tz="UTC")).total_seconds()

    # Without a scheduled open, count from when this process took the event.
    opened_at = ev.open_time if ev.open_time is not None else pd.Timestamp.now(tz="UTC")

    def inside_real_cutoff() -> bool:
        cut, rem = cfg.trading_time.real_trading_cutoff_s, remaining_s()
        return cut > 0 and rem is not None and rem <= cut

    def inside_real_delay() -> bool:
        d = cfg.trading_time.real_trading_delay_s
        return d > 0 and (pd.Timestamp.now(tz="UTC") - opened_at).total_seconds() < d

    def outside_trading_time() -> bool:
        return inside_real_cutoff() or inside_real_delay()

    # What the dashboard asked for, kept apart from what the engine is doing:
    # the cutoff disarm is temporary, and returning the engine's state made
    # the next event start in shadow until the switch was toggled off and on.
    arm = {"want": live_trading, "retry_at": 0.0}

    def reconcile_arm():
        if engine.live_trading and (not arm["want"] or outside_trading_time()):
            if arm["want"] and inside_real_cutoff():
                _log(S, f"real trading OFF: within {cfg.trading_time.real_trading_cutoff_s:g}s of close; "
                        f"paper continues, re-arms next event")
            elif arm["want"]:
                _log(S, f"real trading OFF: within {cfg.trading_time.real_trading_delay_s:g}s of open; "
                        f"paper continues, arms once the delay passes")
            engine.set_live_trading(False)
        elif (arm["want"] and not engine.live_trading and not outside_trading_time()
              and not engine.halted and time.monotonic() >= arm["retry_at"]):
            if not engine.set_live_trading(True):
                arm["retry_at"] = time.monotonic() + RETRY_S
                _log(S, f"arm failed; retrying in {RETRY_S:.0f}s (disarm from the dashboard to stop)")

    reconcile_arm()

    timings = {k: collections.deque(maxlen=400) for k in ("reprice", "dispatch", "loop_lag")}

    def _stat(d):
        if not d:
            return None
        v = sorted(d)
        return {"median_ms": round(v[len(v) // 2] * 1000, 2), "p95_ms": round(v[int(len(v) * .95)] * 1000, 2),
                "max_ms": round(v[-1] * 1000, 2), "n": len(v)}

    # --- Kalshi ---
    model_on_book = getattr(model, "on_book", None)   # optional: models that read the Kalshi book

    def on_quote(book):
        writer.bbo(book)
        engine.update_book(book)
        if model_on_book is not None:
            model_on_book(book)
        engine.dispatch(book, pd.Timestamp.now(tz="UTC"))

    def on_trade(ticker, trade):
        now = pd.Timestamp.now(tz="UTC")
        engine.on_trade(ticker, trade, now)
        engine.dispatch(trade, now)

    def on_fill(f):
        # Our own fills off the websocket: books resting-quote fills within
        # one message of Kalshi matching them (the REST poll was up to 1s).
        if engine.live_trading:
            engine.on_real_fill(f)

    kalshi = LiveKalshiFeed(tickers, on_quote, on_trade, label=S, on_fill=on_fill)

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
                    # A withheld fair is recorded as NaN so the chart shows a gap.
                    writer.fair({"time": now, "ticker": t, "prob": f.prob if f else float("nan"),
                                 "prob_lo": f.lo if f else float("nan"), "prob_hi": f.hi if f else float("nan")})
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
                if want is not None:
                    arm["want"], arm["retry_at"] = want, 0.0
                    if want and inside_real_cutoff():
                        _log(S, "arm request held: inside real_trading_cutoff_s, arms next event")
                    elif want and inside_real_delay():
                        _log(S, "arm request held: inside real_trading_delay_s, arms once it passes")
                reconcile_arm()
                if want is not None:
                    _log(S, f"trading mode requested live={want}, in effect live={engine.live_trading}")
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
                before = engine.ledger_positions()
                real = await asyncio.get_running_loop().run_in_executor(
                    None, engine.order_client.positions, ev.event_ticker)
                engine.audit_positions(real, before=before)
            except Exception:
                _log(S, "WARN: position audit failed:")
                traceback.print_exc()

    async def quote_fill_loop():
        """REST backstop for resting-quote fills. The fill websocket is the
        primary path; this catches anything a reconnect dropped. Deduped on
        (order_id, trade_id), so overlap is harmless."""
        while True:
            await asyncio.sleep(cfg.runtime.quote_fill_poll_s)
            if not engine.live_trading or not engine.has_quote_history():
                continue
            try:
                fills = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(engine.order_client.fills, ev.event_ticker,
                                            min_ts=engine.fill_poll_min_ts()))
                engine.apply_real_fills(fills)
            except Exception:
                _log(S, "WARN: quote fill poll failed:")
                traceback.print_exc()

    async def lip_queue_loop():
        """Every lip_hold_s: Kalshi's queue position for our LIP quotes (a GET,
        read bucket) and re-queue to the back any with fewer than
        lip_requeue_min_ahead ahead. Each check is logged, so fill quality can
        later be costed against queue position."""
        while True:
            await asyncio.sleep(max(cfg.lip.lip_hold_s, 0.25))
            lc = cfg.lip
            if (not engine.live_trading or not lc.enable_lip_quoting or lc.lip_requeue_min_ahead is None
                    or not engine.om.live(role="lip")):
                continue
            try:
                pos = await asyncio.get_running_loop().run_in_executor(
                    None, engine.order_client.queue_positions, ev.event_ticker)
                for tk, side, px, ahead, behind, done in engine.requeue_lip(pos, lc.lip_requeue_min_ahead):
                    if done:
                        _log(S, f"LIP requeue {tk} {side} @{px:.2f}: {ahead:.0f} ahead, {behind:.0f} behind")
            except Exception:
                _log(S, "WARN: LIP queue-position poll failed:")
                traceback.print_exc()

    async def pump_loop():
        """Drain queued quote creates as the quote budget refills, even when
        no market event is waking the strategy."""
        while True:
            await asyncio.sleep(PUMP_INTERVAL_S)
            try:
                engine.pump_quotes()
            except Exception:
                _log(S, "WARN: quote pump failed:")
                traceback.print_exc()

    async def lag_probe():
        probe = 0.02
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(probe)
            timings["loop_lag"].append(max(0.0, time.perf_counter() - t0 - probe))

    def _flush(now):
        writer.pnl(now, engine.current_pnl(mark="prob") + (lip.total_usd() if lip is not None else 0.0))
        for t, v in engine.pnl_by_ticker(mark="prob").items():
            writer.pnl_ticker(t, now, v)
        summary = persist.build_summary(series=S, event_ticker=ev.event_ticker, engine=engine,
                                        quoter=quoter, store=store, model=model, now=now,
                                        timings={k: _stat(v) for k, v in timings.items()},
                                        arm_requested=arm["want"])
        writer.flush(summary, engine.portfolio.fills, engine.live_trading)

    async def flush_loop():
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            try:
                engine.check_failsafe()
                if engine.live_trading:
                    event_real["pnl"] = min(engine.current_pnl(mark="touch"), engine.current_pnl(mark="prob"))
                if engine.halt_kind == "loss":
                    request_stop_all(f"{S} loss limit: {engine.halt_reason}")
                    _stop(f"loss limit -- {engine.halt_reason}")
                elif stopper is not None and (reason := stopper.poll()) is not None:
                    _stop(reason)
                blocked = engine.blocked_reason()
                # Resting quotes keep taking risk through a halt until withdrawn.
                if blocked is not None and engine.has_uncancelled_quotes():
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
            prep = await _prepare(S, nxt_ev, series.write_group)
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
             quote_fill_loop(), pump_loop(), lip_queue_loop(), lag_probe(), flush_loop(), prefetch_next(), lip_lookup()]
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        closer, stopped = asyncio.create_task(until_close()), asyncio.create_task(stop_evt.wait())
        await asyncio.wait({closer, stopped}, return_when=asyncio.FIRST_COMPLETED)
        closer.cancel(); stopped.cancel()
        if stop["reason"] is None:
            _log(S, f"{ev.event_ticker} closed")
        else:
            _log(S, f"STOP: {stop['reason']} -- disarming, pulling every quote, exiting")
    finally:
        for t in tasks:
            t.cancel()
        # Pull quotes BEFORE awaiting cancellation: a quote resting after we
        # stop pricing keeps filling unwatched.
        if engine.live_trading:
            try:
                engine.pull_quotes()
                engine.cancel_resting()
                if engine.has_quote_history():
                    engine.apply_real_fills(engine.order_client.fills(
                        ev.event_ticker, min_ts=engine.fill_poll_min_ts()))
                event_real["pnl"] = min(engine.current_pnl(mark="touch"), engine.current_pnl(mark="prob"))
                engine.live_trading = False
            except Exception:
                _log(S, f"WARN: shutdown cleanup failed -- CHECK KALSHI BY HAND for resting "
                        f"orders on {ev.event_ticker}:")
                traceback.print_exc()
        await asyncio.gather(*tasks, return_exceptions=True)
        if group is not None:
            # Its cancels were sent above; leaving keeps a closed event's
            # quotes out of the next pass and its reserve out of the max.
            group.detach(S)
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
        # Marked at the last armed moment (worse of touch/fair), not at
        # settlement, so it can differ from the account by the move after.
        if event_real["pnl"] is not None:
            run_state["real_pnl"] += event_real["pnl"]
            _log(S, f"{ev.event_ticker} real PnL {event_real['pnl']:+.2f}; "
                    f"this run {run_state['real_pnl']:+.2f} (halt at -{cfg.risk.max_loss_usd:g})")
    if stop["reason"] is not None:
        raise StopAll(stop["reason"])
    return arm["want"]
