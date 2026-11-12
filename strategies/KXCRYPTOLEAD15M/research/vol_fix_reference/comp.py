"""Price composites from per-second L2 snapshots (single venue = Coinbase)."""
import numpy as np

def mid(bp, ap):
    return (bp[:, 0] + ap[:, 0]) / 2

def microprice(bp, bq, ap, aq):
    return (bp[:, 0] * aq[:, 0] + ap[:, 0] * bq[:, 0]) / (bq[:, 0] + aq[:, 0])

def cf_rate(bp, bq, ap, aq, deviation=0.01, n_levels=100, lam_frac=0.3, ngrid=200):
    """Approximate CF Spot Rate: mid price-volume curve weighted by an exponential density up to utilized depth.
    Single-venue, no size cap, level-based spacing only. Inputs: (T, L) arrays, NaN-padded."""
    T = len(bp); out = np.full(T, np.nan); depth = np.full(T, np.nan)
    for t in range(T):
        b = ~np.isnan(bp[t]) & ~np.isnan(bq[t]); a = ~np.isnan(ap[t]) & ~np.isnan(aq[t])
        if not b.any() or not a.any(): continue
        cb = np.cumsum(bq[t][b]); ca = np.cumsum(aq[t][a])
        vmax = min(cb[-1], ca[-1]); spacing = vmax / n_levels
        v = np.arange(1, ngrid + 1) * spacing * (n_levels / ngrid)            # volume grid up to the thinner side
        bi = np.minimum(np.searchsorted(cb, v), b.sum() - 1); ai = np.minimum(np.searchsorted(ca, v), a.sum() - 1)
        bv, av = bp[t][b][bi], ap[t][a][ai]                                    # marginal price to sell / buy volume v
        m = (bv + av) / 2; ms = (av - m) / m
        ok = ms <= deviation
        D = v[ok][-1] if ok.any() else v[0]
        D = max(D, spacing)
        use = v <= D
        lam = 1 / (lam_frac * D); w = lam * np.exp(-lam * v[use]); w /= w.sum()
        out[t] = (m[use] * w).sum(); depth[t] = D
    return out, depth
