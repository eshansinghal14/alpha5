"""
Backtest every runnable event on IB-shaped quotes against the raw Databento
baseline, so the cost of IB's 100ms conflation is a measured number rather
than an assumption. See ib_emulation for what "IB-shaped" means.

Runs one variant per process-free pass but reuses the cached quote parquet,
so the repeated cost is the pricing pass, not the download.

Usage:
    python run_ib_sweep.py                  # both strategies
    python run_ib_sweep.py KXINX            # one series
    python run_ib_sweep.py KXINX EVENT...   # specific events
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))

# (label, conflate_ms, transport_ms). transport is swept rather than fixed:
# IB exposes no exchange timestamp, so this project has no measurement of its
# exchange->Gateway->socket delay, and a single guessed constant would read
# as calibrated when it isn't.
SWEEP = [
    # Conflation is a measured no-op for this strategy: tick-by-tick, IB
    # 100ms and IB 250ms were byte-identical across 7 event-days (2026-08-29)
    # because the 2s signal grid is 20x coarser than the finest window. Only
    # the config we would actually trade is run; re-add the others to re-test
    # that finding if `freq` ever changes.
    ("IB 100ms (live config)", 100, 0.0),
]


def _runner(series: str):
    """Import the right run_backtest. The two strategies have same-named
    modules and deliberately-separate sys.paths, so this swaps the strategy
    dir to the front and drops any previously-imported copy."""
    strat_dir = os.path.join(_HERE, "spxw_kxinx" if series == "KXINX" else "spxw_kxinxu")
    for p in (os.path.join(_HERE, "..", "kalshi_infra"), _HERE, strat_dir):
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    for mod in ("run_backtest", "sp_options_lag_quoting", "kxinxu_lag_quoting", "markout_analysis"):
        sys.modules.pop(mod, None)
    import importlib
    return importlib.import_module("run_backtest"), importlib.import_module("results")


def main(series_list, only_events=None):
    for series in series_list:
        try:
            rb, results = _runner(series)
        except Exception:
            print(f"\n{series}: could not import runner"); traceback.print_exc(); continue

        events = rb.discover_runnable_events()
        if only_events:
            events = [e for e in events if e in only_events]
        print(f"\n{'='*92}\n{series}: {len(events)} runnable event(s)\n{'='*92}")
        if not events:
            print("  (none -- needs both a cached Kalshi source and a full SPXW quote cache)")
            continue

        for ev in events:
            print(f"\n--- {ev} ---")
            print(f"{'variant':<22}{'realized PnL':>14}{'fees':>10}{'net':>12}"
                  f"{'orders':>8}{'fills':>7}{'peak pos':>10}")
            for label, cms, tms in SWEEP:
                try:
                    res, _ = rb.run_one(ev, verbose=False, auto_plot=False,
                                        conflate_ms=cms, transport_ms=tms)
                    s = results.summary(res)
                    pnl, fees = s["total_realized_pnl"], s["total_fees"]
                    print(f"{label:<22}{pnl:>14,.2f}{fees:>10,.2f}{pnl - fees:>12,.2f}"
                          f"{s['n_orders']:>8}{s['n_fills']:>7}{s['max_abs_net_position']:>10,.0f}")
                except Exception as e:
                    print(f"{label:<22}{'ERROR':>14}  {type(e).__name__}: {e}")


if __name__ == "__main__":
    args = sys.argv[1:]
    series = [a for a in args if a in ("KXINX", "KXINXU")]
    events = [a for a in args if "-" in a]
    main(series or ["KXINX", "KXINXU"], only_events=events or None)
