"""
Backtest every runnable event on IB-shaped quotes (see ib_emulation), so the
cost of IB's 100ms conflation is a measured number rather than an
assumption.

Usage:
    python -m kalshi_strategies.KXINX.research.run_ib_sweep                  # both series
    python -m kalshi_strategies.KXINX.research.run_ib_sweep KXINXU           # one series
    python -m kalshi_strategies.KXINX.research.run_ib_sweep KXINXU EVENT...  # specific events
"""
import sys
import traceback

from kalshi_backtest import results
from kalshi_strategies.KXINX.backtest.run_backtest import discover_runnable_events, run_one

# (label, conflate_ms). Conflation was a measured no-op on 2026-08-29: tick,
# 100ms and 250ms were byte-identical across 7 event-days because the 2s
# signal grid is 20x coarser than the finest window. Only the traded config is
# run; re-add others to re-test that if the grid `freq` changes.
SWEEP = [("IB 100ms (live config)", 100)]


def main(series_list, only_events=None):
    for series in series_list:
        events = discover_runnable_events(series)
        if only_events:
            events = [e for e in events if e in only_events]
        print(f"\n{'=' * 92}\n{series}: {len(events)} runnable event(s)\n{'=' * 92}")
        for ev in events:
            print(f"\n--- {ev} ---")
            print(f"{'variant':<22}{'realized PnL':>14}{'fees':>10}{'net':>12}{'orders':>8}{'fills':>7}{'peak pos':>10}")
            for label, cms in SWEEP:
                try:
                    res, _q, _g = run_one(ev, conflate_ms=cms, verbose=False)
                    s = results.summary(res)
                    pnl, fees = s["total_realized_pnl"], s["total_fees"]
                    print(f"{label:<22}{pnl:>14,.2f}{fees:>10,.2f}{pnl - fees:>12,.2f}"
                          f"{s['n_orders']:>8}{s['n_fills']:>7}{s['max_abs_net_position']:>10,.0f}")
                except Exception as e:
                    print(f"{label:<22}{'ERROR':>14}  {type(e).__name__}: {e}")
                    traceback.print_exc()


if __name__ == "__main__":
    args = sys.argv[1:]
    series = [a for a in args if a in ("KXINXU", "KXINXDUD")]
    events = [a for a in args if "-" in a]
    main(series or ["KXINXU", "KXINXDUD"], only_events=events or None)
