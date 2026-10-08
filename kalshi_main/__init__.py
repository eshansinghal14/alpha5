"""
kalshi_main -- single CLI entrypoint for the alpha5 live stack.

The alpha5 repo has grown multiple live strategies, each with its own
run_live.py (SPX options lag quoting under kalshi_latency_arbs/live/,
Coin Race market making under kalshi_crypto/live/). They need genuinely
different internals -- IB options feed vs Coinbase WS, Breeden-Littzenberger
pricing vs CF-rate composite -- so merging them line-for-line would
multiply coupling without buying anything.

This package centralizes only what IS shared:
  * CLI (which --series) and event resolution
  * data/live/{series}/ directory conventions
  * The decision of which coroutine to invoke

Each series' own run() stays where its code lives.

Usage:
    python kalshi_main/run_live.py --series KXINX
    python kalshi_main/run_live.py --series KXINXU --live-trading
    python kalshi_main/run_live.py --series KXCRYPTOLEAD15M
"""
