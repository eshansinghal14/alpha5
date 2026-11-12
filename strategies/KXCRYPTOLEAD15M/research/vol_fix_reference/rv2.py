"""Robust realized-variance features + HAR fit, shared by experiments and the reference implementation."""
import numpy as np
from scipy.signal import lfilter
WINDOWS = (60, 900, 3600)

def ewma(x, hl, x0):
    a = 1 - 0.5 ** (1 / hl)
    return lfilter([a], [1, -(1 - a)], x, zi=[x0 * (1 - a)])[0]

def rv_features(p, k=1, clip=None, windows=WINDOWS, vfloor=1e-10):
    """p: 1s log prices (forward-filled). Returns dict W -> per-second variance rate over the trailing W seconds,
    evaluated at every second. k: return interval (subsampled over all offsets). clip: cap |r| at clip * running sd."""
    n = len(p); r = np.zeros(n); r[k:] = p[k:] - p[:-k]
    if clip is not None:
        base = np.maximum(ewma(r ** 2, 900, np.nanmean(r[:3600] ** 2)), 1e-14)    # running variance of k-s returns
        s = np.sqrt(np.concatenate([[base[0]], base[:-1]]))                        # lagged one second (no look-ahead)
        r = np.clip(r, -clip * s, clip * s)
    q = r ** 2 / k
    cs = np.concatenate([[0.0], np.cumsum(q)])
    out = {}
    for W in windows:
        v = np.full(n, np.nan); v[W:] = (cs[W + 1:] - cs[1:n - W + 1]) / W
        out[W] = np.maximum(v, vfloor)
    return out
