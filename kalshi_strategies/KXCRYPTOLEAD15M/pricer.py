"""
CoinRacePricer: fair-value computation for one event's 5 markets.

Section 4 of the spec. Given the current composite prices, the locked
(or partially-locked) start TWAP, the partial end-TWAP running sum, and
a covariance forecast, return `p_i = P(coin i wins)` for each coin.

Ranking on log returns is equivalent to ranking on arithmetic returns,
so this file works entirely in logs. The SettlementTracker in
settlement_tracker.py is the one piece that still uses arithmetic
returns -- because that is what Kalshi rounds and compares.

The final log return vector is Y = mu + sqrt(tau_eff) * E, with
Cov(E) = R * outer(s, s), s_i^2 the per-second HAR rate for a return over
the time actually left (h = tau), and R the slow EWMA correlation:

    Phase                              mu            tau_eff
    ---------------------------------- ------------- ------------------
    Pre-open / forming start           0             tau - 40 + rem^2/180
                                                       (rem = start-avg time left)
    In-window (now >= S, tau >= 60)    x             tau - 40
    Last minute (u = tau < 60)         I + (u/60) x  u^3 / 10800

Matches research/vol_fix_reference/pricer_ref.py (validated against
Kalshi settlement and the market mid) and its golden cases.

Derivations (kept in comments next to the arithmetic):

  * Var of avg of Brownian motion over [0, L] is L/3. The start/end
    averages are each L=60 seconds of a running integrated price, so each
    contributes 60/3 = 20 seconds of effective variance BEFORE the window
    "has arrived" at it.
  * In the last minute, we observe partial average
      I = (1/60) * sum_s ln(S_s / start) for s in [T-60, now).
    The remaining u = tau seconds will contribute
      (1/60) * integral_0^u W_v dv
    where W is BM with the per-second covariance Sigma_bar_rem. Its
    variance is u^3 / 3 / 3600 = u^3 / 10800. Mean contribution of what
    we already observed to the final average is just I; of what remains,
    it's (u/60) * current-level x -- the drift from `now` is zero
    conditional on x.

Deterministic given the seeded `Z` the constructor pre-generates --
reused every call so theo doesn't jitter on sampling noise even as the
inputs move continuously.
"""
from __future__ import annotations

import collections
import dataclasses
import math

import numpy as np

from .cov_forecaster import CovarianceForecaster
from .series import COINS, START_END_AVG_S, EVENT_WINDOW_S


N_COINS = len(COINS)
# L = averaging length (s). The end average "effective time" is L/3 = 20s,
# which is what makes mu/V continuous at tau = 60.
L = START_END_AVG_S


@dataclasses.dataclass
class FairResult:
    probs: np.ndarray                 # shape (5,), sums to 1
    mu: np.ndarray                    # shape (5,)
    V: np.ndarray                     # shape (5, 5), PSD
    tau: float
    tau_eff: float                    # trace(V) / sum of diag of Sigma_rate -- "equivalent seconds"
    phase: str
    delta: np.ndarray                 # shape (5, 5), dp_i / dx_j
    theo_vol: np.ndarray              # shape (5,), 1-sigma theo move over reaction_latency_s
    diagnostics: dict


