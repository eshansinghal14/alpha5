import sys, numpy as np, pandas as pd
rows = pd.read_pickle(sys.argv[1]); df = pd.DataFrame(rows)
def ll(P, Y): return -np.log(np.clip((np.stack(P) * np.stack(Y)).sum(1), 1e-3, 1)).mean()
def ce(P, M): P = np.clip(np.stack(P), 1e-3, 1); M = np.stack(M); return -(M * np.log(P)).sum(1).mean()
df['bucket'] = pd.cut(df.tau, [0, 60, 180, 420, 900], labels=['<=1m', '1-3m', '3-7m', '7-15m'])
has = df.mkt.notna()
cols = [c for c in df.columns if c.startswith(('normal', 't5', 'V_'))]
out = {}
for b, g in df[has].groupby('bucket', observed=True):
    r = {'n': len(g), 'market': ll(g.mkt, g.y)}
    for c in cols: r[c] = ll(g[c], g.y)
    out[b] = r
T = pd.DataFrame(out); print('LOG LOSS vs settlement (lower=better), rows with market quotes'); print(T.round(3).to_string())
out = {}
for b, g in df[has].groupby('bucket', observed=True):
    out[b] = {c: ce(g[c], g.mkt) - ce(g.mkt, g.mkt) for c in cols}
print('\nKL(market || theo) (lower = closer to market)'); print(pd.DataFrame(out).round(3).to_string())
# leader bias: theo vs market for coin the market favours
g = df[has]; M = np.stack(g.mkt); top = M.argmax(1)
for c in ['normal_1.0', 't5_1.0', 'normal_0.5']:
    Pm = np.stack(g[c]); print(c, 'market-favourite: mean market %.3f theo %.3f | HYPE mean market %.3f theo %.3f' % (M[np.arange(len(M)), top].mean(), Pm[np.arange(len(M)), top].mean(), M[:, 4].mean(), Pm[:, 4].mean()))
