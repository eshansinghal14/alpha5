import numpy as np, pandas as pd
df = pd.concat([pd.DataFrame(pd.read_pickle(f'/home/user/workspace/val/rows_{d}.pkl')) for d in ('20261001', '20260901')], ignore_index=True)
df = df[df.mkt.notna()].copy()
def shrink(P, cap=0.88, floor=0.03):
    P = np.clip(np.stack(P), floor, cap); return list(P / P.sum(1, keepdims=True))
df['V_ref_shrunk'] = shrink(df['t5_1.0']); df['V_ref'] = df['t5_1.0']
for w in (0.25, 0.5):
    df[f'V_blend_mkt{w}'] = [np.exp((1 - w) * np.log(np.clip(a, 1e-4, 1)) + w * np.log(np.clip(b, 1e-4, 1))) for a, b in zip(df['t5_1.0'], df.mkt)]
    df[f'V_blend_mkt{w}'] = [v / v.sum() for v in df[f'V_blend_mkt{w}']]
df['bucket'] = pd.cut(df.tau, [0, 60, 180, 420, 900], labels=['<=1m', '1-3m', '3-7m', '7-15m'])
def ll(P, Y): return -np.log(np.clip((np.stack(P) * np.stack(Y)).sum(1), 1e-3, 1)).mean()
cols = ['mkt', 'V_ref', 'normal_1.0', 'V_ref_shrunk', 'V_blend_mkt0.25', 'V_blend_mkt0.5', 'V_uservol', 'V_user_both', 'V_lowcorr', 'V_nocorr']
T = pd.DataFrame({b: {'n': len(g), **{c: ll(g[c], g.y) for c in cols}} for b, g in df.groupby('bucket', observed=True)})
T['all'] = [len(df)] + [ll(df[c], df.y) for c in cols]
print(T.round(3).to_string())
# bootstrap by event: ref vs market
ev = df.ev.unique(); rng = np.random.default_rng(0)
d = {e: g for e, g in df.groupby('ev')}
diffs = []
for _ in range(2000):
    s = rng.choice(ev, len(ev)); g = pd.concat([d[e] for e in s]); diffs.append(ll(g.mkt, g.y) - ll(g['V_ref'], g.y))
print('market minus ref log loss: mean %.3f, 90%% CI [%.3f, %.3f], events %d' % (np.mean(diffs), *np.percentile(diffs, [5, 95]), len(ev)))
# calibration of ref: bins of predicted prob
P = np.concatenate([np.stack(df['V_ref'])]); Y = np.stack(df.y); M = np.stack(df.mkt)
bins = [0, .05, .15, .3, .5, .7, .85, 1]
print('calibration (pred bin: ref mean pred / realized freq | market mean / realized)')
for a, b in zip(bins[:-1], bins[1:]):
    m = (P >= a) & (P < b); mm = (M >= a) & (M < b)
    print(f'[{a:.2f},{b:.2f}) ref {P[m].mean():.3f}/{Y[m].mean():.3f} n={m.sum():4d} | mkt {M[mm].mean():.3f}/{Y[mm].mean():.3f} n={mm.sum()}')
