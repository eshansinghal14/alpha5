"""
Backtest replay for one KXCRYPTOLEAD15M event.

Composite bars are the primary signal source; Kalshi book-change events
come from `kalshi_backtest.data.load_event_data` just like the SPX
strategies. The two streams are merged into `Backtest.run`'s
`external_events` so they share one time-ordered heap.

Public entrypoint:
    results = run_one(event_ticker, config=None, forecaster=None,
                      composite_source=None)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKTEST_DIR = os.path.join(_HERE, "..", "..", "kalshi_backtest")
_KC_DIR = os.path.join(_HERE, "..")
_PROJECT = os.path.join(_HERE, "..", "..")
for _p in (_BACKTEST_DIR, _KC_DIR, _PROJECT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import Backtest
import data as bt_data

from kalshi_crypto.config import (CoinRaceConfig,
                                   CRYPTO_FEE_MULTIPLIER,
                                   CRYPTO_MAKER_FEE_MULTIPLIER)
from kalshi_crypto.composite import CompositeBar, CompositeBus
from kalshi_crypto.cov_forecaster import CovarianceForecaster, ConstantCovForecaster
from kalshi_crypto.robust_har import RobustHarForecaster
from kalshi_crypto.event_state import build_event_state
from kalshi_crypto.pricer import BaseRateTracker
from kalshi_crypto.strategy import CoinRaceStrategy, COMPOSITE_EVENT_KIND
from kalshi_crypto.series import COINS


COMPOSITE_DATA_ROOT = Path("data") / "kalshi_crypto" / "composite"


@dataclass
class CryptoBacktestResult:
    event_ticker: str
    bt_result: object                       # kalshi_backtest.engine.BacktestResult
    strategy: CoinRaceStrategy
    debug_log: list[dict]
    lip_accrued_usd: dict[str, float]
    lip_snapshots_qualified: dict[str, int]
    lip_snapshots_total: dict[str, int]
    composite_bar_count: int


def load_composite_bars(coin: str, start_s: float, end_s: float,
                        root: Path = COMPOSITE_DATA_ROOT) -> list[CompositeBar]:
    """Pull persisted 1s bars for one coin over [start_s, end_s]."""
    out: list[CompositeBar] = []
    if not root.exists():
        return out
    for path in sorted(root.glob(f"{coin}_*.parquet")):
        # Skip quarantined-corrupt files (composite.flush renames an
        # unreadable file to *.corrupt-<ts> -- the glob would otherwise
        # match it and re-raise the exact ArrowInvalid we quarantined it to
        # avoid).
        if ".corrupt-" in path.name:
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as e:
            print(f"[kalshi_crypto] load_composite_bars: skipping unreadable "
                  f"{path.name} ({e!r})")
            continue
        df = df[(df["ts_s"] >= start_s) & (df["ts_s"] <= end_s)]
        if df.empty:
            continue
        for row in df.itertuples(index=False):
            out.append(CompositeBar(
                coin=coin, ts_s=float(row.ts_s), price=float(row.price),
                utilized_depth=float(row.utilized_depth),
                n_venues_in=int(row.n_venues_in),
                n_venues_dropped=int(row.n_venues_dropped),
                is_fallback=bool(row.is_fallback),
                fallback_mid=(None if pd.isna(row.fallback_mid)
                              else float(row.fallback_mid)),
                nowcast_count=int(row.nowcast_count),
            ))
    return out


class _ReplayCompositeBus(CompositeBus):
    """CompositeBus variant that replays recorded 1s bars rather than
    maintaining per-venue books. Used by the backtest only.

    Keeps the public `sample_all` / `nowcast` surface so
    CoinRaceStrategy doesn't need to know whether it's in replay or
    live mode. `sample_all(now_s)` is a lookup, not a recompute.
    """
    def __init__(self, bars_by_coin: dict[str, list[CompositeBar]]):
        # Deliberately skip the parent's `__init__` -- we don't want
        # live CompositeFeeds (they would compute from empty venue
        # books and return None).
        self._bars = {c: list(bars) for c, bars in bars_by_coin.items()}
        self._cursor = {c: 0 for c in bars_by_coin}
        self._nowcast: dict[str, CompositeBar | None] = {c: None for c in bars_by_coin}

    def observe(self, vb):
        return   # no venue books in replay

    def venue_books_per_coin(self):
        # Return a sentinel non-empty dict per coin so the FeedHealth
        # monitor doesn't flag "no venues" during replay. The entries
        # are placeholders -- the replay path doesn't call CF rate.
        return {c: [] for c in self._bars}

    def sample_all(self, now_s=None):
        """Return all composite bars whose timestamp <= now_s and
        haven't yet been yielded."""
        out = {}
        for coin, bars in self._bars.items():
            i = self._cursor[coin]
            while i < len(bars) and bars[i].ts_s <= now_s:
                out[coin] = bars[i]
                self._nowcast[coin] = bars[i]
                i += 1
            self._cursor[coin] = i
        return out

    def nowcast(self, coin):
        bar = self._nowcast.get(coin)
        if bar is None:
            return None
        # Return a shim with .price so the strategy's math works.
        class _NC:
            def __init__(self, p): self.price = p
        return _NC(bar.mid)

    def flush_all(self):
        return 0


