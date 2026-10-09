"""
Production vol / correlation forecaster for the Coin Race pricer.

Online port of the reference pipeline in research/vol_fix_reference/
(rv2.rv_features, pricer_ref.HAR, pricer_ref.corr_series), which was shown
calibrated on two held-out days and to match or beat the Kalshi mid's log
loss at every time-remaining bucket. Feed it the venue TOP-OF-BOOK MID
(CompositeBar.mid), not the CF replica -- see that property for why.

  * Features: per-second RV over 60/900/3600 s from squared 10 s returns
    taken at every 1 s offset, each clipped at 6x a lagged EWMA sd
    (900 s half-life). One 30 bp glitch inflates the forecast ~1.5x
    instead of ~5.7x with raw 1 s returns.
  * HAR: research/har_coefs_robust.json, fitted for h = 30..900 s on
    those features. `rate_per_s(h)` interpolates log-rate in log h, so the
    pricer can ask for the horizon that is actually left.
  * Correlation: causal EWMA of 60 s returns sampled every 10 s, 4 h half-
    life. 1 s returns understate it badly (asynchronous updates).
  * Scale monitor: c = EWMA(r_300^2 / f_300) on our own feed. A MONITOR,
    not a fix: applied clamped to [0.6, 1.6]; outside [0.4, 2.5] the feed
    is broken (2026-10-08's CF-replica HYPE read 0.06) and `alarms()`
    names the coin so the strategy stops pricing.

`update(ts, {coin: price})` expects at most one price per coin per second;
repeats of a second already seen are ignored. Missing seconds are forward-
filled, as in the reference.
"""
from __future__ import annotations

import collections
import json
import math
from pathlib import Path

import numpy as np

from .cov_forecaster import CovarianceForecaster
from .series import COINS

ARTIFACT = Path(__file__).with_name("research") / "har_coefs_robust.json"
WINDOWS = (60, 900, 3600)
RET_K = 10
CLIP_SD = 6.0
BASE_HL_S = 900.0
VFLOOR = 1e-10
# Production has no look-ahead x0 for the clip's EWMA, so it starts from the
# mean r^2 of this many returns (clipping off until then).
BASE_WARMUP_N = 600

CORR_STEP_S = 10
CORR_RET_S = 60
CORR_HL_S = 4 * 3600.0
CORR_INIT_N = 360            # first hour of 10 s samples seeds the EWMA, as in the reference
# Used until CORR_INIT_N samples exist. Same structure as the reference golden
# cases; the live mid on 2026-10-08 measured 0.76-0.83 majors, 0.58-0.65 HYPE.
PRIOR_CORR = np.full((5, 5), 0.85)
PRIOR_CORR[4, :] = PRIOR_CORR[:, 4] = 0.60
np.fill_diagonal(PRIOR_CORR, 1.0)

SCALE_H_S = 300
SCALE_EVERY_S = 60
SCALE_HL_N = 2 * 86400 / SCALE_EVERY_S     # 2-day half-life, in samples
SCALE_MIN_N = 60                           # an hour of 300 s outcomes before it acts
SCALE_CLAMP = (0.6, 1.6)
SCALE_ALARM = (0.4, 2.5)


class HarCoefs:
    """pricer_ref.HAR: one (theta, mu) row per fitted horizon."""

    def __init__(self, path: Path | str = ARTIFACT):
        m = json.loads(Path(path).read_text())
        self.hs = np.array(sorted(int(h) for h in m), dtype=float)
        self.theta = np.array([m[str(int(h))]["theta"] for h in self.hs])
        self.mu = np.array([m[str(int(h))]["mu"] for h in self.hs])
        self._log_hs = np.log(self.hs)

    def log_rate(self, logF: np.ndarray, h: float) -> float:
        lr = self.theta[:, 0] + ((logF[None, :] - self.mu) * self.theta[:, 1:]).sum(-1)
        lh = min(max(math.log(max(h, 1e-9)), self._log_hs[0]), self._log_hs[-1])
        return float(np.interp(lh, self._log_hs, lr))

    def rate(self, logF: np.ndarray, h: float) -> float:
        return math.exp(self.log_rate(logF, h))

    def neutral_logF(self) -> np.ndarray:
        """Training-mean features: used for a coin with no data yet."""
        return self.mu[0].copy()


