"""Live (paper or real) runner for the Coin Race market maker.

See `run_live.py`. Paper mode is the default -- the harness never calls
the real Kalshi order endpoints unless `--live-trading` is passed AND
the dashboard's trading-mode file flips to armed.
"""
