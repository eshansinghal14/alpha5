"""
How long can a coin's implied-vol estimate be held when its 15M market stops
giving readings (mid near 0.5, pinned)? From the single-coin journals:

    python -m kalshi_strategies.CRYPTO15M.research.implied_hold --since 2026-10-18T23:25

Per coin, per-minute median of the EMA vol the model priced with (x_vol_bp)
and of the HAR rate (x_realized_vol_bp / sqrt(coin vol_mult) x sqrt(race
vol_mult), i.e. what the race's fallback would use). For lag k minutes:

    hold error     |ln(implied_{t+k} / implied_t)|   -- price at t+k on the t estimate
    fallback error |ln(HAR_{t+k} / implied_{t+k})|  -- price at t+k on HAR instead

against the implied estimate at t+k as the reference. Holding is the better
choice up to the k where its error passes the fallback's.
"""
from __future__ import annotations

import argparse
import glob
import math
import os

import numpy as np
import pandas as pd

from kalshi_infra.config import load

COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
LAGS = (1, 2, 3, 5, 7, 10, 15, 20)


def read(series: str, since: pd.Timestamp) -> pd.DataFrame:
    cols = ["time", "x_vol_bp", "x_realized_vol_bp"]
    fr = []
    days = pd.date_range(since.floor("D"), pd.Timestamp.now(tz="UTC").floor("D"), freq="D")
    for day in days:
        for p in sorted(glob.glob(f"data/live/{series}/{day:%Y-%m-%d}/journal/decisions/part-*.parquet")):
            if os.path.getmtime(p) < since.timestamp() - 120:
                continue
            try:
                df = pd.read_parquet(p, columns=cols)
            except Exception:
                continue
            fr.append(df[df.time >= since])
    return pd.concat(fr) if fr else pd.DataFrame(columns=cols)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    a = ap.parse_args(argv)
    since = pd.Timestamp(a.since, tz="UTC")
    from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH as CP, UpDownConfig
    from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH as RP, CryptoConfig
    race_vm = load(CryptoConfig, RP).pricing.vol_mult
    pd.set_option("display.width", 220)
    hold, fb, tail, n = {}, {}, {}, {}
    for c in COINS:
        s = f"KX{c}15M"
        vm = load(UpDownConfig, CP, s).pricing.vol_mult
        d = read(s, since).dropna(subset=["x_vol_bp", "x_realized_vol_bp"])
        if d.empty:
            continue
        m = d.set_index("time").resample("1min").median()
        iv = np.log(m.x_vol_bp)
        har = np.log(m.x_realized_vol_bp / math.sqrt(vm) * math.sqrt(race_vm))
        n[c] = int(iv.notna().sum())
        hold[c], fb[c], tail[c] = {}, {}, {}
        for k in LAGS:
            later = iv.shift(-k)
            h = (later - iv).abs().dropna()
            f = (har.shift(-k) - later).abs().dropna()
            hold[c][f"{k}m"] = h.median()
            tail[c][f"{k}m"] = h.quantile(0.9)
            fb[c][f"{k}m"] = f.median()
    print(f"since {since}; race vol_mult {race_vm}; minutes with an implied estimate: {n}")
    print("\nhold error, median |ln(implied_t+k / implied_t)|  (0.10 ~ 10%)")
    print(pd.DataFrame(hold).T.round(3).to_string())
    print("\nhold error, 90th percentile")
    print(pd.DataFrame(tail).T.round(3).to_string())
    print("\nfallback error, median |ln(HAR / implied)| at the same minute")
    print(pd.DataFrame(fb).T.round(3).to_string())


if __name__ == "__main__":
    main()
