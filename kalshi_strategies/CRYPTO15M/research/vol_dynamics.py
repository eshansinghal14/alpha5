"""
How fast and how abruptly does each coin's short-horizon vol change? Sets how
long an implied-vol estimate can be held before it is stale.

    python -m kalshi_strategies.CRYPTO15M.research.vol_dynamics --days 7

From the persisted 1s venue-median mids (data/kalshi_crypto/composite):
  * spot vol per 1-minute block = rms of 10s log returns (every 1s offset),
    in bp per sqrt(second); minutes with gaps are dropped
  * persistence: autocorrelation of log minute-vol at lags 1..60 min, and
    the median |log ratio| between minute-vols k minutes apart, i.e. how far
    a k-minute-old estimate typically is from now
  * staleness: the error of a vol estimated over the previous W minutes as a
    forecast of the next 1 / 5 / 15 minutes (median |log ratio|, and the share
    off by more than 1.5x), with the noise floor of a 1-minute sample shown by
    comparing two halves of the same minute
  * abruptness: share of minutes where vol jumps >= 2x / 3x the previous
    15-minute level, and how long such a jump takes to decay back (half-life)
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

from kalshi_strategies.KXCRYPTOLEAD15M.model import load_composite_bars
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS

RET_S = 10


def minute_vol(coin: str, lo: float, hi: float) -> pd.DataFrame:
    bars = load_composite_bars(coin, lo, hi)
    s = pd.Series({int(b.ts_s): b.mid for b in bars}).sort_index()
    s = s[s > 0]
    full = s.reindex(range(int(s.index[0]), int(s.index[-1]) + 1))
    lp = np.log(full)
    r = (lp - lp.shift(RET_S)).dropna()
    r = r[r.abs() < 0.01]   # drop >100bp 10s glitches (bad book)
    t = pd.to_datetime(r.index, unit="s", utc=True)
    df = pd.DataFrame({"r2": r.values ** 2, "half": (r.index % 60) < 30}, index=t)
    g = df.groupby(df.index.floor("min"))
    n = g.size()
    out = pd.DataFrame({"var": g["r2"].mean() / RET_S, "n": n})
    h = df.groupby([df.index.floor("min"), "half"])["r2"].mean().unstack()
    out["v_a"], out["v_b"] = h.get(True) / RET_S, h.get(False) / RET_S
    out = out[out.n >= 50]
    out["vol"] = np.sqrt(out["var"]) * 1e4       # bp per sqrt(s)
    return out


def analyse(coin: str, m: pd.DataFrame) -> dict:
    v = m["vol"]
    v = v.reindex(pd.date_range(v.index[0], v.index[-1], freq="min"))
    lv = np.log(v)
    res = {"minutes": int(v.notna().sum()), "vol_median": float(v.median()),
           "vol_p10": float(v.quantile(.1)), "vol_p90": float(v.quantile(.9))}
    # Sampling noise of a 1-minute estimate: two halves of the same minute.
    a, b = np.sqrt(m["v_a"]), np.sqrt(m["v_b"])
    ok = (a > 0) & (b > 0)
    res["noise_1m_halves"] = float(np.median(np.abs(np.log(a[ok] / b[ok]))))
    for k in (1, 5, 15, 30, 60, 120):
        d = (lv - lv.shift(k)).abs().dropna()
        res[f"drift_{k}m"] = float(d.median())
    for k in (1, 5, 15, 60):
        x = pd.concat([lv, lv.shift(k)], axis=1).dropna()
        res[f"acf_{k}m"] = float(np.corrcoef(x.iloc[:, 0], x.iloc[:, 1])[0, 1])
    # Forecast error: vol over the previous W minutes vs the next H minutes.
    var = v ** 2
    for W in (1, 5, 15, 60):
        past = np.sqrt(var.rolling(W, min_periods=max(1, int(.8 * W))).mean())
        for H in (1, 5, 15):
            fut = np.sqrt(var[::-1].rolling(H, min_periods=max(1, int(.8 * H))).mean()[::-1].shift(-1))
            e = np.log(fut / past).dropna()
            res[f"err_W{W}_H{H}"] = float(e.abs().median())
            res[f"off1.5x_W{W}_H{H}"] = float((e.abs() > math.log(1.5)).mean())
    # Abruptness: a minute against the previous 15-minute level.
    base = np.sqrt(var.shift(1).rolling(15, min_periods=12).mean())
    ratio = (v / base).dropna()
    res["jump2x_share"] = float((ratio >= 2).mean())
    res["jump3x_share"] = float((ratio >= 3).mean())
    res["drop0.5x_share"] = float((ratio <= 0.5).mean())
    # Decay after a 2x jump: median ratio k minutes later, vs the same base.
    jumps = ratio[ratio >= 2].index
    dec = {}
    def later_ratio(k):
        num = v.reindex(jumps + pd.Timedelta(minutes=k)).to_numpy()
        return num / base.reindex(jumps).to_numpy()

    for k in (1, 2, 5, 10, 15, 30):
        x = later_ratio(k)
        x = x[np.isfinite(x)]
        dec[k] = float(np.median(x)) if len(x) else float("nan")
    res["after_jump"] = dec
    res["n_jumps"] = int(len(jumps))
    # Jumps cluster: is the minute after a jump also >= 1.5x?
    x = later_ratio(1)
    x = x[np.isfinite(x)]
    res["jump_followed_1.5x"] = float(np.mean(x >= 1.5)) if len(x) else float("nan")
    # Time-of-day: median vol per UTC hour, as a ratio to the coin's median.
    hod = v.groupby(v.index.hour).median() / v.median()
    res["hour_of_day"] = hod.round(2).to_dict()
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", nargs="+", default=list(COINS))
    a = ap.parse_args(argv)
    hi = pd.Timestamp.now(tz="UTC").floor("min").timestamp()
    lo = hi - a.days * 86400
    rows, extra = {}, {}
    for c in a.coins:
        m = minute_vol(c, lo, hi)
        r = analyse(c, m)
        extra[c] = (r.pop("after_jump"), r.pop("hour_of_day"))
        rows[c] = r
    t = pd.DataFrame(rows).T
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    print("spot vol per minute, bp/sqrt(s) from 10s returns\n")
    print(t[["minutes", "vol_p10", "vol_median", "vol_p90", "noise_1m_halves"]].round(3).to_string(), "\n")
    print("drift: median |ln(vol_t / vol_t-k)| between 1-minute vols (0.10 ~ 10% apart; includes 1m sampling noise)")
    print(t[[f"drift_{k}m" for k in (1, 5, 15, 30, 60, 120)]].round(3).to_string(), "\n")
    print("autocorrelation of log minute-vol")
    print(t[[f"acf_{k}m" for k in (1, 5, 15, 60)]].round(3).to_string(), "\n")
    print("forecast error: vol over previous W min as a forecast of the next H min, median |ln ratio|")
    for H in (1, 5, 15):
        print(f"  next {H} min")
        print(t[[f"err_W{W}_H{H}" for W in (1, 5, 15, 60)]].round(3).to_string())
    print("\nshare of forecasts off by more than 1.5x")
    for H in (1, 5, 15):
        print(f"  next {H} min")
        print(t[[f"off1.5x_W{W}_H{H}" for W in (1, 5, 15, 60)]].round(3).to_string())
    print("\nabrupt changes: a minute vs the previous 15 minutes' vol")
    print(t[["n_jumps", "jump2x_share", "jump3x_share", "drop0.5x_share", "jump_followed_1.5x"]].round(4).to_string())
    print("\nafter a >=2x minute: median vol k minutes later / the pre-jump 15-min level")
    print(pd.DataFrame({c: e[0] for c, e in extra.items()}).T.round(2).to_string())
    print("\nmedian vol by UTC hour / coin median")
    print(pd.DataFrame({c: e[1] for c, e in extra.items()}).T.round(2).to_string())


if __name__ == "__main__":
    main()
