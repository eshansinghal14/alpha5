"""
Does our theo know anything the Kalshi mid doesn't? And where does the gap
between them come from?

    python -m kalshi_strategies.CRYPTO15M.research.theo_vs_market --date 2026-10-18 \
        --start 2026-10-18T09:00 --end 2026-10-18T18:20

On a 1s grid per market (theo and Kalshi mid both on our clock):
  1. slope of the future mid change (h seconds) on gap = theo - mid
       ~1: the market moves to us (our deviations are information)
       ~0: it doesn't (they are noise; anchor to the mid)
  2. blend weight w in p = mid + w * gap that best predicts settlement (Brier)
  3. where the gap comes from: the gap as a spot error in bp (our mu minus
     the mu that would price at the mid), split into a per-event constant
     (strike / level offset) and what moves within the event; how much of it
     tracks the last 2s of spot (latency); and the error in the final return
     we priced off, against Kalshi's published start and end values.
SEs are clustered by event (markets settle once per event).
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd
from scipy import special

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.runtime.journal import read_journal
from kalshi_strategies.KXCRYPTOLEAD15M.model import load_composite_bars

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
NU = 5.0
HORIZONS = (5, 10, 30, 60)
TAU_BINS = [60, 180, 420, 900]
FFILL_S = 30


def markets(series, lo, hi):
    r = kalshi_get("/trade-api/v2/markets", {"series_ticker": series, "min_close_ts": int(lo.timestamp()),
                                             "max_close_ts": int(hi.timestamp()) + 900, "limit": 1000})
    df = pd.DataFrame(r["markets"])
    df["close"] = pd.to_datetime(df["close_time"], utc=True)
    df["y"] = df["result"].map({"yes": 1.0, "no": 0.0})
    df["exp_value"] = pd.to_numeric(df["expiration_value"], errors="coerce")
    return df[["ticker", "close", "y", "floor_strike", "exp_value"]]


def grid(series, date, lo, hi):
    d = read_journal(series, date, "decisions", columns=["time", "ticker", "prob_raw", "x_mu_bp", "x_sd_bp",
                                                         "x_spot_mid", "x_anchor", "x_anchor_src"])
    b = read_journal(series, date, "bbo", columns=["time", "ticker", "bid", "ask"])
    d, b = d[(d.time >= lo) & (d.time < hi)], b[(b.time >= lo) & (b.time < hi)]
    out = []
    for tk, g in d.groupby("ticker"):
        g = g.set_index("time").sort_index()
        g["valid"] = g["prob_raw"].notna()
        num = g[["valid", "prob_raw", "x_mu_bp", "x_sd_bp", "x_spot_mid", "x_anchor"]].resample("1s").last()
        num = num.ffill(limit=FFILL_S)
        num = num[num["valid"].fillna(False).astype(bool)]
        src = g["x_anchor_src"].resample("1s").last().ffill(limit=FFILL_S)
        bb = b[b.ticker == tk].set_index("time").sort_index()[["bid", "ask"]].resample("1s").last()
        bb = bb.ffill(limit=FFILL_S)
        j = num.join(bb, how="inner").join(src)
        j["ticker"] = tk
        out.append(j.reset_index())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def cluster_slope(x, y, cl, intercept=True):
    """OLS slope of y on x with an event-clustered SE."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    xd = x - x.mean() if intercept else x
    b = float(np.sum(xd * (y - (y.mean() if intercept else 0))) / np.sum(xd * xd))
    e = y - (y.mean() - b * x.mean() if intercept else 0) - b * x
    s = pd.Series(xd * e).groupby(np.asarray(cl)).sum()
    return b, float(math.sqrt(np.sum(s ** 2)) / np.sum(xd * xd))


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def implied_mu_bp(p, sd_bp):
    p = np.clip(p, 0.03, 0.97)
    return sd_bp * special.stdtrit(NU, p) / math.sqrt(NU / (NU - 2.0))


def logistic_slope(x, y, iters=30):
    """(a, b) of P(y=1) = sigmoid(a + b x), by Newton."""
    X = np.column_stack([np.ones_like(x), x])
    w = np.zeros(2)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p))[:, None]) + 1e-9 * np.eye(2)
        w = w + np.linalg.solve(H, X.T @ (y - p))
    return w


