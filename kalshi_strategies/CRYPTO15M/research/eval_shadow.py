"""
Shadow evaluation for the single-coin 15M series: how our theo lines up with
the Kalshi touch, and how often our fills are picked off.

    python -m kalshi_strategies.CRYPTO15M.research.eval_shadow --dates 2026-10-18 [--since 2026-10-18T12:00]

Clocks: Kalshi trade prints carry the exchange time; everything else is our
local wall clock, which drifts against Kalshi's (no NTP on this box). Per
hour, local - exchange is the 0.1% quantile of (received - exchange time)
less NET_FLOOR_S, and fill times are moved onto the local clock with it, so
"theo before the fill" means before the trade happened at Kalshi, not before
we heard about it.

Pickoff: a fill whose theo moves against us by at least PICKOFF_C within
PICKOFF_S of the exchange-time fill. Markouts are signed so positive = good
for us, per contract, against our theo and against the Kalshi mid.

Writes data/live/CRYPTO15M_eval/report_<stamp>.md and fills_<stamp>.csv.
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

from kalshi_infra.runtime.journal import read_journal
from kalshi_infra.runtime.persist import LIVE_ROOT

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
# Assumed minimum one-way Kalshi->us print latency; the skew estimate is only
# good to this. Real warm order sends measured 34ms from here (execution.py).
NET_FLOOR_S = 0.02
HORIZONS_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
PICKOFF_S = 2.0
PICKOFF_C = 0.03
TAU_BINS = [0, 60, 120, 300, 600, 900]
OUT = os.path.join(LIVE_ROOT, "CRYPTO15M_eval")


def _read(series, dates, stream, columns=None):
    frames = [read_journal(series, d, stream, columns=columns) for d in dates]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True).sort_values("time") if frames else pd.DataFrame()


def skew_by_hour(trades: pd.DataFrame) -> pd.Series:
    lag = (trades["received"] - trades["time"]).dt.total_seconds()
    return (lag.groupby(trades["time"].dt.floor("h")).quantile(0.001) - NET_FLOOR_S).rename("skew_s")


def _asof(left_t: pd.Series, right: pd.DataFrame, cols, by="ticker", left_by=None, direction="backward"):
    l = pd.DataFrame({"t": pd.to_datetime(left_t, utc=True).values, by: left_by.values})
    l["t"] = pd.to_datetime(l["t"], utc=True)
    l = l.reset_index(drop=False)
    r = right[["time", by, *cols]].rename(columns={"time": "t"}).sort_values("t")
    m = pd.merge_asof(l.sort_values("t"), r, on="t", by=by, direction=direction)
    return m.sort_values("index").set_index("index")[cols]


def load(series: str, dates, since=None):
    dec = _read(series, dates, "decisions", ["time", "ticker", "prob_raw", "market_bid", "market_ask",
                                             "x_tau", "block_bid", "block_ask", "signal_age_s"])
    fills = _read(series, dates, "fills")
    bbo = _read(series, dates, "bbo", ["time", "ticker", "bid", "ask"])
    trades = _read(series, dates, "trades", ["time", "received", "ticker", "yes_price", "size", "taker_side"])
    if since is not None:
        dec, fills, bbo, trades = (d[d["time"] >= since] if not d.empty else d for d in (dec, fills, bbo, trades))
    theo = dec.dropna(subset=["prob_raw"])[["time", "ticker", "prob_raw", "x_tau"]]
    theo = theo[theo["prob_raw"].diff().ne(0) | theo["ticker"].ne(theo["ticker"].shift())]
    bbo = bbo.dropna(subset=["bid", "ask"])
    bbo = bbo.assign(mid=(bbo["bid"] + bbo["ask"]) / 2)
    return dec, fills, bbo, trades, theo


def theo_vs_book(dec: pd.DataFrame) -> dict:
    d = dec.dropna(subset=["prob_raw", "market_bid", "market_ask"]).copy()
    d = d[(d["market_ask"] > d["market_bid"])]
    d["mid"] = (d["market_bid"] + d["market_ask"]) / 2
    # Only the tradeable band: outside it the 0.1c ticks are rounded away.
    d = d[(d["mid"] >= 0.10) & (d["mid"] <= 0.90)]
    if d.empty:
        return {}
    d["diff"] = d["prob_raw"] - d["mid"]
    d["outside"] = (d["prob_raw"] < d["market_bid"]) | (d["prob_raw"] > d["market_ask"])
    # Time-weighted: each row holds until the next row of the same ticker.
    d["w"] = (-d.groupby("ticker")["time"].diff(-1)).dt.total_seconds().clip(0, 5).fillna(0)
    d["tau_b"] = pd.cut(d["x_tau"], TAU_BINS)
    w = d["w"].sum()

    def wq(x, wt, q):
        o = np.argsort(x.values)
        c = np.cumsum(wt.values[o])
        return float(x.values[o][np.searchsorted(c, q * c[-1])]) if len(c) and c[-1] > 0 else float("nan")

    by_tau = d.groupby("tau_b", observed=True).apply(
        lambda g: pd.Series({"hours": g["w"].sum() / 3600, "mean_diff_c": 100 * np.average(g["diff"], weights=g["w"] + 1e-12),
                             "mad_c": 100 * np.average(g["diff"].abs(), weights=g["w"] + 1e-12),
                             "outside_touch": np.average(g["outside"], weights=g["w"] + 1e-12)}),
        include_groups=False)
    return {"hours": w / 3600, "mean_diff_c": 100 * np.average(d["diff"], weights=d["w"] + 1e-12),
            "mad_c": 100 * np.average(d["diff"].abs(), weights=d["w"] + 1e-12),
            "p90_abs_c": 100 * wq(d["diff"].abs(), d["w"], 0.9),
            "outside_touch": float(np.average(d["outside"], weights=d["w"] + 1e-12)),
            "by_tau": by_tau}


def lead_lag(theo: pd.DataFrame, bbo: pd.DataFrame, lags=(-3, -2, -1, 0, 1, 2, 3, 5)) -> dict:
    """Corr of 1s theo changes at t with 1s Kalshi mid changes at t+lag. A peak
    at lag > 0 = theo leads the market by that many seconds."""
    out = {}
    rows = []
    for tk, g in theo.groupby("ticker"):
        b = bbo[bbo["ticker"] == tk]
        if len(g) < 50 or len(b) < 50:
            continue
        th = g.set_index("time")["prob_raw"].resample("1s").last().ffill()
        md = b.set_index("time")["mid"].resample("1s").last().ffill()
        j = pd.concat([th.rename("th"), md.rename("md")], axis=1).dropna()
        j = j[(j["md"] >= 0.1) & (j["md"] <= 0.9)]
        rows.append(j.diff().dropna())
    if not rows:
        return out
    for lag in lags:
        xs, ys = [], []
        for j in rows:
            x, y = j["th"], j["md"].shift(-lag)
            ok = x.notna() & y.notna()
            xs.append(x[ok]); ys.append(y[ok])
        x, y = pd.concat(xs), pd.concat(ys)
        out[lag] = float(np.corrcoef(x, y)[0, 1]) if len(x) > 10 else float("nan")
    return out


def fill_markouts(fills: pd.DataFrame, trades: pd.DataFrame, theo: pd.DataFrame, bbo: pd.DataFrame,
                  skew: pd.Series) -> pd.DataFrame:
    if fills.empty:
        return fills
    f = fills.copy().reset_index(drop=True)
    f["yes_px"] = np.where(f["side"] == "yes", f["price"], 1.0 - f["price"])
    f["sign"] = np.where(f["side"] == "yes", 1.0, -1.0)
    # Exchange time of the print that filled a maker quote: the last print on
    # the ticker at that yes price received at or before our fill.
    t = trades.copy()
    t["yes_px"] = t["yes_price"].round(4)
    f["t_exch"] = pd.Series(pd.NaT, index=f.index, dtype="datetime64[ns, UTC]")
    mk = f["liquidity"] == "maker"
    if mk.any():
        l = f.loc[mk, ["time", "ticker", "yes_px"]].copy()
        l["yes_px"] = l["yes_px"].round(4)
        r = t[["received", "time", "ticker", "yes_px"]].rename(columns={"time": "t_ex", "received": "time"})
        m = pd.merge_asof(l.reset_index().sort_values("time"), r.sort_values("time"), on="time",
                          by=["ticker", "yes_px"], direction="backward", tolerance=pd.Timedelta("3s"))
        f.loc[m["index"].values, "t_exch"] = pd.to_datetime(m["t_ex"], utc=True).values
    # Takes: decided on our clock; the engine resolves them SHADOW_SEND_S later.
    sk = skew.reindex(f["time"].dt.floor("h")).ffill().bfill().values
    f["skew_s"] = sk
    f["t_event"] = f["time"]
    has = f["t_exch"].notna()
    f["t_event"] = f["t_event"].where(~has, pd.to_datetime(f["t_exch"], utc=True)
                                      + pd.to_timedelta(f["skew_s"], unit="s"))
    f["notice_s"] = (f["time"] - f["t_event"]).dt.total_seconds()

    f["theo0"] = _asof(f["t_event"], theo, ["prob_raw"], left_by=f["ticker"])["prob_raw"].values
    f["mid0"] = _asof(f["t_event"], bbo, ["mid"], left_by=f["ticker"])["mid"].values
    f["tau"] = _asof(f["t_event"], theo, ["x_tau"], left_by=f["ticker"])["x_tau"].values
    f["edge0_c"] = 100 * f["sign"] * (f["theo0"] - f["yes_px"])
    for h in HORIZONS_S:
        tt = f["t_event"] + pd.Timedelta(seconds=h)
        th = _asof(tt, theo, ["prob_raw"], left_by=f["ticker"])["prob_raw"].values
        md = _asof(tt, bbo, ["mid"], left_by=f["ticker"])["mid"].values
        f[f"mo_theo_{h:g}"] = 100 * f["sign"] * (th - f["yes_px"])
        f[f"mo_mid_{h:g}"] = 100 * f["sign"] * (md - f["yes_px"])
        f[f"dtheo_{h:g}"] = 100 * f["sign"] * (th - f["theo0"])
    # Worst adverse theo move inside the pickoff window, from 50ms samples.
    worst = np.zeros(len(f))
    for k in np.linspace(0.05, PICKOFF_S, 40):
        th = _asof(f["t_event"] + pd.Timedelta(seconds=float(k)), theo, ["prob_raw"], left_by=f["ticker"])["prob_raw"].values
        worst = np.minimum(worst, np.nan_to_num(100 * f["sign"].values * (th - f["theo0"].values)))
    f["worst_dtheo_2s_c"] = worst
    f["picked_off"] = f["worst_dtheo_2s_c"] <= -100 * PICKOFF_C
    return f


def _fmt(df: pd.DataFrame) -> str:
    return "```\n" + df.to_string() + "\n```\n"


def summarize_fills(f: pd.DataFrame) -> pd.DataFrame:
    if f.empty:
        return pd.DataFrame()
    cols = {"n": ("size", "size"), "lots": ("size", "sum"),
            "edge0_c": ("edge0_c", "mean"),
            **{f"theo_{h:g}s": (f"mo_theo_{h:g}", "mean") for h in (0.5, 2.0, 10.0, 30.0)},
            **{f"mid_{h:g}s": (f"mo_mid_{h:g}", "mean") for h in (2.0, 10.0, 30.0)},
            "pickoff": ("picked_off", "mean"), "notice_s": ("notice_s", "median")}
    return f.groupby(["coin", "liquidity"]).agg(**cols).round(3)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--since", default=None, help="UTC timestamp; ignore data before it")
    ap.add_argument("--tag", default=None)
    a = ap.parse_args(argv)
    since = None if a.since is None else pd.Timestamp(a.since, tz="UTC")
    os.makedirs(OUT, exist_ok=True)
    tag = a.tag or pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M")
    lines = [f"# CRYPTO15M shadow eval {tag}", f"dates {a.dates}, since {since}", ""]
    all_fills, tvb, ll = [], {}, {}
    for s in SERIES:
        dec, fills, bbo, trades, theo = load(s, a.dates, since)
        if dec.empty:
            continue
        coin = s[2:-3]
        sk = skew_by_hour(trades)
        tvb[coin] = theo_vs_book(dec)
        ll[coin] = lead_lag(theo, bbo)
        fm = fill_markouts(fills, trades, theo, bbo, sk)
        if not fm.empty:
            fm["coin"] = coin
            all_fills.append(fm)
        blocks = dec["block_bid"].fillna("none").value_counts(normalize=True).round(3).head(6).to_dict()
        lines.append(f"- {coin}: decisions {len(dec)}, fills {len(fills)}, blocks {blocks}, "
                     f"clock skew local-exch {sk.min():.2f}..{sk.max():.2f}s")
    lines += ["", "## Theo vs Kalshi touch (time-weighted, mid in [0.10, 0.90])", ""]
    t = pd.DataFrame({c: {k: v for k, v in d.items() if k != "by_tau"} for c, d in tvb.items() if d}).T
    lines.append(_fmt(t.round(3)))
    for c, d in tvb.items():
        if d:
            lines += [f"{c} by seconds to close:", _fmt(d["by_tau"].round(3))]
    lines += ["## Lead-lag: corr(1s theo change at t, 1s Kalshi mid change at t+lag)", "",
              _fmt(pd.DataFrame(ll).T.round(3))]
    if all_fills:
        f = pd.concat(all_fills, ignore_index=True)
        f.to_csv(os.path.join(OUT, f"fills_{tag}.csv"), index=False)
        lines += ["## Fills: markouts (c/contract, + = good for us) and pickoff rate", "",
                  f"pickoff = theo moves >= {100 * PICKOFF_C:.0f}c against us within {PICKOFF_S:g}s of the "
                  "exchange-time fill. notice_s = how long after the trade at Kalshi our engine booked it.", "",
                  _fmt(summarize_fills(f))]
        mk = f[f["liquidity"] == "maker"]
        if not mk.empty:
            lines += ["### Maker fills: adverse theo move after the fill (c, signed; median and share <= -3c)", ""]
            rows = {}
            for h in HORIZONS_S:
                x = mk[f"dtheo_{h:g}"]
                rows[f"{h:g}s"] = {"median": x.median(), "mean": x.mean(), "share<=-3c": (x <= -3).mean(),
                                   "share<=-5c": (x <= -5).mean()}
            lines.append(_fmt(pd.DataFrame(rows).T.round(3)))
            mk = mk.assign(tau_b=pd.cut(mk["tau"], TAU_BINS))
            lines += ["### Maker pickoff by seconds to close", "",
                      _fmt(mk.groupby(["tau_b"], observed=True).agg(n=("size", "size"), pickoff=("picked_off", "mean"),
                                                                    mo_theo_10=("mo_theo_10", "mean"),
                                                                    mo_mid_10=("mo_mid_10", "mean")).round(3))]
            po = mk[mk["picked_off"]]
            lines += [f"### Picked-off maker fills: {len(po)} of {len(mk)}; their 30s theo markout "
                      f"{po['mo_theo_30'].mean():.2f}c vs {mk.loc[~mk['picked_off'], 'mo_theo_30'].mean():.2f}c for the rest", ""]
    path = os.path.join(OUT, f"report_{tag}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
