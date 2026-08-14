"""
ws_raw_ob_collector.py – Full L2 tick-level orderbook logger, across every
currently open market of every series in HIGH_LOW_TEMP_SERIES_TICKERS.

Subscribes to orderbook_delta for every currently open market across all
expiries (events) of every configured series and appends every raw
snapshot/delta message to per-event parquet files, preserving full
price-level depth so the exact book can be reconstructed at any timestamp
for backtesting.

Keeps the full raw tick stream -- every price level, not just the touch --
so the exact book state can be reconstructed at any timestamp.

Usage:
    python ws_raw_ob_collector.py            # run until Ctrl+C
    python ws_raw_ob_collector.py 480         # run for 480 minutes then stop
"""

import asyncio
import base64
import datetime
import json
import logging
import os
import sys
import time

import boto3
import pandas as pd
import websockets
from botocore.exceptions import ClientError
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, os.path.dirname(__file__))
from api_functions import API_KEY, HIGH_LOW_TEMP_SERIES_TICKERS, kalshi_get

SERIES_TICKERS = HIGH_LOW_TEMP_SERIES_TICKERS

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "kalshi_btc_hedge", "eshan-ws-key.txt"
)
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


def _out_dir(event_ticker: str) -> str:
    """data/kalshi_ws/{series}/raw_ob_ticks/ -- one subfolder per market type (series)."""
    series = event_ticker.split("-")[0]
    return os.path.join(OUT_DIR_ROOT, series, "raw_ob_ticks")

SUBSCRIBE_BATCH = 50
AUTOSAVE_INTERVAL_S = 5 * 60
RESCAN_INTERVAL_S = 15 * 60
RECONNECT_DELAY_S = 5


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _ws_auth_headers():
    with open(WS_KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + "GET" + "/trade-api/ws/v2").encode()
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return [
        ("KALSHI-ACCESS-KEY", API_KEY),
        ("KALSHI-ACCESS-SIGNATURE", base64.b64encode(sig).decode()),
        ("KALSHI-ACCESS-TIMESTAMP", ts),
    ]


def _get_open_markets() -> list[dict]:
    """All open markets across every currently-trading expiry of every configured series.
    Paced across series to avoid tripping Kalshi's rate limit in the first place
    (confirmed directly that ~20 back-to-back per-series calls with no delay trips a
    real 429); kalshi_get itself now retries/backs off on 429 if one slips through."""
    all_markets = []
    for i, series in enumerate(SERIES_TICKERS):
        if i > 0:
            time.sleep(0.3)
        data = kalshi_get(
            "/trade-api/v2/markets",
            params={"series_ticker": series, "status": "open"},
            auth=True,
        )
        all_markets.extend(data.get("markets", []))
    return all_markets


