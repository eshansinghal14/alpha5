"""
Fit the HAR realized-vol model and dump a JSON artifact the live
forecaster loads at startup.

Reproduces cell 20 of `Realized-Vol Model for Kalshi Coin Race.ipynb`:
one `fit_loglinear` per forecast horizon (60, 300, 900 s), pooled across
coins, features (log RV_1m, log RV_15m, log RV_1h) from `rv1_60`,
`rv1_900`, `rv1_3600`. Also computes per-coin calibration scales from
the mean of `r^2 / forecast` at h=900s (notebook cell 22).

Artifact schema (JSON):
    {
      "horizons_s":    [60, 300, 900],
      "windows_s":     [60, 900, 3600],
      "coin_order":    ["BTC", "ETH", "SOL", "XRP", "HYPE"],
      "intercept":     [a_60, a_300, a_900],              # fitted a_h, 3 scalars
      "feature_mean":  [[mu_1m, mu_15m, mu_1h], ...],     # 3 x 3; one mu-vector per horizon
      "slopes":        [[b_1m, b_15m, b_1h], ...],        # 3 x 3; one slope row per horizon
      "coin_scale":    {"BTC": ..., ...},
      "fitted_on":     "ISO date",
      "n_rows_fit":    113760,
      "notes":         "..."
    }

Usage:
    python kalshi_strategies/KXCRYPTOLEAD15M/research/fit_har.py
      -> writes kalshi_strategies/KXCRYPTOLEAD15M/research/har_fit.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT = _HERE.parents[2]   # research/ -> KXCRYPTOLEAD15M/ -> kalshi_strategies/ -> repo
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# rvlib.py has a space-in-filename issue when imported normally; load
# directly from path.
import importlib.util
_rvlib_path = _HERE / "rvlib.py (RV model helpers).py"
_spec = importlib.util.spec_from_file_location("rvlib", _rvlib_path)
rvlib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rvlib)

COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE"]
HORIZONS = [60, 300, 900]
# Three HAR windows -- the notebook's "HAR 1m, 15m, 1h" model (cell 20).
WINDOWS = [60, 900, 3600]
TRAINING_TABLE = _HERE / "data" / "training_features_targets.parquet"
ARTIFACT = _HERE / "har_fit.json"


def _features(df: pd.DataFrame) -> np.ndarray:
    """log RV features for the three HAR windows, from 1-second returns.
    Matches rvlib.har(ws=[60, 900, 3600], k=1) exactly."""
    return np.column_stack([np.log(df[f"rv1_{W}"].values) for W in WINDOWS])


def fit(df: pd.DataFrame) -> dict:
    """Returns a dict ready to serialize. Model: one fit_loglinear per h.

    `fit_loglinear` fits  log f = theta[0] + (X - mu) @ theta[1:]
    with Gaussian NLL on y = r2 / h (per-second variance target). The
    pricer needs `forecast_var(h) = h * f(h)`, so we store `theta` and
    `mu` and multiply by `h` at inference.
    """
    out: dict = {
        "horizons_s": HORIZONS,
        "windows_s": WINDOWS,
        "coin_order": COINS,
        "intercept": [],
        "feature_mean": [],
        "slopes": [],
        "coin_scale": {},
        "fitted_on": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "n_rows_fit": int(len(df)),
        "notes": "Pooled across coins; one HAR per horizon. Base: HAR 1m/15m/1h.",
    }
    X = _features(df)
    # Fit per horizon.
    for h in HORIZONS:
        # Target = r^2 / h  (per-second variance at horizon h). Matches
        # rvlib.loo_days where feat_fn returns base=h and y = r2 / base.
        y = df[f"r2_{h}"].values / float(h)
        theta, mu = rvlib.fit_loglinear(X, y)
        out["intercept"].append(float(theta[0]))
        out["slopes"].append([float(theta[1]), float(theta[2]), float(theta[3])])
        out["feature_mean"].append([float(mu[0]), float(mu[1]), float(mu[2])])

    # Per-coin calibration: mean(r^2 / forecast) at h=900. Replicates
    # notebook cell 22. Values near 1.0 are unbiased; HYPE is ~1.15.
    h_cal = 900
    y_cal = df[f"r2_{h_cal}"].values / float(h_cal)
    # Re-use the h=900 fit.
    theta_cal = np.array([out["intercept"][HORIZONS.index(h_cal)]]
                         + out["slopes"][HORIZONS.index(h_cal)])
    mu_cal = np.array(out["feature_mean"][HORIZONS.index(h_cal)])
    Xc = np.column_stack([np.ones(len(X)), X - mu_cal])
    f_cal = np.exp(Xc @ theta_cal)
    ratio = y_cal / f_cal
    for coin in COINS:
        m = df["coin"].values == coin
        out["coin_scale"][coin] = float(ratio[m].mean())
    return out


def write_artifact(fit_dict: dict, path: Path = ARTIFACT):
    path.write_text(json.dumps(fit_dict, indent=2))
    print(f"[fit_har] wrote {path}")


def summarize(fit_dict: dict):
    print(f"[fit_har] rows: {fit_dict['n_rows_fit']}  coins: {len(fit_dict['coin_order'])}")
    print(f"[fit_har] horizons: {fit_dict['horizons_s']}  HAR windows: {fit_dict['windows_s']}")
    print()
    print("per-horizon slopes (b_1m, b_15m, b_1h) + intercept:")
    for h, a, b in zip(fit_dict["horizons_s"],
                        fit_dict["intercept"], fit_dict["slopes"]):
        s = sum(b)
        print(f"  h={h:4d}s  a={a:+.4f}  b_1m={b[0]:.3f}  b_15m={b[1]:.3f}"
              f"  b_1h={b[2]:.3f}  sum={s:.3f}")
    print()
    print("per-coin calibration scale (mean r^2 / forecast at h=900):")
    for c, s in fit_dict["coin_scale"].items():
        print(f"  {c:5s}  {s:.3f}")


def main():
    if not TRAINING_TABLE.exists():
        print(f"[fit_har] no training table at {TRAINING_TABLE}; "
              "generate it via the notebook or rvlib.build().", file=sys.stderr)
        sys.exit(1)
    df = pd.read_parquet(TRAINING_TABLE)
    fit_dict = fit(df)
    summarize(fit_dict)
    write_artifact(fit_dict)


if __name__ == "__main__":
    main()
