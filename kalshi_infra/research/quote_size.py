"""
Would larger resting quotes collect more edge? From one series' REAL maker fills:

    python -m kalshi_infra.research.quote_size --series KXCRYPTOLEAD15M --dates 2026-10-18 2026-10-19

Each maker fill is matched (trade_id, else ticker/side/price/time) to the public print that
filled it, and that print to its sweep: every print of the same taker order (same ticker,
taker side and exchange millisecond). In the maker's own price terms a sweep runs from the
best price down, so
  - volume at prices worse than ours traded after us: our order was used up, and a bigger one
    at our price would have taken it (extra_min);
  - volume at our price by other makers may have been behind us in the queue (extra_max adds it).
Markouts are per contract, signed for us, vs the Kalshi mid at +5/10/30/60s and to settlement.
Counterfactual for a quote m x as large: extra = min(available, (m - 1) x our fill in that
sweep), at our price, marked like our own fill. Assumes takers' sizes don't change and ignores
position caps (a bigger quote reaches max_position sooner).
"""
from __future__ import annotations

import argparse
import glob
import os

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.runtime.journal import journal_dir

H = (5, 10, 30, 60)
MULTS = (1.5, 2.0, 3.0, 5.0)
ET = "America/New_York"


def _read(series: str, date: str, stream: str, cols: list[str], tickers=None) -> pd.DataFrame:
    parts = sorted(glob.glob(os.path.join(journal_dir(series, date), stream, "part-*.parquet")))
    if not parts:
        return pd.DataFrame(columns=cols)
    flt = None if tickers is None else pc.field("ticker").isin(list(tickers))
    try:
        return ds.dataset(parts, format="parquet").to_table(columns=cols, filter=flt).to_pandas()
    except Exception:
        fr = []
        for p in parts:
            try:
                df = pd.read_parquet(p)
            except Exception:
                continue
            df = df[[c for c in cols if c in df.columns]]
            if tickers is not None:
                df = df[df.ticker.isin(tickers)]
            fr.append(df)
        return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame(columns=cols)


def _close_of(ticker: str) -> pd.Timestamp | None:
    try:
        return pd.to_datetime(ticker.split("-")[1], format="%y%b%d%H%M").tz_localize(ET).tz_convert("UTC")
    except Exception:
        return None


def _asof(t, tk, right: pd.DataFrame, col: str) -> np.ndarray:
    l = pd.DataFrame({"t": pd.to_datetime(t, utc=True), "ticker": tk}).reset_index()
    r = right.rename(columns={"time": "t"})[["t", "ticker", col]].sort_values("t")
    m = pd.merge_asof(l.sort_values("t"), r, on="t", by="ticker", direction="backward")
    return m.sort_values("index")[col].to_numpy()


def load(series: str, dates: list[str]):
    fills, trades, bbo = [], [], []
    for d in dates:
        f = _read(series, d, "fills", ["time", "ticker", "side", "price", "size", "liquidity", "trade_id", "role",
                                       "mode", "seeded"])
        if f.empty or "mode" not in f:
            continue
        f = f[(f["mode"] == "real") & (f.liquidity == "maker") & ~f.seeded.fillna(False).astype(bool)]
        if f.empty:
            continue
        tks = set(f.ticker)
        fills.append(f)
        trades.append(_read(series, d, "trades", ["time", "ticker", "trade_id", "yes_price", "size", "taker_side"], tks))
        bbo.append(_read(series, d, "bbo", ["time", "ticker", "bid", "ask"], tks))
    if not fills:
        return None
    f = pd.concat(fills, ignore_index=True).drop_duplicates(["trade_id", "time", "ticker", "side", "price", "size"])
    t = pd.concat(trades, ignore_index=True).drop_duplicates("trade_id")
    b = pd.concat(bbo, ignore_index=True).dropna(subset=["bid", "ask"])
    b = b[b.ask > b.bid].sort_values("time")
    b["mid"] = (b.bid + b.ask) / 2
    return f, t, b


