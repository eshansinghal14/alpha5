"""
Was the race theo we traded a better forecast of the winner than Kalshi's own
race mids? From the live journals only -- no replay, so it scores whatever
model was live at the time (several versions over the period).

    python -m kalshi_strategies.CRYPTOLEAD15M... (see main) --dates 2026-10-12 ... 2026-10-19

Per event, every `--step` seconds with > 60s left and all five legs quoted
(spread <= --max-spread on each): our five fairs (prob_raw, renormalised) and
Kalshi's five mids (renormalised), scored against the settled winner:
multi-class log loss and Brier. Paired difference per event (ours - Kalshi),
with a bootstrap CI over events. Also by time left, by day, and the best blend
p = w * ours + (1 - w) * Kalshi.
"""
from __future__ import annotations

import argparse
import glob
import os

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.runtime.journal import journal_dir

COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
ET = "America/New_York"
TAU_BINS = [60, 120, 300, 600, 900]


def close_of(ev: str) -> float:
    return pd.to_datetime(ev.split("-")[1], format="%y%b%d%H%M").tz_localize(ET).tz_convert("UTC").timestamp()


def winners(lo: float, hi: float) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cursor = None
    while True:
        prm = {"series_ticker": "KXCRYPTOLEAD15M", "min_close_ts": int(lo) - 1, "max_close_ts": int(hi) + 1,
               "limit": 1000}
        if cursor:
            prm["cursor"] = cursor
        r = kalshi_get("/trade-api/v2/markets", prm)
        for m in r.get("markets", []):
            if m.get("result") == "yes":
                out.setdefault(m["event_ticker"], []).append(m["ticker"].rsplit("-", 1)[-1])
        cursor = r.get("cursor")
        if not cursor or not r.get("markets"):
            return out


