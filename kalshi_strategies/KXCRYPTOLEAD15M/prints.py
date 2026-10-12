"""
Coinbase trade prints: a burst detector that pulls quotes, and hourly
persistence so its thresholds can be re-derived.

A burst is one coin's trailing-1s print notional crossing a per-coin
threshold. Calibrated 2026-10-10 against the 10/09 21:00 - 10/10 04:50 UTC
real sessions (231 maker fills, Coinbase prints backfilled from the public
trades endpoint): pulling at each coin's p99.5 for 1.5s was off 5.4% of the
time and covered 66 maker fills carrying $90 of the $153 5s adverse markout.
In-sample, so recheck once these files hold a few days of prints.
"""
from __future__ import annotations

import collections
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT as _DATA_ROOT

PRINTS_ROOT = Path(_DATA_ROOT) / "kalshi_crypto" / "prints"
WINDOW_S = 1.0

# Hyperliquid trailing-1s print notional (USD) that pulls every quote in the
# event; HYPE sums perp and spot. Fallback only, until BurstCalibrator has
# MIN_CALIB_S of samples: each coin's p99.5 over 2026-10-11 06:00-15:50 UTC,
# so a cold start errs toward pulling less, not more.
HL_BURST_USD_1S = {"BTC": 200_700, "ETH": 625_400, "SOL": 13_100, "XRP": 5_000, "HYPE": 39_800}

# Offshore perps. No static fallback: each stays record-only until its own
# BurstCalibrator has MIN_CALIB_S of live samples. These trade 10-50x Coinbase
# volume, so borrowing Coinbase's or Hyperliquid's numbers would trip constantly.
PERP_SOURCES = ("binance_perp", "bybit_perp", "okx_perp")
PERP_BURST_USD_1S: dict[str, dict[str, float]] = {v: {} for v in PERP_SOURCES}


# Auto-calibration: each source's burst threshold is the BURST_QUANTILE of its
# own trailing-1s notional, sampled every SAMPLE_S over the last
# CALIB_WINDOW_S. Static thresholds drifted badly with the session: HL ETH's
# p99.5 was $50k at 05:30 UTC and $625k by 15:00, which had quotes pulled 31%
# of the 15:00 hour (2026-10-11). Same quantile and 250ms grid the static
# values were fitted on. Until MIN_CALIB_S of samples exist (seeded at start
# from the persisted prints), the static values above are used.
BURST_QUANTILE = 0.995
CALIB_WINDOW_S = 2 * 3600.0
SAMPLE_S = 0.25
MIN_CALIB_S = 1800.0
REFRESH_S = 60.0
# Sources whose thresholds self-calibrate. Each adds its own ~0.5% of trips on
# top: simulated over 2026-10-11 ~18:00-21:00 UTC, the perps took the share of
# time quotes are pulled from 5.7% (Coinbase + Hyperliquid) to 9.0%.
AUTO_SOURCES = ("coinbase", "hyperliquid", *PERP_SOURCES)


# Calibrated thresholds, saved on every refresh so a restart can reuse them
# until its own calibrator is ready. Without this a restart ran on the static
# fallbacks for 30+ min: HL XRP's $5k fallback vs ~$150k calibrated pulled
# quotes 54% of the time after the 2026-10-12 16:46 UTC restart.
SAVED_PATH = Path(_DATA_ROOT) / "kalshi_crypto" / "burst_thresholds.json"
# Saved thresholds older than this are ignored: thresholds drift with the
# session (HL ETH p99.5 $50k at 05:30 vs $625k at 15:00 UTC, 2026-10-11), so
# only a quick restart should reuse them.
SAVED_MAX_AGE_S = 1800.0


def save_thresholds(by_source: dict[str, dict[str, float]], path: Path = SAVED_PATH, now: float | None = None):
    """Atomic write of {source: {coin: usd_1s}} with the time they were computed.
    Merges into the file, so a source that isn't ready yet keeps its last values."""
    now = time.time() if now is None else now
    doc = {"sources": {}}
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        pass
    srcs = doc.get("sources") or {}
    for s, th in by_source.items():
        srcs[s] = {"written_at": now, "usd_1s": {c: float(v) for c, v in th.items()}}
    doc = {"sources": srcs}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)


