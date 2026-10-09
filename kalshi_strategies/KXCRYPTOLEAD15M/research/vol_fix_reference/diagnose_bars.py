"""Diagnose a persisted 1s bar file from the live pricer.

Input: CSV or parquet with columns ts (unix seconds), coin, price (the composite used for HAR features / x),
       and optionally mid (raw Coinbase top-of-book mid at the same second).
Usage: python diagnose_bars.py bars.parquet [--har har_coefs.json]

Prints, per coin:
  * timestamp gaps (missing seconds) and returns that span a gap
  * signature ratios RV(k)/RV(60) on the price column (clean feed: 0.7-1.2 at every k; noise: >>1 at k=1)
  * share of trailing-1h RV contributed by the 5 largest 1s returns (clean: <15%)
  * count of reverting spikes (|r_t| > 8 robust sd and r_{t+1} ~ -r_t)
  * |price - mid| statistics in bp and its autocorrelation half-life, if mid is present
  * 30s / 60s return correlation vs BTC, raw and with robust 10s returns
  * HAR 300s forecast from 1s-raw features vs robust 10s features vs realized, mean(r2)/mean(f)
"""
import sys, argparse, numpy as np, pandas as pd
sys.path.insert(0, __file__.rsplit('/', 1)[0] if '/' in __file__ else '.')
from rv2 import rv_features
from pricer_ref import HAR

ap = argparse.ArgumentParser(); ap.add_argument('path'); ap.add_argument('--har', default='har_coefs.json')
a = ap.parse_args()
df = pd.read_parquet(a.path) if a.path.endswith('.parquet') else pd.read_csv(a.path)
df['ts'] = df.ts.astype(float).round().astype(int)
har = HAR(a.har)
wide = df.pivot_table(index='ts', columns='coin', values='price', aggfunc='last')
full = np.arange(wide.index.min(), wide.index.max() + 1)
missing = {c: int(wide[c].reindex(full).isna().sum()) for c in wide}
wide = wide.reindex(full).ffill()
mids = None
if 'mid' in df:
    mids = df.pivot_table(index='ts', columns='coin', values='mid', aggfunc='last').reindex(full).ffill()
coins = list(wide.columns); base = 'BTC' if 'BTC' in coins else coins[0]
L = np.log(wide)
print(f'{len(full)} seconds ({len(full) / 3600:.1f} h), coins {coins}')
for c in coins:
    p = L[c].values; r = np.diff(p); nz = r[r != 0]
    rs = 1.4826 * np.median(np.abs(nz)) if len(nz) else np.nan
    print(f'\n=== {c} ===')
    gaps = np.diff(df[df.coin == c].ts.sort_values().values)
    print(f'missing seconds {missing[c]}, gaps >2s: {int((gaps > 2).sum())}, max gap {gaps.max() if len(gaps) else 0}s')
    rv60 = np.mean((p[60:] - p[:-60]) ** 2) / 60
    sig = {k: np.mean((p[k:] - p[:-k]) ** 2) / k / rv60 for k in (1, 5, 10, 30)}
    print('signature RV(k)/RV(60):', {k: round(v, 2) for k, v in sig.items()}, ' <- >2 at k=1 means the series carries microstructure noise')
    sq = r ** 2; hshare = []
    for s in range(0, len(r) - 3600, 3600):
        w = sq[s:s + 3600]; hshare.append(np.sort(w)[-5:].sum() / max(w.sum(), 1e-20))
    if hshare: print(f'top-5 1s returns share of hourly RV: median {np.median(hshare):.0%}, max {np.max(hshare):.0%}  (clean feed < 15%)')
    spikes = int(np.sum((np.abs(r[:-1]) > 8 * rs) & (np.abs(r[:-1] + r[1:]) < 0.3 * np.abs(r[:-1]))))
    print(f'reverting 1s spikes (>8 robust sd): {spikes}  ({spikes / max(len(r) / 3600, 1e-9):.1f}/h)')
    if mids is not None and c in mids:
        d = (L[c] - np.log(mids[c])).values * 1e4; d = d[~np.isnan(d)]
        ac = np.corrcoef(d[:-10], d[10:])[0, 1] if len(d) > 20 else np.nan
        hl = -10 * np.log(2) / np.log(ac) if 0 < ac < 1 else np.inf
        print(f'price - mid: rms {np.sqrt(np.mean(d ** 2)):.2f} bp, p99 |.| {np.percentile(np.abs(d), 99):.2f} bp, half-life {hl:.0f}s')
    if c != base:
        for k in (30, 60):
            x, y = L[base].values, p
            cr = np.corrcoef(x[k::k] - x[:-k:k], y[k::k] - y[:-k:k])[0, 1]
            print(f'corr vs {base} at {k}s returns: {cr:.2f}', end='; ')
        print()
    if len(p) > 3600 + 600:
        t = np.arange(3600, len(p) - 300, 30); res = {}
        for nm, (k, cl) in {'raw 1s': (1, None), 'robust 10s': (10, 6)}.items():
            F = rv_features(p, k, cl); lf = np.log(np.column_stack([F[W][t] for W in (60, 900, 3600)]))
            f = np.array([har.rate(v, 300) for v in lf]) * 300; res[nm] = np.mean((p[t + 300] - p[t]) ** 2) / f.mean()
        print('mean(r2_300)/mean(f_300):', {k: round(v, 2) for k, v in res.items()}, ' (calibrated ~0.85-1.0)')
