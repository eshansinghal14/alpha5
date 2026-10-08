"""Backtest harness for the Coin Race market maker.

See `run_backtest.py` for the single public entrypoint. Separate from
kalshi_backtest/ (which is Kalshi-market-only) because this harness also
needs a REPLAY path for the per-second composite -- the strategy ingests
composite ticks via external_events, not just Kalshi order book updates.
"""
