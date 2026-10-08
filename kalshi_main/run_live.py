"""
Single live-trading entrypoint for the alpha5 repo.

Replaces the two per-strategy run_live.py commands with one:

    python kalshi_main/run_live.py --series KXINX
    python kalshi_main/run_live.py --series KXINXU --live-trading
    python kalshi_main/run_live.py --series KXCRYPTOLEAD15M
    python kalshi_main/run_live.py --series KXINX KXINXU      # or KXINX,KXINXU

Several series run as one child process each (see launch_many), with the
order-rate budget split between them.

The dispatcher is deliberately thin -- each strategy's run()/session_loop()
still lives beside its own code (SPX in kalshi_latency_arbs/live/,
Coin Race in kalshi_crypto/live/). This file:

  1. Parses the shared `--series` argument.
  2. Parses enough series-specific flags to be useful (series-shared
     flags like --live-trading, --max-position; per-series config
     flags passed through transparently).
  3. Imports the right module and calls its session_loop.

Why not merge the whole loop body: the SPX runner needs an IB options
feed, a Breeden-Litzenberger pricing pass on every option tick, and a
~60-market Kalshi subscription; the Coin Race runner needs a Coinbase
WS, a CF-rate composite sampler, and per-second LIP accrual. Flattening
them into one 1000+ line file with internal if-series branching would
move the coupling into a worse place.

Dashboard integration: viz/app.py's "no live session" message points
here, and the dashboard writes `data/live/{series}/trading_mode.json`
and `data/live/{series}/config_override.json` which both runners
already read.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time


_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


SPX_SERIES = ("KXINX", "KXINXU")
CRYPTO_SERIES = ("KXCRYPTOLEAD15M",)
ALL_SERIES = SPX_SERIES + CRYPTO_SERIES

# How long to wait after Ctrl-C for children to pull their quotes and exit
# before force-terminating. Termination skips run()'s quote pull, so this
# errs long.
SHUTDOWN_GRACE_S = 60.0


def treat_break_as_interrupt() -> None:
    """On Windows, Python's default Ctrl-Break handler kills the process
    without unwinding, so run()'s teardown (pulling resting quotes) is
    skipped. Re-raise it as SIGINT so it takes the same path as Ctrl-C:
    asyncio's handler cancels the main task, or KeyboardInterrupt outside
    an event loop."""
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: signal.raise_signal(signal.SIGINT))


def parse_series(parser: argparse.ArgumentParser, raw: list[str],
                 allowed: tuple[str, ...]) -> list[str]:
    """Accept `--series A B` and `--series A,B`; validate, dedupe, keep order."""
    out = []
    for tok in raw:
        for s in tok.split(","):
            s = s.strip().upper()
            if not s:
                continue
            if s not in allowed:
                parser.error(f"unknown series {s!r}; choose from {', '.join(allowed)}")
            if s not in out:
                out.append(s)
    if not out:
        parser.error("--series needs at least one series")
    return out


def forward_flags(parser: argparse.ArgumentParser, args: argparse.Namespace,
                  skip: tuple[str, ...]) -> list[str]:
    """Rebuild CLI flags for every option that differs from its default,
    so a child process gets the same settings."""
    out = []
    for action in parser._actions:
        if action.dest in skip or action.dest == "help" or not action.option_strings:
            continue
        val = getattr(args, action.dest, None)
        if val == action.default:
            continue
        flag = action.option_strings[-1]
        if isinstance(action, argparse._StoreTrueAction):
            out.append(flag)
        elif val is not None:
            out += [flag, str(val)]
    return out


_print_lock = threading.Lock()


def _pump(series: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        with _print_lock:
            print(f"[{series}] {line.rstrip()}", flush=True)


def launch_many(series_list: list[str], forwarded: list[str],
                total_orders_per_s: float) -> int:
    """Run each series as its own `kalshi_main/run_live.py --series S`
    child and wait for all of them. Returns the worst exit code.

    Kalshi's order-rate limit is per account, so the budget is split
    evenly; otherwise N children would each send up to the full limit.

    Ctrl-C reaches the children directly (same console / process group),
    and each runs its own shutdown -- including pulling resting quotes --
    so the parent only waits, and terminates after SHUTDOWN_GRACE_S.
    """
    per_series = total_orders_per_s / len(series_list)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    procs: list[tuple[str, subprocess.Popen]] = []
    for s in series_list:
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--series", s,
               "--max-orders-per-s", f"{per_series:g}", *forwarded]
        print(f"[kalshi_main] starting {s}: {' '.join(cmd[2:])}", flush=True)
        p = subprocess.Popen(cmd, cwd=_PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1)
        threading.Thread(target=_pump, args=(s, p), daemon=True).start()
        procs.append((s, p))
    print(f"[kalshi_main] {len(procs)} series running; order budget "
          f"{per_series:g}/s each ({total_orders_per_s:g}/s total)", flush=True)

    reported: set[str] = set()

    def _report_exits():
        for s, p in procs:
            if s not in reported and p.poll() is not None:
                reported.add(s)
                print(f"[kalshi_main] {s} exited with code {p.returncode}", flush=True)

    try:
        while any(p.poll() is None for _, p in procs):
            _report_exits()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"[kalshi_main] Ctrl-C: waiting up to {SHUTDOWN_GRACE_S:.0f}s for series to "
              f"pull quotes and exit (Ctrl-C again to force)", flush=True)
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        try:
            while any(p.poll() is None for _, p in procs) and time.monotonic() < deadline:
                _report_exits()
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        for s, p in procs:
            if p.poll() is None:
                print(f"[kalshi_main] force-terminating {s} -- CHECK KALSHI BY HAND "
                      f"for resting orders", flush=True)
                p.terminate()
        for _, p in procs:
            p.wait()
    _report_exits()
    return 0 if all(p.returncode == 0 for _, p in procs) else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--series", nargs="+", required=True, metavar="SERIES",
                   help=f"One or more of {', '.join(ALL_SERIES)} (space- or comma-separated). "
                        "SPX series (KXINX, KXINXU) use the IB options signal; "
                        "KXCRYPTOLEAD15M uses the Coinbase composite. Several series "
                        "run as separate child processes.")
    p.add_argument("--event-ticker", default=None,
                   help="Specific event (else: current/soonest-closing). "
                        "Naming an event disables rollover.")
    p.add_argument("--live-trading", action="store_true",
                   help="Arm REAL orders on start. Default: paper/shadow mode.")
    p.add_argument("--no-carry-arm", action="store_true",
                   help="At event rollover, drop back to shadow instead of "
                        "carrying armed real-trading forward.")
    p.add_argument("--max-orders-per-s", type=float, default=None,
                   help="Client-side rate limit on real orders. Series-specific "
                        "default from the strategy's own run_live.")

    # --- SPX-only (KXINX/KXINXU) config pass-through. Ignored for the
    #     crypto series; validated below so a typo doesn't silently
    #     disappear. ---
    spx = p.add_argument_group("SPX config (KXINX/KXINXU only)")
    spx.add_argument("--edge-fraction", type=float, default=None)
    spx.add_argument("--max-position", type=float, default=None)
    spx.add_argument("--constant-edge", type=float, default=None)
    spx.add_argument("--enable-quoting", action="store_true")
    spx.add_argument("--quoting-edge", type=float, default=None)
    spx.add_argument("--quote-size", type=float, default=None)
    spx.add_argument("--requote-delay-s", type=float, default=None)
    spx.add_argument("--max-loss-usd", type=float, default=80.0,
                     help="(SPX only) Failsafe halt threshold.")
    spx.add_argument("--max-signal-age-s", type=float, default=None,
                     help="(SPX only) Stop trading if the option signal "
                          "hasn't refreshed in this long.")

    # --- Coin Race-only config pass-through. --max-position is shared,
    # declared in the SPX group above and reused here.
    cr = p.add_argument_group("Coin Race config (KXCRYPTOLEAD15M only)")
    cr.add_argument("--ci-vol-multiplier", type=float, default=None)
    return p


def _dispatch_spx(args):
    """Delegate to kalshi_latency_arbs/live/run_live.py's session_loop.

    That file's main() parses argparse itself; rather than re-parse into
    its format we call session_loop directly with a reconstructed
    config -- same wiring main() does internally."""
    import asyncio

    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "live"))
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs"))
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_infra"))
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_backtest"))
    if args.series == "KXINXU":
        sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "spxw_kxinxu"))
        sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "spxw_kxinx"))
        import kxinxu_lag_quoting as series_strat
    else:
        sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "spxw_kxinx"))
        import sp_options_lag_quoting as series_strat

    import run_live as spx_run_live
    from live_orders import DEFAULT_MAX_ORDERS_PER_S
    from live_context import MAX_SIGNAL_AGE_S

    # Reconstruct the SPX QuotingConfig the same way SPX's own main does
    # (kalshi_latency_arbs/live/run_live.py:180).
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

    common = dict(
        max_loss_usd=args.max_loss_usd,
        max_orders_per_s=(args.max_orders_per_s
                          if args.max_orders_per_s is not None else DEFAULT_MAX_ORDERS_PER_S),
        max_signal_age_s=(args.max_signal_age_s
                          if args.max_signal_age_s is not None else MAX_SIGNAL_AGE_S),
    )
    if args.event_ticker:
        # Explicit event: no rollover. Same semantics as SPX's main.
        print(f"[kalshi_main] {args.series}: trading {args.event_ticker} "
              f"(no rollover -- event was named explicitly)")
        asyncio.run(spx_run_live.run(args.series, args.event_ticker,
                                     series_strat, config,
                                     live_trading=args.live_trading, **common))
    else:
        asyncio.run(spx_run_live.session_loop(
            args.series, series_strat, config,
            live_trading=args.live_trading,
            carry_arm=not args.no_carry_arm,
            **common))


def _dispatch_crypto(args):
    """Delegate to kalshi_crypto/live/run_live.py's session_loop."""
    import asyncio
    from kalshi_crypto.live.run_live import session_loop, load_saved_config
    from live_orders import DEFAULT_MAX_ORDERS_PER_S   # same default as SPX

    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "live"))

    # Start from the dashboard's last-applied values; CLI flags override.
    config = load_saved_config()
    for arg, field in [("constant_edge", "constant_edge"),
                       ("edge_fraction", "edge_fraction"),
                       ("ci_vol_multiplier", "ci_vol_multiplier"),
                       ("quote_size", "quote_size"),
                       ("max_position", "max_position")]:
        v = getattr(args, arg, None)
        if v is not None:
            setattr(config, field, v)
    if args.enable_quoting:
        config.enable_quoting = True

    max_orders_per_s = (args.max_orders_per_s
                        if args.max_orders_per_s is not None else DEFAULT_MAX_ORDERS_PER_S)
    asyncio.run(session_loop(
        config=config,
        live_trading=args.live_trading,
        max_orders_per_s=max_orders_per_s,
        carry_arm=not args.no_carry_arm,
        event_ticker_override=args.event_ticker,
    ))


def main(argv: list[str] | None = None):
    treat_break_as_interrupt()
    parser = _build_parser()
    args = parser.parse_args(argv)
    series_list = parse_series(parser, args.series, ALL_SERIES)
    if len(series_list) > 1:
        if args.event_ticker:
            parser.error("--event-ticker names one event, so it needs exactly one --series")
        sys.path.insert(0, os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "live"))
        from live_orders import DEFAULT_MAX_ORDERS_PER_S
        total = args.max_orders_per_s if args.max_orders_per_s is not None else DEFAULT_MAX_ORDERS_PER_S
        sys.exit(launch_many(series_list,
                             forward_flags(parser, args, skip=("series", "max_orders_per_s")),
                             total))
    args.series = series_list[0]
    try:
        if args.series in SPX_SERIES:
            _dispatch_spx(args)
        elif args.series in CRYPTO_SERIES:
            _dispatch_crypto(args)
        else:
            raise ValueError(f"no dispatcher wired for series {args.series!r}")
    except KeyboardInterrupt:
        # asyncio.run has already cancelled the session and run its teardown.
        print(f"[kalshi_main] {args.series} stopped", flush=True)


if __name__ == "__main__":
    main()
