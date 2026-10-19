"""
Coin correlation for the race, fitted to the race's own Kalshi mids.

The HAR correlation (4h EWMA of 300s returns, 0.68-0.91 between coins on
10-19) is scaled toward independence, R_g = g R + (1 - g) I, with g chosen so
our five fairs best match the five Kalshi race mids (renormalised to sum 1,
weighted 1 / spread). Lower correlation spreads the probabilities out: with
coins moving together the current leader is likelier to stay ahead.

Replay 10-19 02:33-04:32 UTC (9 events): g 0.9 (R - 0.10) cut |theo - mid|
from 2.70 to 2.16c and the ETH/SOL/HYPE per-coin bias by ~half, settled-winner
log loss unchanged (1.37 vs 1.37). A scalar g, not a full matrix: four
independent mid differences can't pin ten correlations, and the market shift
(model._update_market_shift) already takes the per-coin level. Fitted on the
unshifted fair, before the shift, so the two don't trade off against each
other inside one snapshot; g moves slowly (EMA), the shift tracks levels.

The fit runs off the event loop (one grid of reprices at fit_mc_n draws on its
own pricer, ~10-20ms) and lands on the loop when done.
"""
from __future__ import annotations

import asyncio
import dataclasses
import math

import numpy as np

from kalshi_strategies.KXCRYPTOLEAD15M.pricer import CoinRacePricer


class _Fixed:
    """Forecaster stand-in: the fit pricer gets its rates and correlation per call."""
    version = None

    def __init__(self):
        self.R = np.eye(5)

    def rate_per_s(self, h, t=None):
        return np.full(5, 1e-9)

    def correlation(self, t=None):
        return self.R


def fit_g(pricer: CoinRacePricer, stub: _Fixed, R: np.ndarray, x, I, tau, rate, S, T, now_s,
          target: np.ndarray, weight: np.ndarray, grid: np.ndarray) -> tuple[float, float]:
    """(best g on the grid, its weighted RMS gap in probability units). Blocking."""
    stub.R = R
    best = (1.0, math.inf)
    for g in grid:
        p = pricer.fairs_at(x=x, I=I, tau=tau, now_s=now_s, S=S, T=T, start_locked=True,
                            compute_delta=False, rate_override=rate, corr_mult=float(g)).probs
        err = float(np.sqrt(np.sum(weight * (p - target) ** 2) / np.sum(weight)))
        if err < best[1]:
            best = (float(g), err)
    return best


class CorrFit:
    """Session-lifetime (on the SpotFeed): g carries across events."""

    def __init__(self):
        self.g = 1.0
        self.raw: float | None = None
        self.err: float | None = None
        self.n_fits = 0
        self.n_errors = 0
        self._t: float | None = None      # wall time of the last EMA step
        self._last_fit = -math.inf
        self._busy = False
        self._pricer: CoinRacePricer | None = None
        self._stub = _Fixed()
        self._mc_n = None

    def value(self, p) -> float:
        return min(max(self.g, p.corr_mult_min), p.corr_mult_max) if p.corr_fit else 1.0

    def _apply(self, now_s: float, g: float, err: float, p):
        self.raw, self.err = g, err
        self.n_fits += 1
        if self._t is None:
            self.g = g
        else:
            lam = 1.0 - 0.5 ** (max(0.0, now_s - self._t) / p.corr_half_life_s)
            self.g += lam * (g - self.g)
        self._t = now_s

    def maybe_fit(self, now_s: float, p, R, x, I, tau, rate, S, T, kbid: dict, kask: dict, coins):
        """Start a fit if one is due and the race book is good enough. Returns at once."""
        if not p.corr_fit or self._busy or now_s - self._last_fit < p.corr_fit_s:
            return
        if any(c not in kbid or c not in kask for c in coins):
            return
        bid = np.array([kbid[c] for c in coins])
        ask = np.array([kask[c] for c in coins])
        spread = ask - bid
        if np.any(spread <= 0):
            return
        # Every leg is in the renormalisation, but only the tight ones carry the fit:
        # all five within 3c never happened 10-19 02:33-04:49 UTC (widest leg 10c median).
        ok = spread <= p.corr_fit_max_spread + 1e-9
        if ok.sum() < p.corr_fit_min_legs:
            return
        m = (bid + ask) / 2
        target = m / m.sum()
        weight = np.where(ok, 1.0 / np.maximum(spread, 0.01), 0.0)
        if self._pricer is None or self._mc_n != p.corr_fit_mc_n:
            cfg = dataclasses.replace(p, mc_n=int(p.corr_fit_mc_n))
            self._pricer = CoinRacePricer(self._stub, cfg)
            self._mc_n = p.corr_fit_mc_n
        grid = np.linspace(p.corr_mult_min, p.corr_mult_max, int(p.corr_fit_grid))
        args = (self._pricer, self._stub, np.array(R, dtype=float), np.array(x, dtype=float), np.array(I, dtype=float),
                float(tau), None if rate is None else np.array(rate, dtype=float), S, T, now_s, target, weight, grid)
        self._last_fit = now_s
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._apply(now_s, *fit_g(*args), p)
            return
        self._busy = True

        def done(fut):
            self._busy = False
            try:
                g, err = fut.result()
            except Exception:
                self.n_errors += 1
                return
            self._apply(now_s, g, err, p)

        loop.run_in_executor(None, fit_g, *args).add_done_callback(lambda f: loop.call_soon_threadsafe(done, f))

    def snapshot(self, p) -> dict:
        return {"g": round(self.value(p), 4), "raw": None if self.raw is None else round(self.raw, 4),
                "rms_gap_c": None if self.err is None else round(100 * self.err, 3), "n_fits": self.n_fits,
                "errors": self.n_errors}


__all__ = ["CorrFit", "fit_g"]
