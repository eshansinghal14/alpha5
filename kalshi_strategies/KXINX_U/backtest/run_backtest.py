"""
Backtest the generic Quoter on a KXINX / KXINXU event: cached Kalshi quotes
+ Databento SPXW options, through kalshi_backtest.engine with the same
config.json section the live session uses.

    python -m kalshi_strategies.KXINX_U.backtest.run_backtest KXINX [event_ticker ...]

With no event tickers, runs every event of the series that has both a cached
Kalshi source and an unscoped (full-chain) SPXW cache.
"""
from __future__ import annotations

import glob
import os
import sys
import types

import pandas as pd

from kalshi_backtest import data, engine, results
from kalshi_infra import DATA_ROOT
from kalshi_infra.config import load
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.quoter import Quoter
from kalshi_strategies.KXINX_U import ib_emulation as ibe
from kalshi_strategies.KXINX_U import signal_grid
from kalshi_strategies.KXINX_U.config import CONFIG_PATH, SpxConfig

_SPXW_QUOTES = os.path.join(DATA_ROOT, "databento", "SPXW", "quotes")


def discover_runnable_events(series: str) -> list[str]:
    ws = {os.path.splitext(os.path.basename(p))[0]
          for p in glob.glob(os.path.join(DATA_ROOT, "kalshi_ws", "*", "raw_ob_ticks", "*.parquet"))}
    cached = set(data.pf.all_cached_predexon_event_tickers()) | set(
        data.pf.all_cached_kalshi_1min_event_tickers()) | ws
    return sorted(et for et in cached if et.startswith(f"{series}-")
                  and os.path.exists(os.path.join(_SPXW_QUOTES, f"{et}.parquet")))


class _BacktestModel:
    """Fairs arrive as precomputed external events, so the model is inert;
    staleness is meaningless on a replay grid."""

    def on_market_event(self, ticker, now_s):
        return None

    def signal_age_s(self):
        return 0.0

    def side_block(self, *a):
        return None


def run_one(event_ticker: str, cfg=None, *, conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS,
            allow_predexon: bool = True, verbose: bool = True):
    """Returns (BacktestResult, Quoter, probs_mid grid)."""
    series = event_ticker.split("-")[0]
    cfg = cfg or load(SpxConfig, CONFIG_PATH, series)
    # Freshness and close-time guards are wall-clock live concepts.
    cfg = types.SimpleNamespace(**{**vars(cfg), "risk": types.SimpleNamespace(
        **{**vars(cfg.risk), "max_signal_age_s": float("inf"),
           "fresh_recovery_s": 0.0})})
    event_data = data.load_event_data(event_ticker, allow_predexon=allow_predexon)
    markets, events, _grid, _lo, probs_mid, _hi = signal_grid.precompute_signal(
        event_ticker, conflate_ms=conflate_ms, return_grid=True)
    ext = [(t, FairUpdate({p["ticker"]: Fair(p["prob"], p["prob_lo"], p["prob_hi"])})) for t, p in events]
    close = pd.Timestamp(min(m["close_time"] for m in markets))
    # Fees as of the event (the series' schedule can change between days).
    fee_sched = kfees.for_event(series, event_ticker, at=close)
    quoter = Quoter(cfg, _BacktestModel(), fee_sched, close_time=close, record_debug=True)
    bt = engine.Backtest(latency=engine.LatencyModel(wire_s=data.empirical_wire_s(event_ticker)),
                         fee_multiplier=fee_sched.taker_rate,
                         maker_fee_multiplier=fee_sched.maker_rate,
                         max_position=cfg.quoting.max_position)
    result = bt.run(quoter.__call__, event_ticker, event_data=event_data, external_events=ext)
    if verbose:
        print(f"=== {event_ticker} ({event_data.source}/{event_data.fidelity}, {len(ext)} fair updates) ===")
        for k, v in results.summary(result).items():
            print(f"  {k}: {v}")
    return result, quoter, probs_mid


def main(argv: list[str]):
    if not argv:
        print(__doc__)
        return
    events = argv[1:] or discover_runnable_events(argv[0])
    out = []
    for et in events:
        try:
            out.append(run_one(et)[0])
        except Exception as e:
            print(f"  [skip] {et}: {e}")
    if len(out) > 1:
        df = results.summarize_many(out)
        print(df.to_string(index=False))
        print(f"total realized pnl: ${df['total_realized_pnl'].sum():.2f}  fees ${df['total_fees'].sum():.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
