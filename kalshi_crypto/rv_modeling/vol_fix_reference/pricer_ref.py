"""Reference Coin Race pricer: robust HAR vol + slow correlation + exact TWAP mechanics + CRN Monte Carlo."""
import numpy as np, json
from rv2 import rv_features, ewma
COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'HYPE']

class HAR:
    def __init__(self, path):
        m = json.load(open(path)); self.hs = np.array(sorted(int(h) for h in m))
        self.th = np.array([m[str(h)]['theta'] for h in self.hs]); self.mu = np.array([m[str(h)]['mu'] for h in self.hs])
    def rate(self, logF, h):
        """logF: (..., 3) log RV rates [1m, 15m, 1h]. Returns per-second variance rate for horizon h (log-h interpolation)."""
        lr = self.th[:, 0] + ((logF[..., None, :] - self.mu) * self.th[:, 1:]).sum(-1)       # (..., n_h)
        lh = np.clip(np.log(h), np.log(self.hs[0]), np.log(self.hs[-1]))
        return np.exp(np.apply_along_axis(lambda v: np.interp(lh, np.log(self.hs), v), -1, lr)) if np.ndim(lh) == 0 else None

def tau_eff(tau):
    return tau - 40.0 if tau >= 60 else tau ** 3 / 10800.0

def corr_series(P, step=10, ret=60, hl=4 * 3600):
    """Causal EWMA correlation of overlapping `ret`-second returns sampled every `step` s. P: (T,5) log prices.
    Returns list of (t_index, R)."""
    T = P.shape[0]; idx = np.arange(ret, T, step); r = P[idx] - P[idx - ret]
    a = 1 - 0.5 ** (step / hl); C = np.cov(r[:360].T); out = {}
    for j, t in enumerate(idx):
        C = (1 - a) * C + a * np.outer(r[j], r[j]); d = np.sqrt(np.diag(C)); out[t] = C / np.outer(d, d)
    return idx, out

class Pricer:
    def __init__(self, har, n=20000, seed=0, nu=None):
        rng = np.random.default_rng(seed); self.Z = rng.standard_normal((n, 5)); self.har = har
        self.w = None if nu is None else np.sqrt(nu / rng.chisquare(nu, n) * (nu - 2) / nu)[:, None]   # unit-variance t mixture
    def fairs(self, x, I, tau, logF, R, vol_mult=1.0):
        te = tau_eff(tau); h = max(tau, 1.0)
        rate = np.array([self.har.rate(logF[i], h) for i in range(5)]) * vol_mult
        sd = np.sqrt(rate * te)
        mu = x if tau >= 60 else I + (tau / 60.0) * x
        L = np.linalg.cholesky(R * np.outer(sd, sd) + 1e-18 * np.eye(5))
        E = self.Z @ L.T
        if self.w is not None: E = E * self.w
        return np.bincount((mu + E).argmax(1), minlength=5) / len(self.Z), sd
