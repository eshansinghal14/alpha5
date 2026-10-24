"""
Run one or more Kalshi strategies live (shadow by default).

    python -m kalshi_strategies.run --series KXINX                  # every S&P series + the pricing hub
    python -m kalshi_strategies.run --series KXINX KXCRYPTOLEAD15M
    python -m kalshi_strategies.run --series KXINX --only KXINXU    # one series of the folder
    python -m kalshi_strategies.run --series KXINX --live-trading   # REAL MONEY from the start
    python -m kalshi_strategies.run --only KXINX --event-ticker KXINX-26OCT09H1600

--series takes strategy folder names (kalshi_strategies/<FOLDER>) and runs
every series that folder registers, plus its shared SERVICES. --only names
series tickers directly. All tuning lives in each strategy folder's
config.json (edited from the dashboard).

Several series run as one child process each -- isolation from each other's
CPU stalls and crashes. Kalshi's write budget is per exchange SHARD (read
from /account/limits at each event start; every order write is pinned to
its event's shard), so series split it only with others on the same shard.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import pkgutil
import signal
import socket
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
SERVICE_START_TIMEOUT_S = 20.0
SERVICE_RESTART_S = 3.0


def folders() -> dict[str, object]:
    """Strategy folder name -> its module, for every folder defining SERIES."""
    out = {}
    for m in pkgutil.iter_modules(kalshi_strategies.__path__):
        if not m.ispkg:
            continue
        mod = importlib.import_module(f"kalshi_strategies.{m.name}")
        if getattr(mod, "SERIES", None):
            out[m.name] = mod
    return out


def registry() -> dict:
    """series ticker -> Series, from every kalshi_strategies/<FOLDER>/__init__.py
    that defines SERIES. Adding a strategy = adding a folder."""
    out = {}
    for mod in folders().values():
        for ticker, s in mod.SERIES.items():
            if ticker in out:
                raise RuntimeError(f"series {ticker} registered twice")
            out[ticker] = s
    return out


def _treat_break_as_interrupt():
    # Windows' default Ctrl-Break kills without unwinding, skipping the quote pull.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: signal.raise_signal(signal.SIGINT))


def _tokens(raw) -> list[str]:
    return [s.strip().upper() for tok in (raw or []) for s in tok.split(",") if s.strip()]


def resolve(parser, folder_args, only_args, fmap) -> tuple[list[str], list]:
    """(series tickers, services to run). --series names folders (all their
    series and services); --only names series tickers (plus their folder's
    services)."""
    by_folder = {name.upper(): mod for name, mod in fmap.items()}
    owner = {t: mod for mod in fmap.values() for t in mod.SERIES}
    series, mods = [], []
    for name in _tokens(folder_args):
        mod = by_folder.get(name)
        if mod is None:
            parser.error(f"unknown strategy {name!r}; folders: {', '.join(sorted(by_folder))}"
                         + ("" if name not in owner else f" (to run just {name}, use --only {name})"))
        series += list(mod.SERIES)
        mods.append(mod)
    for t in _tokens(only_args):
        if t not in owner:
            parser.error(f"unknown series {t!r}; known: {', '.join(sorted(owner))}")
        series.append(t)
        mods.append(owner[t])
    series = list(dict.fromkeys(series))
    if not series:
        parser.error("name a strategy with --series or a series with --only")
    services, seen = [], set()
    for mod in mods:
        for svc in getattr(mod, "SERVICES", []):
            if svc.name not in seen:
                seen.add(svc.name)
                services.append(svc)
    return series, services


_print_lock = threading.Lock()


def _pump(label, proc):
    for line in proc.stdout:
        with _print_lock:
            print(line.rstrip(), flush=True)


def write_shares(units: list[list[str]], shard_of) -> dict[str, float]:
    """Each process's share of the write budget, keyed by its first series.
    `units` are the processes to start, each a list of series (a write group
    is one process). Writes pinned to a shard bill only that shard's bucket
    (each with the full tier budget), so processes only split the budget with
    others ON THE SAME SHARD. A process whose shard can't be looked up is
    assumed to share with everyone (the old, safe split)."""
    shards = {}
    for unit in units:
        try:
            got = {shard_of(s) for s in unit}
            shards[unit[0]] = got.pop() if len(got) == 1 else None
        except Exception as e:
            print(f"[run] WARN: shard lookup for {unit[0]} failed ({e!r}); sharing the whole budget", flush=True)
            shards[unit[0]] = None
    if any(v is None for v in shards.values()):
        return {u[0]: 1.0 / len(units) for u in units}
    n = {}
    for v in shards.values():
        n[v] = n.get(v, 0) + 1
    return {k: 1.0 / n[v] for k, v in shards.items()}


def units_of(series_list: list[str], reg: dict) -> list[list[str]]:
    """Processes to start: one per write group (all its series together),
    one per ungrouped series. Order follows series_list."""
    units, by_group = [], {}
    for s in series_list:
        g = reg[s].write_group
        if g is None:
            units.append([s])
        elif g in by_group:
            by_group[g].append(s)
        else:
            by_group[g] = [s]
            units.append(by_group[g])
    return units


def _child_env(extra: dict | None = None) -> dict:
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **(extra or {})}


def _popen(cmd, env):
    p = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=_pump, args=(cmd[-1], p), daemon=True).start()
    return p


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class ServiceKeeper:
    """Starts each service, restarts it if it dies, and stops it on exit.
    A service already listening on its port (another launcher's) is reused
    and left alone."""

    def __init__(self, services):
        self.services = services
        self.procs: dict[str, subprocess.Popen | None] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spawn(self, svc) -> subprocess.Popen | None:
        if _listening(svc.port()):
            print(f"[run] {svc.name}: already running on port {svc.port()}; reusing it", flush=True)
            return None
        p = _popen([sys.executable, "-u", "-m", svc.module, "--parent-pid", str(os.getpid())], _child_env())
        print(f"[run] started {svc.name} (pid {p.pid})", flush=True)
        return p

    def start(self):
        for svc in self.services:
            self.procs[svc.name] = self._spawn(svc)
        for svc in self.services:
            deadline = time.monotonic() + SERVICE_START_TIMEOUT_S
            while not _listening(svc.port()) and time.monotonic() < deadline:
                p = self.procs[svc.name]
                if p is not None and p.poll() is not None:
                    break
                time.sleep(0.2)
            if not _listening(svc.port()):
                print(f"[run] WARN: {svc.name} not listening on {svc.port()} yet; series will retry", flush=True)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self):
        while not self._stop.wait(SERVICE_RESTART_S):
            for svc in self.services:
                p = self.procs.get(svc.name)
                if p is not None and p.poll() is None:
                    continue
                if p is None and _listening(svc.port()):
                    continue    # someone else's, still up
                if p is not None:
                    print(f"[run] {svc.name} exited with code {p.returncode}; restarting", flush=True)
                self.procs[svc.name] = self._spawn(svc)

    def stop(self):
        self._stop.set()
        for name, p in self.procs.items():
            if p is not None and p.poll() is None:
                p.terminate()
        for p in self.procs.values():
            if p is not None:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()


def launch_many(units: list[list[str]], args) -> int:
    from kalshi_infra.kalshi.markets import series_shard
    reg = registry()
    # A runner strategy's ticker isn't a Kalshi series (no shard to look up);
    # it pins every write to its markets' shards, so the share doesn't apply.
    own = [u for u in units if reg[u[0]].runner is not None]
    shares = {**write_shares([u for u in units if u not in own], series_shard), **{u[0]: 1.0 for u in own}}
    procs = []
    for unit in units:
        s = ", ".join(unit)
        share = shares[unit[0]]
        print(f"[run] {s}: {share:.0%} of its shard's write budget"
              + (" (one shared bucket)" if len(unit) > 1 else ""), flush=True)
        cmd = [sys.executable, "-u", "-m", "kalshi_strategies.run", "--only", *unit, "--no-services"]
        if args.live_trading:
            cmd.append("--live-trading")
        if args.no_carry_arm:
            cmd.append("--no-carry-arm")
        if args.resume_arm_h:
            cmd += ["--resume-arm-h", str(args.resume_arm_h)]
        p = _popen(cmd, _child_env({WRITE_SHARE_ENV: f"{share:.6f}"}))
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
    fmap = folders()
    reg = registry()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", nargs="+", default=None, metavar="STRATEGY",
                    help=f"Strategy folders; runs all their series. Known: {', '.join(sorted(fmap))}")
    ap.add_argument("--only", nargs="+", default=None, metavar="SERIES",
                    help=f"Individual series tickers. Known: {', '.join(sorted(reg))}")
    ap.add_argument("--event-ticker", default=None, help="Trade this one event only (no rollover).")
    ap.add_argument("--live-trading", action="store_true",
                    help="Arm REAL orders at start. Default shadow; can be armed from the dashboard.")
    ap.add_argument("--no-carry-arm", action="store_true",
                    help="Drop back to shadow at each event rollover.")
    ap.add_argument("--resume-arm-h", type=float, default=0.0, metavar="HOURS",
                    help="After a restart, start armed if the dashboard's last request was an arm made within "
                         "this many hours and no STOP (stop-all, loss halt, balance watchdog) came after it. "
                         "0 = always start in shadow.")
    ap.add_argument("--no-services", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    series_list, services = resolve(ap, args.series, args.only, fmap)

    # Validate every config before starting anything.
    for s in series_list:
        load(reg[s].config_cls, reg[s].config_path, reg[s].config_key)
    if args.event_ticker and len(series_list) > 1:
        ap.error("--event-ticker names one event, so it needs exactly one series (use --only)")

    keeper = ServiceKeeper([] if args.no_services else services)
    keeper.start()
    try:
        units = units_of(series_list, reg)
        if len(units) > 1:
            sys.exit(launch_many(units, args))
        from kalshi_infra.runtime.priority import set_priority
        # Trading outranks everything else on the box. Children inherit this and
        # the writer drops itself back down; may be refused without privileges.
        # ALPHA5_PRIORITY lets a second strategy on the same box sit below the
        # first (deploy/systemd/alpha5-spx.service runs S&P at "normal").
        set_priority(os.environ.get("ALPHA5_PRIORITY", "above_normal"))
        try:
            asyncio.run(_run_unit([reg[s] for s in units[0]], args))
        except KeyboardInterrupt:
            print(f"[{', '.join(units[0])}] stopped", flush=True)
    finally:
        keeper.stop()


async def _run_unit(series, args):
    """Every series of one process on one event loop (a write group shares
    its bucket and quote queue only within a process)."""
    from kalshi_infra.runtime.session import session_loop
    resume = args.resume_arm_h * 3600 or None

    def one(s):
        if s.runner is not None:
            return s.runner(s, live_trading=args.live_trading, resume_arm_s=resume)
        return session_loop(s, live_trading=args.live_trading, carry_arm=not args.no_carry_arm,
                            event_ticker=args.event_ticker, resume_arm_s=resume)
    await asyncio.gather(*(one(s) for s in series))


if __name__ == "__main__":
    main()
