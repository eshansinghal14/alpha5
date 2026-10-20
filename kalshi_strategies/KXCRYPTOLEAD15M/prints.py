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
PERP_SOURCES = ("binance_perp", "okx_perp")      # Bybit dropped 10-20 (spot_feed.venues.LEADING_VENUES)
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
# A p99.5 threshold is crossed 0.5% of the time on the samples it came from;
# bursts cluster, so allow 10x that over 5 min before calling it mis-scaled.
# Needs a minute of samples first.
SANITY_WINDOW_S = 300.0
SANITY_MIN_S = 60.0
SANITY_MAX_TRIP = 0.05


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
            return self.live[coin]          # a quantile of these same samples: consistent by construction
        v = self.saved.get(coin, self.fallback.get(coin))
        if v is not None and self.too_hot(coin, v):
            return None
        return v

    def too_hot(self, coin: str, thr: float) -> bool:
        """Whether a saved or fallback `thr` is crossed far more often than a
        p99.5 can be on the last SANITY_WINDOW_S of samples: a mis-scaled
        value, so the source is record-only for that coin until it recalibrates
        (its live quantile then takes over). Saved values ~1,000x
        too low (HL ETH $91/s) held every quote pulled for 12 min after the
        2026-10-16 18:34 UTC restart."""
        n = min(self._n, int(SANITY_WINDOW_S / SAMPLE_S))
        if n < int(SANITY_MIN_S / SAMPLE_S) or coin not in self.coins:
            return False
        k = self.coins.index(coin)
        idx = (self._i - 1 - np.arange(n)) % self._buf.shape[1]
        return float(np.mean(self._buf[k, idx] >= thr)) > SANITY_MAX_TRIP

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