def load_thresholds(path: Path = SAVED_PATH, now: float | None = None,
                    max_age_s: float = SAVED_MAX_AGE_S) -> dict[str, dict[str, float]]:
    """{source: {coin: usd_1s}} for each source saved within max_age_s; {} if none."""
    now = time.time() if now is None else now
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for s, rec in (doc.get("sources") or {}).items():
        try:
            if 0 <= now - float(rec["written_at"]) <= max_age_s:
                out[s] = {c: float(v) for c, v in rec["usd_1s"].items() if float(v) > 0}
        except (KeyError, TypeError, ValueError):
            continue
    return out


class BurstDetector:
    """Per-coin trailing notional. Clocks are the adapter's monotonic
    `as_of`, the same clock feed health and signal age use."""

    def __init__(self, coins):
        self._prints = {c: collections.deque() for c in coins}
        self._sum = {c: 0.0 for c in coins}

    def add(self, coin: str, usd: float, as_of: float) -> float:
        """Record a print; returns the coin's notional over the trailing WINDOW_S."""
        q = self._prints.get(coin)
        if q is None:
            return 0.0
        q.append((as_of, usd))
        self._sum[coin] += usd
        return self.level(coin, as_of)

    def level(self, coin: str, now: float) -> float:
        """Notional over the trailing WINDOW_S as of `now`, prints or not."""
        q = self._prints.get(coin)
        if q is None:
            return 0.0
        while q and q[0][0] <= now - WINDOW_S:
            self._sum[coin] -= q.popleft()[1]
        if not q:
            self._sum[coin] = 0.0      # clears float drift from the running sum
        return max(self._sum[coin], 0.0)


class BurstCalibrator:
    """Rolling BURST_QUANTILE of one source's trailing-1s notional per coin.
    sample() is O(coins) on the loop; quantiles() is the heavy part and runs
    off it on a snapshot()."""

    def __init__(self, coins, fallback: dict[str, float] | None):
        self.coins = list(coins)
        self._buf = np.zeros((len(self.coins), int(CALIB_WINDOW_S / SAMPLE_S)))
        self._i = 0
        self._n = 0
        self.fallback = dict(fallback or {})
        self.live: dict[str, float] = {}
        # Recently saved calibration from a previous run, used until ready.
        self.saved: dict[str, float] = {}

    def sample(self, levels: dict[str, float]):
        for k, c in enumerate(self.coins):
            self._buf[k, self._i] = levels.get(c, 0.0)
        self._i = (self._i + 1) % self._buf.shape[1]
        self._n = min(self._n + 1, self._buf.shape[1])

    def seed(self, rows: np.ndarray):
        """Oldest-first (n, coins) samples on the SAMPLE_S grid, from disk."""
        for r in rows[-self._buf.shape[1]:]:
            self.sample(dict(zip(self.coins, r)))

    @property
    def ready(self) -> bool:
        return self._n * SAMPLE_S >= MIN_CALIB_S

    def snapshot(self) -> np.ndarray:
        return self._buf[:, :self._n].copy()

    @staticmethod
    def quantiles(snap: np.ndarray) -> np.ndarray:
        return np.quantile(snap, BURST_QUANTILE, axis=1) if snap.shape[1] else np.zeros(snap.shape[0])

    def adopt(self, q: np.ndarray):
        # A zero quantile means the coin printed in under 0.5% of windows; any
        # single print would trip, so keep the previous value instead.
        for c, v in zip(self.coins, q):
            if v > 0:
                self.live[c] = float(v)

    def threshold(self, coin: str) -> float | None:
        if self.ready and coin in self.live:
            return self.live[coin]
        if coin in self.saved:
            return self.saved[coin]
        return self.fallback.get(coin)

    @property
    def source(self) -> str:
        """Where threshold() is reading from: live, saved or fallback."""
        return "live" if self.ready else ("saved" if self.saved else "fallback")


