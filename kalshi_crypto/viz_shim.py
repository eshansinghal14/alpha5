"""
Dashboard shim so `viz/data_loader.py` can register KXCRYPTOLEAD15M as a
first-class series without a parallel branch for every SPX-shaped
accessor.

The viz loads one `run_backtest` module per series via
`importlib.util.spec_from_file_location` and expects it to expose:
  * `discover_runnable_events()` -> list[str]
  * `run_one(event_ticker, config=None, ...)` -> something DayData-shaped
  * `strat` -- a module with `QuotingConfig`, a Strategy class,
    `precompute_signal`

This file re-exports those symbols from `kalshi_crypto.backtest.run_backtest`
and `kalshi_crypto.*` so a single `_load_module_from_path` call works.
`QuotingConfig = CoinRaceConfig` by name alias so viz widgets whose
help text/defaults assume the SPX config shape at least don't crash
when they encounter one of our fields -- they instead read the matching
`CoinRaceConfig` field (if any) or fall through to the dataclass
defaults.

Historical mode currently needs `precompute_signal` to produce a prob
grid (option-implied for SPX; the Coin Race equivalent is the composite
replay in `backtest.run_backtest`). This shim exposes a shim
`precompute_signal` that returns the composite-driven external events;
the dashboard's `_mask_stale_bbo` and `_build_quote_df` work on debug
log columns alone and so are unaffected.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

# Ensure the project root is on sys.path so these absolute imports work
# regardless of whether this module was loaded as `kalshi_crypto.viz_shim`
# (package import) or via `importlib.util.spec_from_file_location`
# (dashboard load path) -- the latter doesn't establish a package
# context, so relative imports fail.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from kalshi_crypto.config import CoinRaceConfig
from kalshi_crypto.strategy import CoinRaceStrategy
from kalshi_crypto.backtest.run_backtest import run_one as _run_one_impl, discover_runnable_events


# viz calls `strat.QuotingConfig()` -- make our dataclass the same name
# at the module-level so the alias is seamless.
QuotingConfig = CoinRaceConfig


class _StratShim:
    """Mimics the shape of `sp_options_lag_quoting` as seen by
    data_loader._RB[series].strat."""
    QuotingConfig = CoinRaceConfig
    SPOptionsLagQuotingStrategy = CoinRaceStrategy

    @staticmethod
    def precompute_signal(event_ticker: str, return_grid: bool = True,
                          should_abort=None):
        """SPX-shaped tuple: (markets, signal_events, grid, lo, mid, hi).

        For the Coin Race series the "signal" is the composite tick
        stream; markets come from the Kalshi event. Return a reasonable
        shape so viz doesn't crash -- the historical dashboard isn't
        yet fully supported for this series (the composite replay needs
        both Kalshi book history AND persisted 1s composite bars),
        and we signal that via empty frames.
        """
        from .event_state import event_markets, parse_event_window
        markets_dict = event_markets(event_ticker)
        markets = list(markets_dict.values())
        signal_events = []
        grid = pd.DatetimeIndex([])
        empty = pd.DataFrame()
        if return_grid:
            return markets, signal_events, grid, empty, empty, empty
        return markets, signal_events


strat = _StratShim()


def run_one(event_ticker: str, config: CoinRaceConfig | None = None, **kw):
    """Rough-equivalent of SPX's run_one: returns `(BacktestResult,
    strategy)` tuple. Viz's `_build_quote_df` reads `.debug_log` off
    the second element, same as the SPX path.
    """
    r = _run_one_impl(event_ticker, config=config)
    return r.bt_result, r.strategy


__all__ = ["QuotingConfig", "strat", "run_one", "discover_runnable_events"]
