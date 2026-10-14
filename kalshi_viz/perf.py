"""Strategy performance from the day journals: every real fill, its markout
vs Kalshi mid, and settled PnL.

Incremental. Journal parts are immutable, so a refresh reads only new fills
parts and the bbo parts around fills that just became markable, a half hour
of book at a time and only for the traded tickers. A full day of bbo is ~3M
rows; reading it whole on the live box (3.8 GB, shared with trading) is what
OOM-killed the review queries on 2026-10-12.

    python -m kalshi_viz.perf --series KXCRYPTOLEAD15M      # print the metrics
"""
from __future__ import annotations

import argparse
import functools
import glob
import json
import math
import os
import re
import threading
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from kalshi_infra.runtime.persist import LIVE_ROOT

# Markout horizons, seconds after the fill. "mo0" is the mid 1ms BEFORE the
# fill: the spread we captured.
HORIZONS = {"mo0": -0.001, "mo1": 1.0, "mo10": 10.0, "mo60": 60.0}
MAX_H = max(HORIZONS.values())
# bbo only records touch changes, so the mid in force at a fill can be this
# old in a quiet market.
LOOKBACK = pd.Timedelta("10min")
CHUNK = pd.Timedelta("30min")
# Crypto results were final ~3 min after close (2026-10-11).
SETTLE_WAIT = pd.Timedelta("4min")
SETTLE_RETRY_S = 60
SETTLE_PER_REFRESH = 25
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILL_KEY = ["exchange_order_id", "trade_id", "time", "price", "size"]


@functools.lru_cache(maxsize=4096)
def event_close(ticker: str) -> pd.Timestamp | None:
    """Close from the ticker, which is stamped in New York time:
    KXCRYPTOLEAD15M-26OCT120300-BTC, KXINX-26OCT09H1600-B7662."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    try:
        t = pd.to_datetime(parts[1].replace("H", ""), format="%y%b%d%H%M")
    except ValueError:
        return None
    return pd.Timestamp(t).tz_localize("America/New_York").tz_convert("UTC")


def _days(series: str) -> list[str]:
    root = os.path.join(LIVE_ROOT, series)
    return sorted(d for d in os.listdir(root) if _DAY.match(d)) if os.path.isdir(root) else []


def _time_range(path: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """From the parquet row-group stats; falls back to reading the column.
    None for a part that can't be read yet (the newest can be mid-write)."""
    try:
        md = pq.ParquetFile(path).metadata
        i = md.schema.to_arrow_schema().get_field_index("time")
        lo = hi = None
        for g in range(md.num_row_groups):
            s = md.row_group(g).column(i).statistics
            if s is None or not s.has_min_max:
                raise ValueError("no stats")
            lo = s.min if lo is None else min(lo, s.min)
            hi = s.max if hi is None else max(hi, s.max)
        if lo is None:
            return None
        lo, hi = pd.Timestamp(lo), pd.Timestamp(hi)
    except Exception:
        try:
            t = pq.read_table(path, columns=["time"]).column("time").to_pandas()
        except Exception:
            return None
        if t.empty:
            return None
        lo, hi = pd.Timestamp(t.min()), pd.Timestamp(t.max())
    if lo.tzinfo is None:
        lo, hi = lo.tz_localize("UTC"), hi.tz_localize("UTC")
    return lo, hi