def analyse(f: pd.DataFrame, t: pd.DataFrame, b: pd.DataFrame) -> dict:
    t = t.dropna(subset=["taker_side"]).copy()
    t["maker_side"] = np.where(t.taker_side == "no", "yes", "no")
    t["mpx"] = np.where(t.maker_side == "yes", t.yes_price, 1 - t.yes_price).round(4)
    t["sweep"] = t.groupby(["ticker", "taker_side", "time"], sort=False).ngroup()
    f = f.copy().reset_index(drop=True)
    f["px"] = f.price.round(4)
    # Match on trade_id; the rest by ticker/side/price within 2s of the print.
    m = f.merge(t[["trade_id", "sweep", "mpx", "maker_side"]], on="trade_id", how="left")
    miss = m.sweep.isna()
    if miss.any():
        l = m.loc[miss, ["time", "ticker", "side", "px"]].reset_index()
        r = t[["time", "ticker", "maker_side", "mpx", "sweep", "trade_id"]].rename(
            columns={"maker_side": "side", "mpx": "px", "trade_id": "tid2"}).sort_values("time")
        a = pd.merge_asof(l.sort_values("time"), r, on="time", by=["ticker", "side", "px"], direction="nearest",
                          tolerance=pd.Timedelta("2s"))
        m.loc[a["index"].values, "sweep"] = a.sweep.values
        m.loc[a["index"].values, "trade_id"] = m.loc[a["index"].values, "trade_id"].fillna(pd.Series(a.tid2.values,
                                                                                                    index=a["index"].values))
    match_rate = float(m.sweep.notna().mean())
    m = m.dropna(subset=["sweep"])
    m["sweep"] = m.sweep.astype(int)
    m["sign"] = np.where(m.side == "yes", 1.0, -1.0)
    m["ypx"] = np.where(m.side == "yes", m.price, 1 - m.price)
    for h in H:
        mid = _asof(m.time + pd.Timedelta(seconds=h), m.ticker.values, b, "mid")
        m[f"mo{h}"] = 100 * m.sign * (mid - m.ypx)
    # Settlement per market.
    res = {}
    for ev in sorted({tk.rsplit("-", 1)[0] for tk in m.ticker}):
        try:
            for mk in kalshi_get("/trade-api/v2/markets", {"event_ticker": ev, "limit": 100})["markets"]:
                res[mk["ticker"]] = {"yes": 1.0, "no": 0.0}.get(mk.get("result"))
        except Exception:
            pass
    m["y"] = m.ticker.map(res)
    m["mo_settle"] = 100 * m.sign * (m.y - m.ypx)
    closes = {tk: _close_of(tk) for tk in m.ticker.unique()}
    m["tau"] = [(closes[tk] - tt).total_seconds() if closes[tk] is not None else np.nan
                for tk, tt in zip(m.ticker, m.time)]

    # One row per (sweep we were in): our size, our deepest price, what traded after us.
    ours = set(m.trade_id.dropna())
    tt = t[t.sweep.isin(m.sweep.unique())]
    rows = []
    for sw, g in m.groupby("sweep"):
        p_us = g.px.min()
        s = tt[tt.sweep == sw]
        others = s[~s.trade_id.isin(ours)]
        worse = float(others[others.mpx < p_us - 1e-9]["size"].sum())
        same = float(others[(others.mpx - p_us).abs() <= 1e-9]["size"].sum())
        w = g["size"]
        row = {"sweep": sw, "ticker": g.ticker.iloc[0], "role": g.role.fillna("edge").iloc[0],
               "our": float(w.sum()), "sweep_size": float(s["size"].sum()), "extra_min": worse, "extra_max": worse + same,
               "tau": float(g.tau.iloc[0]), "price": float(g.ypx.iloc[0])}
        for c in [f"mo{h}" for h in H] + ["mo_settle"]:
            v = g[c]
            row[c] = float(np.average(v, weights=w)) if v.notna().all() else np.nan
        rows.append(row)
    return {"fills": m, "sweeps": pd.DataFrame(rows), "match_rate": match_rate}