# ---------------------------------------------------------------------------
# Collector: buffers raw messages, dedupes, and flushes to per-event parquet
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self):
        self.market_meta: dict[str, dict] = {}    # ticker -> {event_ticker, floor_strike, cap_strike}
        self.records: dict[str, list[dict]] = {}   # event_ticker -> buffered new rows
        self.last_seq: int | None = None
        self.n_gap_events = 0
        self.n_dropped_messages = 0
        self._s3_client = boto3.Session(profile_name=S3_PROFILE).client("s3", region_name=S3_REGION)

    def reset_seq_tracking(self):
        """Call once per fresh WS connection -- seq is scoped to the connection
        (confirmed directly against real captured data: it's ONE counter shared
        across every subscribed ticker's messages, not per-market -- a given
        ticker's own seq values jump by more than 1 routinely simply because
        other tickers' messages land in between), so it restarts on reconnect."""
        self.last_seq = None

    def note_seq(self, seq: int | None) -> bool:
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

    def register_markets(self, markets: list[dict]) -> list[str]:
        """Add newly-seen markets. Returns tickers not previously known."""
        new_tickers = []
        for m in markets:
            ticker = m["ticker"]
            if ticker in self.market_meta or m.get("floor_strike") is None:
                continue
            self.market_meta[ticker] = {
                "event_ticker": m["event_ticker"],
                "floor_strike": float(m["floor_strike"]),
                "cap_strike": float(m["cap_strike"]) if m.get("cap_strike") is not None else float("nan"),
            }
            self.records.setdefault(m["event_ticker"], [])
            new_tickers.append(ticker)
        return new_tickers

    def handle_message(self, d: dict) -> bool:
        """Returns True if this message revealed a seq gap (caller should
        force a reconnect+resubscribe to resync, see run())."""
        mtype = d.get("type")
        msg = d.get("msg", {})
        seq = d.get("seq")
        prev_seq = self.last_seq
        gap = self.note_seq(seq)
        if gap:
            logger.warning(f"seq gap: {prev_seq} -> {seq} ({seq - prev_seq - 1} message(s) missed; "
                  f"{self.n_dropped_messages} total across {self.n_gap_events} gaps so far this connection) "
                  f"-- forcing reconnect to resync via fresh snapshots")

        if mtype == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            meta = self.market_meta.get(ticker)
            if meta is None:
                return gap
            ts_ms, ts_str = msg.get("ts_ms"), msg.get("ts")
            for side_key, side in (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no")):
                levels = msg.get(side_key) or []
                self.records[meta["event_ticker"]].append({
                    "ticker": ticker,
                    "event_ticker": meta["event_ticker"],
                    "floor_strike": meta["floor_strike"],
                    "cap_strike": meta["cap_strike"],
                    "msg_type": "snapshot",
                    "seq": seq,
                    "ts_ms": ts_ms,
                    "time": ts_str,
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
                "ts_ms": msg.get("ts_ms"),
                "time": msg.get("ts"),
                "side": msg.get("side"),
                "price": float(msg["price_dollars"]),
                "delta": float(msg["delta_fp"]),
                "levels_json": None,
            })

        return gap

    def _out_path(self, event_ticker: str) -> str:
        out_dir = _out_dir(event_ticker)
        os.makedirs(out_dir, exist_ok=True)
        return os.path.join(out_dir, f"{event_ticker}.parquet")

    def finalize_event_to_s3(self, event_ticker: str, skip_if_exists: bool = False) -> bool:
        """Uploads the complete local parquet for a (presumably closed)
        event as a single S3 object, keyed by event_ticker only (no
        timestamp) so re-running just overwrites in place. Returns whether
        an upload happened. `skip_if_exists` lets a manual re-run avoid
        re-uploading days already pushed up."""
        path = self._out_path(event_ticker)
        if not os.path.exists(path):
            return False
        series = event_ticker.split("-")[0]
        key = f"{S3_PREFIX}/{series}/raw_ob_ticks/{event_ticker}/{event_ticker}.parquet"
        if skip_if_exists:
            try:
                self._s3_client.head_object(Bucket=S3_BUCKET, Key=key)
                return False  # already uploaded
            except ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    raise
        try:
            self._s3_client.upload_file(path, S3_BUCKET, key)
            logger.info(f"Uploaded {event_ticker} -> s3://{S3_BUCKET}/{key}")
            return True
        except Exception as e:
            logger.warning(f"S3 upload failed for {event_ticker}: {e}")
            return False

    def save_all(self):
        for event_ticker, new_recs in self.records.items():
            if not new_recs:
                continue
            path = self._out_path(event_ticker)
            existing = []
            if os.path.exists(path):
                try:
                    existing = pd.read_parquet(path).to_dict("records")
                except Exception as e:
                    logger.warning(f"could not load existing {path}: {e}")

            df = pd.DataFrame(existing + new_recs)
            # Kalshi's ts strings aren't consistently formatted -- some carry
            # fractional seconds, some don't (confirmed directly: this crashed
            # a live save_all() when pandas inferred a %f format from early
            # rows then hit a whole-second "...50Z" row later in the same
            # batch). format="ISO8601" parses each value on its own terms
            # instead of locking to one inferred format for the whole column.
            df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601")
            df = (
                df.sort_values("time")
                .drop_duplicates(subset=["ticker", "msg_type", "seq", "side"])
                .reset_index(drop=True)
            )
            df.to_parquet(path, index=False)
            logger.info(f"Saved {len(df)} total rows -> {path}")
            self.records[event_ticker] = []


# ---------------------------------------------------------------------------
# Async collection loop (reconnects on drop, rescans for new expiries)
# ---------------------------------------------------------------------------

async def run(duration_minutes: float | None = None):
    collector = Collector()
    markets = _get_open_markets()
    if not markets:
        logger.warning("No open markets found.")
        return
    collector.register_markets(markets)
    events = sorted({m["event_ticker"] for m in collector.market_meta.values()})
    logger.info(f"Open events: {events}")
    logger.info(f"Total markets: {len(collector.market_meta)}")

    deadline = time.monotonic() + duration_minutes * 60 if duration_minutes else None
    last_autosave = time.monotonic()
    last_rescan = time.monotonic()

    try:
        while not (deadline and time.monotonic() > deadline):
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=_ws_auth_headers(), open_timeout=15
                ) as ws:
                    collector.reset_seq_tracking()
                    tickers = list(collector.market_meta.keys())
                    sub_id = 1
                    for i in range(0, len(tickers), SUBSCRIBE_BATCH):
                        batch = tickers[i : i + SUBSCRIBE_BATCH]
                        await ws.send(json.dumps({
                            "id": sub_id,
                            "cmd": "subscribe",
                            "params": {"channels": ["orderbook_delta"], "market_tickers": batch},
                        }))
                        sub_id += 1
                    logger.info("Connected & subscribed. Press Ctrl+C to stop and save.")

                    while True:
                        now = time.monotonic()
                        if deadline and now > deadline:
                            logger.info("Duration reached - stopping.")
                            return

                        if now - last_autosave > AUTOSAVE_INTERVAL_S:
                            collector.save_all()
                            last_autosave = now

                        if now - last_rescan > RESCAN_INTERVAL_S:
                            # Don't let a rescan failure (e.g. a 429 that outlasts
                            # kalshi_get's retries) tear down an otherwise-healthy
                            # websocket connection -- confirmed directly this was
                            # happening: the HTTPError propagated out of this block,
                            # got caught by the outer `except OSError` (HTTPError
                            # subclasses it), and forced a full reconnect+resubscribe
                            # even though nothing was wrong with the connection itself.
                            try:
                                new_tickers = collector.register_markets(_get_open_markets())
                            except Exception as e:
                                logger.warning(f"rescan failed, keeping existing connection: {e}")
                                last_rescan = now
                                new_tickers = []
                            else:
                                if new_tickers:
                                    logger.info(f"[rescan] {len(new_tickers)} new markets -> subscribing: {new_tickers}")
                                    await ws.send(json.dumps({
                                        "id": sub_id,
                                        "cmd": "subscribe",
                                        "params": {"channels": ["orderbook_delta"], "market_tickers": new_tickers},
                                    }))
                                    sub_id += 1
                                last_rescan = now

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue

                        gap = collector.handle_message(json.loads(raw))
                        if gap:
                            # Simplest reliable resync: reconnect + resubscribe, which
                            # re-fetches a fresh orderbook_snapshot for every ticker via
                            # the existing subscribe flow (already-verified code path),
                            # rather than special-casing the undocumented get_snapshot
                            # wire format for a handful of dropped messages.
                            collector.save_all()
                            break

                        total = sum(len(v) for v in collector.records.values())
                        if total and total % 500 == 0:
                            logger.info(f"[{total} buffered new rows]")

            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning(f"websocket disconnected: {e} - reconnecting in {RECONNECT_DELAY_S}s")
                await asyncio.sleep(RECONNECT_DELAY_S)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        collector.save_all()
        logger.info("Done.")


def main():
    _setup_logging()
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
