"""
Run kxinxu_lag_quoting.py through the kalshi_backtest engine against cached
Kalshi + Databento data and print a PnL summary -- the KXINXU counterpart
of spxw_kxinx/run_backtest.py (see that module's docstring; this one is
structurally identical, just pointed at the KXINXU- series and importing
kxinxu_lag_quoting instead of sp_options_lag_quoting).

Usage (from inside kalshi_latency_arbs/spxw_kxinxu/, or with it on sys.path):
    python run_backtest.py [event_ticker ...]

With no arguments, runs every KXINXU event that has BOTH a cached Kalshi
quote source AND a full Databento SPXW quote file -- see
spxw_kxinx/run_backtest.py's own docstring for exactly what "full" means.

Plots land in spxw_kxinx/analysis/{event_ticker}/ -- deliberately reusing
that folder/module rather than a second copy of markout_analysis.py, since
nothing in it is KXINX-specific (confirmed: the only KXINX references left
in that file are a default argument value, not an assumption baked into
the computation) and event_ticker already disambiguates KXINXU- from
KXINX- days within it.
"""
import glob
import os
import sys

_STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
_ARBS_DIR = os.path.join(_STRATEGY_DIR, "..")
_KXINX_STRATEGY_DIR = os.path.join(_ARBS_DIR, "spxw_kxinx")  # path only, deliberately NOT added to sys.path -- see kxinxu_lag_quoting.py's docstring on the run_backtest.py name collision
_BACKTEST_DIR = os.path.join(_ARBS_DIR, "..", "kalshi_backtest")
_ANALYSIS_DIR = os.path.join(_KXINX_STRATEGY_DIR, "analysis")
for _p in (_STRATEGY_DIR, _BACKTEST_DIR, _ANALYSIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import data
import engine
import results

import kxinxu_lag_quoting as strat
import markout_analysis as ma
import ib_emulation as ibe

_DATABENTO_ROOT = os.path.join(_STRATEGY_DIR, "..", "..", "data", "databento")
_DATABENTO_SPXW_QUOTES_DIR = os.path.join(_DATABENTO_ROOT, "SPXW", "quotes")


def _has_full_spxw_cache(event_ticker: str) -> bool:
    """True only for an UNSCOPED cache (see spxw_kxinx/run_backtest.py's own docstring)."""
    return os.path.exists(os.path.join(_DATABENTO_SPXW_QUOTES_DIR, f"{event_ticker}.parquet"))


def _cached_kalshi_ws_event_tickers() -> set[str]:
    pattern = os.path.join(_STRATEGY_DIR, "..", "..", "data", "kalshi_ws", "*", "raw_ob_ticks", "*.parquet")
    return {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(pattern)}


def discover_runnable_events() -> list[str]:
    # H1600 (4pm) only -- KXINXU also lists other intraday hourly expiries
    # (H1000..H1500), but this strategy was built specifically for the 4pm
    # bucket (same close_time as KXINX) and was never validated against the
    # others; "KXINXU-" alone would also match those if their data ever
    # got cached.
    kalshi_cached = {
        et for et in (
            data.pf.all_cached_predexon_event_tickers()
            + data.pf.all_cached_kalshi_1min_event_tickers()
            + list(_cached_kalshi_ws_event_tickers())
        )
        if et.startswith("KXINXU-") and et.endswith("H1600")
    }
    return sorted(et for et in kalshi_cached if _has_full_spxw_cache(et))


def run_one(event_ticker: str, config: strat.QuotingConfig = None, verbose: bool = True,
            record_debug: bool = False, allow_predexon: bool = True, auto_plot: bool = True,
            conflate_ms: int = ibe.IB_OPTIONS_CONFLATION_MS, transport_ms: float = 0.0
            ) -> tuple[engine.BacktestResult, strat.SPOptionsLagQuotingStrategy]:
    if verbose:
        print(f"=== {event_ticker} ===")
    event_data = data.load_event_data(event_ticker, allow_predexon=allow_predexon)
    if verbose:
        print(f"  kalshi source={event_data.source} fidelity={event_data.fidelity}")

    markets, signal_events = strat.precompute_signal(
        event_ticker, conflate_ms=conflate_ms, transport_ms=transport_ms)
    if verbose:
        print(f"  {len(markets)} markets, {len(signal_events)} option-side signal updates")

    strategy = strat.SPOptionsLagQuotingStrategy(config, record_debug=record_debug)
    # wire_s measured directly from this event's own Kalshi WS ticks (see
    # data.empirical_wire_s) instead of LatencyModel's assumed 1.0s default.
    wire_s = data.empirical_wire_s(event_ticker)
    if verbose:
        print(f"  empirical wire_s={wire_s:.4f}s (vs. assumed default 1.0s)")
    bt = engine.Backtest(latency=engine.LatencyModel(wire_s=wire_s),
                         max_position=strategy.config.max_position)
    result = bt.run(strategy.__call__, event_ticker, event_data=event_data, external_events=signal_events)

    if verbose:
        for k, v in results.summary(result).items():
            print(f"  {k}: {v}")
        fills = results.fills_df(result)
        if not fills.empty:
            print("  --- fills by ticker/side/liquidity ---")
            agg = fills.groupby(["ticker", "side", "liquidity"]).agg(
                n=("price", "size"), total_size=("size", "sum"),
                avg_price=("price", "mean"), total_fees=("fee", "sum"),
            )
            print(agg.to_string())
            print("  --- positions (final, settled) ---")
            print(results.positions_df(result).to_string(index=False))
            print("  --- peak net position (running max |yes_qty - no_qty| over the session) ---")
            print(results.peak_net_position_df(result).to_string(index=False))
        if record_debug:
            qdf = strat.analyze_quote_placement(strategy.debug_log)
            print(f"  --- quote placement classification ({len(qdf)} _requote calls) ---")
            print("  bid_class counts:")
            print(qdf["bid_class"].value_counts().to_string())
            print("  ask_class counts:")
            print(qdf["ask_class"].value_counts().to_string())
            print("  bid_edge / ask_edge / market_spread / edge_vs_half_spread summary:")
            print(qdf[["bid_edge", "ask_edge", "market_spread", "edge_vs_half_spread"]].describe().to_string())

    if auto_plot:
        paths = ma.generate_day_plots(result, event_data, event_ticker, signal_events=signal_events,
                                       config=strategy.config)
        if verbose:
            print(f"  --- plots saved to {ma.day_dir(event_ticker)} ---")
            for name, path in paths.items():
                print(f"    {name}: {path}")

    return result, strategy


def main():
    event_tickers = sys.argv[1:] or discover_runnable_events()
    if not event_tickers:
        print("no events with both a cached Kalshi source and a full Databento SPXW cache -- "
              "nothing to run. Pass event_ticker(s) explicitly.")
        return

    all_results = []
    for et in event_tickers:
        try:
            result, _strategy = run_one(et)
            all_results.append(result)
        except Exception as e:
            print(f"  [skip] {et}: {e}")

    if len(all_results) > 1:
        print("\n=== aggregate across events ===")
        summary_df = results.summarize_many(all_results)
        print(summary_df.to_string(index=False))
        print(f"\ntotal realized pnl across {len(all_results)} event(s): "
              f"${summary_df['total_realized_pnl'].sum():.2f}")
        print(f"total fees: ${summary_df['total_fees'].sum():.2f}")


if __name__ == "__main__":
    main()