class CoinRacePricer:

    def __init__(self, forecaster: CovarianceForecaster, config,
                 base_rate: np.ndarray | None = None, basis_sd: np.ndarray | None = None):
        """`config`: the series' CryptoPricingParams section (mc_n, mc_seed,
        t_nu, vol_mult, shrinkage_lambda, reaction_latency_s). `basis_sd`:
        per-coin sd (log units) of our final 15-min return vs the one Kalshi
        settles on, added as independent noise to every draw; None = treat
        our spot as exact."""
        self.forecaster = forecaster
        self.config = config
        # Common random numbers: pre-generate ONCE at construction and
        # reuse on every tick so theo doesn't jitter on sampling noise.
        rng = np.random.default_rng(self.config.mc_seed)
        self._Z = rng.standard_normal((self.config.mc_n, N_COINS))
        # Own stream, so adding it leaves the diffusion draws unchanged.
        self._B = (None if basis_sd is None else
                   np.random.default_rng(self.config.mc_seed + 1).standard_normal((self.config.mc_n, N_COINS))
                   * np.asarray(basis_sd, dtype=float))
        if self.config.t_nu is not None:
            # Unit-variance multivariate t: scale by sqrt(nu/chi2 * (nu-2)/nu)
            # so the t mixture changes the TAILS, not the variance. The old
            # 1/sqrt(Gamma(nu/2, 2/nu)) mixture had variance nu/(nu-2) (5/3
            # at nu=5), silently inflating every sd by ~29%.
            nu = self.config.t_nu
            self._tw = np.sqrt(nu / rng.chisquare(nu, self.config.mc_n)
                               * (nu - 2.0) / nu)[:, None]
        else:
            self._tw = None
        self._base_rate = (np.ones(N_COINS) / N_COINS if base_rate is None
                           else np.asarray(base_rate, dtype=float))
        # Draws E = (Z L^T) * tw for the current covariance; rebuilt only when
        # the forecaster's state or the horizon key changes.
        self._E_key = None
        self._E: np.ndarray | None = None
        self._last_corr: np.ndarray | None = None
        self._last_rate: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Variance clock
    # ------------------------------------------------------------------

    @staticmethod
    def tau_eff(tau: float, now_s: float | None = None, S: float | None = None,
                start_locked: bool = True) -> float:
        """Effective seconds of variance left in the final log-return.

        In-window: (tau - 60) of diffusion + 60/3 for the end average
        = tau - 40. Last minute: u^3/10800. Before the start average has
        formed, the unformed part of it adds (rem/60)*(rem/3) with rem the
        start-average time not yet elapsed (60 before S-60)."""
        tau = max(float(tau), 0.0)
        te = tau - 40.0 if tau >= L else tau ** 3 / 10800.0
        if not start_locked and now_s is not None and S is not None:
            rem = min(max(S - now_s, 0.0), float(L))
            te += rem * rem / (3.0 * L)
        return te

    def _rates_corr(self, h: float, now_s: float):
        """Per-coin per-second rate for horizon h and the correlation matrix,
        from a RobustHarForecaster if available, else derived from the
        legacy integrated-covariance API."""
        f = self.forecaster
        if hasattr(f, "rate_per_s") and hasattr(f, "correlation"):
            rate = np.asarray(f.rate_per_s(h, now_s), dtype=float)
            R = np.asarray(f.correlation(now_s), dtype=float)
        else:
            C = f.forecast_cov(now_s, now_s + h) / h
            rate = np.clip(np.diag(C), 0.0, None)
            d = np.sqrt(np.maximum(rate, 1e-300))
            R = C / np.outer(d, d)
        return rate * self.config.vol_mult, R

    def _draws(self, rate: np.ndarray, R: np.ndarray, key) -> np.ndarray:
        """E with Cov(E) = R * outer(sqrt(rate), sqrt(rate)) per unit tau_eff.
        Cached on `key`: the 20k x 5 matmul is the only O(n*25) step, so
        each tick otherwise costs one scale + add + argmax."""
        if key is not None and key == self._E_key and self._E is not None:
            return self._E
        s = np.sqrt(np.maximum(rate, 0.0))
        C = R * np.outer(s, s) + 1e-30 * np.eye(N_COINS)
        try:
            Lc = np.linalg.cholesky(C)
        except np.linalg.LinAlgError:
            w, Q = np.linalg.eigh((C + C.T) / 2.0)
            Lc = Q @ np.diag(np.sqrt(np.clip(w, 1e-30, None)))
        E = self._Z @ Lc.T
        if self._tw is not None:
            E = E * self._tw
        self._E, self._E_key = E, key
        return E

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fairs_at(self, x: np.ndarray, I: np.ndarray, tau: float,
                 *, now_s: float, S: float, T: float,
                 start_locked: bool,
                 compute_delta: bool = True,
                 prev_result: "FairResult | None" = None) -> FairResult:
        """Fair win probabilities. `x`, `I` are 5-vectors of log returns
        (x vs the start TWAP; I = partial end-average, last minute only);
        `tau = T - now_s` in seconds.

        Y = mu + sqrt(tau_eff) * E, with E drawn once per covariance (see
        `_draws`): the per-second rate is the HAR rate for a return over
        the horizon actually left (h = tau), so both the clock and the
        rate shrink with time remaining. Costs ~1ms per tick at 20k draws.

        `compute_delta=False` skips the deltas and carries `prev_result`'s
        (they only size the edge). No base-rate shrink unless
        config.shrinkage_lambda > 0 -- it capped the leader at 0.88 in the
        last minute and cost ~0.09 nats there on validation."""
        x = np.asarray(x, dtype=float)
        I = np.asarray(I, dtype=float)
        tau = max(float(tau), 0.0)
        te = self.tau_eff(tau, now_s, S, start_locked)
        if not start_locked and tau >= L:
            mu, phase = np.zeros(N_COINS), ("pre_start_avg" if now_s < S - L else "forming_start")
        elif tau >= L:
            mu, phase = x.copy(), "in_window"
        elif tau > 0:
            mu, phase = I + (tau / L) * x, "last_minute"
        else:
            mu, phase = I.copy(), "settled"

        h = max(tau, 1.0)
        rate, R = self._rates_corr(h, now_s)
        self._last_rate, self._last_corr = rate, R
        sd = np.sqrt(np.maximum(rate * te, 0.0))
        V = R * np.outer(sd, sd)

        if te <= 0 or not np.any(sd > 0):
            E = None
            probs = self._argmax_probs(mu[None, :] if self._B is None else mu + self._B)
        else:
            # Re-draw only when the covariance itself changes: forecaster
            # state (new 1s data) or horizon bucket (whole seconds of h).
            key = (getattr(self.forecaster, "version", None), round(h))
            E = self._draws(rate, R, key if key[0] is not None else None)
            Y = mu + math.sqrt(te) * E
            if self._B is not None:
                Y = Y + self._B
            probs = self._argmax_probs(Y)

        lam = self.config.shrinkage_lambda
        if lam > 0:
            probs = (1.0 - lam) * probs + lam * self._base_rate
            probs = probs / probs.sum()

        if compute_delta and E is not None:
            scaled = math.sqrt(te) * E if self._B is None else math.sqrt(te) * E + self._B
            delta = self._bump_deltas(mu, scaled, sd, probs)
            lat = self.config.reaction_latency_s
            A = R * np.outer(np.sqrt(rate), np.sqrt(rate)) * lat
            theo_vol = np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", delta, A, delta), 0.0))
        elif compute_delta:
            delta, theo_vol = np.zeros((N_COINS, N_COINS)), np.zeros(N_COINS)
        elif prev_result is not None:
            delta, theo_vol = prev_result.delta, prev_result.theo_vol
        else:
            delta, theo_vol = np.zeros((N_COINS, N_COINS)), np.zeros(N_COINS)

        diagnostics = {
            "mc_n": self.config.mc_n,
            "shrinkage_lambda": lam,
            "rate_per_s": rate.copy(),
            "sd": sd.copy(),
            "R": R.copy(),
            "V_diag": np.diag(V).copy(),
            "tau_eff": te,
            "phase": phase,
            "start_locked": start_locked,
        }
        return FairResult(
            probs=probs, mu=mu, V=V, tau=tau, tau_eff=te,
            phase=phase, delta=delta, theo_vol=theo_vol,
            diagnostics=diagnostics,
        )

    def implied_vol_mult(self, x, I, tau, market_probs, *, now_s, S, T,
                         start_locked=True, grid=None) -> float:
        """Scalar k on V that makes the fairs best match `market_probs`
        (cross-entropy), on the same draws. Diagnostic: k persistently < 1
        late in the window means our variance is too high."""
        m = np.asarray(market_probs, dtype=float)
        m = m / m.sum()
        r = self.fairs_at(x, I, tau, now_s=now_s, S=S, T=T,
                          start_locked=start_locked, compute_delta=False)
        if self._E is None or r.tau_eff <= 0:
            return float("nan")
        grid = np.exp(np.linspace(np.log(0.05), np.log(4.0), 61)) if grid is None else grid
        base = math.sqrt(r.tau_eff) * self._E
        best, best_ll = float("nan"), float("inf")
        for k in grid:
            p = self._argmax_probs(r.mu + math.sqrt(k) * base)
            ll = -float(np.sum(m * np.log(np.clip(p, 1e-6, 1.0))))
            if ll < best_ll:
                best, best_ll = float(k), ll
        return best

    # ------------------------------------------------------------------
    # Monte Carlo with common random numbers
    # ------------------------------------------------------------------

    @staticmethod
    def _argmax_probs(Y: np.ndarray) -> np.ndarray:
        """Share of rows each coin tops; exact ties split evenly (the
        degenerate tau=0 / V=0 case)."""
        if Y.shape[0] == 1:
            best = float(Y[0].max())
            ties = np.isclose(Y[0], best, atol=1e-12)
            return ties / ties.sum()
        return np.bincount(Y.argmax(1), minlength=N_COINS).astype(float) / Y.shape[0]

    def _mc_fairs(self, mu: np.ndarray, V: np.ndarray) -> np.ndarray:
        """P(argmax(mu + chol(V) Z) == i) for an explicit V, on the same
        CRN draws (and t mixture). Used by tests and diagnostics; the live
        path goes through fairs_at."""
        if np.trace(V) <= 1e-18:
            return self._argmax_probs(np.asarray(mu, dtype=float)[None, :])
        try:
            Lc = np.linalg.cholesky(V)
        except np.linalg.LinAlgError:
            w, Q = np.linalg.eigh((V + V.T) / 2.0)
            Lc = Q @ np.diag(np.sqrt(np.clip(w, 1e-16, None)))
        E = self._Z @ Lc.T
        if self._tw is not None:
            E = E * self._tw
        return self._argmax_probs(mu + E)

    def _bump_deltas(self, mu: np.ndarray, E_scaled: np.ndarray, sd: np.ndarray,
                     probs_0: np.ndarray) -> np.ndarray:
        """dp_i / dx_j by central differences on the SAME draws: shift
        column j of Y by +-b and redo the argmax. b = 0.25 x that coin's
        spread sd -- 1bp bumps on indicator counts are mostly noise.
        In the last minute dmu/dx = u/60; that factor is folded in by the
        caller's mu, so this is d p / d mu (the strategy uses it as edge
        size only)."""
        Y0 = mu + E_scaled
        D = np.zeros((N_COINS, N_COINS))
        for j in range(N_COINS):
            b = 0.25 * float(sd[j])
            if b <= 0:
                continue
            Yp = Y0.copy(); Yp[:, j] += b
            Ym = Y0.copy(); Ym[:, j] -= b
            D[:, j] = (self._argmax_probs(Yp) - self._argmax_probs(Ym)) / (2.0 * b)
        return D