def load(date, lo, hi):
    frames, mkts = [], []
    for s in SERIES:
        g = grid(s, date, lo, hi)
        if g.empty:
            continue
        m = markets(s, lo, hi)
        g = g.merge(m, on="ticker", how="inner")
        g["coin"] = s[2:-3]
        frames.append(g)
        m["coin"] = s[2:-3]
        mkts.append(m)
    g = pd.concat(frames, ignore_index=True)
    g["mid"] = (g["bid"] + g["ask"]) / 2
    g["spread"] = g["ask"] - g["bid"]
    g["tau"] = (g["close"] - g["time"]).dt.total_seconds()
    g = g.sort_values(["ticker", "time"]).reset_index(drop=True)
    by = g.groupby("ticker")
    for h in HORIZONS:
        fut = by["mid"].shift(-h)
        ok = by["time"].shift(-h) == g["time"] + pd.Timedelta(seconds=h)
        g[f"dmid_{h}"] = (fut - g["mid"]).where(ok)
    past = by["x_spot_mid"].shift(2)
    ok = by["time"].shift(2) == g["time"] - pd.Timedelta(seconds=2)
    g["spot_ret2_bp"] = (np.log(g["x_spot_mid"] / past) * 1e4).where(ok)
    g["gap"] = g["prob_raw"] - g["mid"]
    g["gap_bp"] = g["x_mu_bp"] - implied_mu_bp(g["mid"].to_numpy(), g["x_sd_bp"].to_numpy())
    return g, pd.concat(mkts, ignore_index=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    a = ap.parse_args(argv)
    lo, hi = pd.Timestamp(a.start, tz="UTC"), pd.Timestamp(a.end, tz="UTC")
    g, mk = load(a.date, lo, hi)
    base = g[(g.mid >= 0.10) & (g.mid <= 0.90) & (g.spread <= 0.03) & (g.tau > 60)]
    print(f"{len(base):,} market-seconds, {base.ticker.nunique()} events, {lo} .. {hi}\n")

    print("1. future mid change on gap = theo - mid  (slope, clustered SE)")
    rows = {}
    for coin, sub in [("ALL", base), *base.groupby("coin")]:
        r = {}
        for h in HORIZONS:
            s = sub.dropna(subset=[f"dmid_{h}"])
            s = s[s.tau - h > 15]
            b, se = cluster_slope(s.gap, s[f"dmid_{h}"], s.ticker)
            r[f"{h}s"] = f"{b:+.2f} ({se:.2f})"
        rows[coin] = r
    print(pd.DataFrame(rows).T.to_string(), "\n")
    rows = {}
    for lo_t, hi_t in zip(TAU_BINS[:-1], TAU_BINS[1:]):
        s = base[(base.tau > lo_t) & (base.tau <= hi_t)].dropna(subset=["dmid_30"])
        b, se = cluster_slope(s.gap, s["dmid_30"], s.ticker)
        rows[f"tau ({lo_t},{hi_t}]"] = {"slope_30s": f"{b:+.2f} ({se:.2f})", "n": len(s)}
    print(pd.DataFrame(rows).T.to_string(), "\n")

    print("2. settlement: blend weight w (p = mid + w*gap), Brier / log loss x1000")
    st = g[(g.mid >= 0.10) & (g.mid <= 0.90) & (g.spread <= 0.03) & g.y.notna()]
    rows = {}
    for name, s in [("ALL tau>60", st[st.tau > 60]), ("last minute", st[st.tau <= 60]),
                    *[(f"{c} tau>60", x[x.tau > 60]) for c, x in st.groupby("coin")]]:
        w, se = cluster_slope(s.gap, s.y - s.mid, s.ticker, intercept=False)

        def brier(p):
            return 1000 * float(np.mean((s.y - p) ** 2))

        def ll(p):
            p = np.clip(p, 1e-3, 1 - 1e-3)
            return -1000 * float(np.mean(s.y * np.log(p) + (1 - s.y) * np.log(1 - p)))

        rows[name] = {"w": f"{w:+.2f} ({se:.2f})", "brier_mid": brier(s.mid), "brier_theo": brier(s.prob_raw),
                      "ll_mid": ll(s.mid), "ll_theo": ll(s.prob_raw), "events": s.ticker.nunique()}
    print(pd.DataFrame(rows).T.round(1).to_string(), "\n")

    print("3. where the gap comes from")
    s = base.dropna(subset=["gap_bp"])
    s = s[s.gap_bp.abs() < 50]
    ev = s.groupby("ticker")["gap_bp"]
    between = float(np.average((ev.transform("mean") - s.gap_bp.mean()) ** 2))
    total = float(s.gap_bp.var())
    rows = {}
    for coin, x in [("ALL", s), *s.groupby("coin")]:
        e = x.groupby("ticker")["gap_bp"]
        tot = float(x.gap_bp.var())
        btw = float(np.mean((e.transform("mean") - x.gap_bp.mean()) ** 2))
        l = x.dropna(subset=["spot_ret2_bp"])
        c = float(np.corrcoef(l.gap_bp, l.spot_ret2_bp)[0, 1])
        rows[coin] = {"gap_bp_mean": x.gap_bp.mean(), "gap_bp_sd": math.sqrt(tot),
                      "per_event_share": btw / tot, "corr_spot_last2s": c,
                      "theo_more_extreme": float(np.mean(np.abs(x.prob_raw - .5) > np.abs(x.mid - .5))),
                      "sd_bp_median": x.x_sd_bp.median()}
    print("gap as spot error (bp): our mu - mu implied by the Kalshi mid")
    print(pd.DataFrame(rows).T.round(3).to_string(), "\n")
    print(f"(overall per-event share {between / total:.2f}: that part is a constant level error for the "
          f"whole event -- strike / mid-vs-index offset)\n")
    rows = {}
    for coin, x in [("ALL", s), *s.groupby("coin")]:
        rows[coin] = {"logit_mid_on_logit_theo": cluster_slope(logit(x.prob_raw), logit(x.mid), x.ticker)[0]}
        y = x.dropna(subset=["y"])
        if y.ticker.nunique() >= 10:
            rows[coin]["outcome_slope_theo"] = logistic_slope(logit(y.prob_raw.to_numpy()), y.y.to_numpy())[1]
            rows[coin]["outcome_slope_mid"] = logistic_slope(logit(y.mid.to_numpy()), y.y.to_numpy())[1]
    print("extremity (1 = calibrated; <1 = too extreme / vol too low, >1 = too timid / vol too high)")
    print(pd.DataFrame(rows).T.round(2).to_string(), "\n")

    rows = []
    for coin, x in g.groupby("coin"):
        for tk, e in x.groupby("ticker"):
            m = mk[mk.ticker == tk].iloc[0]
            anc = e.x_anchor.dropna()
            if anc.empty or not (m.exp_value > 0) or not (m.floor_strike > 0):
                continue
            T = m.close.timestamp()
            bars = load_composite_bars(coin, T - 60, T - 1)
            if len(bars) < 50:
                continue
            end_ours = float(np.mean([b.mid for b in bars]))
            start_off = math.log(anc.iloc[-1] / m.floor_strike) * 1e4
            end_off = math.log(end_ours / m.exp_value) * 1e4
            rows.append({"coin": coin, "start_off_bp": start_off, "end_off_bp": end_off,
                         "ret_err_bp": end_off - start_off, "src": e.x_anchor_src.dropna().iloc[-1]})
    r = pd.DataFrame(rows)
    if not r.empty:
        print("final-return error vs Kalshi's published start/end (bp): our end avg vs index, minus the start "
              "offset we priced off")
        agg = r.groupby("coin").agg(n=("ret_err_bp", "size"), start_off=("start_off_bp", "mean"),
                                    end_off=("end_off_bp", "mean"), err_mean=("ret_err_bp", "mean"),
                                    err_rms=("ret_err_bp", lambda v: math.sqrt(np.mean(v ** 2))))
        sd = base.groupby("coin")["x_sd_bp"].median()
        # dP of an ATM fair for a return error of err_rms at the median sd.
        agg["atm_cents_at_median_sd"] = 100 * agg.err_rms / sd.reindex(agg.index) * 0.375
        print(agg.round(2).to_string())
        print(r.groupby("src").ret_err_bp.agg(["size", "mean", lambda v: math.sqrt(np.mean(v ** 2))]).round(2))


if __name__ == "__main__":
    main()
