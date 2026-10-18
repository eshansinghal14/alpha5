"""
Replay one KXCRYPTOLEAD15M event: persisted 1s composite bars drive the
same CoinRaceModel + generic Quoter the live session runs, against recorded
Kalshi books in kalshi_backtest.engine.

    python -m kalshi_strategies.KXCRYPTOLEAD15M.backtest.run_backtest [event_ticker ...]

Needs data/kalshi_crypto/composite/{coin}_*.parquet over [S-120, T], the CF
index under data/kalshi_crypto/cf_index/, and a cached Kalshi source for the
event.
"""
from __future__ import annotations

import math
import sys
import types

import numpy as np
import pandas as pd

from kalshi_backtest import data, engine, results
from kalshi_infra import DATA_ROOT
from kalshi_infra.config import load
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.kalshi import markets as km
from kalshi_infra.trading.lip import LipTracker, params_from_program
from kalshi_infra.trading.fair import FairUpdate
from kalshi_infra.trading.quoter import Quoter
from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import CF_ROOT, load_cf
from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBar
from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig
from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel, load_composite_bars
from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS


class _ReplayBus:
    """Stands in for CompositeBus: bars are already sampled, so "now" is the
    replay clock and each coin's newest book is its newest bar."""

    def __init__(self, bars_by_coin: dict[str, list[CompositeBar]]):
        self._bars = bars_by_coin
        self._i = {c: 0 for c in bars_by_coin}
        self._last: dict[str, CompositeBar | None] = {c: None for c in bars_by_coin}
        self.now_s = 0.0

    def sample_all(self, now_s):
        out = {}
        for c, bars in self._bars.items():
            i = self._i[c]
            while i < len(bars) and bars[i].ts_s <= now_s:
                out[c] = self._last[c] = bars[i]
                i += 1
            self._i[c] = i
        return out

    def mid_nowcast(self, coin):
        b = self._last.get(coin)
        return None if b is None else b.mid

    @property
    def feeds(self):
        """Per coin, the replayed bars up to the clock (CompositeFeed.tail/newest_ts)."""
        bus = self

        class _F:
            def __init__(self, c):
                self.c = c

            def tail(self, n):
                return bus._bars[self.c][max(0, bus._i[self.c] - n):bus._i[self.c]]

            def newest_ts(self):
                b = bus._last[self.c]
                return None if b is None else b.ts_s

            def _live_books(self, _):
                return []
        return {c: _F(c) for c in self._bars}

    def venue_books_per_coin(self):
        # Fresh as of the replay clock -> feed health OK, skew = bar spacing.
        return {c: ([types.SimpleNamespace(receive_ts=b.ts_s, is_resyncing=False, is_one_sided=lambda: False,
                                           best_bid=lambda: 1.0, best_ask=lambda: 1.0)]
                    if (b := self._last[c]) is not None else []) for c in COINS}

    def flush_all(self):
        return 0


class _ReplayCf:
    """Persisted CF seconds revealed up to the replay clock, each one second
    after its stamp (live ticks land ~70ms after the second; a 1s reveal is
    the conservative whole-second version)."""

    def __init__(self, values: dict[str, dict[int, float]]):
        self._all = values
        self.now_s = -math.inf
        self.version = 0

    def advance(self, now_s: float):
        if int(now_s) != int(self.now_s):
            self.version += 1
        self.now_s = now_s

    def _visible(self, t: int) -> bool:
        return t + 1 <= self.now_s

    def value(self, coin, ts_s):
        ts_s = int(ts_s)
        return self._all[coin].get(ts_s) if self._visible(ts_s) else None

    def window(self, coin, lo, hi):
        return [v for t in range(int(lo), int(hi)) if (v := self.value(coin, t)) is not None]

    def latest(self, coin):
        t = int(self.now_s) - 1
        for k in range(t, t - 10, -1):
            v = self._all[coin].get(k)
            if v is not None:
                return k, v
        return None

    def age_s(self, now_mono=None):
        return 0.0

    def request_fill(self, coin):
        pass


class _ReplayModel(CoinRaceModel):
    """Monotonic-clock checks (feed health, skew, signal age) run on the
    replay clock instead of wall time."""

    def _price(self, now_s, compute_delta):
        import kalshi_strategies.KXCRYPTOLEAD15M.model as m
        self.cf.advance(now_s)
        real = m.time.monotonic
        m.time.monotonic = lambda: now_s
        try:
            return super()._price(now_s, compute_delta)
        finally:
            m.time.monotonic = real

    def signal_age_s(self):
        return 0.0 if self.result is not None else None


