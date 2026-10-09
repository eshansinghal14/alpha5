"""Which feed defect reproduces the live symptoms (HYPE features ~16x variance, 30-60s corr ~0.15, forward-300s r2 looks clean)?"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, '.')
from replicate import load_comp; from rv2 import rv_features; from pricer_ref import HAR, COINS
har = HAR('har_hold_20261001_20260901.json'); ts, Z = load_comp('20261001')
P = np.log(np.column_stack([pd.Series(Z[c]['mid']).ffill().bfill().values for c in COINS])); rng = np.random.default_rng(1)
def stats(p, P):
    t = np.arange(3600, 86400 - 900, 30); out = {}
    for name, (k, clip) in {'raw1s': (1, None), 'robust10s': (10, 6)}.items():
        F = rv_features(p, k, clip); lf = np.log(np.column_stack([F[W][t] for W in (60, 900, 3600)]))
        out[f'f300 infl {name}'] = np.mean([har.rate(v, 300) for v in lf]) * 300
    clean = P[:, 4]; out['fwd r2_300 ratio'] = np.mean((p[t + 300] - p[t]) ** 2) / np.mean((clean[t + 300] - clean[t]) ** 2)
    for k in (30, 60):
        r = np.column_stack([P[k::k, 0], p[k::k]]) - np.column_stack([P[:-k:k, 0], p[:-k:k]]); out[f'corr BTC k={k}'] = np.corrcoef(r.T)[0, 1]
    return out
base = stats(P[:, 4], P); rows = {'clean': base}
def glitch(p, rate, size_bp, dur=1):
    q = p.copy(); idx = np.where(rng.random(len(p)) < rate)[0]
    for i in idx: q[i:i + dur] += rng.choice([-1, 1]) * size_bp * 1e-4
    return q
def stale(p, rate, length):
    q = p.copy(); idx = np.where(rng.random(len(p)) < rate)[0]
    for i in idx: q[i:i + length] = q[i]
    return q
def noise(p, bp): return p + rng.standard_normal(len(p)) * bp * 1e-4
for nm, q in {'iid noise 2bp': noise(P[:, 4], 2), 'iid noise 4bp': noise(P[:, 4], 4),
              'reverting 1s glitch 30bp @0.5%': glitch(P[:, 4], .005, 30), 'reverting 5s glitch 30bp @0.5%': glitch(P[:, 4], .005, 30, 5),
              'stale 30s @0.3%': stale(P[:, 4], .003, 30), 'one-sided-book 60s glitch 50bp @0.05%': glitch(P[:, 4], .0005, 50, 60)}.items():
    rows[nm] = stats(q, P)
T = pd.DataFrame(rows).T
for c in ['f300 infl raw1s', 'f300 infl robust10s']: T[c] = T[c] / base[c]
print(T.round(2).to_string())
