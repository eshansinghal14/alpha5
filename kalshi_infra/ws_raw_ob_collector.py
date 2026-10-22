"""
ws_raw_ob_collector.py – Full L2 tick-level orderbook + trade logger,
across every currently open market of every configured series.

Subscribes to orderbook_delta and trade for every currently open market
across all expiries (events) of every configured series. Orderbook
snapshot/delta messages go to per-event parquet files under
raw_ob_ticks/, preserving full price-level depth so the exact book can be
reconstructed at any timestamp for backtesting; trade (executed fill)
messages go to separate per-event parquet files under trades/, since the
schema differs and the two channels have independent seq counters.

Keeps the full raw tick stream -- every price level, not just the touch --
so the exact book state can be reconstructed at any timestamp.

Usage:
    python -m kalshi_infra.ws_raw_ob_collector            # run until Ctrl+C
    python -m kalshi_infra.ws_raw_ob_collector 480         # run for 480 minutes then stop
"""

import asyncio
import ctypes
import datetime
import gc
import glob
import io
import json
import logging
import os
import sys
import time
import tracemalloc
import zlib

import boto3
import pandas as pd
import pyarrow as pa
import websockets
from botocore.exceptions import ClientError

from kalshi_infra.api_functions import (
    FIFTEEN_MIN_SERIES_TICKERS,
    HOURLY_SERIES_TICKERS,
    SP500_SERIES_TICKERS,
    kalshi_get,
)
from kalshi_infra.kalshi import auth

SERIES_TICKERS = FIFTEEN_MIN_SERIES_TICKERS + HOURLY_SERIES_TICKERS + SP500_SERIES_TICKERS

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
OUT_DIR_ROOT = os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_ws")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Local parquet stays the source of truth (read-modify-write, deduped) --
# S3 only gets an append-only copy of each flush's new rows as its own part
# file, so a live collector run never has to download/re-upload a
# growing multi-hour history just to add a few minutes of new rows.
S3_BUCKET = "kalshi-ws"
S3_PROFILE = "kalshi-ws-collector"
S3_REGION = "us-east-2"
S3_PREFIX = "kalshi_ws"


def _make_s3_client():
    """Uses the named local profile when it exists (laptop dev); otherwise
    falls back to the default credential chain, which on EC2 resolves to
    the instance's attached IAM role -- same code, no profile needed there."""
    session = boto3.Session()
    if S3_PROFILE in session.available_profiles:
        session = boto3.Session(profile_name=S3_PROFILE)
    return session.client("s3", region_name=S3_REGION)

logger = logging.getLogger("ws_raw_ob_collector")


def _setup_logging():
    """Console + persistent per-run file so overnight/unattended runs (like
    the reconnect churn from rescans hitting 429s) can be reviewed after the
    fact instead of only living in scrollback."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"ws_raw_ob_collector_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.info(f"Logging to {log_path}")


def _out_dir(event_ticker: str, kind: str = "raw_ob_ticks") -> str:
    """data/kalshi_ws/{series}/{kind}/ -- one subfolder per market type
    (series), `kind` is "raw_ob_ticks" (default) or "trades"."""
    series = event_ticker.split("-")[0]
    return os.path.join(OUT_DIR_ROOT, series, kind)


def _parts_dir(event_ticker: str, kind: str = "raw_ob_ticks") -> str:
    """data/kalshi_ws/{series}/{kind}/{event_ticker}_parts/ -- each
    autosave writes a fresh, uniquely-named part file here instead of
    ever reading+rewriting one growing per-event file. Confirmed directly
    (2026-08-25) the old always-merge design was the actual OOM cause: a
    single still-open event's file had grown to 4.5M rows (62MB on disk,
    632MB as an in-memory DataFrame -- a 10x expansion) after 24+ hours
    without closing, and every 5-minute autosave was reloading that whole
    thing just to add a handful of new rows. Parts only ever get merged
    once, at actual event close (see Collector.finalize_event_to_s3)."""
    return os.path.join(_out_dir(event_ticker, kind), f"{event_ticker}_parts")

SUBSCRIBE_BATCH = 50
# Confirmed directly (2026-08-26) as the leading suspect for a ~12-hour
# silent orderbook data gap on one event: a reconnect with ~1000+ already-
# known tickers fires ~20 back-to-back subscribe batches with zero pacing,
# and nothing checks Kalshi's response for a rejection (see the new
# catch-all in handle_message). A small gap between batches is cheap
# insurance against a per-connection subscribe-rate limit being the actual
# mechanism, whether or not that's confirmed as the exact cause.
SUBSCRIBE_BATCH_PACING_S = 0.2
# Confirmed directly (2026-08-31) that 5 minutes was too long a window to
# leave messages unflushed at current message volume: a fresh process hit
# ~91,500 buffered rows (and got OOM-killed) within under 4 minutes on a
# high-volatility Monday open, with buffered-row counts climbing this fast
# even without the added pressure of catching up after downtime. The
# unflushed buffer is the only thing standing between "normal operation"
# and "unbounded growth until OOM" -- autosave is what turns buffered
# Python objects into an on-disk part file and clears them from memory
# (see save_snapshot()), so shortening this directly caps the worst case
# regardless of how high the live message rate spikes.
AUTOSAVE_INTERVAL_S = 60
RESCAN_INTERVAL_S = 120  # was 15 min -- too coarse now that 15-min-expiry
# markets (KXBTC15M etc.) are subscribed too: a market with a 15-min
# lifecycle could open and close entirely between two 15-min-spaced
# rescans and never get subscribed or finalized at all. 120s bounds that
# miss window to ~13% of one market's life, worst case. Set higher than the
# 60s originally tried: with the hourly-series expansion, a real rescan
# across all ~59 series (~1700+ open markets, several series running wide
# strike ladders like KXINX's 58) measured ~45s and even hit a ReadTimeout
# once under retry load -- 60s left too little margin over that.
RECONNECT_DELAY_S = 5
# Kalshi only ever sends orderbook_snapshot on SUBSCRIBE -- there is no
# unsolicited periodic snapshot -- so a connection that never drops
# accumulates delta-only state indefinitely, and a delta-only ladder
# genuinely drifts (44+ minutes observed on a real ticker, see
# kalshi_backtest/book.py's L2Book docstring). Nothing else re-supplies the
# ground truth that book.to_quote's max_snapshot_age_s (1800s) demands, so
# without a timed reconnect every ladder goes UNTRUSTED 30 minutes after
# subscribe and stays that way -- the healthier the connection, the longer
# the blind stretch. Measured on real captures before this existed: 21% of
# KXINXU 2026-08-26's session and 17% of 2026-08-28's fell in that window.
# Well under 1800s even for the last-staggered shard.
RESYNC_INTERVAL_S = 900

# Confirmed directly (2026-08-26) via Kalshi's own docs: the account-wide cap
# is 5 concurrent WebSocket connections, and "Subscription buffer overflow"
# is a slow-consumer problem -- after any reconnect, Kalshi immediately floods
# the connection with a fresh orderbook_snapshot for every subscribed ticker,
# and if processing that burst falls behind, Kalshi's outbound buffer for the
# connection overflows and it kills the subscription. Splitting the single
# ~1200-ticker orderbook connection into shards cuts each connection's
# post-reconnect flood proportionally, directly reducing how often any one
# connection falls behind.
#
# Started at 3 shards; confirmed directly (2026-08-27) that was too many for
# this box -- sustained 85-91% memory usage for hours-long stretches, not
# brief spikes, and 3 separate OOM kills in one day. Each concurrent
# WebSocket connection carries real fixed overhead (protocol buffers, task
# stack, its own `subscribed` set) independent of how many tickers it
# carries, so more shards isn't free -- it trades reconnect-burst size
# against baseline memory. Dropped to 2 (+ 1 for the trade stream = 3 total,
# leaving 2 of the account's 5 connections spare rather than just 1) to cut
# that fixed overhead while still keeping meaningfully smaller
# post-reconnect bursts than the original single-connection design.
# Ticker->shard assignment must be a stable hash (not Python's
# randomized-per-process hash()) so the same ticker always lands on the same
# shard across rescans and reconnects.
NUM_ORDERBOOK_SHARDS = 2
# Offset each shard's periodic resync so they never resubscribe at the same
# moment: three near-simultaneous reconnects, each resubscribing 1000+
# tickers, tripped Kalshi's own subscription rate limit once already.
RESYNC_STAGGER_S = RESYNC_INTERVAL_S / NUM_ORDERBOOK_SHARDS


def _shard_of(ticker: str, num_shards: int = NUM_ORDERBOOK_SHARDS) -> int:
    return zlib.crc32(ticker.encode()) % num_shards


# Daily-duration wide-ladder series (KX*D) stay open a full day, so without
# this, all their local parts accumulate untouched until actual close --
# confirmed directly (2026-08-26) this produced the single largest merges
# seen (one KXBTCD event hit ~587K rows), and the biggest RSS spikes, since
# the whole day's data gets read/concat/sorted/deduped in one shot. Flushing
# these to hourly S3 chunks instead (still under the same event_ticker,
# suffixed by hour, not overwriting each other) bounds any one merge to
# roughly an hour's worth, and as a side benefit reduces how much is ever
# only-local: previously up to a full day at risk if the process died before
# real close, now at most the last hour.
DAILY_CHUNK_SERIES_TICKERS = {"KXBTCD", "KXETHD", "KXBNBD", "KXSOLD", "KXXRPD", "KXHYPED"}
DAILY_CHUNK_INTERVAL_S = 60 * 60


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _ws_auth_headers():
    return auth.ws_headers()


def _get_open_markets() -> list[dict]:
    """All open markets across every currently-trading expiry of every configured series.
    Paced across series to avoid tripping Kalshi's rate limit in the first place
    (confirmed directly that ~20 back-to-back per-series calls with no delay trips a
    real 429); kalshi_get itself now retries/backs off on 429 if one slips through.

    Follows `cursor` pagination within each series -- confirmed live
    (2026-08-22) this matters for real: 14 of the higher-strike-density
    hourly series (KXBTC/KXBTCD/KXETH/KXETHD/KXBNB/KXBNBD/KXSOLE/KXSOLD/
    KXXRP/KXXRPD/KXHYPE/KXHYPED/KXINXU/KXNASDAQ100U) currently have MORE
    than one page (>100) of open markets at once, and the earlier
    single-page version silently never subscribed to anything past the
    first page for those series. KXINX itself (this project's primary
    target) wasn't affected -- 60 open markets, well under one page -- but
    left unfixed this would have quietly undercounted a real fraction of
    every wide crypto/financial series' book depth."""
    all_markets = []
    for i, series in enumerate(SERIES_TICKERS):
        if i > 0:
            time.sleep(0.3)
        cursor = None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = kalshi_get("/trade-api/v2/markets", params=params, auth=True)
            all_markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
    return all_markets