# ---------------------------------------------------------------------------
# Base-rate tracker: empirical P(winner == i), rolling over the last N
# settled events. Used by `CoinRacePricer` as the shrink target. Separate
# class so backtest replay and live replay share one implementation.
# ---------------------------------------------------------------------------

class BaseRateTracker:
    def __init__(self, window: int = 500):
        self.window = window
        self._wins: dict[str, int] = {c: 0 for c in COINS}
        self._events: collections.deque = collections.deque(maxlen=window)

    def record(self, winners: list[str]):
        """Settlement outcome, one or more coins. On a tie each winner
        gets 1 / n_tied of the "win". That is NOT exactly Kalshi's
        payout (floor(100/n_tied) cents each), but it matches the base
        rate we want: expected probability of a particular coin being
        the top in rounded return."""
        if not winners:
            return
        contribution = {c: 1.0 / len(winners) if c in winners else 0.0 for c in COINS}
        if len(self._events) == self.window:
            evicted = self._events.popleft()
            for c, v in evicted.items():
                self._wins[c] -= v
        for c, v in contribution.items():
            self._wins[c] += v
        self._events.append(contribution)

    def rate(self) -> np.ndarray:
        n = len(self._events)
        if n == 0:
            return np.ones(N_COINS) / N_COINS
        return np.array([self._wins[c] / n for c in COINS])


__all__ = ["CoinRacePricer", "FairResult", "BaseRateTracker"]
