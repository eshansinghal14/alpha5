"""
Per-second composite price bars.

Each `CompositeFeed` instance owns one coin's rolling 1s history. The
spot-feed adapters push `VenueBook`s into it as they arrive; a
`sample(now)` call pins the current CF Spot Rate at the wall-clock
second.

Why not emit on every book update: Kalshi settles on 60-sample TWAPs at
exactly `[S-60, S)` and `[T-60, T)`. The one source of truth has to be
these per-second samples; the nowcast on every update is a separate
output used only for quoting responsiveness.

Persistence (section 2.1, "Persist everything"): raw ticks are already
caught by the adapters as they call back; this module appends 1s bars to
`data/kalshi_crypto/composite/{coin}_{date}_{HH}.parquet`, one per hour. Hot path
keeps them in memory; disk writes happen on `flush()` to avoid taking a
GIL hit every second.
"""
from __future__ import annotations

import collections
import dataclasses
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT as _DATA_ROOT

from .cf_rate import compute_cf_spot, CFSpotResult
from .spot_feed import VenueBook
from .series import COINS


@dataclasses.dataclass(frozen=True)
class CompositeBar:
    coin: str
    ts_s: float               # wall-clock second (unix seconds)
    price: float
    utilized_depth: float
    n_venues_in: int
    n_venues_dropped: int
    is_fallback: bool
    fallback_mid: float | None
    nowcast_count: int        # how many underlying venue-book updates landed in this second

    @property
    def mid(self) -> float:
        """The PRICING series: median of venue top-of-book mids (= the raw
        Coinbase BBO mid while Coinbase is the only venue). The CF-replica
        `price` is kept for diagnostics only -- on thin books its weighting
        flips between levels and adds persistent noise (HYPE: 40bp rms vs
        the mid, 1s-signature 9.4x; 2026-10-08), which inflated HAR
        forecasts ~16x and diluted correlations to ~0.2. The mid matched
        the Kalshi winner on 6/7 settled events that day vs 4/7 for the
        replica. Falls back to `price` for bars persisted without a mid."""
        return self.fallback_mid if self.fallback_mid is not None else self.price


DATA_ROOT = Path(_DATA_ROOT) / "kalshi_crypto" / "composite"


def bar_filename(coin: str, hour: pd.Timestamp) -> str:
    """{coin}_{YYYY-MM-DD}_{HH}.parquet. Older files are daily,
    {coin}_{YYYY-MM-DD}.parquet; readers handle both."""
    return f"{coin}_{hour.strftime('%Y-%m-%d')}_{hour.strftime('%H')}.parquet"


def bar_files(coin: str, start_s: float, end_s: float, root: Path = DATA_ROOT) -> list[Path]:
    """Files that can hold bars in [start_s, end_s], by name alone."""
    lo = pd.Timestamp(start_s, unit="s", tz="UTC")
    hi = pd.Timestamp(end_s, unit="s", tz="UTC")
    out = []
    for path in sorted(root.glob(f"{coin}_*.parquet")):
        if ".tmp-" in path.name:   # a flush interrupted before its replace
            continue
        parts = path.stem.split("_")
        try:
            day = pd.Timestamp(parts[1], tz="UTC")
            if len(parts) == 2:
                first, last = day, day + pd.Timedelta(days=1)
            else:
                first = day + pd.Timedelta(hours=int(parts[2]))
                last = first + pd.Timedelta(hours=1)
        except (IndexError, ValueError):
            continue
        if first <= hi and last > lo:
            out.append(path)
    return out


