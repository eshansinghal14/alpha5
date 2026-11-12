"""Helpers for the Coinbase 1s realized-vol study."""
import glob, os, numpy as np, pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter

COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'HYPE']
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
WINDOWS = [60, 300, 900, 1800, 3600, 14400]   # feature look-backs (s)
HORIZONS = [60, 300, 900]                      # forecast horizons (s)
WARMUP, STEP = 14400, 60                       # first eval point 04:00 UTC, sample every 60s
VFLOOR = 1e-10                                 # per-second variance floor (stale-quote windows)

def load_day(coin, day):
    s = pd.read_parquet(f'{DATA}/s1/{coin}_{day}.parquet')
    return np.log(s['mid'].ffill().bfill().values), s['spr'].values

def days_for(coin):
    return sorted(os.path.basename(f).split('_')[1].split('.')[0] for f in glob.glob(f'{DATA}/s1/{coin}_*.parquet'))

def seasonal_profile(m1, exclude_days):
    """Minute-of-day variance multiplier per coin (mean 1), from 1m candles, excluding test days."""
    r2 = np.log(m1).diff() ** 2
    keep = ~r2.index.strftime('%Y%m%d').isin(exclude_days)
    r2 = r2[keep]
    prof = r2.groupby(r2.index.hour * 60 + r2.index.minute).mean()
    prof = pd.concat([prof.iloc[-15:], prof, prof.iloc[:15]]).rolling(31, center=True).mean().iloc[15:-15]
    return prof / prof.mean()

def ksub_rv_cumsum(p, k):
    """Cumulative sum of subsampled k-second squared returns (per-second contribution)."""
    q = np.zeros_like(p); q[k:] = (p[k:] - p[:-k]) ** 2 / k
    return np.concatenate([[0.0], np.cumsum(q)])

def build(coin, day, season=None, ks=(1, 10, 60), ewma_hls=(300, 900, 1800, 3600, 7200)):
    p, spr = load_day(coin, day)
    n = len(p); t = np.arange(WARMUP, n - max(HORIZONS), STEP)
    out = {'coin': np.repeat(coin, len(t)), 'day': np.repeat(day, len(t)), 't': t}
    for k in ks:
        cs = ksub_rv_cumsum(p, k)
        for W in WINDOWS:
            out[f'rv{k}_{W}'] = np.maximum((cs[t + 1] - cs[t + 1 - W]) / W, VFLOOR)   # per-second variance rate
    r1 = np.diff(p, prepend=p[0]) ** 2
    for hl in ewma_hls:                                                 # EWMA of 1s squared returns
        a = 1 - 0.5 ** (1 / hl)
        e = lfilter([a], [1, -(1 - a)], r1, zi=[r1[:600].mean() * (1 - a)])[0]
        out[f'ewma_{hl}'] = np.maximum(e[t], VFLOOR)
    cs60 = ksub_rv_cumsum(p, 60)
    for h in HORIZONS:
        out[f'r2_{h}'] = (p[t + h] - p[t]) ** 2                         # squared future h-return (pricing target)
        if h >= 300:
            out[f'srv_{h}'] = cs60[t + h + 1] - cs60[t + 1 + 60]          # subsampled 60s RV, lower-noise proxy
    out['spr'] = pd.Series(spr).ffill().values[t]
    if season is not None:
        sv = season[coin].values; mod = (t // 60) % 1440
        csv = np.concatenate([[0], np.cumsum(np.tile(sv, 2))])
        for W in WINDOWS:   # avg seasonal factor over past window
            out[f'sp_{W}'] = (csv[mod + 1440 + 1] - csv[mod + 1440 + 1 - W // 60]) / max(W // 60, 1)
        for h in HORIZONS:  # avg seasonal factor over future horizon
            out[f'sf_{h}'] = (csv[mod + 1 + h // 60] - csv[mod + 1]) / max(h // 60, 1)
    return pd.DataFrame(out)

def nll(f, r2):
    """Mean Gaussian negative log-likelihood (up to constants) of h-returns given forecast variance f."""
    f = np.maximum(f, 1e-14); return np.mean(np.log(f) + r2 / f) / 2

def fit_loglinear(X, r2, l2=1e-4):
    """Fit log f = a + (X - mean) @ b by Gaussian NLL (a variance GLM). Returns (theta, mean)."""
    mu = X.mean(0); Xc = np.column_stack([np.ones(len(X)), X - mu])
    th0 = np.zeros(Xc.shape[1]); th0[0] = np.log(r2.mean())
    def obj(th):
        lf = np.clip(Xc @ th, -60, 10); e = r2 * np.exp(-lf)
        g = Xc.T @ (1 - e) / (2 * len(lf)); g[1:] += 2 * l2 * th[1:]
        return np.mean(lf + e) / 2 + l2 * np.sum(th[1:] ** 2), g
    th = minimize(obj, th0, jac=True, method='BFGS', options={'gtol': 1e-9, 'maxiter': 500}).x
    return th, mu

def predict_loglinear(fit, X):
    th, mu = fit
    return np.exp(np.column_stack([np.ones(len(X)), X - mu]) @ th)

def loo_days(df, h, feat_fn, scale_only=False):
    """Leave-one-day-out CV. feat_fn(df, h) -> (X, base) with forecast f = base * exp(X @ theta)."""
    preds = np.zeros(len(df))
    for d in df['day'].unique():
        tr, te = df['day'] != d, df['day'] == d
        Xtr, btr = feat_fn(df[tr], h); Xte, bte = feat_fn(df[te], h)
        y = df.loc[tr, f'r2_{h}'].values / btr
        if scale_only or Xtr is None:
            preds[te.values] = bte * y.mean()
        else:
            fit = fit_loglinear(Xtr, y); preds[te.values] = bte * predict_loglinear(fit, Xte)
    return preds