# Wide-ladder series only -- confirmed directly (2026-08-24) these can have
# 2000+ simultaneously open strikes with the vast majority (2798/2800 for
# KXNASDAQ100U in that check) at exactly zero cumulative volume ever, since
# most strikes on a wide ladder are far enough from the money that nobody
# ever trades them. Every other series (temp markets, 15-min directionals,
# hourly hi/lo, etc.) has few enough simultaneous markets that this
# problem doesn't apply, and those keep full from-open tracking regardless
# of volume -- see _filter_wide_ladder_series() for why scoping matters.
WIDE_LADDER_SERIES_TICKERS = {
    "KXBTC", "KXBTCD", "KXETH", "KXETHD", "KXBNB", "KXBNBD", "KXSOLE", "KXSOLD",
    "KXXRP", "KXXRPD", "KXHYPE", "KXHYPED", "KXINXU", "KXNASDAQ100U", "KXINX",
}
MAX_TRACKED_PER_WIDE_LADDER_SERIES = 100

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def _release_memory():
    """Confirmed directly (2026-08-26) that RSS climbs during heavy merge
    activity (startup orphan sweep, large single-event finalizes) and only
    drains back down gradually over hours rather than immediately -- glibc's
    allocator doesn't always return freed heap to the OS promptly, especially
    after the kind of mixed small/large allocation churn a pandas
    concat/sort/dedup chain produces. gc.collect() clears cyclic garbage;
    malloc_trim(0) is the actual fix -- it asks glibc to unmap/return freed
    arenas to the OS immediately. No-op if libc isn't glibc (e.g. not on
    Linux) since malloc_trim is a glibc-specific extension.

    Confirmed directly (2026-08-31) that this pair alone wasn't enough during
    a severe live incident: RSS hit 1.5GB+ within minutes while tracemalloc
    showed under 60MB of actual tracked Python-object growth -- the
    remaining ~1.4GB was invisible to both tracemalloc and malloc_trim
    because it wasn't glibc's allocator at all. pyarrow's default memory
    pool (used internally by every to_parquet()/read_parquet() call, i.e.
    every save/merge/finalize) runs on its own separate allocator --
    mimalloc on this box, confirmed via pa.default_memory_pool().backend_name
    -- which caches freed memory for reuse rather than returning it to the
    OS, completely independent of glibc. release_unused() is the
    documented way to force that pool to give back what it isn't using;
    without it, the parquet-heavy workload this whole process is built
    around had a second, larger, entirely separate leak-shaped hole that
    the original glibc-only fix could never have closed."""
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)
    pa.default_memory_pool().release_unused()


def _normalize_part_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Old part files (written before the exchange_tx/receive_tx split,
    including everything migrated from the pre-redesign flat-file layout
    on 2026-08-25) have 'time'/'ts_ms' instead. Confirmed directly this
    needed handling: without it, merging a still-around old-format part
    alongside newer ones crashed on sort_values("receive_tx") with a
    KeyError, for every one of thousands of orphaned events accumulated
    over the prior several days of bugs. Backfills both new columns from
    the old 'time' value so old and new parts can merge -- there's no way
    to retroactively recover the real exchange-vs-receive split for
    already-collected data, so both just get the same single timestamp
    that was recorded at the time."""
    if "receive_tx" not in df.columns:
        df["receive_tx"] = df.get("time")
        df["exchange_tx"] = df.get("time")
        df = df.drop(columns=[c for c in ("time", "ts_ms") if c in df.columns])
    return df


def _local_events_with_data(kind: str = "raw_ob_ticks") -> set[str]:
    """Every event_ticker with at least one local part file under this
    kind, found by scanning disk directly rather than any in-memory
    tracking. Used at startup to catch events that closed entirely
    *before* this process started (see run()) -- close_time-based
    detection can't help there since a fresh process never learns about
    an event it never saw in the currently-open list at all; comparing
    local disk against that same fresh open-list is the only way to
    catch it, and doesn't need to know anything about *when* it closed,
    only that it isn't open anymore."""
    found = set()
    for parts_dir in glob.glob(os.path.join(OUT_DIR_ROOT, "*", kind, "*_parts")):
        if glob.glob(os.path.join(parts_dir, "*.parquet")):
            found.add(os.path.basename(parts_dir).removesuffix("_parts"))
    return found