class CompositeFeed:
    """One instance per coin. Holds the most recent per-venue `VenueBook`,
    produces per-second composite bars + fast nowcast prices, and persists
    the 1s bars to parquet."""

    def __init__(self, coin: str, history_s: int = 3600,
                 persist: bool = True, persist_dir: Path = DATA_ROOT):
        self.coin = coin
        self._venue_books: dict[str, VenueBook] = {}
        self._bars: collections.deque[CompositeBar] = collections.deque(maxlen=history_s)
        self._nowcast: CFSpotResult | None = None
        self._mid: float | None = None
        self._nowcast_count: int = 0
        self._bar_last_second_s: int | None = None
        self._persist = persist
        self._persist_dir = Path(persist_dir)
        if self._persist:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._pending_disk: list[CompositeBar] = []

    def observe(self, vb: VenueBook):
        """One update from any contributing venue. Updates stored venue
        state and refreshes the nowcast.

        Keeps the LAST good nowcast if this update can't produce a valid
        composite (e.g. the only venue is momentarily one-sided or
        resyncing). The feed-health monitor decides when a coin is too
        stale to use; the nowcast itself should not flicker to None on a
        single bad book and strand the pricer -- a DEGRADED coin is
        priced off exactly this last-good value."""
        if vb.coin != self.coin:
            return
        self._venue_books[vb.venue] = vb
        # Per message only the MID is needed (the pricing series). The CF
        # replica is depth-weighted over hundreds of levels and only feeds
        # diagnostics, so it runs once a second in sample(). Running it on
        # every message was ~4ms x ~85 msg/s of event-loop time.
        mids = [b.mid() for b in self._venue_books.values()
                if not b.is_resyncing and b.mid() is not None]
        if mids:
            m = float(np.median(mids))
            self._mid = m
            prev = self._nowcast
            self._nowcast = CFSpotResult(
                coin=self.coin, price=prev.price if prev is not None else m,
                utilized_depth=prev.utilized_depth if prev is not None else 0.0,
                n_venues_in=len(mids), n_venues_dropped=len(self._venue_books) - len(mids),
                drop_reasons={}, fallback_median_mid=m, is_fallback=prev is None)
        self._nowcast_count += 1

    def nowcast(self) -> CFSpotResult | None:
        """Latest composite price given the latest known venue books, no
        clock dependency. Used for quoting responsiveness."""
        return self._nowcast

    def mid_nowcast(self) -> float | None:
        """Latest venue-median top-of-book mid -- the pricing series (see
        CompositeBar.mid). Last-good value, same semantics as nowcast()."""
        nc = self._nowcast
        if nc is None:
            return None
        return nc.fallback_median_mid if nc.fallback_median_mid is not None else nc.price

    def venue_books(self) -> list[VenueBook]:
        return list(self._venue_books.values())

    def sample(self, now_s: float) -> CompositeBar | None:
        """Pin a composite bar for the wall-clock second containing
        `now_s`. Idempotent per second: calling it repeatedly inside the
        same second returns None after the first successful bar."""
        second_s = int(now_s)
        if self._bar_last_second_s is not None and second_s <= self._bar_last_second_s:
            return None
        res = compute_cf_spot(self.coin, list(self._venue_books.values()))
        if res is None:
            # No sample this second -- do not advance last_second_s so
            # the next tick can try again inside the same second if a
            # venue update arrives.
            return None
        self._nowcast = res
        bar = CompositeBar(
            coin=self.coin, ts_s=float(second_s), price=res.price,
            utilized_depth=res.utilized_depth,
            n_venues_in=res.n_venues_in,
            n_venues_dropped=res.n_venues_dropped,
            is_fallback=res.is_fallback,
            fallback_mid=res.fallback_median_mid,
            nowcast_count=self._nowcast_count,
        )
        self._bars.append(bar)
        self._pending_disk.append(bar)
        self._bar_last_second_s = second_s
        self._nowcast_count = 0
        return bar

    def bars(self, since_s: float | None = None) -> list[CompositeBar]:
        if since_s is None:
            return list(self._bars)
        return [b for b in self._bars if b.ts_s >= since_s]

    def flush(self) -> int:
        """Append any pending bars to parquet. Returns the number of
        bars written. Rotates hourly (see bar_filename).

        Done on a cadence by the caller -- NOT every bar -- because
        `to_parquet` is CPU-bound pandas work (holds the GIL) and this is
        a hot async loop. Call on a 10-30s cadence from a flush task.

        Crash-safe (hardened 2026-10-07 after an ArrowInvalid storm):
          * ATOMIC write -- serialise to a temp file, then os.replace onto
            the target. A process killed mid-write (asyncio task cancelled
            during shutdown, OOM, Ctrl-C) can only ever leave a stray .tmp,
            never a half-written target whose parquet footer points past
            the real bytes. The old in-place to_parquet produced exactly
            that: a 25KB file whose footer claimed 827MB, which then made
            EVERY subsequent flush raise on read and abort.
          * RECOVER from a corrupt existing file -- if the read of `old`
            fails, quarantine it (.corrupt-<ts>) and start the day's file
            fresh from the pending bars rather than wedging the flush loop
            forever.
          * KEEP pending bars until the write actually succeeds -- the old
            code cleared `_pending_disk` up front, so any exception dropped
            that cadence's bars on the floor.
        """
        if not self._persist or not self._pending_disk:
            return 0
        rows = [dataclasses.asdict(b) for b in self._pending_disk]
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts_s"], unit="s", utc=True)
        # One file per UTC hour: each flush re-reads and rewrites the file it
        # appends to, and a whole day's file reached 86ms per coin by 15:00
        # UTC (2026-10-09), on a thread that holds the trading loop's GIL.
        written = 0
        for hour, chunk in df.groupby(df["ts"].dt.floor("h")):
            path = self._persist_dir / bar_filename(self.coin, hour)
            if path.exists():
                try:
                    old = pd.read_parquet(path)
                    chunk = pd.concat([old, chunk], ignore_index=True)
                    chunk = chunk.drop_duplicates(subset=["ts_s"], keep="last")
                except Exception as e:
                    # Suffix deliberately does NOT end in .parquet so a
                    # later {coin}_*.parquet glob (load_composite_bars) can't
                    # match it and re-raise the same corruption.
                    quarantine = path.with_suffix(
                        f".parquet.corrupt-{int(time.time())}")
                    try:
                        os.replace(path, quarantine)
                    except OSError:
                        pass
                    print(f"[KXCRYPTOLEAD15M] composite: {path.name} unreadable "
                          f"({e!r}); quarantined to {quarantine.name}, "
                          f"starting a fresh file")
            tmp = path.with_suffix(f".tmp-{os.getpid()}.parquet")
            chunk.to_parquet(tmp, index=False)
            os.replace(tmp, path)   # atomic on the same filesystem
            written += len(chunk)
        # Only now that every date-chunk is durably written do we drop the
        # pending buffer.
        self._pending_disk.clear()
        return len(rows)