def run_one(event_ticker: str, cfg=None, verbose: bool = True):
    cfg = cfg or load(CryptoConfig, CONFIG_PATH)
    ev = km.event(event_ticker)
    S, T = ev.open_time.timestamp(), ev.close_time.timestamp()
    bars = {c: load_composite_bars(c, S - cfg.pricing.warmup_s, T + 60) for c in COINS}
    n = sum(len(b) for b in bars.values())
    if n == 0:
        raise RuntimeError(f"no composite bars for {event_ticker} under {DATA_ROOT}/kalshi_crypto/composite")
    # Warm the forecaster on bars before the event, replay the rest.
    warm = {c: [b for b in bl if b.ts_s < S - 120] for c, bl in bars.items()}
    live = {c: [b for b in bl if b.ts_s >= S - 120] for c, bl in bars.items()}
    fc = RobustHarForecaster()
    by_s: dict[int, dict] = {}
    for c, bl in warm.items():
        for b in bl:
            by_s.setdefault(int(b.ts_s), {})[c] = b.mid
    for ts in sorted(by_s):
        fc.update(float(ts), by_s[ts])

    cf = _ReplayCf({c: load_cf(c, S - 300, T + 1) for c in COINS})
    if any(len(cf._all[c]) < 900 for c in COINS):
        raise RuntimeError(f"CF index missing for {event_ticker} under {CF_ROOT}")
    model = _ReplayModel(ev, cfg, cf=cf, bus=_ReplayBus(live), forecaster=fc)
    # Freshness is a wall-clock concept: the replay clock would read every fair as stale.
    cfg = types.SimpleNamespace(**{**vars(cfg), "risk": types.SimpleNamespace(
        **{**vars(cfg.risk), "max_signal_age_s": math.inf})})
    model.cfg = cfg
    fee_sched = kfees.for_event(ev.event_ticker.split('-')[0], ev.event_ticker, at=ev.close_time)
    lip = None
    try:
        progs = km.incentive_programs(set(model.tickers))
        lip = LipTracker({t: params_from_program(p) for t, p in progs.items()})
    except Exception:
        pass   # programs are only listed while active; historical events replay without LIP
    quoter = Quoter(cfg, model, fee_sched, close_time=ev.close_time, lip=lip, record_debug=True)

    class _Drive:
        """Each second with a new bar: reprice and dispatch a FairUpdate."""
        def __call__(self, ctx):
            evt = ctx.last_event
            if isinstance(evt, dict) and evt.get("kind") == "tick":
                fairs = model.reprice(evt["ts"])
                if fairs:
                    ctx.last_event = FairUpdate(fairs)
                    quoter(ctx)
                return
            quoter(ctx)

    ticks = sorted({int(b.ts_s) for bl in live.values() for b in bl if S - 120 <= b.ts_s <= T})
    ext = [(pd.Timestamp(t, unit="s", tz="UTC"), {"kind": "tick", "ts": float(t)}) for t in ticks]
    bt = engine.Backtest(fee_multiplier=fee_sched.taker_rate,
                         maker_fee_multiplier=fee_sched.maker_rate,
                         max_position=cfg.quoting.max_position,
                         cancels_token=cfg.quoting.cancels_token)
    result = bt.run(_Drive(), event_ticker, event_data=data.load_event_data(event_ticker), external_events=ext)
    if verbose:
        print(f"=== {event_ticker}: {n} bars, {len(ext)} ticks, {len(quoter.debug_log)} decisions ===")
        for k, v in results.summary(result).items():
            print(f"  {k}: {v}")
    return result, quoter, model


def theo_calibration(quoter, model) -> pd.DataFrame:
    """Log loss / Brier of the fair at tau checkpoints, against the settled winner."""
    model.cf.advance(model.window.T + 1)
    out = model.tracker.finalize()
    if out is None:
        return pd.DataFrame()
    winners = set(out.winners)
    df = pd.DataFrame(quoter.debug_log).dropna(subset=["prob_raw"])
    rows = []
    for tau in (900, 600, 300, 120, 60, 30, 10):
        near = df.iloc[(df["x_tau"] - tau).abs().argsort()[:25]]
        last = near.sort_values("time").groupby("x_coin").tail(1)
        if last.empty:
            continue
        y = last["x_coin"].isin(winners).astype(float).to_numpy()
        p = np.clip(last["prob_raw"].to_numpy(), 1e-6, 1 - 1e-6)
        rows.append({"tau_s": tau, "log_loss": float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()),
                     "brier": float(((p - y) ** 2).mean())})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    for et in sys.argv[1:]:
        r, q, m = run_one(et)
        print(theo_calibration(q, m).to_string(index=False))
