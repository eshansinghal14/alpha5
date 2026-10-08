"""
Populate the backtest cache one event at a time, in a fresh process each.

Two reasons it is not a loop in one process:

  * memory. A single KXINXU event's quote frame runs to 175M ticks; two of
    them alive at once OOM-kills a 16GB machine, and pandas does not return
    freed blocks to the OS promptly enough to rely on `del`. A process
    boundary is the only reliable release.
  * the code fingerprint is memoised per process (see
    backtest_cache.code_fingerprint), so a fresh process per event also means
    an edit made mid-run is picked up rather than silently ignored.

Usage:
    python warm_backtest_cache.py                 # every runnable event, both series
    python warm_backtest_cache.py KXINX           # one series
    python warm_backtest_cache.py KXINX-26AUG27H1600 ...   # named events
    python warm_backtest_cache.py --force         # recompute even on a cache hit
"""
import os
import subprocess
import sys
import time

_VIZ_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_VIZ_DIR, ".."))


def _one(event_ticker: str, series: str, force: bool) -> int:
    """Child entry point: build (or reuse) one event's DayData."""
    sys.path.insert(0, _VIZ_DIR)
    import backtest_cache
    from data_loader import load_backtest_day, strategy_module

    config = strategy_module(series).QuotingConfig()
    if not force and backtest_cache.load(event_ticker, series, config) is not None:
        print(f"  [cache] {event_ticker}: already current -- skipped", flush=True)
        return 0
    t0 = time.time()
    day = load_backtest_day(event_ticker, series=series, use_cache=False)
    path = backtest_cache.save(day, event_ticker, series, config)
    n_fills = len(day.fills_df)
    pnl = day.summary.get("total_realized_pnl", 0.0) - day.summary.get("total_fees", 0.0)
    print(f"  [cache] {event_ticker}: net {pnl:+.2f}, {n_fills} fills, "
          f"{time.time()-t0:,.0f}s -> {os.path.basename(path)}", flush=True)
    return 0


def _discover(series: str) -> list[str]:
    sys.path.insert(0, _VIZ_DIR)
    from data_loader import discover_days
    return [d["event_ticker"] for d in discover_days(series)]


def main(argv: list[str]) -> int:
    force = "--force" in argv
    args = [a for a in argv if not a.startswith("--")]
    series_args = [a for a in args if a in ("KXINX", "KXINXU")]
    event_args = [a for a in args if "-" in a]

    jobs: list[tuple[str, str]] = []
    if event_args:
        jobs = [(e, e.split("-")[0]) for e in event_args]
    else:
        for series in (series_args or ["KXINX", "KXINXU"]):
            for ev in _discover(series):
                jobs.append((ev, series))

    print(f"warming {len(jobs)} event(s), one process each"
          f"{' (forced)' if force else ''}")
    failed = []
    for i, (ev, series) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {ev}", flush=True)
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--child", ev, series]
        if force:
            cmd.append("--force")
        r = subprocess.run(cmd, cwd=_VIZ_DIR)
        if r.returncode != 0:
            # A day that cannot fit in memory or has no usable data must not
            # take the rest of the run down with it.
            print(f"  [cache] {ev}: FAILED (exit {r.returncode})", flush=True)
            failed.append(ev)
    print(f"\ndone. {len(jobs)-len(failed)}/{len(jobs)} cached."
          + (f" failed: {failed}" if failed else ""))
    return 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        sys.exit(_one(sys.argv[i + 1], sys.argv[i + 2], "--force" in sys.argv))
    sys.exit(main(sys.argv[1:]))
