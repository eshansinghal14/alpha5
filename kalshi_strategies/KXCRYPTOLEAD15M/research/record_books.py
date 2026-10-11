"""
Standalone recorder: every CF constituent venue we can reach, every coin, one
snapshot per venue/coin per second, plus Kalshi's own 15-minute settlement
values as ground truth. Runs beside live trading in its own process.

    python -m kalshi_strategies.KXCRYPTOLEAD15M.research.record_books

Writes data/kalshi_crypto/books/{YYYY-MM-DD}/{HH}.parquet (top 50 levels per
side, one row per venue/coin/second) and data/kalshi_crypto/rti_truth.parquet.
At ~5 venues x 5 coins x 50 levels x 2 sides that is ~2.5k levels/s, about
1 GB/day uncompressed and ~150 MB as zstd parquet.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT
from kalshi_strategies.KXCRYPTOLEAD15M.series import CF_CONSTITUENTS, COINS
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import CoinbaseAdapter
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venues import ADAPTERS, LEADING_VENUES

ROOT = Path(DATA_ROOT) / "kalshi_crypto" / "books"
TRUTH = Path(DATA_ROOT) / "kalshi_crypto" / "rti_truth.parquet"
LEVELS = 50
TRUTH_SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "HYPE": "KXHYPE15M"}


class Recorder:
    """Samples books and logs prints from adapters it is handed. Owns no
    sockets itself, so the venue process can record off the same connections
    it trades on (one parse per message instead of two)."""

    def __init__(self, adapters: dict | None = None, venues: list[str] | None = None):
        if adapters is None:
            adapters = {}
            for v in venues or []:
                if v == "coinbase":
                    adapters[v] = CoinbaseAdapter(coins=[c for c in COINS if v in CF_CONSTITUENTS[c]])
                elif v in LEADING_VENUES:
                    adapters[v] = ADAPTERS[v](list(COINS))     # leading signal, not a constituent
                else:
                    adapters[v] = ADAPTERS[v]([c for c in COINS if v in CF_CONSTITUENTS[c]])
        self.adapters = adapters
        self.latest: dict[tuple[str, str], object] = {}
        self.rows: list[dict] = []
        self.prints: list[dict] = []
        self.counts: dict[tuple[str, str], int] = {}

    def wire(self):
        """Standalone mode: point the adapters' callbacks at this recorder."""
        if "coinbase" in self.adapters:
            self.adapters["coinbase"].on_print = (
                lambda coin, t, as_of, exch_ts: self.on_print("coinbase", coin, t, exch_ts))
        for v in LEADING_VENUES:
            if v in self.adapters:
                self.adapters[v].on_print = (
                    lambda coin, t, as_of, exch_ts, kind: self.on_print(kind, coin, t, exch_ts))

    def on_print(self, venue, coin, t, exch_ts):
        self._print(venue, coin, t, exch_ts)

    def _print(self, venue, coin, t, exch_ts):
        """Every live print, stamped at receipt on the wall clock: the
        lead-lag between venues is measured off recv_ts, never exch_ts."""
        try:
            self.prints.append({"recv_ts": time.time(), "exch_ts": exch_ts, "venue": venue, "coin": coin,
                                "price": float(t["price"]), "size": float(t["size"]),
                                "side": t.get("side"), "trade_id": str(t.get("trade_id"))})
        except (KeyError, TypeError, ValueError):
            pass

    def on_update(self, vb):
        self.latest[(vb.venue, vb.coin)] = vb
        k = (vb.venue, vb.coin)
        self.counts[k] = self.counts.get(k, 0) + 1

    async def sample_loop(self):
        """Pin the latest book per venue/coin once a second, wall-clock aligned
        like the composite bars. `age_s` is how stale that book was."""
        while True:
            now = time.time()
            await asyncio.sleep(int(now) + 1 - now)
            ts = int(time.time())
            mono = time.monotonic()
            for (venue, coin), vb in list(self.latest.items()):
                if vb.is_resyncing or not vb.bids or not vb.asks:
                    continue
                b = vb.bids[:LEVELS]; a = vb.asks[:LEVELS]
                self.rows.append({
                    "ts": ts, "venue": venue, "coin": coin, "age_s": mono - vb.receive_ts,
                    "exch_ts": vb.exchange_ts,
                    "bid_px": np.array([l.price for l in b]), "bid_sz": np.array([l.size for l in b]),
                    "ask_px": np.array([l.price for l in a]), "ask_sz": np.array([l.size for l in a]),
                })

    async def flush_loop(self, every_s=60):
        """Parquet writes run on an executor thread. In the venue process that
        thread shares a GIL with the price feeds, so each write is also kept
        short (one minute of rows) rather than batched larger."""
        while True:
            await asyncio.sleep(every_s)
            rows, self.rows = self.rows, []
            prints, self.prints = self.prints, []
            if rows:
                await asyncio.get_running_loop().run_in_executor(None, self._write, rows)
            if prints:
                await asyncio.get_running_loop().run_in_executor(None, self._write_prints, prints)

    @staticmethod
    def _write_prints(rows):
        df = pd.DataFrame(rows)
        day = pd.Timestamp(df.recv_ts.iloc[0], unit="s", tz="UTC").strftime("%Y-%m-%d")
        d = ROOT.parent / "venue_prints" / day
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / f"{int(time.time() * 1000)}.parquet", compression="zstd", index=False)

    @staticmethod
    def _write(rows):
        df = pd.DataFrame(rows)
        df["hour"] = pd.to_datetime(df.ts, unit="s", utc=True).dt.floor("h")
        for hour, chunk in df.groupby("hour"):
            d = ROOT / hour.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            # One file per flush: append-only parts, so a crash never corrupts history.
            path = d / f"{hour.strftime('%H')}-{int(time.time() * 1000)}.parquet"
            chunk.drop(columns="hour").to_parquet(path, compression="zstd", index=False)

    async def truth_loop(self, every_s=300):
        """Kalshi's single-coin 15M markets publish each boundary's 60 s RTI
        average as `expiration_value` ~4 s after close; keep a running file."""
        from kalshi_infra import api_functions as api
        while True:
            try:
                rows = []
                since = int(time.time()) - 3 * 86400
                for coin, s in TRUTH_SERIES.items():
                    r = await asyncio.get_running_loop().run_in_executor(None, lambda: api.kalshi_get(
                        "/trade-api/v2/markets", params={"series_ticker": s, "status": "settled",
                                                         "limit": 1000, "min_close_ts": since}))
                    for m in r.get("markets", []):
                        try:
                            rows.append({"coin": coin, "close": pd.Timestamp(m["close_time"]),
                                         "value": float(m["expiration_value"])})
                        except (KeyError, TypeError, ValueError):
                            pass
                new = pd.DataFrame(rows)
                if TRUTH.exists():
                    new = pd.concat([pd.read_parquet(TRUTH), new])
                new = new.drop_duplicates(["coin", "close"]).sort_values(["close", "coin"])
                TRUTH.parent.mkdir(parents=True, exist_ok=True)
                tmp = TRUTH.with_suffix(".tmp.parquet")
                new.to_parquet(tmp, index=False)
                os.replace(tmp, TRUTH)
            except Exception as e:
                print(f"[record] truth fetch failed: {e!r}", flush=True)
            await asyncio.sleep(every_s)

    async def status_loop(self, every_s=60):
        last = {}
        while True:
            await asyncio.sleep(every_s)
            parts = []
            for v, ad in self.adapters.items():
                kinds = ("hl_perp", "hl_spot") if v == "hyperliquid" else (v,)
                rates = {f"{c}{'s' if k == 'hl_spot' else ''}": (self.counts.get((k, c), 0) - last.get((k, c), 0)) / every_s
                         for k in kinds for c in COINS if (k, c) in self.counts}
                extra = ""
                for attr in ("n_checksum_fail", "n_seq_gaps", "n_resyncs"):
                    if hasattr(ad, attr):
                        extra += f" {attr.split('_', 1)[1]}={getattr(ad, attr)}"
                parts.append(f"{v}: " + " ".join(f"{c}{r:.0f}/s" for c, r in rates.items()) + extra)
            last = dict(self.counts)
            print("[record] " + " | ".join(parts), flush=True)

    def background_tasks(self):
        """Sampling, writing and the truth fetch -- everything but the sockets."""
        return [self.sample_loop(), self.flush_loop(), self.truth_loop(), self.status_loop()]

    async def run(self):
        self.wire()
        tasks = [ad.run(self.on_update) for ad in self.adapters.values()]
        await asyncio.gather(*tasks, *self.background_tasks())


def main():
    """Standalone recorder, for when live trading isn't running. While it is,
    the venue process records (spot_feed/venue_process.py, RECORD_IN_VENUE_PROCESS)
    and this would only duplicate every connection."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", nargs="+",
                    default=["coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish", *LEADING_VENUES])
    args = ap.parse_args()
    asyncio.run(Recorder(venues=args.venues).run())


if __name__ == "__main__":
    main()