def _filter_wide_ladder_series(markets: list[dict]) -> list[dict]:
    """Keeps every market from a non-wide-ladder series untouched (full
    from-open tracking, same as before). For wide-ladder series, keeps
    only markets with any cumulative trading volume at all (volume_fp >
    0), with a MAX_TRACKED_PER_WIDE_LADDER_SERIES safety cap (highest
    volume first) in case a series unexpectedly has more simultaneously-
    active markets than that.

    Deliberately NOT applied globally: volume_fp is cumulative since the
    market opened, so every market starts at 0 -- filtering on it means a
    market goes untracked until its first trade clears (at most one
    RESCAN_INTERVAL_S late, since register_markets() re-derives this every
    rescan). That's an acceptable tradeoff for wide-ladder strikes that
    mostly never trade at all, but would be a real regression for
    single/few-market-per-event series (temp markets, KXBTC15M, etc.)
    where every market matters and losing the pre-first-trade quote
    history isn't worth it.

    Only ever used to decide what to *subscribe* to (register_markets()).
    update_open_events() must keep using the full, unfiltered market list
    -- finalize_event_to_s3() always overwrites the same S3 key on the
    assumption that a "closed" event is genuinely done, so if this filter
    fed into closure detection too, a market merely going quiet (not
    actually closed) would trigger a premature finalize+delete, and if it
    later traded again the next finalize would silently overwrite and
    destroy the earlier data already sitting in S3."""
    kept = []
    by_wide_series: dict[str, list[dict]] = {}
    for m in markets:
        series = m["ticker"].split("-")[0]
        if series not in WIDE_LADDER_SERIES_TICKERS:
            kept.append(m)
        else:
            by_wide_series.setdefault(series, []).append(m)

    for series_markets in by_wide_series.values():
        active = [m for m in series_markets if float(m.get("volume_fp") or 0) > 0]
        if len(active) > MAX_TRACKED_PER_WIDE_LADDER_SERIES:
            active.sort(key=lambda m: float(m.get("volume_fp") or 0), reverse=True)
            active = active[:MAX_TRACKED_PER_WIDE_LADDER_SERIES]
        kept.extend(active)
    return kept


# ---------------------------------------------------------------------------
# Collector: buffers raw messages, dedupes, and flushes to per-event parquet
# ---------------------------------------------------------------------------

class _SeqTracker:
    """Per-connection seq-gap tracking. Deliberately NOT shared Collector
    state: seq is scoped to the connection (confirmed against real captured
    data -- it's ONE counter shared across every subscribed ticker's
    messages on that connection, not per-market, and it restarts on
    reconnect). With multiple concurrent orderbook shard connections (see
    NUM_ORDERBOOK_SHARDS), each shard has its own independent seq numbering
    from Kalshi -- sharing one tracker across shards would corrupt gap
    detection by comparing unrelated connections' seq values against each
    other."""
    def __init__(self):
        self.last_seq: int | None = None
        self.n_gap_events = 0
        self.n_dropped_messages = 0

    def reset(self):
        self.last_seq = None

    def note(self, seq: int | None) -> bool:
        """Kalshi does not guarantee no missed WS messages -- seq exists
        specifically so clients can detect drops (see docs.kalshi.com
        websockets/orderbook-updates). Returns True if this message reveals a
        gap (one or more messages missed) since the last one seen this
        connection. Confirmed this isn't theoretical: a real ~9-hour capture
        today showed 9 messages actually missing out of ~108,865, in 7
        separate gap events."""
        if seq is None:
            return False
        gap = self.last_seq is not None and seq != self.last_seq + 1
        if gap:
            self.n_gap_events += 1
            self.n_dropped_messages += seq - self.last_seq - 1
        self.last_seq = seq
        return gap