def load_day(date: str, step: int) -> pd.DataFrame:
    parts = sorted(glob.glob(os.path.join(journal_dir("KXCRYPTOLEAD15M", date), "decisions", "part-*.parquet")))
    if not parts:
        return pd.DataFrame()
    cols = ["time", "ticker", "prob_raw", "market_bid", "market_ask", "x_tau"]
    fr = []
    # A part at a time, thinned at once: a full day of decisions is ~2.5 GB.
    for p in parts:
        try:
            t = ds.dataset(p, format="parquet").to_table(columns=cols).to_pandas()
        except Exception:
            try:
                t = pd.read_parquet(p).reindex(columns=cols)
            except Exception:
                continue
        t = t.dropna(subset=["prob_raw", "market_bid", "market_ask", "x_tau"])
        if t.empty:
            continue
        t["s"] = (t.time.astype("int64") // 10**9) // step * step
        fr.append(t.drop_duplicates(["ticker", "s"], keep="last").drop(columns="time"))
    if not fr:
        return pd.DataFrame()
    d = pd.concat(fr, ignore_index=True).drop_duplicates(["ticker", "s"], keep="last")
    d["coin"] = d.ticker.str.rsplit("-", n=1).str[-1]
    d["event"] = d.ticker.str.rsplit("-", n=1).str[0]
    return d[d.coin.isin(COINS)]


def score(d: pd.DataFrame, win: dict[str, list[str]], max_spread: float) -> pd.DataFrame:
    d = d[d.event.isin([e for e, w in win.items() if len(w) == 1])]
    d = d[(d.market_ask > d.market_bid) & (d.market_ask - d.market_bid <= max_spread + 1e-9) & (d.x_tau > 60)]
    w = d.pivot_table(index=["event", "s"], columns="coin", values=["prob_raw", "market_bid", "market_ask"],
                      aggfunc="last")
    w = w.dropna()
    if w.empty:
        return pd.DataFrame()
    ours = w["prob_raw"][list(COINS)].to_numpy()
    km = ((w["market_bid"] + w["market_ask"]) / 2)[list(COINS)].to_numpy()
    ours = np.clip(ours, 1e-4, None)
    ours = ours / ours.sum(1, keepdims=True)
    kalshi = km / km.sum(1, keepdims=True)
    ev = w.index.get_level_values(0)
    yi = np.array([COINS.index(win[e][0]) for e in ev])
    y = np.zeros_like(ours)
    y[np.arange(len(yi)), yi] = 1.0
    tau = d.groupby(["event", "s"]).x_tau.first().reindex(w.index).to_numpy()
    out = pd.DataFrame({"event": ev, "s": w.index.get_level_values(1), "tau": tau,
                        "ll_ours": -np.log(np.clip(ours[np.arange(len(yi)), yi], 1e-4, 1)),
                        "ll_kalshi": -np.log(np.clip(kalshi[np.arange(len(yi)), yi], 1e-4, 1)),
                        "br_ours": ((ours - y) ** 2).sum(1), "br_kalshi": ((kalshi - y) ** 2).sum(1),
                        "ksum": km.sum(1)})
    for wgt in np.linspace(0, 1, 11):
        p = wgt * ours + (1 - wgt) * kalshi
        out[f"ll_w{wgt:.1f}"] = -np.log(np.clip(p[np.arange(len(yi)), yi], 1e-4, 1))
    return out


def boot(x: np.ndarray, n: int = 2000, seed: int = 0) -> tuple[float, float]:
    r = np.random.default_rng(seed)
    m = [x[r.integers(0, len(x), len(x))].mean() for _ in range(n)]
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--max-spread", type=float, default=0.10)
    a = ap.parse_args(argv)
    pd.set_option("display.width", 220)
    days = []
    for date in a.dates:
        d = load_day(date, a.step)
        if d.empty:
            print(date, "no decisions")
            continue
        lo = min(close_of(e) for e in d.event.unique()) - 900
        hi = max(close_of(e) for e in d.event.unique())
        win = winners(lo, hi)
        s = score(d, win, a.max_spread)
        if s.empty:
            print(date, "nothing scorable")
            continue
        s["day"] = date
        days.append(s)
        print(f"{date}: {s.event.nunique()} events, {len(s)} snapshots", flush=True)
    s = pd.concat(days, ignore_index=True)
    pe = s.groupby("event")[["ll_ours", "ll_kalshi", "br_ours", "br_kalshi"]].mean()
    dll = (pe.ll_ours - pe.ll_kalshi).to_numpy()
    dbr = (pe.br_ours - pe.br_kalshi).to_numpy()
    print(f"\n== {len(pe)} settled events, {len(s)} snapshots (every {a.step}s, > 60s left, every leg spread <= "
          f"{a.max_spread * 100:.0f}c); Kalshi mids summed to {s.ksum.median():.3f} (median)")
    print(f"log loss: ours {pe.ll_ours.mean():.4f}  kalshi {pe.ll_kalshi.mean():.4f}  diff {dll.mean():+.4f} "
          f"90% CI {boot(dll)[0]:+.4f}..{boot(dll)[1]:+.4f}  ours better on {(dll < 0).mean():.0%} of events")
    print(f"Brier:    ours {pe.br_ours.mean():.4f}  kalshi {pe.br_kalshi.mean():.4f}  diff {dbr.mean():+.4f} "
          f"90% CI {boot(dbr)[0]:+.4f}..{boot(dbr)[1]:+.4f}")
    s["tau_b"] = pd.cut(s.tau, TAU_BINS)
    print("\nby time left (per-event means, then averaged):")
    bt = s.groupby(["tau_b", "event"], observed=True)[["ll_ours", "ll_kalshi"]].mean().groupby("tau_b", observed=True)
    t = bt.mean()
    t["diff"] = t.ll_ours - t.ll_kalshi
    t["events"] = bt.size()
    print(t.round(4).to_string())
    print("\nby day:")
    bd = s.groupby(["day", "event"])[["ll_ours", "ll_kalshi"]].mean().groupby("day")
    t = bd.mean()
    t["diff"] = t.ll_ours - t.ll_kalshi
    t["events"] = bd.size()
    t["ours_better"] = bd.apply(lambda g: (g.ll_ours < g.ll_kalshi).mean())
    print(t.round(4).to_string())
    wcols = [c for c in s.columns if c.startswith("ll_w")]
    bw = s.groupby("event")[wcols].mean().mean()
    print("\nblend p = w*ours + (1-w)*kalshi, log loss by w:")
    print(bw.round(4).to_string())
    print(f"best w = {bw.idxmin()[4:]}")


if __name__ == "__main__":
    main()
