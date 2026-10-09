"""Fit the robust HAR (k=10s subsampled, clipped at 6 sd) on the 1s quote-mid training days, excluding holdout days."""
import sys, numpy as np, pandas as pd, json
sys.path.insert(0, '/home/user/workspace/rv_model'); sys.path.insert(0, '.')
from rvlib import load_day, days_for, COINS, fit_loglinear
from rv2 import rv_features
HS = (30, 60, 120, 300, 600, 900)
def fit(hold=(), k=10, clip=6, step=30):
    X, Y = [], {h: [] for h in HS}
    for c in COINS:
        for d in days_for(c):
            if d in hold: continue
            p, _ = load_day(c, d); F = rv_features(p, k, clip)
            t = np.arange(3600, 86400 - 900, step)
            X.append(np.log(np.column_stack([F[W][t] for W in (60, 900, 3600)])))
            for h in HS: Y[h].append((p[t + h] - p[t]) ** 2 / h)
    X = np.vstack(X); out = {}
    for h in HS:
        th, mu = fit_loglinear(X, np.concatenate(Y[h])); out[h] = {'theta': th.tolist(), 'mu': mu.tolist()}
    return out
if __name__ == '__main__':
    m = fit(hold=tuple(sys.argv[1:]))
    json.dump(m, open('har_' + ('hold_' + '_'.join(sys.argv[1:]) if len(sys.argv) > 1 else 'all') + '.json', 'w'), indent=1)
    for h, v in m.items(): print(h, np.round(v['theta'], 3))
