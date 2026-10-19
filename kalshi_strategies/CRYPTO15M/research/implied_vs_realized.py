"""
Is the vol backed out of the single-coin 15M mids a better vol for the Coin
Race than the HAR realized model? Two tests, from the live journals:

    python -m kalshi_strategies.CRYPTO15M.research.implied_vs_realized --since 2026-10-19T01:45 [--race]

A. Vol forecast, per coin: at each second with > 60s left, the forecast
   per-second variance -- implied (x_vol_bp, the EMA the coin model priced
   with) vs HAR (x_realized_vol_bp / vol_mult) -- against the variance the CF
   index then realized over the rest of the window (10s returns, every 1s
   offset). QLIKE (lower is better) and median |ln(forecast / realized)|.

B. Race fairs (--race): the race's own pricer on the same x (the coin models'
   x_x_bp: CF start, 30s mid->CF basis, identical to the race's) and the same
   HAR correlation, with per-coin rates from HAR vs implied. Scored against
   the settled race winner (log loss) and the race's Kalshi mids. Only
   seconds where all five coins have a row, > 60s left (the coin journals
   don't log the race's end-average term).
"""
from __future__ import annotations

import argparse
import glob
import math

import numpy as np
import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import load
from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import load_cf

COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
SER = {c: f"KX{c}15M" for c in COINS}
COLS = ["time", "ticker", "x_tau", "x_x_bp", "x_vol_bp", "x_realized_vol_bp", "market_bid", "market_ask"]


def _days(since: pd.Timestamp) -> list[str]:
    now = pd.Timestamp.now(tz="UTC")
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(since.floor("D"), now.floor("D"), freq="D")]