class RobustRv:
    """rv2.rv_features(p, k=10, clip=6) evaluated online, one grid second at
    a time. `init_base_var` reproduces the reference's x0 exactly (tests);
    production leaves it None and warms up from the data."""

    def __init__(self, k: int = RET_K, clip: float = CLIP_SD,
                 init_base_var: float | None = None, max_ffill_s: int = max(WINDOWS)):
        self.k, self.clip, self.max_ffill_s = k, clip, max_ffill_s
        self.a = 1.0 - 0.5 ** (1.0 / BASE_HL_S)
        self.base = init_base_var
        self._warm_sum, self._warm_n = 0.0, 0
        self.lp: collections.deque[float] = collections.deque(maxlen=k + 1)
        self.q = {W: collections.deque(maxlen=W) for W in WINDOWS}
        self.qsum = {W: 0.0 for W in WINDOWS}
        self.n = 0
        self.last_ts: int | None = None

    def update(self, ts: float, price: float) -> bool:
        if price is None or not (price > 0) or not math.isfinite(price):
            return False
        t = int(round(ts))
        if self.last_ts is not None:
            if t <= self.last_ts:
                return False
            gap = t - self.last_ts - 1
            if gap > self.max_ffill_s:
                self._reset_rings()
            else:
                for _ in range(gap):
                    self._push(self.lp[-1])
        self._push(math.log(price))
        self.last_ts = t
        return True

    def _reset_rings(self):
        self.lp.clear()
        for W in WINDOWS:
            self.q[W].clear()
            self.qsum[W] = 0.0
        self.n = 0

    def _push(self, lpx: float):
        n = self.n
        self.lp.append(lpx)
        r = lpx - self.lp[0] if len(self.lp) == self.k + 1 else 0.0
        r2 = r * r
        if self.base is None:
            if n >= self.k:
                self._warm_sum += r2
                self._warm_n += 1
                if self._warm_n >= BASE_WARMUP_N:
                    self.base = self._warm_sum / self._warm_n
            rc = r
        else:
            prev = self.base
            new = self.a * r2 + (1.0 - self.a) * prev
            # Reference: s[0] = sqrt(base[0]), s[n] = sqrt(base[n-1]) -- lagged, no look-ahead.
            s = math.sqrt(max(new if n == 0 else prev, 1e-14))
            rc = min(max(r, -self.clip * s), self.clip * s)
            self.base = new
        qv = rc * rc / self.k
        for W in WINDOWS:
            d = self.q[W]
            if len(d) == W:
                self.qsum[W] -= d[0]
            d.append(qv)
            self.qsum[W] += qv
        self.n += 1

    def ready(self) -> bool:
        return self.n > max(WINDOWS)

    def logF(self) -> np.ndarray | None:
        """log per-second RV [1m, 15m, 1h]. Before a window is full it uses
        the samples it has (warm-up only; the reference is NaN there)."""
        out = []
        for W in WINDOWS:
            m = len(self.q[W])
            if m == 0:
                return None
            v = self.qsum[W] / (W if self.n > W else m)
            out.append(math.log(max(v, VFLOOR)))
        return np.array(out)


class EwmaCorr:
    """pricer_ref.corr_series online. Samples land on absolute seconds that
    are multiples of CORR_STEP_S (the reference starts at row 0, which is
    such a second for its data)."""

    def __init__(self, n: int = 5, init_cov: np.ndarray | None = None):
        self.n = n
        self.a = 1.0 - 0.5 ** (CORR_STEP_S / CORR_HL_S)
        self.C = None if init_cov is None else np.array(init_cov, dtype=float)
        self._init_buf: list[np.ndarray] = []
        self.hist: collections.deque[tuple[int, np.ndarray]] = collections.deque(maxlen=CORR_RET_S + 1)

    def push(self, t: int, logp: np.ndarray):
        """Call once per grid second with all coins' (forward-filled) log prices."""
        self.hist.append((t, logp))
        if t % CORR_STEP_S or len(self.hist) < CORR_RET_S + 1:
            return
        t0, p0 = self.hist[0]
        if t - t0 != CORR_RET_S:
            return
        r = logp - p0
        if self.C is None:
            self._init_buf.append(r)
            if len(self._init_buf) == CORR_INIT_N:
                B = np.array(self._init_buf)
                self.C = np.cov(B.T)
                for rr in B:
                    self.C = (1 - self.a) * self.C + self.a * np.outer(rr, rr)
                self._init_buf = []
            return
        self.C = (1 - self.a) * self.C + self.a * np.outer(r, r)

    def corr(self) -> np.ndarray:
        if self.C is None:
            return PRIOR_CORR.copy()
        d = np.sqrt(np.maximum(np.diag(self.C), 1e-30))
        return self.C / np.outer(d, d)