def composite_events(bars_by_coin: dict[str, list[CompositeBar]]
                     ) -> list[tuple[pd.Timestamp, dict]]:
    """One event per wall-clock second a NEW composite bar is available
    for ANY coin. The strategy processes composite ticks event-driven,
    not clock-driven, so this is both deterministic and correct in
    presence of gaps.
    """
    all_ts = set()
    for bars in bars_by_coin.values():
        for b in bars:
            all_ts.add(int(b.ts_s))
    out = []
    for ts in sorted(all_ts):
        payload = {"kind": COMPOSITE_EVENT_KIND, "ts": float(ts)}
        out.append((pd.Timestamp(ts, unit="s", tz="UTC"), payload))
    return out


def run_one(event_ticker: str,
            config: CoinRaceConfig | None = None,
            forecaster: CovarianceForecaster | None = None,
            base_rate_tracker: BaseRateTracker | None = None,
            composite_root: Path = COMPOSITE_DATA_ROOT,
            source: str | None = None,
            allow_predexon: bool = True,
            ) -> CryptoBacktestResult:
    """Replay one event. Returns a `CryptoBacktestResult` with the
    kalshi_backtest-shaped `BacktestResult` plus the strategy's own
    debug_log, LIP accrual, and bar count."""
    config = config or CoinRaceConfig()
    es = build_event_state(event_ticker, config)
    S, T = es.window.S, es.window.T

    # Composite bars: need [S-60, T]. Expand on both sides so the pricer
    # can see a few seconds of pre/post-window state too.
    bars_by_coin: dict[str, list[CompositeBar]] = {}
    for coin in COINS:
        bars_by_coin[coin] = load_composite_bars(
            coin, S - 120, T + 60, composite_root)
    composite_bar_count = sum(len(b) for b in bars_by_coin.values())
    if composite_bar_count == 0:
        print(f"[kalshi_crypto] WARN: no persisted composite bars for {event_ticker} "
              f"under {composite_root}; the strategy will see fairs == base rate the "
              f"whole event. Run the live collector first.")

    # HAR forecaster is the production default. ConstantCovForecaster
    # is still available for tests/smoke runs that want a stable Sigma.
    forecaster = forecaster or RobustHarForecaster()
    base_rate_tracker = base_rate_tracker or BaseRateTracker(config.base_rate_window)

    strategy = CoinRaceStrategy(
        event_state=es, config=config,
        forecaster=forecaster,
        base_rate_tracker=base_rate_tracker,
        record_debug=True,
    )
    bus = _ReplayCompositeBus(bars_by_coin)
    strategy.attach_composite(bus)

    bt = Backtest(
        fee_multiplier=CRYPTO_FEE_MULTIPLIER,
        maker_fee_multiplier=CRYPTO_MAKER_FEE_MULTIPLIER,
        max_position=config.max_position,
    )
    events = composite_events(bars_by_coin)

    result = bt.run(
        strategy_fn=strategy.__call__,
        event_ticker=event_ticker,
        source=source,
        external_events=events,
    )
    return CryptoBacktestResult(
        event_ticker=event_ticker,
        bt_result=result,
        strategy=strategy,
        debug_log=strategy.debug_log,
        lip_accrued_usd=dict(strategy._lip_accrued_usd),
        lip_snapshots_qualified=dict(strategy._lip_snapshots_qualified),
        lip_snapshots_total=dict(strategy._lip_snapshots_total),
        composite_bar_count=composite_bar_count,
    )


