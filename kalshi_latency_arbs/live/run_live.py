"""
Live trading for KXINX/KXINXU. Runs SPOptionsLagQuotingStrategy (the same
class the backtest uses) against a live Kalshi WS book + a live IB SPXW
options feed (live_ib_feed.py). Flushes to data/live/{series}/{date}/ for
the dashboard.

Modes: shadow (default) simulates fills via kalshi_backtest.execution;
real (--live-trading, or the dashboard checkbox) sends IOCs for takes
and resting limits for quotes. Arming reconciles positions with Kalshi
first; disarming pulls resting quotes before returning.

Halts (LiveEngine.blocked_reason, both modes):
  * mark-to-market PnL at or past -$max_loss_usd (either marking)
  * option-implied signal stale > --max-signal-age-s
  * five consecutive real-order errors
  * event about to stop accepting orders

Usage (preferred: the unified entrypoint):
    python kalshi_main/run_live.py --series KXINX [--edge-fraction 0.5]
    python kalshi_main/run_live.py --series KXINX --live-trading     # REAL MONEY
    python kalshi_main/run_live.py --series KXINX KXINXU             # both, one process each

Running this module directly still works and takes the same flags:
    python kalshi_latency_arbs/live/run_live.py --series KXINX

See docs/live-trading.md for the overall architecture and what's
deliberately NOT built yet (multi-event concurrency, deep reconnect).
"""
import argparse
import asyncio
import collections
import datetime
import functools
import json
import os
import sys
import time
import traceback

import numpy as np
import pandas as pd

_ARBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_KXINX_DIR = os.path.join(_ARBS_DIR, "spxw_kxinx")
_INFRA_DIR = os.path.join(_ARBS_DIR, "..", "kalshi_infra")
for _p in (_ARBS_DIR, _KXINX_DIR, _INFRA_DIR, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sp_options_lag_quoting as strat
from api_functions import kalshi_get
import databento_functions as dbf
import ib_functions as ibf
import keys
import spx_cash

from live_context import LiveEngine, MAX_SIGNAL_AGE_S
from live_orders import KalshiOrderClient, DEFAULT_MAX_ORDERS_PER_S
from live_kalshi_ws import LiveKalshiFeed
from live_ib_feed import LiveIBFeed
from live_signal import compute_live_bucket_probs
from spxw_signal import NoiseTracker
import live_persist
import live_journal
from live_persist import (capture_snapshot, write_snapshot, compute_live_pnl,
                          reset_session_files, append_bias_row)

# Snapshot cadence. The write is CPU-bound pandas on an executor but holds
# the GIL, so it still contends with pricing; 3s matches the dashboard poll.
FLUSH_INTERVAL_S = 3.0
# Every Nth flush rebuilds the heavy parquet/stream files (~90% of snapshot
# cost, dashboard-only). PnL + guard state in summary.json refresh every 3s.
HEAVY_FLUSH_EVERY = 10
# Bounds how far a fill-accounting bug can run before the position audit
# catches it. Tuned against the 2026-08-28 overshoot (4x cap over ~1min).
AUDIT_INTERVAL_S = 5.0
# Quote fills arrive without a create-order response to piggyback on, so
# this poll is the only path for them into the ledger. 1s matches
# audit_positions' tolerance for in-flight resting size.
QUOTE_FILL_POLL_S = 1.0
# Event rollover
ROLLOVER_GRACE_S = 60.0   # before teardown, so final snapshot lands
ROLLOVER_CHECK_S = 15.0
ROLLOVER_POLL_S = 300.0   # weekend-friendly idle poll for the next event
# Don't open an event before 09:00 ET (half an hour before the cash open)
# -- SPXW 0DTE isn't listed before then and LiveIBFeed would raise at
# qualifyContractsAsync.
EVENT_OPEN_ET = datetime.time(9, 0)
# In-memory log caps. _prune halves the oldest half rather than truncating,
# so the full session span survives at reduced resolution (chart decimates
# to STREAM_POINTS anyway). Measured 2026-08-28: snapshot cost went 531ms
# at 10.7k decisions -> 1.3s at 16.9k, uncapped reached 117k.
MAX_DEBUG_ROWS = 20000
MAX_PROB_ROWS = 30000     # ~65 dispatches/s on KXINXU; ~500 points per market at cap
# Repricing cadence floor. A pass costs 12-19ms (measured 2026-08-28 after
# the snapshot moved off-loop); 20ms keeps duty cycle sane without
# saturating the loop.
SIGNAL_MIN_INTERVAL_S = 0.02
SIGNAL_ROUND_DECIMALS = 3


_ET = "America/New_York"

DAILY_CLOSE_SUFFIX = "H1600"   # 16:00 ET -- the day's settlement event


def resolve_event_close_time(event_ticker: str) -> pd.Timestamp | None:
    """When `event_ticker` stops accepting orders, for LiveEngine's
    close-time guard. Best-effort: a failure here must not stop a session
    starting, it just leaves that one guard inert."""
    try:
        data = kalshi_get("/trade-api/v2/markets", params={"event_ticker": event_ticker, "limit": 200})
        times = [m["close_time"] for m in data.get("markets", []) if m.get("close_time")]
        return min(pd.to_datetime(times, utc=True)) if times else None
    except Exception:
        print("[live] WARN: could not resolve event close time; close guard disabled:")
        traceback.print_exc()
        return None


def resolve_live_event_ticker(series: str) -> str:
    """Pick the 16:00 ET daily-close event (DAILY_CLOSE_SUFFIX); fall
    back to the soonest-closing event only if none is open.

    Not "soonest close" -- KXINXU lists intraday hourlies alongside the
    daily, and the hourly about to expire would leave the process idling
    on a dead event almost immediately. --event-ticker overrides.
    """
    data = kalshi_get("/trade-api/v2/markets", params={"series_ticker": series, "status": "open", "limit": 200})
    markets = data.get("markets", [])
    if not markets:
        raise ValueError(f"no open {series} markets right now")
    events = {}
    for m in markets:
        events.setdefault(m["event_ticker"], m["close_time"])
    daily = [et for et in events if et.endswith(DAILY_CLOSE_SUFFIX)]
    if daily:
        return min(daily, key=lambda et: events[et])
    return min(events, key=lambda et: events[et])


SERIES = ("KXINX", "KXINXU")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", nargs="+", required=True, metavar="SERIES",
                        help="KXINX and/or KXINXU (space- or comma-separated). Several "
                             "series run as one child process each.")
    parser.add_argument("--edge-fraction", type=float, default=None)
    parser.add_argument("--max-position", type=float, default=None)
    parser.add_argument("--constant-edge", type=float, default=None,
                        help="Flat edge floor ($) required on both sides, on top of the CI/fee/lean terms.")
    parser.add_argument("--enable-quoting", action="store_true",
                        help="Also REST quotes, on top of taking: quote_size on each side at the "
                             "first tick outside the taking edge plus --quoting-edge. Can also be "
                             "turned on/off from the dashboard's config panel.")
    parser.add_argument("--quoting-edge", type=float, default=None,
                        help="Flat edge ($) a resting quote must clear ON TOP of the full taking "
                             "edge. Only used when quoting is enabled.")
    parser.add_argument("--quote-size", type=float, default=None,
                        help="Contracts to rest per side per market.")
    parser.add_argument("--requote-delay-s", type=float, default=None,
                        help="How long a (market, side) stays unquoted after our own quote there "
                             "was hit.")
    parser.add_argument("--max-loss-usd", type=float, default=80.0,
                        help="Failsafe: halt new orders once mark-to-market PnL hits -this on EITHER "
                             "marking convention (touch or markout). Default $80.")
    parser.add_argument("--max-signal-age-s", type=float, default=MAX_SIGNAL_AGE_S,
                        help="Stop trading if the option-implied signal hasn't refreshed in this long "
                             "(a dead IB feed, or a repricing that keeps throwing).")
    parser.add_argument("--event-ticker", default=None, help="Override auto-resolved current event")
    parser.add_argument("--live-trading", action="store_true",
                        help="SEND REAL ORDERS TO KALSHI from the start. Without this the session "
                             "runs in shadow mode, but can still be armed from the dashboard.")
    parser.add_argument("--no-carry-arm", action="store_true",
                        help="At an event rollover, always drop back to shadow instead of carrying "
                             "the previous event's armed state forward.")
    parser.add_argument("--max-orders-per-s", type=float, default=DEFAULT_MAX_ORDERS_PER_S,
                        help="Client-side ceiling on REAL orders per second (see live_orders.py).")
    args = parser.parse_args()

    _root = os.path.abspath(os.path.join(_ARBS_DIR, ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from kalshi_main.run_live import (parse_series, forward_flags, launch_many,
                                      treat_break_as_interrupt)
    treat_break_as_interrupt()
    series_list = parse_series(parser, args.series, SERIES)
    if len(series_list) > 1:
        if args.event_ticker:
            parser.error("--event-ticker names one event, so it needs exactly one --series")
        sys.exit(launch_many(series_list,
                             forward_flags(parser, args, skip=("series", "max_orders_per_s")),
                             args.max_orders_per_s))
    args.series = series_list[0]

    if args.series == "KXINXU":
        _kxinxu_dir = os.path.join(_ARBS_DIR, "spxw_kxinxu")
        if _kxinxu_dir not in sys.path:
            sys.path.insert(0, _kxinxu_dir)
        import kxinxu_lag_quoting as series_strat
    else:
        series_strat = strat

    config_kwargs = {}
    if args.edge_fraction is not None:
        config_kwargs["edge_fraction"] = args.edge_fraction
    if args.max_position is not None:
        config_kwargs["max_position"] = args.max_position
    if args.constant_edge is not None:
        config_kwargs["constant_edge"] = args.constant_edge
    if args.enable_quoting:
        config_kwargs["enable_quoting"] = True
    if args.quoting_edge is not None:
        config_kwargs["quoting_edge"] = args.quoting_edge
    if args.quote_size is not None:
        config_kwargs["quote_size"] = args.quote_size
    if args.requote_delay_s is not None:
        config_kwargs["requote_delay_s"] = args.requote_delay_s
    config = series_strat.QuotingConfig(**config_kwargs)

    common = dict(max_loss_usd=args.max_loss_usd, max_orders_per_s=args.max_orders_per_s,
                  max_signal_age_s=args.max_signal_age_s)
    try:
        if args.event_ticker:
            # A named event is a one-off; rolling on from it would trade
            # something the caller never asked for.
            print(f"[live] {args.series}: trading {args.event_ticker} (no rollover -- event was named explicitly)")
            asyncio.run(run(args.series, args.event_ticker, series_strat, config,
                            live_trading=args.live_trading, **common))
        else:
            asyncio.run(session_loop(args.series, series_strat, config,
                                     live_trading=args.live_trading,
                                     carry_arm=not args.no_carry_arm, **common))
    except KeyboardInterrupt:
        print(f"[live] {args.series} stopped", flush=True)


async def session_loop(series: str, series_strat, config, live_trading: bool = False,
                       carry_arm: bool = True, **run_kwargs):
    """Trade this series' current event, then the next, indefinitely.
    Each event gets a fresh engine/ledger/day directory. `carry_arm` keeps
    real trading on across rollover (set_live_trading still runs the full
    balance/positions check); cold starts always begin disarmed.
    """
    armed = live_trading
    last_event = None
    while True:
        try:
            event_ticker = resolve_live_event_ticker(series)
        except Exception as exc:
            print(f"[live] no open {series} event yet ({exc!r}); retrying in {ROLLOVER_POLL_S / 60:.0f} min")
            await asyncio.sleep(ROLLOVER_POLL_S)
            continue
        if event_ticker == last_event:
            # Kalshi still lists the event we just finished; wait it out
            # rather than immediately re-running a closed market.
            await asyncio.sleep(ROLLOVER_POLL_S)
            continue
        close_time = resolve_event_close_time(event_ticker)
        if close_time is not None:
            event_day = close_time.tz_convert(_ET).date()
            open_at = pd.Timestamp(datetime.datetime.combine(event_day, EVENT_OPEN_ET), tz=_ET)
            if pd.Timestamp.now(tz=_ET) < open_at:
                print(f"[live] next event is {event_ticker} on {event_day}; waiting until "
                      f"{open_at.strftime('%Y-%m-%d %H:%M %Z')} before opening it")
                await asyncio.sleep(ROLLOVER_POLL_S)
                continue
        last_event = event_ticker
        print(f"[live] {series}: trading {event_ticker}"
              + ("  (carrying REAL TRADING over from the previous event)" if armed and carry_arm else ""))
        # Yesterday's chart files and per-ticker PnL are for other markets.
        reset_session_files(series)
        try:
            ended_armed = await run(series, event_ticker, series_strat, config,
                                    live_trading=armed and carry_arm, **run_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One event failing to start (definitions not listed yet, a feed
            # refusing a connection) must not end the session -- that is the
            # whole point of running a loop. Retry it after a poll interval.
            print(f"[live] {event_ticker} failed; retrying after "
                  f"{ROLLOVER_POLL_S / 60:.0f} min:")
            traceback.print_exc()
            last_event = None
            await asyncio.sleep(ROLLOVER_POLL_S)
            continue
        armed = ended_armed if carry_arm else False


async def run(series: str, event_ticker: str, series_strat, config, flush_interval_s: float = FLUSH_INTERVAL_S,
              max_loss_usd: float = 80.0, live_trading: bool = False,
              max_orders_per_s: float = DEFAULT_MAX_ORDERS_PER_S,
              max_signal_age_s: float = MAX_SIGNAL_AGE_S):
    strategy = series_strat.SPOptionsLagQuotingStrategy(config, record_debug=True)
    # Built unconditionally (opens no connection on construction) so the
    # dashboard checkbox can arm real trading without a restart.
    order_client = KalshiOrderClient(max_orders_per_s=max_orders_per_s)
    engine = LiveEngine(strategy, fee_multiplier=config.fee_multiplier, max_loss_usd=max_loss_usd,
                        order_client=order_client, event_ticker=event_ticker,
                        max_signal_age_s=max_signal_age_s,
                        close_time=resolve_event_close_time(event_ticker))

    pnl_history: list[tuple] = []
    prob_history: list[dict] = []
    last_signal_key: dict[str, tuple] = {}   # ticker -> last dispatched rounded (mid, lo, hi)
    noise = NoiseTracker()                   # drops spike-and-revert markets, same filter as the backtest

    # Append-only day record; survives a restart and the ledger reset that
    # arming real trading performs. See live_journal.
    journal = live_journal.DayJournal(series, pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
    # Journal at append time, not flush time: _prune mutates debug_log in
    # place and reset_session clears it, so anything not yet journaled would
    # be lost.
    strategy.debug_log = live_journal.JournalingList(journal.record_decision)

    def on_session_reset():
        """Clear per-module history on arm/reset. last_signal_key and
        noise are market-data state, deliberately NOT reset -- the noise
        filter has already learned which markets to suppress."""
        pnl_history.clear()
        prob_history.clear()
        reset_session_files(series)

    engine.add_session_reset_hook(on_session_reset)

    def on_quote(book_view):
        t0 = time.perf_counter()
        # Record BBO unconditionally: a stretch with no decisions should
        # still show what the market was doing.
        journal.record_bbo(book_view)
        engine.update_book(book_view)
        engine.dispatch(book_view, pd.Timestamp.now(tz="UTC"))
        timings["kalshi_cb"].append(time.perf_counter() - t0)

    def on_trade(ticker, trade):
        now = pd.Timestamp.now(tz="UTC")
        engine.on_trade(ticker, trade, now)
        engine.dispatch(trade, now)

    # Feed callback is coalesced; signal_loop does the actual pricing at
    # SIGNAL_MIN_INTERVAL_S. Inline pricing saturated the loop (~7.5ms per
    # pass x every tick). Stamped only on successful recompute so the
    # staleness guard catches a repricing that keeps throwing.
    last_signal_ok = [time.monotonic()]

    # Loop-lag probe measures sleep overshoot (true starvation, cause-
    # agnostic); phase timers attribute it. Split by flushing-vs-idle to
    # expose the pandas-on-executor GIL contention cost.
    timings = {"loop_lag": collections.deque(maxlen=400),
               "flush": collections.deque(maxlen=60),
               "recompute": collections.deque(maxlen=400),
               "capture": collections.deque(maxlen=400),
               "kalshi_cb": collections.deque(maxlen=400),
               "price": collections.deque(maxlen=400),       # Breeden-Litzenberger pass
               "dispatch": collections.deque(maxlen=400),    # strategy dispatch for moved markets
               "n_dispatched": collections.deque(maxlen=400),
               "recompute_while_flushing": collections.deque(maxlen=400),
               "recompute_while_idle": collections.deque(maxlen=400),
               "loop_lag_while_flushing": collections.deque(maxlen=400),
               "loop_lag_while_idle": collections.deque(maxlen=400)}

    def _stat(key):
        d = timings[key]
        if not d:
            return None
        v = sorted(d)
        return {"median_ms": round(v[len(v) // 2] * 1000, 2),
                "p95_ms": round(v[int(len(v) * 0.95)] * 1000, 2),
                "max_ms": round(v[-1] * 1000, 2), "n": len(v)}

    def recompute_signal():
        _p0 = time.perf_counter()
        arrays = ib_feed.book.snapshot_arrays()
        bucket_probs = compute_live_bucket_probs(
            markets, ib_feed.book.call_strikes, arrays[0], arrays[1], arrays[2],
            ib_feed.book.put_strikes, arrays[3], arrays[4], arrays[5],
        )
        timings["price"].append(time.perf_counter() - _p0)
        _d0 = time.perf_counter()
        _n_disp = 0
        # Dispatch every market whose probability moved (deduped per ticker),
        # matching the backtest's precompute_signal.
        now = pd.Timestamp.now(tz="UTC")
        for ticker, (lo, mid, hi) in bucket_probs.items():
            key = (round(mid, SIGNAL_ROUND_DECIMALS), round(lo, SIGNAL_ROUND_DECIMALS),
                   round(hi, SIGNAL_ROUND_DECIMALS))
            if key == last_signal_key.get(ticker):
                continue
            last_signal_key[ticker] = key
            if not noise.accept(ticker, mid):
                continue
            signal = {"ticker": ticker, "prob": mid, "prob_lo": lo, "prob_hi": hi}
            prob_history.append({"time": now, **signal})
            engine.dispatch(signal, now)
            _n_disp += 1
        timings["dispatch"].append(time.perf_counter() - _d0)
        timings["n_dispatched"].append(float(_n_disp))
        # Last statement so a mid-pass raise doesn't count as a refresh.
        last_signal_ok[0] = time.monotonic()

    markets = dbf._kxinx_event_markets(event_ticker)
    kalshi_feed = LiveKalshiFeed(event_ticker, on_quote, on_trade)
    # IB subscription: a contiguous window around the ATM bucket, wider than
    # the tradeable set by SCOPE_MARGIN_BUCKETS each side so the set can
    # drift without leaving the priced region. rescope_loop moves it.
    # max_markets is pinned to the same count so every bucket we pay IB
    # lines to price is one the strategy may actually trade.
    config.max_markets = ibf.DEFAULT_ATM_BUCKETS[series]
    order = ibf.ordered_tickers(markets)
    scope_centre = [ibf.kalshi_centre(event_ticker)]
    scope = ibf.scope_around(markets, scope_centre[0], series)
    print(f"[live] IB scope: {len(scope)} buckets {scope[0].split('-')[-1]}..{scope[-1].split('-')[-1]} "
          f"around {scope_centre[0].split('-')[-1]}; trading up to {config.max_markets} nearest 50/50")
    ib_feed = LiveIBFeed(event_ticker, tickers=scope,
                         client_id=keys.IB_CLIENT_ID + ibf.CLIENT_ID_OFFSET[series])

    async def rescope_loop():
        """Follow the market: re-centre the IB subscription when the
        option-implied ATM bucket drifts RESCOPE_DRIFT_BUCKETS from where
        we centred it, or reaches the scope's edge."""
        drift = ibf.RESCOPE_DRIFT_BUCKETS[series]
        while True:
            await asyncio.sleep(10.0)
            try:
                in_scope = [t for t in (ib_feed.scope_tickers or []) if t in strategy.signals]
                if not in_scope:
                    continue
                atm = min(in_scope, key=lambda t: abs(strategy.signals[t]["prob"] - 0.5))
                pos, ctr = order.index(atm), order.index(scope_centre[0])
                cur = ib_feed.scope_tickers
                at_edge = atm in (cur[0], cur[-1])
                if abs(pos - ctr) < drift and not at_edge:
                    continue
                new_scope = ibf.scope_around(markets, atm, series)
                if await ib_feed.rescope(new_scope):
                    print(f"[live] IB scope re-centred {scope_centre[0].split('-')[-1]} -> "
                          f"{atm.split('-')[-1]}: {new_scope[0].split('-')[-1]}.."
                          f"{new_scope[-1].split('-')[-1]}")
                scope_centre[0] = atm
            except Exception:
                print("[live] WARN: IB rescope failed:")
                traceback.print_exc()

    # The SIGNAL clock gates trading (set at end of successful recompute);
    # it strictly dominates the feed clock, and also catches a repricing
    # that keeps throwing. Feed clock is reported for stall attribution.
    engine.set_clocks(signal_age_fn=lambda: time.monotonic() - last_signal_ok[0],
                      feed_age_fn=ib_feed.seconds_since_record,
                      applied_age_fn=ib_feed.seconds_since_applied)
    # Read through the config object so a dashboard-lowered max_position
    # tightens immediately.
    engine.set_position_limit_fn(lambda: config.max_position)

    # Armed only now, with reset hook and clocks in place -- same path as
    # the dashboard checkbox.
    if live_trading:
        engine.set_live_trading(True)

    # Dashboard-tunable config fields. _act reads self.config on every
    # decision, so a plain setattr takes effect on the next evaluation.
    # LOWERING max_position does not unwind existing position, only caps
    # growth; disabling quoting stops new quotes but existing resting is
    # withdrawn by the next decision on each market.
    TUNABLE = ("edge_fraction", "constant_edge", "lean_multiplier", "lean_dollars", "max_position",
               "taker_fee_edge_multiplier", "lot_size",
               "enable_quoting", "quoting_edge", "quote_size", "requote_delay_s")
    override_path = os.path.join(_ARBS_DIR, "..", "data", "live", series, "config_override.json")
    # Seeded to the existing mtime, like trading_mode below: an override
    # saved for an earlier session must not silently retune this one (a
    # September file kept setting constant_edge 0.02 / lean_dollars 0.01).
    # Apply from the dashboard again to use it.
    override_mtime = [os.path.getmtime(override_path) if os.path.exists(override_path) else 0.0]
    if override_mtime[0]:
        print(f"[live] ignoring config_override.json saved "
              f"{pd.Timestamp(override_mtime[0], unit='s', tz='UTC').strftime('%Y-%m-%d %H:%MZ')} "
              f"(before this session); re-apply from the dashboard to use it")

    async def config_loop():
        while True:
            await asyncio.sleep(1.0)
            try:
                if not os.path.exists(override_path):
                    continue
                mtime = os.path.getmtime(override_path)
                if mtime <= override_mtime[0]:
                    continue
                override_mtime[0] = mtime
                with open(override_path) as fh:
                    new = json.load(fh)
                changed = []
                for k in TUNABLE:
                    if k in new and getattr(config, k) != new[k]:
                        changed.append(f"{k} {getattr(config, k)} -> {new[k]}")
                        setattr(config, k, new[k])
                if changed:
                    print(f"[live] config updated: {'; '.join(changed)}")
            except Exception:
                print("[live] WARN: could not apply config override:")
                traceback.print_exc()

    # Dashboard arm/disarm. Separate file from config_override.json so a
    # money-moving control isn't a side effect of Apply.
    trading_path = os.path.join(_ARBS_DIR, "..", "data", "live", series, "trading_mode.json")
    # Seeded to the existing mtime: a leftover `live_trading: true` from a
    # prior session must not re-arm on startup. Arming takes a deliberate
    # click that moves the mtime.
    trading_mtime = [os.path.getmtime(trading_path) if os.path.exists(trading_path) else 0.0]

    async def trading_mode_loop():
        while True:
            await asyncio.sleep(1.0)
            try:
                if not os.path.exists(trading_path):
                    continue
                mtime = os.path.getmtime(trading_path)
                if mtime <= trading_mtime[0]:
                    continue
                trading_mtime[0] = mtime
                with open(trading_path) as fh:
                    want = bool(json.load(fh).get("live_trading", False))
                if want != engine.live_trading:
                    engine.set_live_trading(want)
            except Exception:
                print("[live] WARN: could not apply trading mode:")
                traceback.print_exc()

    async def loop_lag_probe():
        """Ask for a fixed sleep, record the overshoot. That overshoot IS
        the event loop's starvation, measured without assuming what causes
        it."""
        PROBE_S = 0.02
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(PROBE_S)
            _lag = max(0.0, time.perf_counter() - t0 - PROBE_S)
            timings["loop_lag"].append(_lag)
            timings["loop_lag_while_flushing" if flush_busy[0]
                    else "loop_lag_while_idle"].append(_lag)

    async def position_audit_loop():
        """Reconcile our ledger with Kalshi's positions. The only guard
        that uses ground truth rather than our own fill accounting (which
        was the thing that was wrong on 2026-08-28, 4x cap for ~1min)."""
        while True:
            await asyncio.sleep(AUDIT_INTERVAL_S)
            if not engine.live_trading:
                continue
            try:
                loop = asyncio.get_running_loop()
                real = await loop.run_in_executor(
                    None, engine.order_client.positions, event_ticker)
                engine.audit_positions(real)
            except Exception:
                # Failed audit stays stale (not "all clear"); dashboard surfaces it.
                print("[live] WARN: position audit failed:")
                traceback.print_exc()

    async def quote_fill_loop():
        """Book fills on RESTING quotes. Takes get fills on their create-
        order response; a quote fill arrives with no request to answer, so
        this poll is the only path into the ledger."""
        while True:
            await asyncio.sleep(QUOTE_FILL_POLL_S)
            if not engine.live_trading or not engine.exchange_to_order:
                continue
            try:
                loop = asyncio.get_running_loop()
                fills = await loop.run_in_executor(
                    None, functools.partial(engine.order_client.fills, event_ticker,
                                            min_ts=engine.fill_poll_min_ts()))
                engine.apply_real_fills(fills)
            except Exception:
                # Non-fatal; position audit is the only remaining watch while this fails.
                print("[live] WARN: quote fill poll failed:")
                traceback.print_exc()

    async def signal_loop():
        """Reprice at most every SIGNAL_MIN_INTERVAL_S, only when the
        option book actually changed."""
        while True:
            await asyncio.sleep(SIGNAL_MIN_INTERVAL_S)
            # Drain is the only writer to the option book; applied right
            # before it's read.
            if not ib_feed.drain():
                continue
            try:
                _t0 = time.perf_counter()
                recompute_signal()
                _dt = time.perf_counter() - _t0
                timings["recompute"].append(_dt)
                timings["recompute_while_flushing" if flush_busy[0]
                        else "recompute_while_idle"].append(_dt)
            except Exception:
                # One bad recompute must not kill the signal producer.
                print("[live] ERROR in signal recompute:")
                traceback.print_exc()

    flush_busy = [False]
    flush_n = [0]

    def _prune(seq, cap):
        """Halve the oldest half once `seq` passes `cap`. Preserves span
        at reduced resolution (chart decimates to STREAM_POINTS anyway)."""
        if len(seq) <= cap:
            return
        half = len(seq) // 2
        seq[:half] = seq[:half:2]

    def _prune_histories():
        _prune(strategy.debug_log, MAX_DEBUG_ROWS)
        _prune(prob_history, MAX_PROB_ROWS)


    def _write(state):
        t0 = time.perf_counter()
        write_snapshot(state)
        # After snapshot so a journal failure doesn't cost the dashboard
        # its frame; rides the same worker thread.
        try:
            live_journal.write(state.get("journal"))
        except Exception:
            print("[live] WARN: journal write failed:")
            traceback.print_exc()
        return time.perf_counter() - t0

    def _flush_done(fut):
        flush_busy[0] = False
        try:
            timings["flush"].append(fut.result())
        except Exception:
            print("[live] WARN: snapshot write failed:")
            traceback.print_exc()

    async def flush_loop():
        # PnL sampled per flush cycle, not per dispatch (which fire much
        # faster than any chart needs to redraw).
        while True:
            await asyncio.sleep(flush_interval_s)
            now = pd.Timestamp.now(tz="UTC")
            # Also evaluated per-order; here as well so a breach trips
            # within one flush even if the strategy stopped sending orders.
            engine.check_failsafe()
            bias = engine.check_pricing_bias()
            # Resting quotes keep taking risk through a halt until someone
            # withdraws them; this is that someone. Strategy requotes
            # itself once the block clears.
            blocked = engine.blocked_reason()
            if blocked is not None and engine.has_quotes():
                print(f"[live] pulling quotes -- {blocked}")
                engine.pull_quotes()
            append_bias_row(series, now.strftime("%Y-%m-%d"), {
                "time": now.isoformat(),
                "n": bias.get("n"), "n_outside": bias.get("n_outside"),
                "median_vs_mid": bias.get("median"), "median_excess": bias.get("median_excess"),
                "same_side": bias.get("same_side"), "systematic": bias.get("systematic"),
            })
            pnl_history.append((now, compute_live_pnl(engine, now)))
            _prune_histories()
            if flush_busy[0]:
                # Snapshot is overwrite-in-place; skipping beats queueing.
                continue
            _t0 = time.perf_counter()
            flush_n[0] += 1
            state = capture_snapshot(series, event_ticker, strategy, engine, config,
                                     pnl_history, prob_history, now,
                                     heavy=(flush_n[0] % HEAVY_FLUSH_EVERY == 1),
                                     feed_rate=ib_feed.records_per_s(),
                                     timings={**{k: _stat(k) for k in timings},
                                              "write_phases_ms": dict(live_persist.WRITE_PHASES)})
            # Journal drain only on heavy cadence: each drain writes a
            # part-file per stream, so 3s cadence would leave ~25k files
            # by day-end. Cost: hard kill loses up to
            # HEAVY_FLUSH_EVERY * flush_interval_s of buffered rows.
            if state["heavy"]:
                journal.record_fills(engine.portfolio.fills, engine.live_trading)
                state["journal"] = journal.capture()
            timings["capture"].append(time.perf_counter() - _t0)
            flush_busy[0] = True
            fut = asyncio.get_running_loop().run_in_executor(None, _write, state)
            fut.add_done_callback(_flush_done)

    async def until_close():
        """Return once this event stops accepting orders."""
        while True:
            await asyncio.sleep(ROLLOVER_CHECK_S)
            if engine.close_time is None:
                continue   # unresolved: stay on this event
            if pd.Timestamp.now(tz="UTC") >= engine.close_time + pd.Timedelta(seconds=ROLLOVER_GRACE_S):
                return

    print(f"[live] starting feeds for {event_ticker}... "
          f"({'REAL ORDERS' if engine.live_trading else 'shadow fills'})")
    tasks = [asyncio.create_task(c) for c in (
        kalshi_feed.run(), ib_feed.run(), rescope_loop(), signal_loop(), flush_loop(),
        config_loop(), trading_mode_loop(), position_audit_loop(),
        quote_fill_loop(), loop_lag_probe())]
    try:
        await until_close()
        print(f"[live] {event_ticker} has closed -- shutting its feeds down")
    finally:
        for t in tasks:
            t.cancel()
        # Pull quotes BEFORE awaiting task cancellation: a quote left
        # resting after we stop pricing would keep filling unwatched.
        if engine.live_trading:
            try:
                engine.pull_quotes()
                engine.cancel_resting()
            except Exception:
                print(f"[live] WARN: could not pull quotes on shutdown -- CHECK KALSHI BY HAND "
                      f"for resting orders on {event_ticker}:")
                traceback.print_exc()
        # Let each task run its own cleanup (IB disconnect, WS close).
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            write_snapshot(capture_snapshot(series, event_ticker, strategy, engine, config,
                                            pnl_history, prob_history, pd.Timestamp.now(tz="UTC")))
        except Exception:
            print("[live] WARN: final snapshot failed:")
            traceback.print_exc()
        # Journal tail exists nowhere else once the process exits.
        try:
            journal.record_fills(engine.portfolio.fills, engine.live_trading)
            live_journal.write(journal.capture())
        except Exception:
            print("[live] WARN: final journal drain failed:")
            traceback.print_exc()
    # Carried into the next event -- see session_loop.
    return engine.live_trading


if __name__ == "__main__":
    main()