def seed_rows(source: str, coins, now_wall: float, root: Path = PRINTS_ROOT) -> np.ndarray:
    """Calibration samples for `source` over the last CALIB_WINDOW_S, rebuilt
    from the hourly print files. Off the loop."""
    lo = now_wall - CALIB_WINDOW_S
    if source in PERP_SOURCES:
        return _seed_perp_rows(source, coins, lo, now_wall)
    hours = pd.date_range(pd.Timestamp(lo, unit="s", tz="UTC").floor("h"),
                          pd.Timestamp(now_wall, unit="s", tz="UTC").floor("h"), freq="h")
    cols = {}
    for c in coins:
        fr = []
        for h in hours:
            path = root / f"{c}_{h.strftime('%Y-%m-%d')}_{h.strftime('%H')}.parquet"
            if path.exists():
                try:
                    fr.append(pd.read_parquet(path, columns=None))
                except Exception:
                    continue
        if not fr:
            continue
        df = pd.concat(fr, ignore_index=True)
        venue = df["venue"] if "venue" in df else pd.Series("coinbase", index=df.index)
        keep = venue == "coinbase" if source == "coinbase" else venue.astype(str).str.startswith("hl_")
        df = df[keep & (df.recv_ts >= lo)]
        if df.empty:
            continue
        usd = pd.Series((df.price * df["size"]).values, index=pd.to_datetime(df.recv_ts, unit="s"))
        b = int(round(WINDOW_S / SAMPLE_S))
        cols[c] = usd.resample(f"{int(SAMPLE_S * 1000)}ms").sum().rolling(b, min_periods=1).sum()
    if not cols:
        return np.zeros((0, len(coins)))
    m = pd.DataFrame(cols).reindex(columns=list(coins)).fillna(0.0)
    return m.values


PERP_PRINTS_ROOT = Path(_DATA_ROOT) / "kalshi_crypto" / "perp_prints"


def _seed_perp_rows(source: str, coins, lo: float, hi: float) -> np.ndarray:
    """Same grid as seed_rows, from venue_process's perp_prints/{day}/ parts.
    Parts are named by write time in ms, so only the window's are read."""
    days = {pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%d") for t in (lo, hi)}
    parts = [p for d in sorted(days) for p in (PERP_PRINTS_ROOT / d).glob("*.parquet")
             if p.stem.isdigit() and int(p.stem) / 1e3 >= lo]
    if not parts:
        return np.zeros((0, len(coins)))
    fr = []
    for p in parts:
        try:
            fr.append(pd.read_parquet(p, columns=["recv_ts", "venue", "coin", "price", "size"]))
        except Exception:
            continue
    if not fr:
        return np.zeros((0, len(coins)))
    df = pd.concat(fr, ignore_index=True)
    df = df[(df.venue == source) & (df.recv_ts >= lo)]
    if df.empty:
        return np.zeros((0, len(coins)))
    b = int(round(WINDOW_S / SAMPLE_S))
    cols = {}
    for c, g in df.groupby("coin"):
        usd = pd.Series((g.price * g["size"]).values, index=pd.to_datetime(g.recv_ts, unit="s"))
        cols[c] = usd.resample(f"{int(SAMPLE_S * 1000)}ms").sum().rolling(b, min_periods=1).sum()
    return pd.DataFrame(cols).reindex(columns=list(coins)).fillna(0.0).sort_index().values


class PrintLog:
    """Raw prints, appended to {coin}_{YYYY-MM-DD}_{HH}.parquet on flush()."""

    def __init__(self, root: Path = PRINTS_ROOT, persist: bool = True):
        self.root = Path(root)
        self.persist = persist
        self._pending: list[dict] = []
        if persist:
            self.root.mkdir(parents=True, exist_ok=True)

    def add(self, coin: str, trade_id, price: float, size: float, side: str | None, exch_ts: float | None,
            venue: str = "coinbase"):
        if self.persist:
            self._pending.append({"coin": coin, "trade_id": str(trade_id), "price": price, "size": size,
                                  "side": side, "exch_ts": exch_ts, "recv_ts": time.time(), "venue": venue})

    def drain(self) -> list[dict]:
        """On the loop: hand over pending rows. Kept apart from write() so the
        loop's appends never race a swap on the executor thread."""
        rows, self._pending = self._pending, []
        return rows

    def write(self, rows: list[dict]) -> int:
        """Off the loop. Same tmp+replace as CompositeFeed.flush."""
        if not rows:
            return 0
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["exch_ts"].fillna(df["recv_ts"]), unit="s", utc=True)
        for (coin, hour), chunk in df.groupby(["coin", df["ts"].dt.floor("h")]):
            path = self.root / f"{coin}_{hour.strftime('%Y-%m-%d')}_{hour.strftime('%H')}.parquet"
            if path.exists():
                try:
                    chunk = pd.concat([pd.read_parquet(path), chunk], ignore_index=True)
                except Exception as e:
                    print(f"[KXCRYPTOLEAD15M] prints: {path.name} unreadable ({e!r}); starting fresh")
            chunk = chunk.drop_duplicates(["venue", "trade_id"] if "venue" in chunk else "trade_id", keep="last")
            tmp = path.with_suffix(f".tmp-{os.getpid()}.parquet")
            chunk.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        return len(rows)
