"""
Do the quoting edge settings make sense? For one series, from the journal:

    python -m kalshi_strategies.CRYPTO15M.research.edge_check --series KXETH15M --date 2026-10-18 --since 2026-10-18T21:20

  * the running quoting config and what it asks for: the edge per side
    (constant + edge_fraction x band + lean) against the Kalshi spread
  * where our quotes sat vs the touch (joined / behind / improving)
  * maker fills: markout vs our theo and vs the Kalshi mid at 1/5/10/30s, the
    share picked off (mid >= 2c against us within 5s), by the edge we asked
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from kalshi_infra.runtime.journal import read_journal
from kalshi_infra.runtime.persist import day_dir

H = (1, 5, 10, 30)


def asof(left_t, left_tk, right, cols):
    l = pd.DataFrame({"t": pd.to_datetime(left_t, utc=True), "ticker": left_tk}).reset_index()
    r = right.rename(columns={"time": "t"})[["t", "ticker", *cols]].sort_values("t")
    m = pd.merge_asof(l.sort_values("t"), r, on="t", by="ticker", direction="backward")
    return m.sort_values("index").set_index("index")[cols]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXETH15M")
    ap.add_argument("--date", required=True)
    ap.add_argument("--since", required=True)
    a = ap.parse_args(argv)
    since = pd.Timestamp(a.since, tz="UTC")
    s = json.load(open(os.path.join(day_dir(a.series, a.date), "summary.json")))
    q = s["config"]["quoting"]
    print("quoting:", {k: q[k] for k in ("constant_edge", "edge_fraction", "lean_dollars", "quoting_edge",
                                         "order_lots", "max_position", "max_improve_ticks",
                                         "quote_levels_behind", "quote_keep_inside", "enable_taking")})
    print("band: ci_vol_multiplier", s["config"]["pricing"]["ci_vol_multiplier"],
          "reaction_latency_s", s["config"]["pricing"]["reaction_latency_s"])

    cols = ["time", "ticker", "prob_raw", "prob_lo", "prob_hi", "market_bid", "market_ask", "quote_bid",
            "quote_ask", "ci_edge", "lean_bid", "lean_ask", "constant_edge", "block_bid", "quote_block", "x_tau"]
    d = read_journal(a.series, a.date, "decisions", columns=None)
    d = d[[c for c in cols if c in d.columns]]
    d = d[d.time >= since]
    f = read_journal(a.series, a.date, "fills")
    f = f[(f.time >= since) & (f.liquidity == "maker")].copy()
    b = read_journal(a.series, a.date, "bbo", columns=["time", "ticker", "bid", "ask"])
    b = b[b.time >= since].dropna()
    b["mid"] = (b.bid + b.ask) / 2

    ok = d.dropna(subset=["prob_raw", "market_bid", "market_ask"])
    ok = ok[ok.block_bid.isna() & ok.quote_block.isna()]
    spread = ok.market_ask - ok.market_bid
    band = (ok.prob_hi - ok.prob_raw)
    edge = ok.constant_edge + q["edge_fraction"] * band + ok.lean_bid.clip(lower=0).fillna(0)
    print(f"\n{len(ok)} unblocked decisions since {since}")
    print("Kalshi spread (c):", (100 * spread).describe(percentiles=[.5, .9]).round(2).to_dict())
    print("band half-width (c):", (100 * band).describe(percentiles=[.1, .5, .9]).round(2).to_dict())
    print("edge asked per side, before fees (c):", (100 * edge).describe(percentiles=[.1, .5, .9]).round(2).to_dict())
    qb = ok.dropna(subset=["quote_bid"])
    if len(qb):
        rel = 100 * (qb.quote_bid - qb.market_bid).round(4)
        print("our top bid vs Kalshi bid (c), share:", rel.value_counts(normalize=True).sort_index().round(3).head(8).to_dict())
        print("share of time a bid was up:", round(ok.quote_bid.notna().mean(), 3))
        gap = 100 * (qb.prob_raw - qb.market_bid - (qb.market_ask - qb.market_bid) / 2)
        print("theo - Kalshi mid while quoting (c):", gap.describe(percentiles=[.1, .5, .9]).round(2).to_dict())
    blk = d.quote_block.fillna("none").value_counts(normalize=True).round(3).to_dict()
    print("quote_block shares:", blk)

    if f.empty:
        print("\nno maker fills")
        return
    f["sign"] = np.where(f.side == "yes", 1.0, -1.0)
    f["yes_px"] = np.where(f.side == "yes", f.price, 1 - f.price)
    th = d.dropna(subset=["prob_raw"])[["time", "ticker", "prob_raw", "constant_edge", "prob_hi", "x_tau"]]
    m0 = asof(f.time, f.ticker.values, th, ["prob_raw", "prob_hi", "x_tau"])
    f["theo0"], f["band0"], f["tau"] = m0.prob_raw.values, (m0.prob_hi - m0.prob_raw).values, m0.x_tau.values
    f["edge_vs_theo_c"] = 100 * f.sign * (f.theo0 - f.yes_px)
    for h in H:
        t = f.time + pd.Timedelta(seconds=h)
        f[f"mid_{h}"] = 100 * f.sign * (asof(t, f.ticker.values, b, ["mid"]).mid.values - f.yes_px)
        f[f"theo_{h}"] = 100 * f.sign * (asof(t, f.ticker.values, th, ["prob_raw"]).prob_raw.values - f.yes_px)
    worst = np.zeros(len(f))
    for k in np.linspace(0.25, 5, 20):
        md = asof(f.time + pd.Timedelta(seconds=float(k)), f.ticker.values, b, ["mid"]).mid.values
        worst = np.minimum(worst, np.nan_to_num(100 * f.sign.values * (md - f.yes_px.values)))
    f["picked_off"] = worst <= -2.0
    print(f"\n{len(f)} maker fills, {f['size'].sum():.0f} lots")
    cols = ["edge_vs_theo_c", *[f"mid_{h}" for h in H], *[f"theo_{h}" for h in H]]
    print("mean c/contract (+ = good for us):")
    print(f[cols].mean().round(2).to_string())
    print("picked off (mid >= 2c against within 5s):", round(f.picked_off.mean(), 3))
    f["eb"] = pd.cut(f.edge_vs_theo_c, [-50, 0, 1, 2, 3, 5, 100])
    print(f.groupby("eb", observed=True).agg(n=("size", "size"), mid_5=("mid_5", "mean"), mid_30=("mid_30", "mean"),
                                             picked=("picked_off", "mean")).round(2).to_string())
    f["tb"] = pd.cut(f.tau, [0, 120, 300, 600, 900])
    print(f.groupby("tb", observed=True).agg(n=("size", "size"), mid_5=("mid_5", "mean"), mid_30=("mid_30", "mean"),
                                             picked=("picked_off", "mean")).round(2).to_string())
    pnl = (f["mid_30"] * f["size"]).sum() / 100
    print(f"\nsum 30s markout vs mid: ${pnl:+.2f} on {f['size'].sum():.0f} lots")


if __name__ == "__main__":
    main()