class CompositeBus:
    """Convenience: one `CompositeFeed` per coin, with a single sampling
    tick that advances them all. Routes incoming venue books to the
    right feed by `vb.coin`."""

    def __init__(self, coins=COINS, persist: bool = True,
                 persist_dir: Path = DATA_ROOT):
        self.feeds: dict[str, CompositeFeed] = {
            c: CompositeFeed(c, persist=persist, persist_dir=persist_dir) for c in coins
        }

    def observe(self, vb: VenueBook):
        feed = self.feeds.get(vb.coin)
        if feed is None:
            return
        feed.observe(vb)

    # Optional: callable(coin) -> deep VenueBook. Per-message books are thin
    # (spot_feed.coinbase.PER_MSG_LEVELS); the once-a-second bar re-reads
    # the deep view so the CF replica still sees its full depth.
    deep_book_fn = None

    def sample_all(self, now_s: float | None = None) -> dict[str, CompositeBar]:
        now_s = time.time() if now_s is None else now_s
        if self.deep_book_fn is not None:
            for c, f in self.feeds.items():
                if f._bar_last_second_s is None or int(now_s) > f._bar_last_second_s:
                    vb = self.deep_book_fn(c)
                    if vb is not None:
                        f._venue_books[vb.venue] = vb
        return {c: b for c, b in ((c, f.sample(now_s)) for c, f in self.feeds.items())
                if b is not None}

    def nowcast(self, coin: str):
        return self.feeds[coin].nowcast()

    def mid_nowcast(self, coin: str) -> float | None:
        return self.feeds[coin].mid_nowcast()

    def venue_books_per_coin(self) -> dict[str, list[VenueBook]]:
        return {c: f.venue_books() for c, f in self.feeds.items()}

    def flush_all(self) -> int:
        return sum(f.flush() for f in self.feeds.values())


__all__ = ["CompositeBar", "CompositeFeed", "CompositeBus"]