def coin_grid(coin: str, since: pd.Timestamp) -> pd.DataFrame:
    """1s grid of the coin model's x, implied vol and HAR vol, per event."""
    fr = []
    for day in _days(since):
        for p in sorted(glob.glob(f"data/live/{SER[coin]}/{day}/journal/decisions/part-*.parquet")):
            try:
                df = pd.read_parquet(p, columns=None)
            except Exception:
                continue
            if "x_vol_bp" not in df.columns or "x_realized_vol_bp" not in df.columns:
                continue
            df = df[df.time >= since]
            if len(df):
                fr.append(df[[c for c in COLS if c in df.columns]])
    if not fr:
        return pd.DataFrame()
    d = pd.concat(fr)
    out = []
    for tk, x in d.groupby("ticker"):
        j = x.set_index("time").select_dtypes("number").resample("1s").last().ffill(limit=3)
        j["ticker"] = tk
        out.append(j.reset_index())
    g = pd.concat(out).dropna(subset=["x_tau", "x_x_bp", "x_vol_bp", "x_realized_vol_bp"])
    g["coin"] = coin
    g["sec"] = (g.time.astype("int64") // 10**9).astype(int)
    g["T"] = (g.sec + g.x_tau.round()).astype(int)
    return g


def realized_rest(coin: str, g: pd.DataFrame) -> pd.Series:
    """Per row: CF per-second variance over [t, T) from 10s log returns at every offset."""
    lo, hi = int(g.sec.min()) - 20, int(g["T"].max()) + 1
    cf = load_cf(coin, lo, hi)
    s = pd.Series(cf).sort_index()
    s = s.reindex(range(lo, hi)).ffill()
    r2 = (np.log(s).diff(10) ** 2).to_numpy() / 10.0
    idx = {t: i for i, t in enumerate(range(lo, hi))}
    cs = np.nancumsum(np.nan_to_num(r2))
    cnt = np.cumsum(~np.isnan(r2))
    out = np.full(len(g), np.nan)
    for k, (t, T) in enumerate(zip(g.sec.to_numpy(), g["T"].to_numpy())):
        a, b = idx.get(int(t) + 10), idx.get(int(T) - 1)
        if a is None or b is None or b - a < 50:
            continue
        n = cnt[b] - cnt[a - 1]
        if n > 0:
            out[k] = (cs[b] - cs[a - 1]) / n
    return pd.Series(out, index=g.index)


def test_a(grids: dict[str, pd.DataFrame], vol_mult: dict[str, float]) -> pd.DataFrame:
    rows = {}
    for c, g in grids.items():
        g = g[g.x_tau > 60].copy()
        if g.empty:
            continue
        g["rv"] = realized_rest(c, g)
        g = g.dropna(subset=["rv"])
        g = g[g.rv > 0]
        res = {"seconds": len(g), "events": g.ticker.nunique(), "realized_bp": float(np.sqrt(g.rv.median()) * 1e4)}
        for name, col, mult in (("implied", "x_vol_bp", 1.0), ("har", "x_realized_vol_bp", vol_mult.get(c, 1.0))):
            f = (g[col] * 1e-4) ** 2 / mult
            q = g.rv / f
            res[f"{name}_bp"] = float(np.sqrt(f.median()) * 1e4)
            res[f"{name}_qlike"] = float((q - np.log(q) - 1).mean())
            res[f"{name}_mae_log"] = float(np.abs(np.log(f / g.rv)).median() / 2)   # in vol, not variance
            # Per event, so one long event doesn't dominate.
            res[f"{name}_qlike_ev"] = float(g.assign(l=q - np.log(q) - 1).groupby("ticker").l.mean().mean())
        rows[c] = res
    return pd.DataFrame(rows).T


def race_winners(events: list[str]) -> dict[str, set[str] | None]:
    out = {}
    for ev in events:
        try:
            ms = kalshi_get("/trade-api/v2/markets", {"event_ticker": ev, "limit": 50})["markets"]
        except Exception:
            out[ev] = None
            continue
        res = {m["ticker"].rsplit("-", 1)[-1]: m.get("result") for m in ms}
        out[ev] = {c for c, r in res.items() if r == "yes"} if all(r in ("yes", "no") for r in res.values()) else None
    return out


def test_b(grids: dict[str, pd.DataFrame], since: pd.Timestamp) -> pd.DataFrame:
    from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig
    from kalshi_strategies.KXCRYPTOLEAD15M.model import load_composite_bars
    from kalshi_strategies.KXCRYPTOLEAD15M.pricer import CoinRacePricer
    from kalshi_strategies.KXCRYPTOLEAD15M.robust_har import RobustHarForecaster

    cfg = load(CryptoConfig, CONFIG_PATH)
    # Race event = same S/T as the coin events: key on the close second.
    wide = None
    for c, g in grids.items():
        x = g[["sec", "T", "x_tau", "x_x_bp", "x_vol_bp"]].rename(columns={"x_x_bp": f"x_{c}", "x_vol_bp": f"iv_{c}"})
        x = x.drop_duplicates("sec").set_index("sec")
        wide = x if wide is None else wide.join(x[[f"x_{c}", f"iv_{c}"]], how="inner")
    if wide is None or wide.empty:
        return pd.DataFrame()
    wide = wide[wide.x_tau > 60].sort_index()
    t0, t1 = int(wide.index.min()), int(wide.index.max())
    # HAR forecaster replayed live-style over the persisted mids.
    fc = RobustHarForecaster()
    by_s: dict[int, dict] = {}
    for c in COINS:
        for b in load_composite_bars(c, t0 - cfg.pricing.warmup_s, t1 + 1):
            by_s.setdefault(int(b.ts_s), {})[c] = b.mid
    secs = sorted(by_s)

    class _Implied:
        version = None

        def __init__(self):
            self.rate, self.corr = None, None

        def rate_per_s(self, h, t=None):
            return self.rate

        def correlation(self, t=None):
            return self.corr

    shim = _Implied()
    p_har = CoinRacePricer(fc, cfg.pricing)
    p_iv = CoinRacePricer(shim, cfg.pricing)
    vm = cfg.pricing.vol_mult
    # Race Kalshi mids.
    rb = []
    for day in _days(since):
        for p in sorted(glob.glob(f"data/live/KXCRYPTOLEAD15M/{day}/journal/bbo/part-*.parquet")):
            try:
                df = pd.read_parquet(p, columns=["time", "ticker", "bid", "ask"])
            except Exception:
                continue
            df = df[df.time >= since]
            if len(df):
                rb.append(df)
    rb = pd.concat(rb).dropna() if rb else pd.DataFrame(columns=["time", "ticker", "bid", "ask"])
    rb = rb[rb.ask > rb.bid]
    rb["sec"] = (rb.time.astype("int64") // 10**9).astype(int)
    rb["coin"] = rb.ticker.str.rsplit("-", n=1).str[-1]
    rmid = rb.groupby(["sec", "coin"]).apply(lambda z: (z.bid.iloc[-1] + z.ask.iloc[-1]) / 2).unstack()
    rmid = rmid.reindex(range(t0, t1 + 1)).ffill(limit=3)

    def race_event_of(T: int) -> str:
        et = pd.Timestamp(T, unit="s", tz="UTC").tz_convert("America/New_York")
        return f"KXCRYPTOLEAD15M-{et.strftime('%y%b%d%H%M').upper()}"

    wide["event"] = [race_event_of(int(T)) for T in wide["T"]]
    win = race_winners(sorted(wide.event.unique()))
    rows = []
    j = 0
    for t, r in wide.iterrows():
        while j < len(secs) and secs[j] <= t:
            fc.update(float(secs[j]), by_s[secs[j]])
            j += 1
        x = np.array([r[f"x_{c}"] for c in COINS]) * 1e-4
        tau = float(r.x_tau)
        T = float(r["T"])
        S = T - 900.0
        a = p_har.fairs_at(x, np.zeros(5), tau, now_s=float(t), S=S, T=T, start_locked=True, compute_delta=False)
        shim.rate = (np.array([r[f"iv_{c}"] for c in COINS]) * 1e-4) ** 2 / vm
        shim.corr = fc.correlation()
        b = p_iv.fairs_at(x, np.zeros(5), tau, now_s=float(t), S=S, T=T, start_locked=True, compute_delta=False)
        m = rmid.loc[t] if t in rmid.index else None
        w = win.get(r.event)
        for i, c in enumerate(COINS):
            km = None if m is None else m.get(c)
            rows.append({"event": r.event, "sec": t, "tau": tau, "coin": c, "har": a.probs[i], "iv": b.probs[i],
                         "kmid": km, "y": None if w is None else float(c in w)})
    d = pd.DataFrame(rows)
    out = {}
    for name in ("har", "iv"):
        p = d[name].clip(1e-4, 1 - 1e-4)
        s = d.dropna(subset=["y"])
        ps = s[name].clip(1e-4, 1 - 1e-4)
        k = d.dropna(subset=["kmid"])
        k = k[(k.kmid > 0.03) & (k.kmid < 0.97)]
        out[name] = {"log_loss": float(-(s.y * np.log(ps) + (1 - s.y) * np.log(1 - ps)).mean()) if len(s) else np.nan,
                     "brier": float(((ps - s.y) ** 2).mean()) if len(s) else np.nan,
                     "abs_gap_to_kalshi_c": float(100 * (k[name] - k.kmid).abs().mean()) if len(k) else np.nan,
                     "gap_to_kalshi_c": float(100 * (k[name] - k.kmid).mean()) if len(k) else np.nan}
    k = d.dropna(subset=["kmid"])
    k = k[(k.kmid > 0.03) & (k.kmid < 0.97)]
    s = d.dropna(subset=["y"])
    km = s.dropna(subset=["kmid"])
    pk = km.kmid.clip(1e-4, 1 - 1e-4)
    out["kalshi_mid"] = {"log_loss": float(-(km.y * np.log(pk) + (1 - km.y) * np.log(1 - pk)).mean()) if len(km) else np.nan,
                         "brier": float(((pk - km.y) ** 2).mean()) if len(km) else np.nan}
    res = pd.DataFrame(out).T
    res.attrs["n"] = (len(d) // 5, d.event.nunique(), int(s.event.nunique()) if len(s) else 0)
    # Per event (log loss), the sign of iv - har.
    if len(s):
        pe = s.assign(llh=-(s.y * np.log(s.har.clip(1e-4, 1)) + (1 - s.y) * np.log((1 - s.har).clip(1e-4, 1))),
                      lli=-(s.y * np.log(s.iv.clip(1e-4, 1)) + (1 - s.y) * np.log((1 - s.iv).clip(1e-4, 1))))
        ev = pe.groupby("event")[["llh", "lli"]].mean()
        res.attrs["per_event"] = ev.assign(diff=ev.lli - ev.llh)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--race", action="store_true")
    a = ap.parse_args(argv)
    since = pd.Timestamp(a.since, tz="UTC")
    from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH as CP, UpDownConfig
    vm = {c: load(UpDownConfig, CP, SER[c]).pricing.vol_mult for c in COINS}
    grids = {c: g for c in COINS if not (g := coin_grid(c, since)).empty}
    print(f"coins with data since {since}: {sorted(grids)}; vol_mult {vm}")
    pd.set_option("display.width", 220)
    print("\nA. vol forecast vs CF realized over the rest of the window (lower QLIKE / mae_log is better)")
    print(test_a(grids, vm).round(4).to_string())
    if a.race:
        if set(grids) != set(COINS):
            print("\nB. skipped: needs all five coins")
            return
        res = test_b(grids, since)
        n = res.attrs.get("n")
        print(f"\nB. race fairs, HAR vs implied rates ({n[0]} seconds, {n[1]} events, {n[2]} settled)")
        print(res.round(4).to_string())
        if "per_event" in res.attrs:
            pe = res.attrs["per_event"]
            print(f"per event: implied better on {(pe['diff'] < 0).sum()}/{len(pe)}; mean diff {pe['diff'].mean():+.4f}")
            print(pe.round(4).to_string())


if __name__ == "__main__":
    main()
