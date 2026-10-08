"""
KXINXU quoting strategy: the "very similar idea" as
spxw_kxinx/sp_options_lag_quoting.py, adapted for KXINXU's market shape --
a ladder of independent, single-sided "S&P >= threshold" markets (Kalshi
strike_type="greater_or_equal", floor_strike set, cap_strike always None --
confirmed directly via the live API), sampled hourly through the day,
rather than KXINX's two-sided floor/cap range buckets.

Why this doesn't need its own pricing logic: the model side needs only ONE
Breeden-Litzenberger CDF evaluation per market (P(S_T >= floor), no
floor/cap difference). latency_arbs_functions.bucket_probabilities_ci
already takes exactly that code path for any floor-only market (it's the
same branch KXINX's own tail "greater than" buckets use) -- so
kalshi_latency_arbs/spxw_signal.py's precompute_atm_signal needs ZERO
changes to work here, just point it at a KXINXU event_ticker instead of a
KXINX one.

Why this doesn't need its own strategy logic either: nothing in
SPOptionsLagQuotingStrategy (position cap, inventory leaning, CI-based
edge sizing, take-only resolution) cares whether its signal came from a
range-bucket difference or a single-sided CDF -- only that it gets
(ticker, prob, prob_lo, prob_hi) per wakeup, which precompute_atm_signal
already provides identically either
way. Reusing that class directly here rather than forking a second copy of
the same ~250 lines: a fork would just be identical logic silently drifting
out of sync over time for zero behavioral difference today. If KXINXU's own
liquidity/microstructure ever warrants GENUINELY different quoting logic
(not just different QuotingConfig numbers), that's the point to actually
split a real second strategy class out into this file -- not before.

Both series therefore share one strategy class, quoting included: enabling
`enable_quoting` here rests orders on KXINXU exactly as it does on KXINX
(see sp_options_lag_quoting.py's module docstring).
"""
import importlib.util
import os
import sys

_STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
_ARBS_DIR = os.path.join(_STRATEGY_DIR, "..")
_KXINX_STRATEGY_DIR = os.path.join(_ARBS_DIR, "spxw_kxinx")
_BACKTEST_DIR = os.path.join(_ARBS_DIR, "..", "kalshi_backtest")
for _p in (_ARBS_DIR, _BACKTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module_from_path(module_name: str, file_path: str):
    """Loads a module by file path instead of sys.path + plain `import` --
    needed specifically because spxw_kxinx/ has its OWN run_backtest.py,
    same base name as this folder's. Confirmed directly this collision is
    real, not theoretical: adding spxw_kxinx/ to sys.path (the "obvious"
    way to reach sp_options_lag_quoting.py) made a later `import
    run_backtest` anywhere in the same process silently resolve to the
    KXINX one instead of this folder's, no error, just the wrong module.
    importlib loads the target file directly, bypassing sys.path
    resolution (and any name collision risk) for this one import."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # so the loaded module's own relative/internal lookups behave normally
    spec.loader.exec_module(module)
    return module


# Re-exports, not private imports -- see module docstring on why both the
# signal computation and the strategy class are reused verbatim rather than
# duplicated. `import kxinxu_lag_quoting as strat; strat.QuotingConfig()` /
# `strat.precompute_atm_signal(...)` work the same way run_backtest.py in
# the KXINX folder uses `sp_options_lag_quoting as strat`.
from spxw_signal import precompute_signal, precompute_atm_signal, compute_prob_grid  # noqa: F401

_sp_options_lag_quoting = _load_module_from_path(
    "_kxinx_sp_options_lag_quoting", os.path.join(_KXINX_STRATEGY_DIR, "sp_options_lag_quoting.py"))
QuotingConfig = _sp_options_lag_quoting.QuotingConfig
SPOptionsLagQuotingStrategy = _sp_options_lag_quoting.SPOptionsLagQuotingStrategy
analyze_quote_placement = _sp_options_lag_quoting.analyze_quote_placement
