"""
Refreshes the dashboard's strategy-performance data for every series, out of
the Streamlit process (see perf.read_result), and keeps each series' state on
disk so a restart only catches up.

    python -m kalshi_viz.perf_worker            # every registered series
    python -m kalshi_viz.perf_worker --series KXINXU KXINXDUD
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

from kalshi_viz import perf

REFRESH_S = 30.0
SAVE_EVERY_S = 300.0


def state_path(series: str) -> str:
    return os.path.join(perf.out_dir(series), "state.pkl")


def run_series(series: str, every_s: float = REFRESH_S, save_every_s: float = SAVE_EVERY_S,
               stop: threading.Event | None = None) -> None:
    p = perf.Perf.load(series, state_path(series))
    saved = time.monotonic()
    first = True
    while stop is None or not stop.is_set():
        t = time.monotonic()
        try:
            df = p.refresh()
            perf.write_result(p, df)
            if first or time.monotonic() - saved >= save_every_s:
                p.save(state_path(series))
                saved = time.monotonic()
            if first:
                print(f"[perf] {series}: first pass {time.monotonic() - t:.1f}s, {len(df)} fills", flush=True)
            first = False
        except Exception as e:
            traceback.print_exc()
            try:
                perf.write_error(series, repr(e))
            except OSError:
                pass
        (stop.wait if stop is not None else time.sleep)(max(1.0, every_s - (time.monotonic() - t)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", help="Default: every registered series.")
    ap.add_argument("--every-s", type=float, default=REFRESH_S)
    args = ap.parse_args()
    if args.series:
        names = args.series
    else:
        from kalshi_strategies.run import registry
        names = sorted(registry())
    # One thread per series: a cold catch-up (minutes on the race's history)
    # must not hold back every other series' panel.
    threads = [threading.Thread(target=run_series, args=(s, args.every_s), daemon=True, name=f"perf-{s}")
               for s in names]
    for t in threads:
        t.start()
    print(f"[perf] refreshing {', '.join(names)} every {args.every_s:g}s", flush=True)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
