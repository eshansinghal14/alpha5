"""
Single-coin 15M quoting, tracked from the live journals: where would quoting
make money, per coin? Run on the box (reads only, column subsets):

    nice -n 19 python -m kalshi_strategies.CRYPTO15M.research.shadow_tracker --since 2026-10-19T07:30

Hourly on EC2 (alpha5-crypto15m-track.timer, last 6h) into data/live/crypto15m_track/.

Two views, because paper fills can't be trusted on their own (replaying 657
real ETH orders through the paper rules filled 47 of 626 real lots, and the
ones it did fill were the better ones; docs/CHANGES.md 2026-10-19):

  tape   every public print in 10-90c, from the side of whoever rested there,
         bucketed by how far the print was from OUR theo at that moment and by
         time to close: the edge on offer and what it was worth 5s / 30s later.
         Independent of our fill model, but it's the average maker; our real
         ETH fills did 0.75c/lot worse than that over the same events.
  paper  our shadow maker fills: lots, markout, share picked off (mid >= 2c
         against within 5s), by edge at fill and by time to close.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from kalshi_infra.runtime.journal import read_journal

COINS = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
TAU_BINS = [0, 120, 300, 450, 600, 750, 900]
EDGE_BINS = [-100, -1, 0, 1, 2, 3, 5, 100]


def _close(ticker: pd.Series) -> pd.Series:
    sfx = ticker.str.split("-").str[1]
    return (pd.to_datetime(sfx, format="%y%b%d%H%M").dt.tz_localize("America/New_York").dt.tz_convert("UTC"))


def _asof(left: pd.DataFrame, right: pd.DataFrame, col: str, dt: float = 0.0) -> np.ndarray:
    q = left[["time", "ticker"]].copy()
    q["time"] = q.time + pd.Timedelta(seconds=dt)
    q = q.reset_index().sort_values("time")
    m = pd.merge_asof(q, right[["time", "ticker", col]].sort_values("time"), on="time", by="ticker",
                      direction="backward", tolerance=pd.Timedelta("60s"))
    return m.set_index("index").sort_index()[col].values


def _wavg(x, w):
    ok = ~np.isnan(x)
    return float(np.average(x[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else float("nan")


def _days(since: pd.Timestamp) -> list[str]:
    now = pd.Timestamp.now(tz="UTC")
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(since.normalize(), now.normalize(), freq="D")]


def _read(series, stream, since, columns=None):
    parts = [read_journal(series, d, stream, columns=columns) for d in _days(since)]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def load(series: str, since: pd.Timestamp):
    b = _read(series, "bbo", since, ["time", "ticker", "bid", "ask"])
    d = _read(series, "decisions", since, ["time", "ticker", "prob"])
    t = _read(series, "trades", since, ["time", "ticker", "trade_id", "yes_price", "size", "taker_side"])
    f = _read(series, "fills", since)
    out = []
    for x in (b, d, t, f):
        if len(x):
            x["time"] = pd.to_datetime(x.time, utc=True)
            x = x[x.time >= since]
        out.append(x)
    b, d, t, f = out
    if len(b):
        b = b.dropna(subset=["bid", "ask"])
        b = b[(b.bid > 0) & (b.ask < 1)].assign(mid=lambda x: (x.bid + x.ask) / 2)
    if len(d):
        d = d.dropna(subset=["prob"])
    if len(t):
        t = t.drop_duplicates(["ticker", "trade_id"])
    return b, d, t, f


def tape(b, d, t) -> pd.DataFrame:
    t = t[(t.yes_price >= 0.10) & (t.yes_price <= 0.90)].reset_index(drop=True)
    if t.empty or d.empty or b.empty:
        return pd.DataFrame()
    t["mdir"] = np.where(t.taker_side == "yes", -1.0, 1.0)          # resting side's yes direction
    t["theo"] = _asof(t, d, "prob", -0.001)
    t["edge"] = t.mdir * (t.theo - t.yes_price) * 100
    for h in (5, 30):
        t[f"mo{h}"] = t.mdir * (_asof(t, b, "mid", h) - t.yes_price) * 100
    t["tau"] = (_close(t.ticker) - t.time).dt.total_seconds()
    return t.dropna(subset=["edge"])


def paper(b, d, f) -> pd.DataFrame:
    if f.empty:
        return f
    f = f[(f.liquidity == "maker") & (f["mode"] == "shadow")].reset_index(drop=True)
    if f.empty:
        return f
    f["dir"] = np.where(f.side == "yes", 1.0, -1.0)
    f["yes_px"] = np.where(f.side == "yes", f.price, 1 - f.price)
    f["edge"] = f.dir * (_asof(f, d, "prob", -0.001) - f.yes_px) * 100
    for h in (5, 30):
        f[f"mo{h}"] = f.dir * (_asof(f, b, "mid", h) - f.yes_px) * 100
    worst = np.zeros(len(f))
    for k in np.linspace(0.25, 5, 20):
        worst = np.minimum(worst, np.nan_to_num(f.dir.values * (_asof(f, b, "mid", float(k)) - f.yes_px.values) * 100))
    f["picked"] = worst <= -2.0
    f["tau"] = (_close(f.ticker) - f.time).dt.total_seconds()
    return f


def _grid(x: pd.DataFrame, by: str, bins, extra=()) -> pd.DataFrame:
    x = x.assign(bucket=pd.cut(x[by], bins))
    rows = {}
    for k, g in x.groupby("bucket", observed=True):
        w = g["size"].values
        r = {"lots": w.sum(), "edge": _wavg(g.edge.values, w), "mo5": _wavg(g.mo5.values, w),
             "mo30": _wavg(g.mo30.values, w), "$mo30": np.nansum(g.mo30.values * w) / 100}
        for c in extra:
            r[c] = _wavg(g[c].values.astype(float), w)
        rows[str(k)] = r
    return pd.DataFrame(rows).T


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="UTC, e.g. 2026-10-19T07:30 (journals of every day since are read)")
    a = ap.parse_args(argv)
    since = pd.Timestamp(a.since, tz="UTC")
    pd.set_option("display.width", 220)
    summary = {}
    for s in COINS:
        b, d, t, f = load(s, since)
        T, P = tape(b, d, t), paper(b, d, f)
        hrs = max((pd.Timestamp.now(tz="UTC") - since).total_seconds() / 3600, 1e-9)
        print(f"\n===== {s}  ({hrs:.1f}h since {since:%H:%M} UTC) =====")
        if len(T):
            near = T[T.tau > 300]
            print("tape (resting side), tau > 300s, by edge vs our theo (c):")
            print(_grid(near, "edge", EDGE_BINS).round(2).to_string())
        if len(P):
            print("paper maker fills by edge at fill (c):")
            print(_grid(P, "edge", EDGE_BINS, extra=("picked",)).round(2).to_string())
            print("paper maker fills by seconds to close:")
            print(_grid(P, "tau", TAU_BINS, extra=("picked",)).round(2).to_string())
        w = P["size"].values if len(P) else np.array([])
        summary[s] = {"tape lots/h": T["size"].sum() / hrs if len(T) else 0,
                      "tape mo30 (edge>=1c, tau>300)": _wavg(*(lambda g: (g.mo30.values, g["size"].values))(
                          T[(T.edge >= 1) & (T.tau > 300)])) if len(T) else np.nan,
                      "paper lots/h": w.sum() / hrs if len(P) else 0,
                      "paper mo30": _wavg(P.mo30.values, w) if len(P) else np.nan,
                      "paper picked": _wavg(P.picked.values.astype(float), w) if len(P) else np.nan,
                      "paper $mo30/h": np.nansum(P.mo30.values * w) / 100 / hrs if len(P) else 0}
    print("\n===== summary =====")
    print(pd.DataFrame(summary).T.round(2).to_string())


if __name__ == "__main__":
    main()