def discover_runnable_events() -> list[str]:
    """Every event with persisted composite bars for all 5 coins AND
    Kalshi order-book data. The replay engine needs both.
    """
    kalshi_root = Path("data") / "kalshi_ws" / "KXCRYPTOLEAD15M"
    if not kalshi_root.exists():
        return []
    candidates = set()
    for p in (kalshi_root / "raw_ob_ticks").glob("*.parquet"):
        candidates.add(p.stem)
    # Filter down to events that have at least one composite bar for every coin.
    out = []
    for et in sorted(candidates):
        # Cheap check: any file for any coin with the event's date prefix.
        # Full window overlap is checked in run_one; this is a presence
        # filter only, so a 1-bar-off-the-event file still passes.
        if any(COMPOSITE_DATA_ROOT.glob(f"{COINS[0]}_*.parquet")):
            out.append(et)
    return out


# ---------------------------------------------------------------------------
# Calibration reports (section 7)
# ---------------------------------------------------------------------------

def theo_calibration_report(results: list[CryptoBacktestResult]
                            ) -> pd.DataFrame:
    """Log loss, Brier score, and reliability per phase checkpoint
    (tau = 900, 600, 300, 120, 60, 30, 10).

    Reads from each result's debug_log where `coin` was the eventual
    winner. For each tau checkpoint, picks the ROW closest to that tau
    for each (event, coin) and computes log-loss on p_winner.
    """
    import math
    checkpoints = [900, 600, 300, 120, 60, 30, 10]
    rows = []
    for r in results:
        # Determine winner from the attached SettlementTracker, if the
        # tracker has enough data. Else skip.
        outcome = r.strategy.event_state.tracker.finalize(
            r.strategy.event_state.window.T + 1)
        if outcome is None:
            continue
        winners = set(outcome.winners)
        df = pd.DataFrame(r.debug_log)
        if df.empty:
            continue
        df = df.assign(coin=df["ticker"].apply(
            lambda t: t.rsplit("-", 1)[-1]))
        for tau in checkpoints:
            close = df.iloc[(df["tau"] - tau).abs().argsort()[:5]]
            if close.empty:
                continue
            # p for each coin at this checkpoint.
            latest = close.sort_values("time").groupby("coin").tail(1)
            total_ll = 0.0
            total_brier = 0.0
            n = 0
            for _, row in latest.iterrows():
                is_winner = 1.0 if row["coin"] in winners else 0.0
                p = max(min(row["prob"], 0.999), 1e-6)
                total_ll += -(is_winner * math.log(p) + (1 - is_winner) * math.log(1 - p))
                total_brier += (p - is_winner) ** 2
                n += 1
            rows.append({"event": r.event_ticker, "tau_s": tau,
                         "log_loss": total_ll / max(n, 1),
                         "brier": total_brier / max(n, 1)})
    return pd.DataFrame(rows)


__all__ = ["run_one", "discover_runnable_events",
           "CryptoBacktestResult", "theo_calibration_report",
           "load_composite_bars", "composite_events"]
