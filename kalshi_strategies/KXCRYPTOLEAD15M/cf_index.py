"""
CF Benchmarks settlement index (BRTI, ETHUSD_RTI, ...) per coin, from
Kalshi's relay: the `cfbenchmarks_value` websocket channel live, and the
REST passthrough (`/cfbenchmarks/values`, the trailing hour) to fill gaps.

The race settles on the mean of each index's whole-second values over
[S-60, S) and [T-60, T): on 2026-10-12..18 that mean matched Kalshi's
published expiration_value to <= 0.02bp (XRP's is rounded to 4 dp), and
the race paid the exact-average winner where the rounded published values
disagreed (26OCT130400, 26OCT161015).

Ticks are whole UTC seconds; `value(coin, ts_s)` is the index at that
second. Persisted under data/kalshi_crypto/cf_index/ so a restart and the
backtest see the same series.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
import websockets

from kalshi_infra import DATA_ROOT as _DATA_ROOT
from kalshi_infra.api_functions import BASE_URL
from kalshi_infra.kalshi import auth
from kalshi_infra.kalshi.ws_feed import WS_URL

from .series import COINS, SETTLEMENT_INDEX

CF_ROOT = Path(_DATA_ROOT) / "kalshi_crypto" / "cf_index"
REST_PATH = "/trade-api/v2/cfbenchmarks/values"
INDEX_COIN = {v: k for k, v in SETTLEMENT_INDEX.items()}
# Two start averages plus an event fit; the forecaster reads mids, not this.
HISTORY_S = 2 * 3600
# Ticks arrive ~70ms after their second (2026-10-18, EC2 us-east-1), one per
# second per index; a coin whose newest tick is older than this has missed
# two and counts as stale.
STALE_S = 2.5
RECONNECT_DELAY_S = 1.0
RECV_TIMEOUT_S = 5.0
# A gap of more than a few seconds is refilled from REST at most this often.
REST_FILL_MIN_S = 5.0


def load_cf(coin: str, lo_s: float, hi_s: float, root: Path = CF_ROOT) -> dict[int, float]:
    """Persisted {ts_s: value} over [lo_s, hi_s)."""
    out: dict[int, float] = {}
    for h in pd.date_range(pd.Timestamp(lo_s, unit="s", tz="UTC").floor("h"),
                           pd.Timestamp(hi_s, unit="s", tz="UTC"), freq="h"):
        p = root / f"{coin}_{h:%Y-%m-%d_%H}.parquet"
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p)
        except Exception as e:
            print(f"[KXCRYPTOLEAD15M] cf_index: {p.name} unreadable ({e!r})", flush=True)
            continue
        for t, v in zip(df.ts_s.to_numpy(), df.v.to_numpy()):
            if lo_s <= t < hi_s:
                out[int(t)] = float(v)
    return out


def fetch_rest(coin: str) -> dict[int, float]:
    """The trailing hour of whole-second values. Blocking; 50 read tokens."""
    r = requests.get(BASE_URL + REST_PATH, headers=auth.rest_headers("GET", REST_PATH),
                     params={"id": SETTLEMENT_INDEX[coin]}, timeout=10)
    r.raise_for_status()
    out = {}
    for row in r.json()["data"]["payload"]:
        t = int(row["time"])
        if t % 1000 == 0:
            out[t // 1000] = float(row["value"])
    return out


class CfIndex:
    """Session-lifetime: one websocket, every coin. `on_tick(coin, ts_s)` is
    called on the loop for each new second."""

    def __init__(self, persist: bool = True, root: Path = CF_ROOT):
        self.persist = persist
        self.root = Path(root)
        self._v: dict[str, dict[int, float]] = {c: {} for c in COINS}
        self._order: dict[str, collections.deque] = {c: collections.deque() for c in COINS}
        self._newest: dict[str, int] = {}
        self._last_rx_mono: dict[str, float] = {}
        self._pending: list[dict] = []
        self._last_fill: dict[str, float] = {}
        self.on_tick = None
        self.version = 0          # bumps on every new value (cache key for readers)
        self.n_reconnects = 0
        self.n_ticks = 0

    # --- state ---------------------------------------------------------------

    def add(self, coin: str, ts_s: int, v: float, mono: float | None = None, live: bool = True) -> bool:
        if coin not in self._v or not (v > 0):
            return False
        d = self._v[coin]
        if ts_s in d or ts_s < self._newest.get(coin, ts_s) - HISTORY_S:
            return False
        d[ts_s] = v
        self._order[coin].append(ts_s)
        if ts_s > self._newest.get(coin, -1):
            self._newest[coin] = ts_s
        # Gap fills arrive out of order, so the deque isn't sorted; trim by value.
        cut = self._newest[coin] - HISTORY_S
        while self._order[coin] and self._order[coin][0] < cut:
            d.pop(self._order[coin].popleft(), None)
        self.version += 1
        if live:
            self._last_rx_mono[coin] = time.monotonic() if mono is None else mono
        if self.persist:
            self._pending.append({"coin": coin, "ts_s": int(ts_s), "v": float(v)})
        return True

    def value(self, coin: str, ts_s: int) -> float | None:
        return self._v[coin].get(int(ts_s))

    def latest(self, coin: str) -> tuple[int, float] | None:
        t = self._newest.get(coin)
        return None if t is None else (t, self._v[coin][t])

    def window(self, coin: str, lo_s: int, hi_s: int) -> list[float]:
        """Values for every second in [lo_s, hi_s) that we hold, in order."""
        d = self._v[coin]
        return [d[t] for t in range(int(lo_s), int(hi_s)) if t in d]

    def age_s(self, now_mono: float | None = None) -> float | None:
        """Since the stalest coin's last live tick; None before every coin has one."""
        if len(self._last_rx_mono) < len(COINS):
            return None
        now = time.monotonic() if now_mono is None else now_mono
        return now - min(self._last_rx_mono.values())

    def stale(self, now_mono: float | None = None) -> bool:
        a = self.age_s(now_mono)
        return a is None or a > STALE_S

    def seed(self, lo_s: float, hi_s: float):
        """Disk, then REST for whatever disk lacks in the last hour. Blocking."""
        for c in COINS:
            for t, v in sorted(load_cf(c, lo_s, hi_s, self.root).items()):
                self.add(c, t, v, live=False)
            if any(t not in self._v[c] for t in range(int(max(lo_s, hi_s - 3600)), int(hi_s) - 5)):
                try:
                    for t, v in sorted(fetch_rest(c).items()):
                        if lo_s <= t < hi_s:
                            self.add(c, t, v, live=False)
                except Exception as e:
                    print(f"[KXCRYPTOLEAD15M] cf_index: REST seed for {c} failed ({e!r})", flush=True)

    def request_fill(self, coin: str):
        """Refill `coin` from REST in the background (rate-limited). No-op off the loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if time.monotonic() - self._last_fill.get(coin, -1e9) >= REST_FILL_MIN_S:
            loop.create_task(self._fill_gap(coin))

    async def _fill_gap(self, coin: str):
        now = time.monotonic()
        if now - self._last_fill.get(coin, -1e9) < REST_FILL_MIN_S:
            return
        self._last_fill[coin] = now
        try:
            vals = await asyncio.get_running_loop().run_in_executor(None, fetch_rest, coin)
        except Exception as e:
            print(f"[KXCRYPTOLEAD15M] cf_index: REST fill for {coin} failed ({e!r})", flush=True)
            return
        n = sum(self.add(coin, t, v, live=False) for t, v in sorted(vals.items()))
        if n:
            print(f"[KXCRYPTOLEAD15M] cf_index: filled {n} {coin} seconds from REST", flush=True)

    # --- feed ----------------------------------------------------------------

    def _handle(self, d: dict) -> tuple[str, int] | None:
        if d.get("type") != "cfbenchmarks_value":
            return None
        msg = d.get("msg") or {}
        coin = INDEX_COIN.get(msg.get("index_id"))
        if coin is None:
            return None
        try:
            raw = json.loads(msg["data"])
            t_ms, v = int(raw["time"]), float(raw["value"])
        except (KeyError, TypeError, ValueError):
            return None
        if t_ms % 1000:
            return None
        ts = t_ms // 1000
        prev = self.latest(coin)
        if not self.add(coin, ts, v):
            return None
        self.n_ticks += 1
        if prev is not None and ts - prev[0] > 2:
            self.request_fill(coin)
        return coin, ts

    async def run(self):
        sub = {"id": 1, "cmd": "subscribe",
               "params": {"channels": ["cfbenchmarks_value"], "index_ids": [SETTLEMENT_INDEX[c] for c in COINS]}}
        while True:
            try:
                async with websockets.connect(WS_URL, additional_headers=auth.ws_headers(), open_timeout=15) as ws:
                    await ws.send(json.dumps(sub))
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            # Kalshi sends one tick per index per second; 5s of
                            # nothing is a dead socket, not a quiet market.
                            print("[KXCRYPTOLEAD15M] cf_index: no ticks for 5s; reconnecting", flush=True)
                            break
                        got = self._handle(json.loads(raw))
                        if got is not None and self.on_tick is not None:
                            self.on_tick(*got)
            except Exception as e:      # handshake rejects (InvalidStatus) too: never let the feed die
                print(f"[KXCRYPTOLEAD15M] cf_index ws error ({e!r}); reconnecting", flush=True)
            self.n_reconnects += 1
            await asyncio.sleep(RECONNECT_DELAY_S)
            for c in COINS:
                self.request_fill(c)

    # --- persistence ---------------------------------------------------------

    def drain(self) -> list[dict]:
        rows, self._pending = self._pending, []
        return rows

    def write(self, rows: list[dict]) -> int:
        """Off the loop. Append-merge into hourly files, atomic replace."""
        if not rows:
            return 0
        self.root.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        df["hour"] = pd.to_datetime(df.ts_s, unit="s", utc=True).dt.floor("h")
        for (coin, hour), chunk in df.groupby(["coin", "hour"]):
            path = self.root / f"{coin}_{hour:%Y-%m-%d_%H}.parquet"
            chunk = chunk[["ts_s", "v"]]
            if path.exists():
                try:
                    chunk = pd.concat([pd.read_parquet(path), chunk], ignore_index=True)
                except Exception as e:
                    print(f"[KXCRYPTOLEAD15M] cf_index: {path.name} unreadable ({e!r}); starting fresh", flush=True)
            chunk = chunk.drop_duplicates("ts_s", keep="first").sort_values("ts_s")
            tmp = path.with_suffix(f".tmp-{os.getpid()}.parquet")
            chunk.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        return len(rows)


__all__ = ["CfIndex", "CF_ROOT", "STALE_S", "load_cf", "fetch_rest"]