class Perf:
    """Per-series accumulator. `refresh()` is safe to call from several
    Streamlit sessions at once."""

    def __init__(self, series: str):
        self.series = series
        self.root = os.path.join(LIVE_ROOT, series)
        self._lock = threading.Lock()
        self._fill_parts: set[str] = set()
        self._keys: set[tuple] = set()
        self._pending: list[pd.DataFrame] = []
        self._done = pd.DataFrame()
        self._bbo: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
        self._last_mid: dict[str, float] = {}
        self._settle_path = os.path.join(self.root, "settlements.json")
        self._settle: dict[str, str] = self._load_settlements()
        self._settle_tried: dict[str, float] = {}
        # Background refresh for the dashboard: the first pass over a day of
        # journals takes ~20-40s, too long to block a page render.
        self.latest: pd.DataFrame | None = None
        self.updated: pd.Timestamp | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._bg_lock = threading.Lock()

    def ensure_background(self, every_s: float = 30.0) -> None:
        with self._bg_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, args=(every_s,), daemon=True,
                                                name=f"perf-{self.series}")
                self._thread.start()

    def _loop(self, every_s: float) -> None:
        while True:
            try:
                self.latest, self.updated, self.error = self.refresh(), pd.Timestamp.now(tz="UTC"), None
            except Exception as e:
                self.error = repr(e)
            time.sleep(every_s)

    # -- settlement ---------------------------------------------------------

    def _load_settlements(self) -> dict[str, str]:
        try:
            with open(self._settle_path) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _update_settlements(self, tickers, limit: int | None = SETTLE_PER_REFRESH) -> None:
        from kalshi_infra.api_functions import kalshi_get
        now_t, now_m = pd.Timestamp.now(tz="UTC"), time.monotonic()
        due = [t for t in tickers if t not in self._settle and (c := event_close(t)) is not None
               and c + SETTLE_WAIT < now_t and now_m - self._settle_tried.get(t, -1e18) > SETTLE_RETRY_S]
        changed = False
        for t in due[:limit]:
            self._settle_tried[t] = now_m
            try:
                m = kalshi_get(f"/trade-api/v2/markets/{t}", max_retries=2).get("market", {})
            except Exception:
                continue
            if m.get("status") in ("finalized", "settled", "determined"):
                # "" marks a market that settled without a yes/no result, so it isn't re-asked.
                self._settle[t] = m.get("result") if m.get("result") in ("yes", "no") else ""
                changed = True
        if changed:
            try:
                tmp = self._settle_path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(self._settle, fh)
                os.replace(tmp, self._settle_path)
            except OSError:
                pass          # read-only: kept in memory, re-fetched after a restart

    # -- journal ------------------------------------------------------------

    def _scan(self) -> None:
        for d in _days(self.series):
            j = os.path.join(self.root, d, "journal")
            for p in sorted(glob.glob(os.path.join(j, "bbo", "part-*.parquet"))):
                if p not in self._bbo:
                    r = _time_range(p)
                    if r is not None:
                        self._bbo[p] = r
            for p in sorted(glob.glob(os.path.join(j, "fills", "part-*.parquet"))):
                if p in self._fill_parts:
                    continue
                try:
                    f = pd.read_parquet(p)
                except Exception:
                    continue          # mid-write; next refresh
                self._fill_parts.add(p)
                if f.empty or "mode" not in f.columns:
                    continue
                seeded = f["seeded"].fillna(False).astype(bool) if "seeded" in f.columns else False
                f = f[(f["mode"] == "real") & ~seeded]
                if f.empty:
                    continue
                for c in _FILL_KEY:
                    if c not in f.columns:
                        f[c] = None
                keys = list(f[_FILL_KEY].astype(str).itertuples(index=False, name=None))
                new = [k not in self._keys for k in keys]
                self._keys.update(keys)
                f = f[new]
                if len(f):
                    self._pending.append(f[["time", "ticker", "side", "price", "size", "fee", "liquidity"]])

    def _book(self, lo, hi, tickers) -> pd.DataFrame:
        parts = [p for p, (a, b) in self._bbo.items() if b >= lo and a <= hi]
        out = []
        for p in parts:
            try:
                x = pq.read_table(p, columns=["time", "ticker", "bid", "ask"],
                                  filters=[("ticker", "in", list(tickers))]).to_pandas()
            except Exception:
                continue
            if len(x):
                out.append(x)
        out = [x.dropna(axis=1, how="all") for x in out]
        if not out:
            return pd.DataFrame(columns=["time", "ticker", "mid"])
        b = pd.concat(out, ignore_index=True)
        for c in ("bid", "ask"):
            if c not in b:
                b[c] = np.nan
        b = b[(b.time >= lo) & (b.time <= hi)].sort_values("time")
        b["mid"] = (b.bid + b.ask) / 2
        return b[["time", "ticker", "mid"]]

    def _mark(self, f: pd.DataFrame) -> pd.DataFrame:
        f = f.sort_values("time").reset_index(drop=True)
        f["yes_px"] = np.where(f.side == "yes", f.price, 1.0 - f.price)
        f["dir"] = np.where(f.side == "yes", 1.0, -1.0)
        b = self._book(f.time.min() - LOOKBACK, f.time.max() + pd.Timedelta(seconds=MAX_H + 1),
                       set(f.ticker))
        for name, h in HORIZONS.items():
            q = f[["time", "ticker"]].copy()
            q["time"] = q.time + pd.Timedelta(seconds=h)
            m = pd.merge_asof(q.reset_index().sort_values("time"), b, on="time", by="ticker",
                              direction="backward").set_index("index").sort_index()
            f[name] = (f.dir * (m.mid - f.yes_px) - f.fee / f["size"]) * 100
        if len(b):
            self._last_mid.update(b.groupby("ticker").mid.last().dropna().to_dict())
        return f

    def refresh(self, settle_all: bool = False) -> pd.DataFrame:
        """Every real fill so far, one row each: time, ticker, event, close,
        side, liquidity, size, yes_px, fee, mo0/mo1/mo10/mo60 and settled
        (cents per contract, fee included), and pnl_c (settled, else marked
        at the latest mid). Settlements are fetched SETTLE_PER_REFRESH at a
        time unless `settle_all`."""
        with self._lock:
            self._scan()
            newest = max((b for _, b in self._bbo.values()), default=None)
            if self._pending and newest is not None:
                p = pd.concat(self._pending, ignore_index=True)
                ready = p.time + pd.Timedelta(seconds=MAX_H + 1) <= newest
                done = []
                for _, g in p[ready].groupby(p[ready].time.dt.floor(CHUNK)):
                    done.append(self._mark(g))
                if done:
                    self._done = pd.concat([self._done] + done, ignore_index=True)
                self._pending = [p[~ready]] if (~ready).any() else []
            if newest is not None:
                recent = [pp for pp, (_, b) in self._bbo.items() if b >= newest - pd.Timedelta("2min")]
                for pp in recent:
                    try:
                        x = pq.read_table(pp, columns=["time", "ticker", "bid", "ask"]).to_pandas()
                    except Exception:
                        continue
                    x = x.sort_values("time")
                    self._last_mid.update(((x.bid + x.ask) / 2).groupby(x.ticker).last().dropna().to_dict())
            d = self._done
            if d.empty:
                return d
            self._update_settlements(sorted(d.ticker.unique()), None if settle_all else SETTLE_PER_REFRESH)
            d = d.copy()
            d["event"] = d.ticker.str.rsplit("-", n=1).str[0]
            d["close"] = d.ticker.map(event_close)
            pay = d.ticker.map(self._settle).map({"yes": 1.0, "no": 0.0})
            d["settled"] = (d.dir * (pay - d.yes_px) - d.fee / d["size"]) * 100
            last = d.ticker.map(self._last_mid)
            d["open_c"] = (d.dir * (last - d.yes_px) - d.fee / d["size"]) * 100
            d["pnl_c"] = d.settled.where(d.settled.notna(), d.open_c)
            return d


