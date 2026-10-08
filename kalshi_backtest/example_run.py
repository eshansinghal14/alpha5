"""
Minimal usage example: run strategies/crossing_arb.py through the engine for
one event and print a summary. Run from the repo root as
`python -m kalshi_backtest.example_run [event_ticker]`.
"""
import sys

from kalshi_backtest import data, engine, results
from kalshi_backtest.strategies import crossing_arb


def main():
    event_ticker = sys.argv[1] if len(sys.argv) > 1 else "KXHIGHLAX-26AUG03"

    avail = data.available_sources(event_ticker)
    print(f"available sources for {event_ticker}: {avail}")
    if not any(avail.values()):
        print("nothing cached for this event -- pick one from kalshi_infra's cached data, "
              "or run the relevant backfill script first.")
        return

    event_data = data.load_event_data(event_ticker)
    print(f"resolved source={event_data.source} fidelity={event_data.fidelity} "
          f"(per-ticker overrides: { {t: s for t, s in event_data.source_by_ticker.items() if s != event_data.source} })")

    signals = crossing_arb.find_crossing_signals(event_ticker)
    print(f"{len(signals)} crossing signal(s) found")

    bt = engine.Backtest()  # default latency: wire=1s, send=1s + measured processing time
    result = bt.run(crossing_arb.strategy_fn, event_ticker, event_data=event_data, external_events=signals)

    print("\n--- summary ---")
    for k, v in results.summary(result).items():
        print(f"  {k}: {v}")

    fills = results.fills_df(result)
    if not fills.empty:
        print("\n--- fills ---")
        print(fills.to_string(index=False))


if __name__ == "__main__":
    main()