class ScaleMonitor:
    """c = EWMA(r_h^2 / f_h) per coin on our own feed, h = 300 s."""

    def __init__(self, n: int = 5):
        self.alpha = 1.0 - 0.5 ** (1.0 / SCALE_HL_N)
        self.c = np.ones(n)
        self.count = np.zeros(n, dtype=int)
        self.pending: collections.deque[tuple[int, np.ndarray, np.ndarray]] = collections.deque()

    def push(self, t: int, logp: np.ndarray, f_h: np.ndarray):
        while self.pending and t - self.pending[0][0] >= SCALE_H_S:
            t0, p0, f0 = self.pending.popleft()
            if t - t0 > SCALE_H_S + 5:          # outcome second missing -- drop it
                continue
            ok = np.isfinite(p0) & np.isfinite(logp) & (f0 > 0)
            ratio = np.where(ok, (logp - p0) ** 2 / np.where(f0 > 0, f0, 1.0), 0.0)
            self.c = np.where(ok, (1 - self.alpha) * self.c + self.alpha * ratio, self.c)
            self.count += ok.astype(int)
        if t % SCALE_EVERY_S == 0:
            self.pending.append((t, logp.copy(), f_h.copy()))

    def applied(self) -> np.ndarray:
        live = self.count >= SCALE_MIN_N
        return np.where(live, np.clip(self.c, *SCALE_CLAMP), 1.0)

    def alarm(self) -> np.ndarray:
        return (self.count >= SCALE_MIN_N) & ((self.c < SCALE_ALARM[0]) | (self.c > SCALE_ALARM[1]))


class RobustHarForecaster(CovarianceForecaster):
    """See module docstring. Pricer-facing API: `rate_per_s(h)`,
    `correlation()`, `version`; the legacy integrated-cov API is kept."""

    def __init__(self, coefs: HarCoefs | None = None, coins=COINS,
                 init_base_var: dict[str, float] | None = None,
                 init_corr_cov: np.ndarray | None = None):
        self.coin_order = tuple(coins)
        self.coefs = coefs or HarCoefs()
        ib = init_base_var or {}
        self.rv = {c: RobustRv(init_base_var=ib.get(c)) for c in self.coin_order}
        self.corr = EwmaCorr(len(self.coin_order), init_corr_cov)
        self.scale = ScaleMonitor(len(self.coin_order))
        self._last_lp = np.full(len(self.coin_order), np.nan)
        self._grid_t: int | None = None
        self.version = 0

    # -- ingestion ----------------------------------------------------------
    def update(self, ts: float, composite_prices: dict[str, float]) -> None:
        t = int(round(ts))
        if self._grid_t is not None and t <= self._grid_t:
            return
        changed = False
        for c, px in composite_prices.items():
            r = self.rv.get(c)
            if r is not None and r.update(t, px):
                self._last_lp[self.coin_order.index(c)] = math.log(px)
                changed = True
        if not changed:
            return
        # Joint grid for correlation and the scale monitor (forward-filled).
        if np.all(np.isfinite(self._last_lp)):
            start = t if self._grid_t is None else self._grid_t + 1
            if t - start > max(WINDOWS):
                start = t
            for g in range(start, t + 1):
                self.corr.push(g, self._last_lp.copy())
            self.scale.push(t, self._last_lp.copy(), self._raw_rates(SCALE_H_S) * SCALE_H_S)
        self._grid_t = t
        self.version += 1

    # -- forecasts ------------------------------------------------------------
    def logF(self) -> np.ndarray:
        out = []
        for c in self.coin_order:
            f = self.rv[c].logF()
            out.append(self.coefs.neutral_logF() if f is None else f)
        return np.array(out)

    def _raw_rates(self, h: float) -> np.ndarray:
        return np.array([self.coefs.rate(f, h) for f in self.logF()])

    def rate_per_s(self, h: float, t: float | None = None) -> np.ndarray:
        """Per-second variance rate for a return over the next `h` seconds,
        times the clamped live scale."""
        return self._raw_rates(h) * self.scale.applied()

    def correlation(self, t: float | None = None) -> np.ndarray:
        return self.corr.corr()

    def alarms(self) -> list[str]:
        a = self.scale.alarm()
        return [c for c, bad in zip(self.coin_order, a) if bad]

    def forecast_cov_rate(self, t: float) -> np.ndarray:
        s = np.sqrt(self.rate_per_s(60.0))
        return self.correlation() * np.outer(s, s)

    def forecast_cov(self, t0: float, t1: float) -> np.ndarray:
        h = max(t1 - t0, 0.0)
        if h <= 0:
            return np.zeros((len(self.coin_order),) * 2)
        s = np.sqrt(self.rate_per_s(h) * h)
        return self.correlation() * np.outer(s, s)

    def diagnostics(self) -> dict:
        return {
            "scale_raw": dict(zip(self.coin_order, map(float, self.scale.c))),
            "scale_applied": dict(zip(self.coin_order, map(float, self.scale.applied()))),
            "scale_n": dict(zip(self.coin_order, map(int, self.scale.count))),
            "alarms": self.alarms(),
            "rv_ready": {c: self.rv[c].ready() for c in self.coin_order},
            "corr_ready": self.corr.C is not None,
            "corr": np.round(self.correlation(), 3).tolist(),
        }


__all__ = ["RobustHarForecaster", "RobustRv", "EwmaCorr", "ScaleMonitor", "HarCoefs",
           "PRIOR_CORR", "ARTIFACT"]
