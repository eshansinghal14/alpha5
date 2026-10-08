"""
kalshi_crypto -- Coin Race 15-minute (KXCRYPTOLEAD15M) market maker.

Primary objective: Kalshi Liquidity Incentive Program (LIP) rewards from
resting two-sided size on all 5 markets of each event. Secondary objective:
avoid adverse selection via a real-time per-coin theoretical fair value.

Entry points
------------
  kalshi_crypto.strategy.CoinRaceStrategy
      strategy_fn(ctx) class with the same ctx surface
      SPOptionsLagQuotingStrategy uses -- plugs straight into both
      kalshi_backtest.engine.Backtest and
      kalshi_latency_arbs.live.live_context.LiveEngine.

  kalshi_crypto.live.run_live
      Paper-trading entrypoint (dry-run is default ON).

  kalshi_crypto.backtest.run_backtest
      Replay historical events (needs recorded 1s composite bars).

See `OpenCode Build Prompt  Kalshi Coin Race LIP Market Maker.md` in this
directory for the full spec.
"""
