"""1-minute YES bid/ask history for every basket leg, cached per ticker.

Kalshi's batch candlestick endpoint (GET /markets/candlesticks) takes up to
100 tickers and refuses a request over 10,000 candles, counted as tickers x
minutes requested whether or not they exist (2 days x 10 tickers at 1 min
was refused at 28,800, 2026-10-24). It only returns minutes where the book
or a trade changed something, so the grid is forward-filled. Public
endpoint, no auth.
"""
from __future__ import annotations

import dataclasses
import os
import time

import numpy as np
import pandas as pd
import requests

from kalshi_infra import DATA_ROOT
from kalshi_infra.api_functions import BASE_URL

CACHE_DIR = os.path.join(DATA_ROOT, "arbs_single_leg", "candles")
PATH = "/trade-api/v2/markets/candlesticks"
MAX_CANDLES = 10_000
MAX_TICKERS = 100
COLS = ["ts", "bid", "ask", "bid_high", "ask_low", "volume"]
# Public reads share the IP's read bucket with the live strategies' REST
# backstops; this pace kept a 2,000-request fill free of 429s.
REQUEST_PACING_S = 0.15


def _get(params: dict, retries: int = 8) -> dict:
    for i in range(retries):
        r = requests.get(BASE_URL + PATH, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(min(2 ** i, 30))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def _rows(candles: list[dict]) -> pd.DataFrame:
    out = []
    for c in candles:
        b, a = c.get("yes_bid") or {}, c.get("yes_ask") or {}
        bid, ask = b.get("close_dollars"), a.get("close_dollars")
        if bid is None or ask is None:
            continue
        out.append((int(c["end_period_ts"]), float(bid), float(ask),
                    float(b.get("high_dollars") or bid), float(a.get("low_dollars") or ask),
                    float(c.get("volume_fp") or 0.0)))
    return pd.DataFrame(out, columns=COLS)


def _path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker}.parquet")


def _meta_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker}.fetched")


def load_cached(ticker: str) -> pd.DataFrame:
    p = _path(ticker)
    return pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame(columns=COLS)


def fetched_until(ticker: str) -> int | None:
    """Last minute already asked for (a quiet market has no candle there)."""
    try:
        with open(_meta_path(ticker)) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def fetch(tickers: list[str], start_ts: int, end_ts: int, log=print, opened: dict[str, int] | None = None) -> int:
    """Fill the cache for `tickers` over [start_ts, end_ts], fetching only
    what's past each ticker's last fetched minute. `opened`: market open
    time per ticker, so nothing before it is requested. Returns requests made."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    since = {}
    for t in tickers:
        f = fetched_until(t)
        s = start_ts if f is None or f < start_ts else f + 60
        if opened and opened.get(t):
            s = max(s, opened[t] - opened[t] % 60)
        since[t] = s
    todo = sorted((t for t in tickers if since[t] < end_ts), key=lambda t: since[t])
    parts: dict[str, list[pd.DataFrame]] = {t: [] for t in todo}
    n_req = 0
    while todo:
        # Tickers with the same start share a request, sized to fit the candle cap.
        s0 = since[todo[0]]
        batch = [t for t in todo if since[t] == s0][:MAX_TICKERS]
        minutes = max(1, MAX_CANDLES // len(batch) - 1)
        s1 = min(end_ts, s0 + minutes * 60)
        data = _get({"market_tickers": ",".join(batch), "start_ts": s0, "end_ts": s1, "period_interval": 1})
        n_req += 1
        by = {m["market_ticker"]: m.get("candlesticks") or [] for m in data.get("markets", [])}
        for t in batch:
            parts[t].append(_rows(by.get(t, [])))
            since[t] = s1 + 60
        done = [t for t in batch if since[t] >= end_ts]
        for t in done:
            _save(t, parts.pop(t), end_ts)
        todo = sorted((t for t in todo if since[t] < end_ts), key=lambda t: since[t])
        if n_req % 100 == 0:
            log(f"[candles] {n_req} requests, {len(todo)} tickers still behind")
        time.sleep(REQUEST_PACING_S)
    for t, ps in parts.items():
        _save(t, ps, end_ts)
    log(f"[candles] {n_req} requests")
    return n_req


def _save(ticker: str, new: list[pd.DataFrame], until: int):
    df = pd.concat([load_cached(ticker), *new], ignore_index=True)
    df = df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    df.to_parquet(_path(ticker), index=False)
    with open(_meta_path(ticker), "w") as fh:
        fh.write(str(int(until)))


@dataclasses.dataclass
class Grid:
    """Minute grid. Each field is ticker -> float32 array (YES terms), NaN
    before the ticker's first candle. bid/ask forward-fill the last close;
    bid_high/ask_low are the candle's own extremes in a minute that had a
    candle and equal bid/ask otherwise."""
    ts: np.ndarray
    bid: dict[str, np.ndarray]
    ask: dict[str, np.ndarray]
    bid_high: dict[str, np.ndarray]
    ask_low: dict[str, np.ndarray]


def _ffill(pos: np.ndarray, vals: np.ndarray, n: int) -> np.ndarray:
    out = np.full(n, np.nan, dtype=np.float32)
    out[pos] = vals
    idx = np.where(~np.isnan(out), np.arange(n), -1)
    np.maximum.accumulate(idx, out=idx)
    res = np.where(idx >= 0, out[np.maximum(idx, 0)], np.nan).astype(np.float32)
    return res


def grid(tickers: list[str], start_ts: int, end_ts: int) -> Grid:
    t0 = start_ts - start_ts % 60
    ts = np.arange(t0, end_ts + 1, 60, dtype=np.int64)
    n = len(ts)
    g = Grid(ts=ts, bid={}, ask={}, bid_high={}, ask_low={})
    for t in tickers:
        df = load_cached(t)
        # One candle before the window seeds the forward fill.
        before = df[df.ts < t0].tail(1)
        df = pd.concat([before.assign(ts=t0), df[(df.ts >= t0) & (df.ts <= ts[-1])]], ignore_index=True)
        df = df.drop_duplicates("ts", keep="last")
        pos = ((df.ts.to_numpy() - t0) // 60).astype(np.int64)
        bid = _ffill(pos, df.bid.to_numpy(np.float32), n)
        ask = _ffill(pos, df.ask.to_numpy(np.float32), n)
        bh, al = bid.copy(), ask.copy()
        real = df.ts.to_numpy() > t0 if len(before) else np.ones(len(df), bool)
        bh[pos[real]] = df.bid_high.to_numpy(np.float32)[real]
        al[pos[real]] = df.ask_low.to_numpy(np.float32)[real]
        g.bid[t], g.ask[t], g.bid_high[t], g.ask_low[t] = bid, ask, bh, al
    return g