class Collector:
    def __init__(self):
        self.market_meta: dict[str, dict] = {}    # ticker -> {event_ticker, floor_strike, cap_strike}
        self.records: dict[str, list[dict]] = {}   # event_ticker -> buffered new orderbook rows
        self.trade_records: dict[str, list[dict]] = {}  # event_ticker -> buffered new trade rows
        self._s3_client = _make_s3_client()
        self.open_events: set[str] = set()  # event_tickers seen open as of the last rescan
        self.event_close_time: dict[str, datetime.datetime] = {}  # event_ticker -> Kalshi's own close_time

    def register_markets(self, markets: list[dict]) -> list[str]:
        """Add newly-seen markets. Returns tickers not previously known.

        Skips only markets with NEITHER floor_strike NOR cap_strike (e.g.
        KXDOGE's verbal/custom strikes -- see api_functions.py's
        HOURLY_SERIES_TICKERS docstring) -- not just floor_strike alone.
        Confirmed live against KXINX (2026-08-22): its 'less'-type tail
        markets (e.g. KXINX-26AUG28H1600-T7275) have floor_strike=None with
        a real cap_strike, the same 'greater'/'less'/'between' shape
        bucket_probabilities elsewhere in this project already handles --
        the old floor_strike-is-None check silently dropped every one of
        these from collection."""
        new_tickers = []
        for m in markets:
            ticker = m["ticker"]
            floor_strike, cap_strike = m.get("floor_strike"), m.get("cap_strike")
            if ticker in self.market_meta or (floor_strike is None and cap_strike is None):
                continue
            self.market_meta[ticker] = {
                "event_ticker": m["event_ticker"],
                "floor_strike": float(floor_strike) if floor_strike is not None else float("nan"),
                "cap_strike": float(cap_strike) if cap_strike is not None else float("nan"),
            }
            self.records.setdefault(m["event_ticker"], [])
            self.trade_records.setdefault(m["event_ticker"], [])
            if m.get("event_ticker") not in self.event_close_time and m.get("close_time"):
                # Kalshi's own authoritative close_time for this event --
                # deliberately not derived by parsing the event_ticker's
                # embedded date/time ourselves (checked live (2026-08-25):
                # a KXBTC15M ticker's embedded "...0300" didn't cleanly
                # match its own close_time of 07:00:00Z, so per-series
                # duration assumptions -- 15min/1hr/daily -- aren't safe to
                # hand-derive; this field is exact and needs no per-series
                # special-casing).
                self.event_close_time[m["event_ticker"]] = pd.Timestamp(m["close_time"]).to_pydatetime()
            new_tickers.append(ticker)
        return new_tickers

    def handle_trade_message(self, d: dict):
        """Buffers one 'trade' message. Called only from the dedicated
        trade WS connection (run_trade_stream()) -- the orderbook
        connection no longer subscribes to "trade" at all. This split
        exists because sharing one connection between orderbook_delta and
        trade corrupted orderbook seq/gap tracking in production
        (confirmed directly: backward jumps like "1842 -> 280" that
        persisted even restricted to a single series, with sid-allocation
        timing, a hidden trade-channel message type, and Kalshi-side
        trade-seq corruption all ruled out as the cause via isolated
        testing) -- the corruption disappeared entirely once the two
        channels stopped sharing a connection, so this keeps them apart
        rather than resolving the exact mechanism."""
        msg = d.get("msg", {})
        ticker = msg.get("market_ticker")
        meta = self.market_meta.get(ticker)
        if meta is None:
            return
        ts_ms = msg.get("ts_ms")
        self.trade_records[meta["event_ticker"]].append({
            "ticker": ticker,
            "event_ticker": meta["event_ticker"],
            "floor_strike": meta["floor_strike"],
            "cap_strike": meta["cap_strike"],
            "trade_id": msg.get("trade_id"),
            "seq": d.get("seq"),
            # exchange_tx: when Kalshi says the trade executed (trade
            # messages carry no ISO "ts" string like orderbook does --
            # only the ts/ts_ms epoch fields -- so build one from ts_ms).
            # receive_tx: when *we* received this message -- these can and
            # do differ (network/processing latency), which is exactly why
            # both get recorded rather than collapsing to one "time".
            "exchange_tx": datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).isoformat(),
            "receive_tx": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "yes_price": float(msg["yes_price_dollars"]),
            "no_price": float(msg["no_price_dollars"]),
            "count": float(msg["count_fp"]),
            "taker_side": msg.get("taker_side"),
            "taker_outcome_side": msg.get("taker_outcome_side"),
            "taker_book_side": msg.get("taker_book_side"),
            "is_block_trade": msg.get("is_block_trade"),
        })

    def handle_message(self, d: dict, seq_tracker: _SeqTracker) -> bool:
        """Returns True if this message revealed a seq gap -- or the WS
        reported an error/unsubscribe -- meaning the caller should force a
        reconnect+resubscribe to resync (see run_orderbook_shard()). Only
        ever receives orderbook_snapshot/orderbook_delta -- the orderbook
        connections don't subscribe to "trade" (see handle_trade_message()).
        `seq_tracker` is per-connection (see _SeqTracker) -- with multiple
        concurrent orderbook shards, each shard passes its own tracker
        instance, never a shared one."""
        mtype = d.get("type")
        msg = d.get("msg", {})
        seq = d.get("seq")
        prev_seq = seq_tracker.last_seq
        gap = seq_tracker.note(seq)
        if gap:
            logger.warning(f"seq gap: {prev_seq} -> {seq} ({seq - prev_seq - 1} message(s) missed; "
                  f"{seq_tracker.n_dropped_messages} total across {seq_tracker.n_gap_events} gaps so far this connection) "
                  f"-- forcing reconnect to resync via fresh snapshots")

        if mtype == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            meta = self.market_meta.get(ticker)
            if meta is None:
                return gap
            # Confirmed directly (2026-08-25): unlike orderbook_delta and
            # trade, Kalshi's orderbook_snapshot payload carries no
            # ts/ts_ms field at all (msg keys are just market_ticker,
            # market_id, yes_dollars_fp, no_dollars_fp) -- every snapshot
            # row was silently getting ts_ms=NaN/time=NaT before this fix.
            # There's genuinely no exchange_tx to record here (Kalshi
            # doesn't stamp snapshots), so only receive_tx is set -- a
            # snapshot is inherently "the state as of right now" from the
            # receiver's vantage anyway.
            receive_tx = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for side_key, side in (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no")):
                levels = msg.get(side_key) or []
                self.records[meta["event_ticker"]].append({
                    "ticker": ticker,
                    "event_ticker": meta["event_ticker"],
                    "floor_strike": meta["floor_strike"],
                    "cap_strike": meta["cap_strike"],
                    "msg_type": "snapshot",
                    "seq": seq,
                    "exchange_tx": None,
                    "receive_tx": receive_tx,
                    "side": side,
                    "price": None,
                    "delta": None,
                    "levels_json": json.dumps([[float(p), float(q)] for p, q in levels]),
                })

        elif mtype == "orderbook_delta":
            ticker = msg.get("market_ticker")
            meta = self.market_meta.get(ticker)
            if meta is None:
                return gap
            self.records[meta["event_ticker"]].append({
                "ticker": ticker,
                "event_ticker": meta["event_ticker"],
                "floor_strike": meta["floor_strike"],
                "cap_strike": meta["cap_strike"],
                "msg_type": "delta",
                "seq": seq,
                "exchange_tx": msg.get("ts"),  # Kalshi's own timestamp for this update
                "receive_tx": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # when we got it
                "side": msg.get("side"),
                "price": float(msg["price_dollars"]),
                "delta": float(msg["delta_fp"]),
                "levels_json": None,
            })

        elif mtype in ("error", "unsubscribed"):
            # Confirmed directly (2026-08-26): caught live via the logging
            # fix below -- three reconnects within ~3 minutes (each having
            # to resubscribe 1000+ tickers) tripped Kalshi's own rate limit:
            # {'type': 'error', 'msg': {'code': 25, 'msg': 'Subscription
            # buffer overflow'}} immediately followed by {'type':
            # 'unsubscribed'}. This is the actual mechanism behind the
            # original Aug 25 incident (12h of silently missing orderbook
            # data for one event) -- Kalshi kills the whole channel
            # subscription server-side, and neither event closes the
            # connection or raises an exception, so without this the process
            # just idles forever on a dead subscription. Returning True
            # reuses the same "force reconnect + fresh resubscribe" path as
            # a seq gap (see run_orderbook_shard()) -- self-heals within
            # RECONNECT_DELAY_S instead of staying silent indefinitely.
            logger.warning(f"WS reported {mtype!r} -- forcing reconnect to resync: {d}")
            return True

        elif mtype not in ("subscribed", "ok"):
            # Confirmed directly (2026-08-26): a whole event's orderbook data
            # went silent for ~12 hours mid-day with the connection staying
            # up the entire time (no disconnect, no seq gap) -- and Kalshi's
            # own candlesticks proved that market's book was genuinely
            # active that whole window, so something server-side almost
            # certainly rejected/dropped part of a subscribe request (most
            # likely candidate: the ~21-batch, no-pacing resubscribe burst
            # this same day, right before the gap started) and we never
            # would have known, because every message type other than the
            # two handled above was being silently discarded here with zero
            # trace. Logging anything unrecognized -- errors, rejections,
            # whatever Kalshi's subscribe responses actually look like --
            # means the *next* occurrence shows up immediately instead of
            # being invisible until someone notices missing data hours
            # later. "subscribed"/"ok" are excluded as the expected
            # successful-ack shape so this doesn't spam every reconnect.
            logger.warning(f"unhandled WS message type {mtype!r}: {d}")

        return gap

    def update_open_events(self, markets: list[dict]) -> list[str]:
        """Call with the fresh result of a `_get_open_markets()` rescan.
        Returns event_tickers that were open as of the previous rescan but
        are no longer open now -- i.e. that market's trading day just
        finished -- so the caller can finalize them to S3. The very first
        call just seeds the baseline and never reports anything closed."""
        current = {m["event_ticker"] for m in markets}
        newly_closed = [e for e in self.open_events if e not in current]
        self.open_events = current
        return newly_closed

    def find_expired_events(self, grace: datetime.timedelta = datetime.timedelta(minutes=10)) -> list[str]:
        """Returns event_tickers whose Kalshi-reported close_time (see
        register_markets()) is already in the past, among events this
        process still has state for. This is a restart-independent
        safety net alongside update_open_events()'s diff-based detection:
        that diff only works by noticing an event vanish from the
        *current* open-markets list relative to *this process's own*
        in-memory baseline -- a baseline that resets to empty on every
        restart. An event that fully closes during a crash/restart gap
        was never in the new process's baseline to begin with, so the
        diff can never catch it (confirmed directly this happens: several
        KXBTCD hourly events sat orphaned, still "open" as far as their
        process's own bookkeeping was concerned, for days). Checking
        against Kalshi's own close_time instead doesn't depend on having
        watched the event go from open to closed -- it just needs to have
        registered the event at all, at any point, including right at
        this process's own startup. `grace` allows for settlement/API
        propagation lag before treating a market as truly done."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return [e for e, close_time in self.event_close_time.items() if now > close_time + grace]

    def forget_event(self, event_ticker: str):
        """Drops all in-memory state for a genuinely-closed event --
        market_meta entries for its tickers, and its buffered-rows dict
        keys. Without this, those accumulate for the entire life of the
        process, even for markets that permanently closed days ago -- a
        real contributor to slow unbounded memory growth on a
        long-running collector. Safe: handle_message()/
        handle_trade_message() already no-op on an unknown ticker (meta
        is None), so a late/stray message for a forgotten ticker is just
        dropped, not a crash. Only call this for events confirmed closed
        via the *unfiltered* market list (see _filter_wide_ladder_series
        for why) -- never for one that merely stopped trading."""
        self.records.pop(event_ticker, None)
        self.trade_records.pop(event_ticker, None)
        self.event_close_time.pop(event_ticker, None)
        stale_tickers = [t for t, meta in self.market_meta.items() if meta["event_ticker"] == event_ticker]
        for t in stale_tickers:
            del self.market_meta[t]

    def finalize_event_to_s3(
        self,
        event_ticker: str,
        kind: str = "raw_ob_ticks",
        skip_if_exists: bool = False,
        delete_local_after_upload: bool = False,
        key_suffix: str = "",
    ) -> bool:
        """Merges every part file currently on disk for an event into one
        deduped DataFrame and uploads it as a single S3 object, keyed by
        event_ticker (+ `key_suffix`) so re-running just overwrites in
        place. `kind` selects "raw_ob_ticks" (default) or "trades" --
        same lifecycle, separate files/S3 keys since the schemas differ.
        For most events this merge-many-parts-into-one only ever happens
        once, at actual close -- see _parts_dir() for why that matters.
        The exception is the daily-duration "D" series (see
        DAILY_CHUNK_SERIES_TICKERS): those get called periodically through
        the day too, with `key_suffix` set to an hour tag so each interim
        flush lands at its own S3 key rather than overwriting/growing one
        big file -- the event stays open and keeps accumulating fresh
        local parts afterward exactly as if this had never run. Returns
        whether an upload happened. `skip_if_exists` lets a manual re-run
        avoid re-uploading days already pushed up (checked before doing the
        local merge, so it's cheap to skip).

        `delete_local_after_upload` removes the local part files, but only
        after a confirmed-successful upload -- never on a failed/skipped
        one, so a local copy is never lost without S3 actually having it.
        Meant for the live collector on EC2, where local disk is small and
        S3 is already the durable copy once a market has settled; the
        standalone manual-upload script and one-off migrations leave local
        files alone (default False) since those typically run from a
        laptop with disk to spare."""
        parts_dir = _parts_dir(event_ticker, kind)
        part_files = sorted(glob.glob(os.path.join(parts_dir, "*.parquet")))
        if not part_files:
            return False
        series = event_ticker.split("-")[0]
        key = f"{S3_PREFIX}/{series}/{kind}/{event_ticker}/{event_ticker}{key_suffix}.parquet"
        if skip_if_exists:
            try:
                self._s3_client.head_object(Bucket=S3_BUCKET, Key=key)
                return False  # already uploaded
            except ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    raise

        dedup_subset = ["ticker", "trade_id"] if kind == "trades" else ["ticker", "msg_type", "seq", "side"]
        # Everything from here on builds full DataFrames (potentially large
        # -- the biggest single events seen so far ran ~600K rows), so it's
        # wrapped in try/finally with _release_memory() below: confirmed
        # directly (2026-08-26) that RSS climbs during heavy merge activity
        # and only drains back down gradually over hours rather than
        # immediately once these temporaries are dereferenced, so this
        # proactively forces glibc to hand freed heap back to the OS after
        # every merge attempt rather than waiting on it.
        try:
            # Reads each part individually and skips any that fail (missing --
            # e.g. deleted out from under a concurrent scan by another
            # finalize -- or genuinely corrupted, e.g. a kill mid-write of
            # that one part) rather than one bad part blocking the *entire*
            # event's finalize forever. Confirmed directly this gap was real,
            # if not yet costly: a diagnostic scan of 7370 local parts found
            # 132 "failures" that were only a read racing a concurrent delete,
            # not real corruption -- but a single genuinely bad part hitting
            # this same all-or-nothing path would have been indistinguishable
            # and permanently stuck that event.
            dfs = []
            for p in part_files:
                try:
                    dfs.append(_normalize_part_schema(pd.read_parquet(p)))
                except Exception as e:
                    logger.warning(f"skipping unreadable part {p}: {e}")
            if not dfs:
                logger.warning(f"no readable parts for {event_ticker} ({kind}) out of {len(part_files)} found")
                return False
            try:
                merged = pd.concat(dfs, ignore_index=True)
                # Sorts by receive_tx, not exchange_tx -- exchange_tx is always
                # null for orderbook_snapshot rows (Kalshi doesn't timestamp
                # those), while receive_tx is set on every row and is
                # guaranteed non-decreasing within our own single-threaded
                # per-message processing order, so it's the reliable ordering
                # key across a merge. exchange_tx stays in the data for anyone
                # who wants exchange-time ordering instead.
                merged = (
                    merged.sort_values("receive_tx")
                    .drop_duplicates(subset=dedup_subset)
                    .reset_index(drop=True)
                )
            except Exception as e:
                logger.warning(f"could not merge {len(dfs)} readable part(s) for {event_ticker} ({kind}): {e}")
                return False

            try:
                buf = io.BytesIO()
                merged.to_parquet(buf, index=False)
                self._s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())
                skipped_note = f", {len(part_files) - len(dfs)} skipped" if len(dfs) < len(part_files) else ""
                logger.info(
                    f"Merged {len(dfs)}/{len(part_files)} part(s){skipped_note} ({len(merged)} rows) -> uploaded s3://{S3_BUCKET}/{key}"
                )
            except Exception as e:
                logger.warning(f"S3 upload failed for {event_ticker} ({kind}): {e}")
                return False

            if delete_local_after_upload:
                for p in part_files:
                    try:
                        os.remove(p)
                    except OSError as e:
                        logger.warning(f"could not delete part {p}: {e}")
                try:
                    os.rmdir(parts_dir)
                except OSError:
                    pass  # not empty (a new part landed mid-finalize) or already gone -- fine either way
                logger.info(f"Deleted local parts for {event_ticker} ({kind}, settled, confirmed in S3)")
            return True
        finally:
            _release_memory()

    @staticmethod
    def _swap_buffer(buffer: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """Atomically swaps out a buffered-records dict (self.records or
        self.trade_records) for a fresh one with the same keys. Returns
        (snapshot, fresh_replacement) -- caller reassigns the source
        attribute to fresh_replacement. Cheap dict/list allocation only (no
        I/O) -- safe to call directly from the event loop. The snapshot can
        then be handed to save_snapshot() in a background thread without
        racing concurrent handle_message() appends: new messages land in
        the fresh dict, never in the snapshot."""
        return buffer, {k: [] for k in buffer}

    def _snapshot_and_clear(self) -> dict[str, list[dict]]:
        """Orderbook-only convenience wrapper around _swap_buffer() for
        callers that don't also need the trade buffer (e.g. save_all())."""
        snapshot, self.records = self._swap_buffer(self.records)
        return snapshot

    def save_snapshot(
        self,
        snapshot: dict[str, list[dict]],
        kind: str = "raw_ob_ticks",
        dedup_subset: list[str] | None = None,
    ):
        """Writes each event's new rows as a fresh, uniquely-named part
        file in _parts_dir() -- no read of any existing data, so this
        never re-loads a growing history into memory. Operates only on
        the passed-in snapshot, never touches self.records, so it's safe
        to run via asyncio.to_thread() while the event loop concurrently
        keeps buffering new messages for the *next* save.

        This replaced an always-merge-into-one-file design (confirmed
        directly (2026-08-25) that was the actual OOM cause even after
        eliminating every synchronous-blocking-call issue: a single
        still-open event's file had grown to 4.5M rows over 24+ hours
        without closing -- 62MB on disk, but 632MB (10x) as an in-memory
        DataFrame -- and every 5-minute autosave was reloading that whole
        thing just to append a handful of new rows). Parts get merged
        into one deduped file only once, at actual event close -- see
        finalize_event_to_s3()."""
        dedup_subset = dedup_subset or ["ticker", "msg_type", "seq", "side"]
        for event_ticker, new_recs in snapshot.items():
            if not new_recs:
                continue

            new_df = pd.DataFrame(new_recs)
            # Kalshi's ts strings aren't consistently formatted -- some carry
            # fractional seconds, some don't (confirmed directly: this crashed
            # a live save when pandas inferred a %f format from early rows
            # then hit a whole-second "...50Z" row later in the same batch).
            # format="ISO8601" parses each value on its own terms instead of
            # locking to one inferred format for the whole column. Handles
            # exchange_tx's None entries (orderbook_snapshot rows, which
            # Kalshi never timestamps) as NaT automatically.
            new_df["exchange_tx"] = pd.to_datetime(new_df["exchange_tx"], utc=True, format="ISO8601")
            new_df["receive_tx"] = pd.to_datetime(new_df["receive_tx"], utc=True, format="ISO8601")
            # A single flush can carry duplicate rows on its own (e.g. a
            # seq-gap-triggered reconnect re-snapshots everything), so still
            # dedupe within this one part even though cross-part dedup only
            # happens at merge time.
            new_df = new_df.drop_duplicates(subset=dedup_subset).reset_index(drop=True)

            parts_dir = _parts_dir(event_ticker, kind)
            os.makedirs(parts_dir, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
            part_path = os.path.join(parts_dir, f"{event_ticker}_{ts}.parquet")
            new_df.to_parquet(part_path, index=False)
            logger.info(f"Saved {len(new_df)} new rows -> {part_path}")

    def save_all(self):
        """Synchronous snapshot+save (both orderbook and trade buffers) in
        one call -- fine to use directly when blocking doesn't matter (e.g.
        final shutdown, where nothing else needs the event loop anymore).
        For the live per-message loop, use _swap_buffer() +
        asyncio.to_thread(save_snapshot, ...) instead."""
        ob_snapshot, self.records = self._swap_buffer(self.records)
        trade_snapshot, self.trade_records = self._swap_buffer(self.trade_records)
        self.save_both_snapshots(ob_snapshot, trade_snapshot)

    def save_both_snapshots(self, ob_snapshot: dict[str, list[dict]], trade_snapshot: dict[str, list[dict]]):
        """Saves an orderbook snapshot and a trade snapshot together --
        the pairing every autosave/gap-flush needs. Meant to be called via
        asyncio.to_thread from the live loop.

        Confirmed directly (2026-08-31) that _release_memory() was only
        ever called from finalize_event_to_s3() -- fine when that ran
        often, but on a high-message-rate day autosave (this function) is
        what actually runs most often, and it was never handing freed heap
        back to the OS. Over many cycles that let RSS climb until OOM, in
        one case within ~2.5 minutes of a fresh process start. Called here
        so every path through this function (regular autosave, gap-flush,
        daily-chunk flush, final shutdown save) gets it, not just closes."""
        self.save_snapshot(ob_snapshot, kind="raw_ob_ticks", dedup_subset=["ticker", "msg_type", "seq", "side"])
        self.save_snapshot(trade_snapshot, kind="trades", dedup_subset=["ticker", "trade_id"])
        _release_memory()

    def flush_and_finalize(
        self,
        ob_snapshot: dict[str, list[dict]],
        trade_snapshot: dict[str, list[dict]],
        event_tickers: list[str],
    ):
        """Runs a save (both kinds) + the per-event S3 finalize (upload,
        then delete local) for a batch of newly-closed events, for both
        orderbook and trade data. Meant to be called via asyncio.to_thread
        since every step here is blocking I/O (local parquet read/write,
        S3 upload) and finalize_event_to_s3() is called twice per closed
        event (once per kind) -- with 15-min markets, several events can
        close in the same rescan cycle.

        Deliberately does NOT call forget_event() here -- confirmed
        directly (2026-08-26) that it used to, and crashed the whole
        process with "dictionary changed size during iteration": this
        method runs via to_thread on a real background OS thread, and
        forget_event() deletes from collector.market_meta, which the
        orderbook shard tasks (see run_orderbook_shard()) concurrently
        iterate on the main event-loop thread -- a genuine cross-thread
        race, not just cooperative-async interleaving, so wrapping it in
        to_thread was never actually safe. The caller must call
        forget_event() itself afterward, synchronously on the main thread,
        once this to_thread call returns."""
        self.save_both_snapshots(ob_snapshot, trade_snapshot)
        for event_ticker in event_tickers:
            self.finalize_event_to_s3(event_ticker, kind="raw_ob_ticks", delete_local_after_upload=True)
            self.finalize_event_to_s3(event_ticker, kind="trades", delete_local_after_upload=True)


# ---------------------------------------------------------------------------
# Async collection loop (reconnects on drop, rescans for new expiries)
# ---------------------------------------------------------------------------

async def _sweep_orphaned_events(collector: Collector, current_open: set[str]):
    """Background task, started once at process startup: finalizes any
    local event data not in the currently-open-markets set (see run()).
    Awaits each event's finalize calls individually rather than wrapping
    the whole sweep in one to_thread call -- confirmed directly this
    mattered: a single-call version blocked the event loop for its
    entire duration, delaying live collection from starting until the
    whole sweep finished (over an hour, with the 9500+ orphaned events
    that had accumulated from several days of prior bugs). Awaiting one
    event at a time instead lets it interleave with normal WS/autosave
    work, and makes it cleanly cancellable between events on shutdown."""
    orphaned = set()
    for kind in ("raw_ob_ticks", "trades"):
        orphaned |= _local_events_with_data(kind) - current_open
    if not orphaned:
        return
    logger.info(f"[startup] {len(orphaned)} orphaned event(s) from a prior run -> finalizing to S3 in the background")
    finalized = 0
    for event_ticker in orphaned:
        await asyncio.to_thread(collector.finalize_event_to_s3, event_ticker, "raw_ob_ticks", True, True)
        await asyncio.to_thread(collector.finalize_event_to_s3, event_ticker, "trades", True, True)
        finalized += 1
        if finalized % 500 == 0:
            logger.info(f"[startup] orphan sweep progress: {finalized}/{len(orphaned)}")
    logger.info(f"[startup] orphan sweep complete: {finalized} event(s) processed.")


async def run_trade_stream(collector: Collector):
    """Independent WS connection subscribed only to "trade" -- deliberately
    never shares a connection with the orderbook stream (see
    Collector.handle_trade_message() for why). Subscribes reactively: on
    each loop tick it diffs collector.market_meta (kept current by the
    orderbook stream's own rescans) against what it's already subscribed
    to and sends subscribe commands for anything new, rather than doing
    its own REST rescans -- avoids duplicating that REST load and avoids
    two tasks racing to update shared collector state.

    Trade data is supplementary here, not required for full-reconstruction
    correctness the way orderbook is, so a seq gap on this stream is only
    logged, never used to force a reconnect. Buffered trade_records get
    saved/finalized by the orderbook stream's existing autosave/rescan
    cycle, not duplicated here -- this loop only ever appends to the
    buffer."""
    while True:
        try:
            async with websockets.connect(
                WS_URL, additional_headers=_ws_auth_headers(), open_timeout=15
            ) as ws:
                subscribed: set[str] = set()
                last_seq: int | None = None
                sub_id = 1
                logger.info("[trade] connected.")

                while True:
                    new_tickers = [t for t in collector.market_meta if t not in subscribed]
                    if new_tickers:
                        for i in range(0, len(new_tickers), SUBSCRIBE_BATCH):
                            batch = new_tickers[i : i + SUBSCRIBE_BATCH]
                            await ws.send(json.dumps({
                                "id": sub_id,
                                "cmd": "subscribe",
                                "params": {"channels": ["trade"], "market_tickers": batch},
                            }))
                            sub_id += 1
                            if i + SUBSCRIBE_BATCH < len(new_tickers):
                                await asyncio.sleep(SUBSCRIBE_BATCH_PACING_S)
                        subscribed.update(new_tickers)
                        logger.info(f"[trade] subscribed to {len(new_tickers)} new ticker(s), {len(subscribed)} total")

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue

                    d = json.loads(raw)
                    if d.get("type") != "trade":
                        continue
                    seq = d.get("seq")
                    if last_seq is not None and seq is not None and seq != last_seq + 1:
                        logger.warning(
                            f"[trade] seq gap: {last_seq} -> {seq} "
                            f"(informational only -- not forcing a reconnect)"
                        )
                    last_seq = seq
                    collector.handle_trade_message(d)

        except (websockets.exceptions.WebSocketException, OSError) as e:
            # Confirmed directly (2026-08-27): a bare ConnectionClosed catch
            # missed websockets.exceptions.InvalidStatus (Kalshi returning an
            # HTTP 500 during the handshake, a transient server-side hiccup)
            # -- that's a distinct exception type raised before a connection
            # is ever established, not a subclass of ConnectionClosed, so it
            # propagated uncaught and crashed the whole process over what
            # should have just been a retry. WebSocketException is the
            # library's base class for everything it raises (ConnectionClosed,
            # InvalidStatus, InvalidHandshake, InvalidURI, ...), so catching
            # that instead closes this class of gap generally, not just this
            # one instance of it.
            logger.warning(f"[trade] websocket disconnected: {e} - reconnecting in {RECONNECT_DELAY_S}s")
            await asyncio.sleep(RECONNECT_DELAY_S)


async def run_orderbook_coordinator(collector: Collector, deadline: float | None = None):
    """Owns every periodic, connection-independent responsibility that used
    to live inside the single orderbook loop: autosave, rescan (discovering
    + registering new markets), closure detection/finalize, and the
    daily-chunk hourly flush. Runs once regardless of how many orderbook
    shard connections exist (see run_orderbook_shard()) -- these can't be
    duplicated per shard: register_markets() only reports a ticker as "new"
    to whichever caller reaches it first, and running autosave/finalize N
    times per cycle would just be redundant thrashing, not a correctness
    issue but pure waste. Shards independently discover their own new
    tickers by diffing collector.market_meta (mutated here) against what
    they've already subscribed to -- the same pattern run_trade_stream()
    already used, which works regardless of how many other tasks are reading
    market_meta concurrently."""
    last_autosave = time.monotonic()
    last_rescan = time.monotonic()
    last_daily_chunk = time.monotonic()
    last_diag = time.monotonic()
    prev_tm_snapshot = None

    while not (deadline and time.monotonic() > deadline):
        now = time.monotonic()

        if now - last_diag > 15:
            # Added 2026-08-31 during a live incident: RSS went from ~0 to
            # 1.5GB+ in under 3 minutes with no clear picture of *what* was
            # growing -- the only periodic size signal (the orderbook
            # "buffered new rows" log) doesn't cover trade_records,
            # market_meta, or open_events, and none of them show actual
            # process RSS. Reading /proc/self/status directly (no extra
            # dependency) gives ground truth on a Linux host regardless of
            # which in-process structure turns out to be responsible.
            try:
                with open("/proc/self/status") as f:
                    vmrss = next((l.split()[1] for l in f if l.startswith("VmRSS:")), "?")
            except OSError:
                vmrss = "?"
            ob_total = sum(len(v) for v in collector.records.values())
            trade_total = sum(len(v) for v in collector.trade_records.values())
            logger.info(
                f"[diag] RSS={vmrss}kB market_meta={len(collector.market_meta)} "
                f"open_events={len(collector.open_events)} buffered_ob={ob_total} buffered_trade={trade_total}"
            )
            # Confirmed directly (2026-08-31) that trade_records and
            # levels_json size were both ruled out as the growth source, but
            # the visible row counts still don't add up to the observed RSS
            # growth by an order of magnitude -- meaning something not
            # covered by any counter above (most likely raw messages
            # queued somewhere before ever reaching Collector.handle_message)
            # is the real driver. tracemalloc gives exact file/line
            # attribution instead of continuing to infer from indirect
            # signals -- diffing against the previous snapshot shows what's
            # *actively growing* between checks, not just cumulative
            # allocation, which is what actually points at the culprit.
            if tracemalloc.is_tracing():
                snapshot = tracemalloc.take_snapshot()
                stats = (
                    snapshot.compare_to(prev_tm_snapshot, "lineno")
                    if prev_tm_snapshot is not None
                    else snapshot.statistics("lineno")
                )
                logger.info("[tracemalloc] top growth by line since last check:" if prev_tm_snapshot else "[tracemalloc] top allocations (first check):")
                for stat in stats[:8]:
                    logger.info(f"[tracemalloc]   {stat}")
                prev_tm_snapshot = snapshot
            last_diag = now

        if now - last_autosave > AUTOSAVE_INTERVAL_S:
            ob_snapshot, collector.records = collector._swap_buffer(collector.records)
            trade_snapshot, collector.trade_records = collector._swap_buffer(collector.trade_records)
            await asyncio.to_thread(collector.save_both_snapshots, ob_snapshot, trade_snapshot)
            last_autosave = now

        if now - last_rescan > RESCAN_INTERVAL_S:
            # Don't let a rescan failure (e.g. a 429 that outlasts
            # kalshi_get's retries) propagate and kill this task -- there's
            # no connection here to tear down, but a crash would still stop
            # all future rescans/autosaves/finalizes for the rest of the run.
            try:
                # Runs the blocking REST calls in a thread so other tasks
                # (the orderbook shards' ws.recv() loops) stay responsive
                # while the rescan is in flight -- with 42 series now (up
                # from 22) this alone can take ~30s, confirmed directly.
                fresh_markets = await asyncio.to_thread(_get_open_markets)
            except Exception as e:
                logger.warning(f"rescan failed: {e}")
            else:
                new_tickers = collector.register_markets(_filter_wide_ladder_series(fresh_markets))
                if new_tickers:
                    logger.info(f"[rescan] {len(new_tickers)} new markets registered (shards will pick them up): {new_tickers}")

                newly_closed = set(collector.update_open_events(fresh_markets))
                # Safety net alongside the diff above -- catches events
                # whose closure that diff can't see because it crossed a
                # crash/restart boundary (see find_expired_events()).
                newly_closed.update(collector.find_expired_events())
                newly_closed = list(newly_closed)
                if newly_closed:
                    logger.info(f"[rescan] {len(newly_closed)} event(s) finished trading -> finalizing to S3: {newly_closed}")
                    ob_snapshot, collector.records = collector._swap_buffer(collector.records)
                    trade_snapshot, collector.trade_records = collector._swap_buffer(collector.trade_records)
                    await asyncio.to_thread(
                        collector.flush_and_finalize, ob_snapshot, trade_snapshot, newly_closed
                    )
                    # Deliberately NOT inside the to_thread call above -- see
                    # flush_and_finalize()'s docstring for the cross-thread
                    # dict-mutation race this used to cause.
                    for event_ticker in newly_closed:
                        collector.forget_event(event_ticker)
            last_rescan = now

        if now - last_daily_chunk > DAILY_CHUNK_INTERVAL_S:
            daily_events = [
                e for e in collector.open_events
                if e.split("-")[0] in DAILY_CHUNK_SERIES_TICKERS
            ]
            if daily_events:
                ob_snapshot, collector.records = collector._swap_buffer(collector.records)
                trade_snapshot, collector.trade_records = collector._swap_buffer(collector.trade_records)
                await asyncio.to_thread(collector.save_both_snapshots, ob_snapshot, trade_snapshot)
                hour_tag = f"_H{datetime.datetime.now(datetime.timezone.utc).strftime('%H')}"
                for event_ticker in daily_events:
                    await asyncio.to_thread(
                        collector.finalize_event_to_s3, event_ticker, "raw_ob_ticks", False, True, hour_tag
                    )
                    await asyncio.to_thread(
                        collector.finalize_event_to_s3, event_ticker, "trades", False, True, hour_tag
                    )
                logger.info(
                    f"[daily-chunk] flushed {len(daily_events)} daily-duration event(s) -> hourly chunk {hour_tag}"
                )
            last_daily_chunk = now

        await asyncio.sleep(5)

    logger.info("[coordinator] duration reached - stopping.")


async def run_orderbook_shard(collector: Collector, shard_id: int, deadline: float | None = None):
    """One of NUM_ORDERBOOK_SHARDS independent WebSocket connections, each
    carrying only the slice of collector.market_meta that hashes to its
    shard_id (see _shard_of()) -- splitting one ~1200-ticker connection into
    several smaller ones so any single connection's post-reconnect
    orderbook_snapshot flood is proportionally smaller (see
    NUM_ORDERBOOK_SHARDS for why that matters). Subscribes reactively by
    diffing market_meta against a locally-tracked `subscribed` set (the same
    pattern run_trade_stream() already used) rather than relying on
    register_markets()'s return value, since only run_orderbook_coordinator()
    calls that -- this shard needs to notice newly-registered tickers on its
    own regardless of which task registered them."""
    last_connect_attempt = None
    consecutive_fast_reconnects = 0

    while not (deadline and time.monotonic() > deadline):
        try:
            now_ = time.monotonic()
            if last_connect_attempt is not None:
                if now_ - last_connect_attempt < 120:
                    consecutive_fast_reconnects += 1
                    backoff = min(RECONNECT_DELAY_S * (2 ** consecutive_fast_reconnects), 60)
                    logger.warning(
                        f"[shard {shard_id}] reconnecting again only {now_ - last_connect_attempt:.0f}s after the "
                        f"last attempt ({consecutive_fast_reconnects} in a row) -- backing off {backoff}s before "
                        f"resubscribing to avoid tripping Kalshi's subscription-rate limit again"
                    )
                    await asyncio.sleep(backoff)
                else:
                    consecutive_fast_reconnects = 0
            last_connect_attempt = time.monotonic()

            seq_tracker = _SeqTracker()
            subscribed: set[str] = set()

            async with websockets.connect(
                WS_URL, additional_headers=_ws_auth_headers(), open_timeout=15
            ) as ws:
                sub_id = 1
                tickers = [t for t in collector.market_meta if _shard_of(t) == shard_id]
                for i in range(0, len(tickers), SUBSCRIBE_BATCH):
                    batch = tickers[i : i + SUBSCRIBE_BATCH]
                    await ws.send(json.dumps({
                        "id": sub_id,
                        "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": batch},
                    }))
                    sub_id += 1
                    if i + SUBSCRIBE_BATCH < len(tickers):
                        await asyncio.sleep(SUBSCRIBE_BATCH_PACING_S)
                subscribed.update(tickers)
                logger.info(f"[shard {shard_id}] Connected & subscribed ({len(tickers)} tickers).")
                # See RESYNC_INTERVAL_S: the only way to get fresh snapshots
                # out of Kalshi is to subscribe again, so the connection is
                # deliberately cycled on a timer rather than held open.
                resync_deadline = time.monotonic() + RESYNC_INTERVAL_S + shard_id * RESYNC_STAGGER_S

                async def _flush_before_reconnect():
                    """Buffered rows belong to the connection that produced
                    them -- flush before dropping it, same as the gap path
                    always did, so a resync never costs a partial buffer."""
                    ob_snapshot, collector.records = collector._swap_buffer(collector.records)
                    trade_snapshot, collector.trade_records = collector._swap_buffer(collector.trade_records)
                    await asyncio.to_thread(collector.save_both_snapshots, ob_snapshot, trade_snapshot)

                while True:
                    now = time.monotonic()
                    if deadline and now > deadline:
                        logger.info(f"[shard {shard_id}] duration reached - stopping.")
                        return

                    if now > resync_deadline:
                        logger.info(f"[shard {shard_id}] periodic resync ({RESYNC_INTERVAL_S:.0f}s since "
                                    f"subscribe) -- reconnecting for fresh snapshots")
                        await _flush_before_reconnect()
                        break

                    new_tickers = [
                        t for t in collector.market_meta
                        if t not in subscribed and _shard_of(t) == shard_id
                    ]
                    if new_tickers:
                        for i in range(0, len(new_tickers), SUBSCRIBE_BATCH):
                            batch = new_tickers[i : i + SUBSCRIBE_BATCH]
                            await ws.send(json.dumps({
                                "id": sub_id,
                                "cmd": "subscribe",
                                "params": {"channels": ["orderbook_delta"], "market_tickers": batch},
                            }))
                            sub_id += 1
                            if i + SUBSCRIBE_BATCH < len(new_tickers):
                                await asyncio.sleep(SUBSCRIBE_BATCH_PACING_S)
                        subscribed.update(new_tickers)
                        logger.info(f"[shard {shard_id}] subscribed to {len(new_tickers)} new ticker(s), {len(subscribed)} total")

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue

                    gap = collector.handle_message(json.loads(raw), seq_tracker)
                    if gap:
                        # Simplest reliable resync: reconnect + resubscribe, which
                        # re-fetches a fresh orderbook_snapshot for every ticker via
                        # the existing subscribe flow (already-verified code path),
                        # rather than special-casing the undocumented get_snapshot
                        # wire format for a handful of dropped messages. Also covers
                        # the "unhandled WS message" error/unsubscribed case (see
                        # Collector.handle_message()) -- same recovery either way.
                        await _flush_before_reconnect()
                        break

                    total = sum(len(v) for v in collector.records.values())
                    if total and total % 500 == 0:
                        # Confirmed directly (2026-08-31) this only ever counted
                        # collector.records (orderbook) -- trade_records (filled
                        # by the completely separate run_trade_stream() task) was
                        # invisible here, so a trade-volume spike wouldn't show up
                        # in this log line at all even while consuming real memory.
                        # Logging both now closes that blind spot.
                        trade_total = sum(len(v) for v in collector.trade_records.values())
                        logger.info(f"[{total} buffered orderbook rows, {trade_total} buffered trade rows]")

        except (websockets.exceptions.WebSocketException, OSError) as e:
            # Confirmed directly (2026-08-27): a bare ConnectionClosed catch
            # missed websockets.exceptions.InvalidStatus (Kalshi returning an
            # HTTP 500 during the handshake, a transient server-side hiccup)
            # -- that's a distinct exception type raised before a connection
            # is ever established, not a subclass of ConnectionClosed, so it
            # propagated uncaught and crashed the whole process (all shards +
            # trade stream) over what should have just been a retry on this
            # one shard. WebSocketException is the library's base class for
            # everything it raises (ConnectionClosed, InvalidStatus,
            # InvalidHandshake, InvalidURI, ...), so catching that instead
            # closes this class of gap generally, not just this one instance.
            logger.warning(f"[shard {shard_id}] websocket disconnected: {e} - reconnecting in {RECONNECT_DELAY_S}s")
            await asyncio.sleep(RECONNECT_DELAY_S)


async def run(duration_minutes: float | None = None):
    """Orchestrator: one shared Collector, NUM_ORDERBOOK_SHARDS independent
    orderbook_delta WS connections (see run_orderbook_shard()) plus one
    trade connection (run_trade_stream() -- kept apart from orderbook for
    unrelated seq-corruption reasons) plus one coordinator task that owns
    autosave/rescan/finalize (run_orderbook_coordinator()) -- that's
    NUM_ORDERBOOK_SHARDS + 1 actual WebSocket connections total (the
    coordinator itself opens no connection) -- deliberately kept under
    Kalshi's 5-connections-per-account cap, leaving one connection free for
    the separate trading EC2 instance. All tasks share one deadline computed
    once here so a duration-limited run (see main()) stops them together."""
    deadline = time.monotonic() + duration_minutes * 60 if duration_minutes else None
    collector = Collector()
    markets = _get_open_markets()
    if not markets:
        logger.warning("No open markets found.")
        return
    collector.register_markets(_filter_wide_ladder_series(markets))
    collector.update_open_events(markets)  # seed baseline; nothing "closes" on the first fetch
    events = sorted({m["event_ticker"] for m in collector.market_meta.values()})
    logger.info(f"Open events: {events}")
    logger.info(f"Total markets: {len(collector.market_meta)}")

    # Sweep for events that closed entirely while this process was down
    # (e.g. a crash right around an event's expiry) -- their local part
    # files were never merged/uploaded because the *new* process's
    # closure-tracking baseline only ever gets seeded from what's
    # currently open, so an already-closed event was never in it to begin
    # with. Runs as its own background task rather than blocking here:
    # confirmed directly a single to_thread call wrapping the whole sweep
    # as one unit delayed live collection from starting at all, with no
    # end in sight -- 9500+ orphaned events had accumulated from several
    # days of prior bugs, which at one merge-and-upload round-trip per
    # event would have taken over an hour before a single live message
    # got processed.
    current_open = {m["event_ticker"] for m in markets}
    sweep_task = asyncio.create_task(_sweep_orphaned_events(collector, current_open))

    trade_task = asyncio.create_task(run_trade_stream(collector))
    coordinator_task = asyncio.create_task(run_orderbook_coordinator(collector, deadline))
    shard_tasks = [
        asyncio.create_task(run_orderbook_shard(collector, shard_id, deadline))
        for shard_id in range(NUM_ORDERBOOK_SHARDS)
    ]
    try:
        await asyncio.gather(coordinator_task, *shard_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        trade_task.cancel()
        sweep_task.cancel()
        coordinator_task.cancel()
        for t in shard_tasks:
            t.cancel()
        for t in (trade_task, sweep_task, coordinator_task, *shard_tasks):
            try:
                await t
            except asyncio.CancelledError:
                pass
        collector.save_all()
        logger.info("Done.")


def main():
    _setup_logging()
    # Added 2026-08-31 during a live OOM incident: RSS was growing far
    # faster than could be explained by the "buffered rows" counters
    # (which only cover already-parsed Collector.records/trade_records) --
    # trade volume and levels_json size were both checked directly and
    # ruled out as the cause. tracemalloc gives exact file/line attribution
    # for every allocation, which is the only way to find what's actually
    # growing rather than continuing to guess from indirect signals (see
    # run_orderbook_coordinator()'s periodic diff-snapshot logging).
    tracemalloc.start(10)
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    if duration:
        logger.info(f"Will collect for {duration:.0f} minutes then auto-stop.")
    try:
        asyncio.run(run(duration))
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()
