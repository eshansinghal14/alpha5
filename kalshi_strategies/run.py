"""
Run one or more Kalshi series live (shadow by default).

    python -m kalshi_strategies.run --series KXINX
    python -m kalshi_strategies.run --series KXINX KXINXU KXCRYPTOLEAD15M
    python -m kalshi_strategies.run --series KXINXU --live-trading        # REAL MONEY from the start
    python -m kalshi_strategies.run --series KXINX --event-ticker KXINX-26OCT09H1600

All tuning lives in each strategy folder's config.json (edited from the
dashboard). Several series run as one child process each -- isolation from
each other's CPU stalls and crashes. Kalshi's write budget is per exchange
SHARD (read from /account/limits at each event start; every order write is
pinned to its event's shard), so series split it only with others on the same
shard.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import pkgutil
import signal
import subprocess
import sys
import threading
import time

import urllib3

import kalshi_strategies
from kalshi_infra import REPO_ROOT
from kalshi_infra.config import load
from kalshi_infra.kalshi.orders import WRITE_SHARE_ENV

# Silences InsecureRequestWarning spam seen on the user's terminal (source not
# found in this repo; verification itself is unchanged).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Wait this long after Ctrl-C for children to pull quotes before force-killing
# (a kill skips the quote pull, so this errs long).
SHUTDOWN_GRACE_S = 60.0


def registry() -> dict:
    """series ticker -> Series, from every kalshi_strategies/<FOLDER>/__init__.py
    that defines SERIES. Adding a strategy = adding a folder."""
    out = {}
    for m in pkgutil.iter_modules(kalshi_strategies.__path__):
        if not m.ispkg:
            continue
        mod = importlib.import_module(f"kalshi_strategies.{m.name}")
        for ticker, s in getattr(mod, "SERIES", {}).items():
            if ticker in out:
                raise RuntimeError(f"series {ticker} registered twice")
            out[ticker] = s
    return out


def _treat_break_as_interrupt():
    # Windows' default Ctrl-Break kills without unwinding, skipping the quote pull.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: signal.raise_signal(signal.SIGINT))


def _parse_series(parser, raw, known) -> list[str]:
    out = []
    for tok in raw:
        for s in tok.split(","):
            s = s.strip().upper()
            if not s:
                continue
            if s not in known:
                parser.error(f"unknown series {s!r}; known: {', '.join(sorted(known))}")
            if s not in out:
                out.append(s)
    if not out:
        parser.error("--series needs at least one series")
    return out


_print_lock = threading.Lock()


def _pump(series, proc):
    for line in proc.stdout:
        with _print_lock:
            print(line.rstrip(), flush=True)


def write_shares(series_list: list[str], shard_of) -> dict[str, float]:
    """Each series' share of the write budget. Writes pinned to a shard bill
    only that shard's bucket (each with the full tier budget), so series only
    split the budget with others ON THE SAME SHARD. A series whose shard
    can't be looked up is assumed to share with everyone (the old, safe split)."""
    shards = {}
    for s in series_list:
        try:
            shards[s] = shard_of(s)
        except Exception as e:
            print(f"[run] WARN: shard lookup for {s} failed ({e!r}); sharing the whole budget", flush=True)
            shards[s] = None
    if any(v is None for v in shards.values()):
        return {s: 1.0 / len(series_list) for s in series_list}
    n = {}
    for v in shards.values():
        n[v] = n.get(v, 0) + 1
    return {s: 1.0 / n[shards[s]] for s in series_list}


def launch_many(series_list: list[str], args) -> int:
    from kalshi_infra.kalshi.markets import series_shard
    shares = write_shares(series_list, series_shard)
    procs = []
    for s in series_list:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               WRITE_SHARE_ENV: f"{shares[s]:.6f}"}
        print(f"[run] {s}: {shares[s]:.0%} of its shard's write budget", flush=True)
        cmd = [sys.executable, "-u", "-m", "kalshi_strategies.run", "--series", s]
        if args.live_trading:
            cmd.append("--live-trading")
        if args.no_carry_arm:
            cmd.append("--no-carry-arm")
        p = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=_pump, args=(s, p), daemon=True).start()
        procs.append((s, p))
        print(f"[run] started {s} (pid {p.pid})", flush=True)
    reported = set()

    def report():
        for s, p in procs:
            if s not in reported and p.poll() is not None:
                reported.add(s)
                print(f"[run] {s} exited with code {p.returncode}", flush=True)

    try:
        while any(p.poll() is None for _, p in procs):
            report()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"[run] Ctrl-C: waiting up to {SHUTDOWN_GRACE_S:.0f}s for series to pull quotes "
              f"(Ctrl-C again to force)", flush=True)
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        try:
            while any(p.poll() is None for _, p in procs) and time.monotonic() < deadline:
                report()
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        for s, p in procs:
            if p.poll() is None:
                print(f"[run] force-terminating {s} -- CHECK KALSHI BY HAND for resting orders", flush=True)
                p.terminate()
        for _, p in procs:
            p.wait()
    report()
    return 0 if all(p.returncode == 0 for _, p in procs) else 1


def main(argv=None):
    _treat_break_as_interrupt()
    reg = registry()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", nargs="+", required=True, metavar="SERIES",
                    help=f"Space- or comma-separated. Known: {', '.join(sorted(reg))}")
    ap.add_argument("--event-ticker", default=None, help="Trade this one event only (no rollover).")
    ap.add_argument("--live-trading", action="store_true",
                    help="Arm REAL orders at start. Default shadow; can be armed from the dashboard.")
    ap.add_argument("--no-carry-arm", action="store_true",
                    help="Drop back to shadow at each event rollover.")
    args = ap.parse_args(argv)
    series_list = _parse_series(ap, args.series, reg)

    # Validate every config before starting anything.
    for s in series_list:
        load(reg[s].config_cls, reg[s].config_path, reg[s].config_key)

    if len(series_list) > 1:
        if args.event_ticker:
            ap.error("--event-ticker names one event, so it needs exactly one --series")
        sys.exit(launch_many(series_list, args))

    from kalshi_infra.runtime.priority import set_priority
    from kalshi_infra.runtime.session import session_loop
    # Trading outranks everything else on the box. Children inherit this and
    # the writer drops itself back down; may be refused without privileges.
    set_priority("above_normal")
    s = series_list[0]
    try:
        asyncio.run(session_loop(reg[s], live_trading=args.live_trading, carry_arm=not args.no_carry_arm,
                                 event_ticker=args.event_ticker))
    except KeyboardInterrupt:
        print(f"[{s}] stopped", flush=True)


if __name__ == "__main__":
    main()