_STORE: dict[str, Perf] = {}
_STORE_LOCK = threading.Lock()


def get(series: str) -> Perf:
    with _STORE_LOCK:
        if series not in _STORE:
            _STORE[series] = Perf(series)
        return _STORE[series]


def window(df: pd.DataFrame, since: pd.Timestamp | None) -> pd.DataFrame:
    return df if since is None or df.empty else df[df.time >= since]


def _wavg(x: pd.Series, w: pd.Series) -> float:
    ok = x.notna()
    return float(np.average(x[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else math.nan


def summarize(df: pd.DataFrame, since: pd.Timestamp | None = None) -> dict:
    """Window metrics. $ figures are sums; markouts are cents per contract."""
    now = pd.Timestamp.now(tz="UTC")
    d = window(df, since)
    if d.empty:
        return {"fills": 0}
    usd = d.pnl_c * d["size"] / 100
    ev = d.assign(usd=usd).groupby("event").usd.sum()
    cum = usd.fillna(0).cumsum()
    start = since if since is not None else d.time.min()
    hours = max((now - start).total_seconds() / 3600, 1e-9)
    out = {
        "fills": len(d), "taker": int((d.liquidity == "taker").sum()), "contracts": float(d["size"].sum()),
        "pnl": float(usd.sum()), "per_h": float(usd.sum() / hours), "hours": hours,
        "settled_usd": float((d.settled * d["size"] / 100).sum()),
        "open_usd": float((d.open_c * d["size"] / 100).where(d.settled.isna()).sum()),
        "unsettled": int(d.settled.isna().sum()), "fees": float(d.fee.sum()),
        "events": len(ev), "won": float((ev > 0).mean()) if len(ev) else math.nan,
        "t_event": float(ev.mean() / (ev.std(ddof=1) / math.sqrt(len(ev)))) if len(ev) > 2 and ev.std() > 0 else math.nan,
        "max_dd": float((cum - cum.cummax()).min()) if len(cum) else 0.0,
    }
    rows = {}
    for lab, g in (("all", d), ("maker", d[d.liquidity == "maker"]), ("taker", d[d.liquidity == "taker"])):
        if len(g):
            rows[lab] = {"fills": len(g), **{c: _wavg(g[c], g["size"]) for c in (*HORIZONS, "pnl_c")}}
    out["markouts"] = rows
    return out


def curves(df: pd.DataFrame, since: pd.Timestamp | None = None) -> pd.DataFrame:
    """Long frame (time, line, usd): cumulative PnL and 1s/10s markout $ by fill time."""
    d = window(df, since)
    # A series with no real fills has a column-less empty frame.
    if d.empty:
        return pd.DataFrame(columns=["time", "line", "usd"])
    d = d.sort_values("time")
    start = since if since is not None else d.time.min()
    lines = {"PnL (settled; open at mid)": d.pnl_c, "1s markout": d.mo1, "10s markout": d.mo10}
    out = []
    for name, c in lines.items():
        v = (c.fillna(0) * d["size"] / 100).cumsum()
        out.append(pd.DataFrame({"time": [start] + list(d.time), "line": name, "usd": [0.0] + list(v)}))
    return pd.concat(out, ignore_index=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXCRYPTOLEAD15M")
    args = ap.parse_args()
    t0 = time.time()
    df = get(args.series).refresh(settle_all=True)
    print(f"{len(df)} real fills marked in {time.time() - t0:.1f}s")
    now = pd.Timestamp.now(tz="UTC")
    for lab, since in (("1h", now - pd.Timedelta(hours=1)), ("6h", now - pd.Timedelta(hours=6)),
                       ("24h", now - pd.Timedelta(hours=24)), ("all", None)):
        s = summarize(df, since)
        if not s.get("fills"):
            print(f"\n{lab}: no fills")
            continue
        print(f"\n{lab}: PnL ${s['pnl']:+.2f} (${s['per_h']:+.2f}/h; settled ${s['settled_usd']:+.2f}, "
              f"open ${s['open_usd']:+.2f} on {s['unsettled']} fills), {s['events']} events "
              f"({s['won']:.0%} won, t={s['t_event']:+.2f}), {s['fills']} fills ({s['taker']} taker), "
              f"{s['contracts']:.0f} contracts, fees ${s['fees']:.2f}, max drawdown ${s['max_dd']:.2f}")
        print(pd.DataFrame(s["markouts"]).T.round(2).to_string())


if __name__ == "__main__":
    main()