def seed_move_rows(venue: str, coins, now_wall: float, window_s: float) -> np.ndarray:
    """|last-trade move over window_s| in bp on the SAMPLE_S grid, last
    CALIB_WINDOW_S, from perp_prints/: the move trigger's calibration seed."""
    lo = now_wall - CALIB_WINDOW_S
    days = {pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%d") for t in (lo, now_wall)}
    parts = [p for d in sorted(days) for p in (PERP_PRINTS_ROOT / d).glob("*.parquet")
             if p.stem.isdigit() and int(p.stem) / 1e3 >= lo]
    fr = []
    for p in parts:
        try:
            x = pd.read_parquet(p, columns=["recv_ts", "venue", "coin", "price"])
        except Exception:
            continue
        fr.append(x[x.venue == venue])
    if not fr:
        return np.zeros((0, len(coins)))
    df = pd.concat(fr, ignore_index=True)
    df = df[df.recv_ts >= lo].sort_values("recv_ts")
    if df.empty:
        return np.zeros((0, len(coins)))
    k = max(1, int(round(window_s / SAMPLE_S)))
    cols = {}
    for c, g in df.groupby("coin"):
        px = pd.Series(g.price.values, index=pd.to_datetime(g.recv_ts, unit="s")).resample(
            f"{int(SAMPLE_S * 1000)}ms").last().ffill()
        cols[c] = (1e4 * np.log(px / px.shift(k))).abs()
    return pd.DataFrame(cols).reindex(columns=list(coins)).fillna(0.0).sort_index().values


class PriceTrail:
    """Per key, the last-trade price now and window_s ago (as-of, on one
    clock). Keeps one sample at or before the window start and nothing older."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._q: dict = {}

    def add(self, key, t: float, px: float):
        q = self._q.get(key)
        if q is None:
            q = self._q[key] = collections.deque()
        q.append((t, px))
        self._trim(q, t)

    def _trim(self, q, now):
        cut = now - self.window_s
        while len(q) >= 2 and q[1][0] <= cut:
            q.popleft()

    def move_bp(self, key, now: float) -> float | None:
        """Log move over the trailing window_s in bp; None without a price
        from at least window_s ago."""
        q = self._q.get(key)
        if not q:
            return None
        self._trim(q, now)
        t0, p0 = q[0]
        if t0 > now - self.window_s or p0 <= 0:
            return None
        return 1e4 * float(np.log(q[-1][1] / p0))


# Perp price-move trigger: 2 perp venues moving >= their own p99.5 (Binance and
# OKX since Bybit was dropped 10-20, so both must agree; was 2 of 3)
# of |0.5s move| in one direction. Replay 10-16 19:05 to 10-18 06:35 UTC on top
# of the volume trigger: pulled 10.5% -> 11.6%, maker $ on prints we stay
# exposed to +$2,560 -> +$3,222, the extra pull time ran -6.5c/lot to makers,
# big moves covered 80% -> 88%. 0.4-0.75s and p99-p99.7 were all positive; a
# single venue fired on the 10-17 22:15:37 all-venue print glitch on every
# coin, 2-of-3 on two.
MOVE_WINDOW_S = 0.5
MOVE_QUANTILE = 0.995
MOVE_AGREE = 2
# Trip frames from the venue process are re-sent at most this often per key
# while it stays over, to extend the pull: well inside burst_hold_s, and caps
# a Binance sweep (thousands of prints/s) at 20 frames/s per key.
TRIP_RESEND_S = 0.05
MOVE_SOURCE = "perp_move"


class LeadTrigger:
    """Burst detection for the non-Coinbase sources, run in the venue process
    on every print so a trip reaches the trading loop without waiting for a
    batch. Thresholds come from the trading process (its calibrators);
    None = record-only.

    Volume: the source's trailing-1s notional at or over its threshold
    (Hyperliquid perp + spot share one window, as in SpotFeed).
    Move: on `agree` of the perp venues, the last-trade move over window_s at
    or over that venue's own threshold, all in the same direction.

    on_print returns trip rows (kind, source, coin, level, thr, onset); a row
    is emitted on the onset and then every TRIP_RESEND_S while it holds."""

    def __init__(self, coins, perps, window_s: float = MOVE_WINDOW_S, agree: int = MOVE_AGREE):
        self.coins = list(coins)
        self.perps = tuple(perps)
        self.det: dict[str, BurstDetector] = {}
        self.trail = PriceTrail(window_s)
        self.agree = agree
        self.vol_thr: dict[str, dict[str, float]] = {}
        self.move_thr: dict[str, dict[str, float]] = {}
        self._over: dict[tuple, bool] = {}
        self._sent: dict[tuple, float] = {}

    def configure(self, vol_thr=None, move_thr=None):
        if vol_thr is not None:
            self.vol_thr = vol_thr
        if move_thr is not None:
            self.move_thr = move_thr

    @staticmethod
    def source_of(kind: str) -> str:
        return "hyperliquid" if kind.startswith("hl_") else kind

    def _emit(self, out, key, hit, now, row):
        was = self._over.get(key, False)
        self._over[key] = hit
        if not hit:
            return
        if not was or now - self._sent.get(key, -1e18) >= TRIP_RESEND_S:
            self._sent[key] = now
            out.append(row + (not was,))

    def on_print(self, kind: str, coin: str, price: float, size: float, as_of: float) -> list[tuple]:
        out: list[tuple] = []
        src = self.source_of(kind)
        det = self.det.get(src)
        if det is None:
            det = self.det[src] = BurstDetector(self.coins)
        level = det.add(coin, price * size, as_of)
        thr = (self.vol_thr.get(src) or {}).get(coin)
        self._emit(out, ("volume", src, coin), thr is not None and level >= thr, as_of,
                   ("volume", src, coin, level, thr if thr is not None else 0.0))
        if kind in self.perps:
            self.trail.add((kind, coin), as_of, price)
            out += self._move(kind, coin, as_of)
        return out

    def _move(self, kind, coin, now) -> list[tuple]:
        out: list[tuple] = []
        hits = self._agrees(coin, now)
        row = ("move", MOVE_SOURCE, coin, float(np.mean([abs(m) for m, _ in hits])) if hits else 0.0,
               float(np.mean([t for _, t in hits])) if hits else 0.0)
        self._emit(out, ("move", MOVE_SOURCE, coin), bool(hits), now, row)
        return out

    def _agrees(self, coin, now) -> list[tuple[float, float]]:
        up, dn = [], []
        for v in self.perps:
            thr = (self.move_thr.get(v) or {}).get(coin)
            m = self.trail.move_bp((v, coin), now)
            if thr is None or m is None or abs(m) < thr:
                continue
            (up if m > 0 else dn).append((m, thr))
        if len(up) >= self.agree:
            return up
        if len(dn) >= self.agree:
            return dn
        return []


TRIPS_ROOT = Path(_DATA_ROOT) / "kalshi_crypto" / "burst_trips"


class TripLog:
    """One row per trip onset, pulling or not, as append-only parts under
    burst_trips/{day}/. kind "volume": level is trailing-1s USD; kind "move":
    level is the mean |move| in bp over the agreeing perp venues."""

    def __init__(self, root: Path = TRIPS_ROOT, persist: bool = True):
        self.root = Path(root)
        self.persist = persist
        self._over: dict[tuple[str, str], bool] = {}
        self._pending: list[dict] = []

    def record(self, kind: str, source: str, coin: str, level: float, thr: float, *, thr_source: str,
               pulls: bool, as_of: float, exch_ts: float | None, now_mono: float | None = None) -> dict:
        mono = time.monotonic() if now_mono is None else now_mono
        row = {"recv_ts": time.time() - (mono - as_of), "exch_ts": exch_ts, "kind": kind, "source": source,
               "coin": coin, "level": float(level), "thr": float(thr),
               "ratio": float(level / thr) if thr else float("nan"), "thr_source": thr_source,
               "pulls": bool(pulls),
               # Receipt -> applied on the trading loop (pipe + loop wake for venue-process trips).
               "seen_delay_s": float(mono - as_of)}
        if self.persist:
            self._pending.append(row)
        return row

    def observe(self, source: str, coin: str, usd_1s: float, thr: float | None, *, thr_source: str,
                pulls: bool, as_of: float, exch_ts: float | None, now_mono: float | None = None) -> dict | None:
        """Volume trips detected in this process (Coinbase): call on every
        print; records onsets only."""
        key = (source, coin)
        over = thr is not None and usd_1s >= thr
        was = self._over.get(key, False)
        self._over[key] = over
        if not over or was:
            return None
        return self.record("volume", source, coin, usd_1s, thr, thr_source=thr_source, pulls=pulls,
                           as_of=as_of, exch_ts=exch_ts, now_mono=now_mono)

    def drain(self) -> list[dict]:
        rows, self._pending = self._pending, []
        return rows

    def write(self, rows: list[dict]) -> int:
        """Off the loop."""
        if not rows:
            return 0
        df = pd.DataFrame(rows)
        day = pd.Timestamp(df.recv_ts.iloc[0], unit="s", tz="UTC").strftime("%Y-%m-%d")
        d = self.root / day
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / f"{int(time.time() * 1000)}.parquet", index=False)
        return len(rows)


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