def report(name: str, r: dict):
    m, s = r["fills"], r["sweeps"]
    pd.set_option("display.width", 220)
    print(f"\n######## {name}: {len(m)} maker fills, {m['size'].sum():.0f} lots, {len(s)} sweeps, "
          f"matched {r['match_rate']:.0%}; roles {m.role.fillna('edge').value_counts().to_dict()}")
    if s.empty:
        return
    s["exhausted"] = s.extra_min > 1e-9
    s["ratio_avail"] = s.extra_min / s.our
    print("our fill per sweep:", s.our.describe(percentiles=[.5, .9]).round(2).to_dict())
    print("sweep size (taker order):", s.sweep_size.describe(percentiles=[.5, .9, .99]).round(1).to_dict())
    print(f"sweeps where the taker kept going past us (our quote used up): {s.exhausted.mean():.0%}; "
          f"volume past us / our fill there: median {s.loc[s.exhausted, 'ratio_avail'].median():.1f}x")

    def wavg(g, c):
        ok = g[c].notna()
        return float(np.average(g.loc[ok, c], weights=g.loc[ok, "our"])) if ok.any() else np.nan

    print("\nmarkout c/contract (+ good for us), weighted by our lots:")
    rows = {}
    for lab, g in [("all", s), ("used up (taker kept going)", s[s.exhausted]), ("not used up", s[~s.exhausted])]:
        rows[lab] = {"sweeps": len(g), "lots": g.our.sum(), **{f"mo{h}": wavg(g, f"mo{h}") for h in H},
                     "settle": wavg(g, "mo_settle")}
    print(pd.DataFrame(rows).T.round(2).to_string())
    s["size_b"] = pd.cut(s.sweep_size / s.our, [0, 1.01, 2, 5, 20, 1e9], labels=["<=1x us", "1-2x", "2-5x", "5-20x", ">20x"])
    print("\nby taker size relative to our fill:")
    print(s.groupby("size_b", observed=True).apply(
        lambda g: pd.Series({"sweeps": len(g), "lots": g.our.sum(), "mo10": wavg(g, "mo10"), "mo30": wavg(g, "mo30"),
                             "mo60": wavg(g, "mo60"), "settle": wavg(g, "mo_settle")}), include_groups=False).round(2).to_string())
    s["good"] = s.mo30 > 0
    print("\ngood (30s markout > 0) vs adverse fills:")
    print(s.dropna(subset=["mo30"]).groupby("good").apply(
        lambda g: pd.Series({"sweeps": len(g), "our_lots": g.our.sum(), "mean_taker_size": g.sweep_size.mean(),
                             "median_taker_size": g.sweep_size.median(), "used_up": g.exhausted.mean(),
                             "lots_past_us": g.extra_min.sum(), "mo30": wavg(g, "mo30")}), include_groups=False).round(2).to_string())

    print("\nquote m x larger (extra at our price, marked like our fill); $ totals:")
    base = {f"${h}s": float((s.our * s[f"mo{h}"]).sum() / 100) for h in (10, 30, 60)}
    base["$settle"] = float((s.our * s.mo_settle).sum() / 100)
    out = {"now (1x)": {"lots": s.our.sum(), **base}}
    for k in MULTS:
        for kind in ("extra_min", "extra_max"):
            ex = np.minimum(s[kind], (k - 1) * s.our)
            out[f"{k:g}x +{'past' if kind == 'extra_min' else 'past+queue'}"] = {
                "lots": float(ex.sum()), **{f"${h}s": float((ex * s[f"mo{h}"]).sum() / 100) for h in (10, 30, 60)},
                "$settle": float((ex * s.mo_settle).sum() / 100)}
    o = pd.DataFrame(out).T
    for h in ("10s", "30s", "60s", "settle"):
        o[f"c/lot {h}"] = 100 * o[f"${h}"] / o.lots.replace(0, np.nan)
    print(o.round(2).to_string())
    s["tau_b"] = pd.cut(s.tau, [0, 300, 600, 900])
    ex = np.minimum(s.extra_min, s.our)
    print("\n2x, extra past us, by time left:")
    print(s.assign(ex=ex).groupby("tau_b", observed=True).apply(
        lambda g: pd.Series({"sweeps": len(g), "extra_lots": g.ex.sum(), "c/lot_30s": 100 * (g.ex * g.mo30).sum() / max(g.ex.sum(), 1e-9) / 100,
                             "$30s": (g.ex * g.mo30).sum() / 100, "$settle": (g.ex * g.mo_settle).sum() / 100}),
        include_groups=False).round(2).to_string())
    if s.ticker.str.contains("CRYPTOLEAD").any():
        s["coin"] = s.ticker.str.rsplit("-", n=1).str[-1]
        print("\n2x, extra past us, by coin:")
        print(s.assign(ex=ex).groupby(["coin", "role"]).apply(
            lambda g: pd.Series({"sweeps": len(g), "our_lots": g.our.sum(), "extra_lots": g.ex.sum(),
                                 "$30s_now": (g.our * g.mo30).sum() / 100, "$30s_extra": (g.ex * g.mo30).sum() / 100,
                                 "$settle_extra": (g.ex * g.mo_settle).sum() / 100}), include_groups=False).round(2).to_string())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="+", required=True)
    ap.add_argument("--dates", nargs="+", required=True)
    a = ap.parse_args(argv)
    for series in a.series:
        got = load(series, a.dates)
        if got is None:
            print(f"\n######## {series}: no real maker fills on {a.dates}")
            continue
        r = analyse(*got)
        report(series, r)
        for role in ("edge", "lip"):
            sub = r["sweeps"][r["sweeps"].role == role]
            if 0 < len(sub) < len(r["sweeps"]):
                fr = r["fills"][r["fills"].role.fillna("edge") == role]
                report(f"{series} [{role}]", {"fills": fr, "sweeps": sub.copy(), "match_rate": r["match_rate"]})


if __name__ == "__main__":
    main()
