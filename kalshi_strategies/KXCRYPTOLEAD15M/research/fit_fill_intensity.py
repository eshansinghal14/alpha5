"""
Fit the fill-intensity model and dump the JSON artifact the live quoter
loads (kalshi_crypto/fill_intensity.json).

Estimates `lambda_side(tau, p)` -- the per-second-per-market rate at which
resting orders at the front of a side's queue get filled -- from Kalshi
trade history, and the queue adjustment `g(ticks, queue_ahead)` from the
raw order-book + trades (optional; falls back to operator-plausible
constants when the OB study isn't run).

lambda_side method (spec section 6.2 "Estimating fill rate")
------------------------------------------------------------
Each trade's `taker_book_side` names the resting side that got filled:
  * taker lifts the ASK  -> a resting YES ASK (== NO bid) filled -> "ask"/no
  * taker hits the BID   -> a resting YES BID filled               -> "bid"/yes
Bucket filled `count` by (book_side, tau_bucket, price_bucket), divide by
the seconds observed in that (tau) bucket across all markets, and by the
number of simultaneous markets, to get a per-second-per-market rate.

  tau   = seconds to the market's close at trade time.
  price = the resting order's own-side price:
            bid fills -> yes_price (the YES bid that was hit)
            ask fills -> no_price  (the NO bid == YES ask that was lifted)

Seconds-observed per (tau) bucket: each market contributes the overlap of
its lifetime with the bucket's [tau_lo, tau_hi) window. We approximate it
as (#markets) * (bucket width in seconds), since every market is live
across the whole 0..900s tau range -- exact enough for a rate estimate.

Usage:
    python kalshi_crypto/rv_modeling/fit_fill_intensity.py
      [--series KXBTC15M]        # source trades (default: KXCRYPTOLEAD15M,
                                 #   falls back to KXBTC15M if empty)
      -> writes kalshi_crypto/fill_intensity.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT = _HERE.parents[1]

# Bucket edges. tau in seconds-to-close; a 15-minute (900s) event, finer
# near the close where fills accelerate. price in cents/dollars 0..1,
# coarse -- the rate's price dependence is weak compared with its tau
# dependence, and the Coin Race lives mostly in 0.02..0.5.
TAU_EDGES = [30.0, 60.0, 120.0, 300.0, 600.0]          # -> 6 tau buckets
PRICE_EDGES = [0.05, 0.10, 0.20, 0.35, 0.50, 0.70]     # -> 7 price buckets

ARTIFACT = _PROJECT / "kalshi_crypto" / "fill_intensity.json"
DEFAULT_SERIES = "KXCRYPTOLEAD15M"
FALLBACK_SERIES = "KXBTC15M"   # structurally identical 15m crypto series


def _event_close_s(trades: pd.DataFrame) -> dict[str, float]:
    """Close time (unix s) per event ticker. Kalshi 15m events close at the
    event's end; we take the max trade time per event as a lower bound and
    round up to the next minute boundary -- trades stop at close. Good
    enough for a tau bucket. (A precise close would come from the Kalshi
    markets endpoint; the last trade is within seconds of it.)"""
    out = {}
    for ev, sub in trades.groupby("event_ticker"):
        last = sub["ts_ms"].max() / 1000.0
        out[ev] = float(np.ceil(last / 60.0) * 60.0)
    return out


def fit(series: str) -> dict:
    trades_dir = _PROJECT / "data" / "kalshi_ws" / series / "trades"
    files = sorted(trades_dir.glob("*.parquet")) if trades_dir.exists() else []
    if not files and series == DEFAULT_SERIES:
        print(f"[fit_fill_intensity] no trades for {series}; "
              f"falling back to {FALLBACK_SERIES}")
        series = FALLBACK_SERIES
        trades_dir = _PROJECT / "data" / "kalshi_ws" / series / "trades"
        files = sorted(trades_dir.glob("*.parquet")) if trades_dir.exists() else []
    if not files:
        raise SystemExit(f"no trade files under {trades_dir}")

    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    close_s = _event_close_s(df)
    df = df.assign(
        tau=lambda d: d["event_ticker"].map(close_s) - d["ts_ms"] / 1000.0)
    df = df[df["tau"] >= 0.0]

    # Resting-side price: bid fills -> yes bid price; ask fills -> no bid price.
    df["rest_price"] = np.where(df["taker_book_side"] == "bid",
                                df["yes_price"], df["no_price"])

    n_events = df["event_ticker"].nunique()
    # Average simultaneous markets = distinct (event, ticker) pairs / events.
    n_markets = df.groupby("event_ticker")["ticker"].nunique().mean()

    tau_bins = TAU_EDGES + [np.inf]
    price_bins = PRICE_EDGES + [np.inf]
    n_tau = len(tau_bins)
    n_price = len(price_bins)

    def bucket_grid(book_side: str) -> list[list[float]]:
        sub = df[df["taker_book_side"] == book_side]
        grid = np.zeros((n_tau, n_price))
        ti = np.digitize(sub["tau"].to_numpy(), TAU_EDGES, right=False)
        pi = np.digitize(sub["rest_price"].to_numpy(), PRICE_EDGES, right=False)
        cnt = sub["count"].to_numpy()
        for t, p, c in zip(ti, pi, cnt):
            grid[t, p] += c
        # Seconds observed per tau bucket: bucket width * n_events * n_markets.
        edges = [0.0] + TAU_EDGES + [900.0]   # cap the open bucket at the 900s event
        for t in range(n_tau):
            width = edges[t + 1] - edges[t]
            denom = max(width * n_events * n_markets, 1e-9)
            grid[t, :] /= denom
        return grid.tolist()

    lam_bid = bucket_grid("bid")   # resting YES bids filled
    lam_ask = bucket_grid("ask")   # resting YES asks (NO bids) filled

    return {
        "tau_edges": TAU_EDGES,
        "price_edges": PRICE_EDGES,
        "lam_bid": lam_bid,
        "lam_ask": lam_ask,
        # Queue adjustment: operator-plausible until the virtual-order OB
        # study refines them (see module docstring / fill_intensity.g).
        "depth_decay": 0.55,
        "queue_half": 50.0,
        "fallback_lambda": 0.02,
        "fitted_on": dt.date.today().isoformat(),
        "source_series": series,
        "n_events": int(n_events),
        "n_markets_avg": float(round(n_markets, 2)),
        "n_trades": int(len(df)),
        "notes": ("lambda_side = filled count per (book_side, tau, price) "
                  "bucket / (bucket_seconds * n_events * n_markets_avg). "
                  "Rate is per-second-per-market, front-of-queue."),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=DEFAULT_SERIES)
    ap.add_argument("--out", default=str(ARTIFACT))
    args = ap.parse_args()
    artifact = fit(args.series)
    with open(args.out, "w") as f:
        json.dump(artifact, f, indent=2)
    print(f"[fit_fill_intensity] wrote {args.out}")
    print(f"  source={artifact['source_series']} events={artifact['n_events']} "
          f"markets/ev={artifact['n_markets_avg']} trades={artifact['n_trades']}")
    # Quick sanity: mean rate per side.
    for k in ("lam_bid", "lam_ask"):
        arr = np.array(artifact[k])
        print(f"  {k}: mean={arr.mean():.4f}/s  max={arr.max():.4f}/s")


if __name__ == "__main__":
    main()
