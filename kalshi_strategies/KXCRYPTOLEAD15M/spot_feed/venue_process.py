"""
Non-Coinbase venues in their own process.

Kraken alone sends 20-60 msg/s across the five coins and Gemini up to ~90/s on
BTC (recorder, 2026-10-11); parsing that on the trading loop would add to the
stale_signal blocks it already has (27% of BTC decisions 05:09-05:13 UTC). The
child runs every adapter and ships, every SHIP_S, the newest top-of-book per
(venue, coin) plus every Hyperliquid print.

The child is a plain `python -m` subprocess talking over its stdin/stdout
pipes, not a multiprocessing child: "spawn" re-imports the parent's __main__,
so any launcher without a main guard started the venue process in a loop
(2026-10-11 smoke test). A subprocess doesn't depend on how the parent was
started. It exits when its stdin closes, so it can't outlive the trading
process even if that is killed.

Freshness crosses the process boundary on the wall clock: the child stamps
each book with time.time() at receipt and the parent converts it back to its
own monotonic clock, so feed health and the cross-coin skew guard see the
venue's true age, not the age of the batch.
"""
from __future__ import annotations

import collections
import json
import os
import pickle
import struct
import subprocess
import sys
import threading
import time

from .base import OrderBookLevel, VenueBook

# Batch interval. Small next to the 0.8 s signal-age limit, large enough that
# the pipe carries ~20 small frames/s.
SHIP_S = 0.05
# Levels per side shipped: the mid needs one, the diagnostic CF replica a few.
SHIP_LEVELS = 10
# Frames buffered on the trading side; on overflow the oldest is dropped (the
# next frame carries newer books for anything that changed).
MAX_FRAMES = 400
# Restart backoff after the child dies: doubles from MIN to MAX, back to MIN
# once a child has stayed up HEALTHY_S.
RESTART_MIN_S, RESTART_MAX_S, HEALTHY_S = 1.0, 30.0, 60.0

# The child also does the order-book recorder's job (1 s book samples, every
# print, Kalshi settlement values) off the sockets it already holds. Run as a
# separate process the recorder duplicated every connection and parse: ~0.9 of
# a core on 2026-10-11. It only records venues in the plan, so Coinbase books
# (owned by the trading process) are not sampled; Coinbase prints are still
# kept by the trading process's PrintLog.
RECORD_IN_VENUE_PROCESS = True

_LEN = struct.Struct("<I")
PERP_FLUSH_S = 30.0


def _write_perp_prints(rows: list[dict]):
    """Append-only part per flush under data/kalshi_crypto/perp_prints/{day}/,
    same layout as the recorder's venue_prints, so a crash loses one part."""
    import pandas as pd
    from kalshi_infra import DATA_ROOT
    df = pd.DataFrame(rows)
    day = pd.Timestamp(df.recv_ts.iloc[0], unit="s", tz="UTC").strftime("%Y-%m-%d")
    d = os.path.join(DATA_ROOT, "kalshi_crypto", "perp_prints", day)
    os.makedirs(d, exist_ok=True)
    df.to_parquet(os.path.join(d, f"{int(time.time() * 1000)}.parquet"), compression="zstd", index=False)


def _write_frame(out, obj):
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    out.write(_LEN.pack(len(data)) + data)
    out.flush()


def _read_frame(inp):
    head = inp.read(_LEN.size)
    if len(head) < _LEN.size:
        return None
    (n,) = _LEN.unpack(head)
    data = inp.read(n)
    if len(data) < n:
        return None
    return pickle.loads(data)


# ---------------------------------------------------------------------------
# Child
# ---------------------------------------------------------------------------

def _child_main(plan: dict[str, list[str]]):
    import asyncio
    import signal
    from .venues import ADAPTERS, LEADING_VENUES
    from ..prints import PERP_BURST_USD_1S

    # The pipe is the protocol; anything an adapter prints goes to stderr,
    # which the parent leaves on the console/run log.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    sys.stdout = sys.stderr
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    # Below the trading process, above the writer: its books feed pricing, but
    # the recorder work it also carries must not outrank the trading loop.
    # Explicit because Windows and Linux differ on what a child inherits.
    from kalshi_infra.runtime.priority import set_priority
    set_priority("normal")

    def watch_parent():
        # Blocks until the parent closes our stdin -- including when it dies.
        sys.stdin.buffer.read()
        os._exit(0)
    threading.Thread(target=watch_parent, daemon=True).start()

    latest: dict[tuple[str, str], tuple] = {}
    prints: list[tuple] = []
    adapters = []

    def on_update(vb: VenueBook):
        latest[(vb.venue, vb.coin)] = (
            vb.venue, vb.coin, vb.product,
            tuple((l.price, l.size) for l in vb.bids[:SHIP_LEVELS]),
            tuple((l.price, l.size) for l in vb.asks[:SHIP_LEVELS]),
            time.time() - (time.monotonic() - vb.receive_ts), vb.exchange_ts, vb.is_resyncing)

    perp_rows: list[dict] = []

    def on_print(coin, t, as_of, exch_ts, kind):
        if kind in PERP_BURST_USD_1S:
            try:
                perp_rows.append({"recv_ts": time.time() - (time.monotonic() - as_of), "exch_ts": exch_ts,
                                  "venue": kind, "coin": coin, "price": float(t["price"]),
                                  "size": float(t["size"]), "side": t.get("side"),
                                  "trade_id": str(t.get("trade_id"))})
            except (KeyError, TypeError, ValueError):
                pass
            # Recorded here, in the child, rather than shipped: at Binance's
            # sweep rates (thousands/s) shipping them loads the trading loop at
            # the worst moment. Only calibrated coins are shipped for bursts.
            if coin not in PERP_BURST_USD_1S[kind]:
                return
        prints.append((kind, coin, t, time.time() - (time.monotonic() - as_of), exch_ts))

    async def record_perps():
        nonlocal perp_rows
        while True:
            await asyncio.sleep(PERP_FLUSH_S)
            rows, perp_rows = perp_rows, []
            if rows:
                try:
                    await asyncio.get_running_loop().run_in_executor(None, _write_perp_prints, rows)
                except Exception as e:
                    print(f"[spot/venues] perp print write failed: {e!r}", flush=True)

    def alive() -> list[tuple]:
        """(venue, coin, wall time) per synced book: a book that hasn't
        changed is still current while its connection is (see
        _Venue._mark_fresh), and without this the parent would age it out."""
        out_ = []
        now_wall, now_mono = time.time(), time.monotonic()
        for ad in adapters:
            if ad.venue in LEADING_VENUES:
                continue    # bbo streams tick constantly, and they never price
            for coin, pb in ad._books.items():
                if not pb.is_resyncing and pb.last_receive_ts > 0.0:
                    out_.append((ad.venue, coin, now_wall - (now_mono - pb.last_receive_ts)))
        return out_

    async def ship():
        nonlocal prints
        while True:
            await asyncio.sleep(SHIP_S)
            books, ps = list(latest.values()), prints
            latest.clear()
            prints = []
            try:
                _write_frame(out, (books, ps, alive()))
            except (BrokenPipeError, OSError):
                os._exit(0)

    recorder = None
    if RECORD_IN_VENUE_PROCESS:
        try:
            from ..research.record_books import Recorder
            recorder = Recorder(adapters={})
        except Exception as e:
            print(f"[spot/venues] recorder disabled ({e!r})", flush=True)

    def on_update_rec(vb: VenueBook):
        on_update(vb)
        recorder.on_update(vb)

    def on_print_rec(coin, t, as_of, exch_ts, kind):
        recorder.on_print(kind, coin, t, exch_ts)
        on_print(coin, t, as_of, exch_ts, kind)

    async def main():
        tasks = []
        for venue, coins in plan.items():
            ad = ADAPTERS[venue](coins)
            if hasattr(ad, "on_print"):
                ad.on_print = on_print if recorder is None else on_print_rec
            adapters.append(ad)
            tasks.append(ad.run(on_update if recorder is None else on_update_rec))
        if recorder is not None:
            recorder.adapters = {ad.venue: ad for ad in adapters}
            tasks += recorder.background_tasks()
        await asyncio.gather(ship(), record_perps(), *tasks)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Parent handle
# ---------------------------------------------------------------------------

class VenueProcess:
    """Trading-side handle. `ensure_running()` and `drain()` are called on the
    event loop and never block; a reader thread moves frames off the pipe."""

    def __init__(self, plan: dict[str, list[str]], clock=time.monotonic):
        self.plan = {v: list(c) for v, c in plan.items() if c}
        self._clock = clock
        self._proc: subprocess.Popen | None = None
        self._frames: collections.deque = collections.deque(maxlen=MAX_FRAMES)
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._next_try = 0.0
        self._backoff = RESTART_MIN_S
        self.n_batches = 0
        self.n_books = 0
        self.n_starts = 0
        self.n_dropped = 0
        self.last_exit: int | None = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        if not self.plan:
            return
        from kalshi_infra import REPO_ROOT
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venue_process",
             json.dumps(self.plan)],
            cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, bufsize=0, creationflags=flags)
        self._started_at = self._clock()
        self.n_starts += 1
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True,
                         name="venue-reader").start()

    def _reader(self, proc: subprocess.Popen):
        inp = proc.stdout
        while True:
            try:
                frame = _read_frame(inp)
            except Exception:
                frame = None
            if frame is None:
                return
            with self._lock:
                if len(self._frames) == self._frames.maxlen:
                    self.n_dropped += 1
                self._frames.append(frame)

    def ensure_running(self):
        """Start the child, or restart a dead one with backoff. Safe to call
        every drain."""
        if not self.plan or self.alive():
            if self.alive() and self._clock() - self._started_at > HEALTHY_S:
                self._backoff = RESTART_MIN_S
            return
        now = self._clock()
        if self._proc is not None:
            if self.last_exit is None:
                self.last_exit = self._proc.returncode
                self._next_try = now + self._backoff
                print(f"[spot/venues] venue process exited ({self.last_exit}); "
                      f"restarting in {self._backoff:.0f}s", flush=True)
                self._backoff = min(self._backoff * 2, RESTART_MAX_S)
            if now < self._next_try:
                return
        try:
            self.last_exit = None
            self.start()
        except Exception as e:
            # Pricing carries on Coinbase-only; venues just drop out of the median.
            print(f"[spot/venues] could not start venue process ({e!r})", flush=True)
            self._proc = None
            self._next_try = now + self._backoff
            self._backoff = min(self._backoff * 2, RESTART_MAX_S)

    def drain(self, on_book, on_print=None, on_alive=None, max_batches: int = 50) -> int:
        """Apply what the child has shipped. Wall-clock receipt times become
        this process's monotonic clock here."""
        with self._lock:
            frames = [self._frames.popleft() for _ in range(min(max_batches, len(self._frames)))]
        off = time.monotonic() - time.time()
        for books, ps, alive in frames:
            if on_alive is not None:
                for venue, coin, wall in alive:
                    on_alive(venue, coin, wall + off)
            for venue, coin, product, bids, asks, recv_wall, exch_ts, resync in books:
                vb = VenueBook(venue=venue, product=product, coin=coin,
                               bids=tuple(OrderBookLevel(p, s) for p, s in bids),
                               asks=tuple(OrderBookLevel(p, s) for p, s in asks),
                               receive_ts=recv_wall + off, exchange_ts=exch_ts, is_resyncing=resync)
                self.n_books += 1
                on_book(vb)
            if on_print is not None:
                for kind, coin, t, recv_wall, exch_ts in ps:
                    on_print(coin, t, recv_wall + off, exch_ts, kind)
        self.n_batches += len(frames)
        return len(frames)

    def stop(self):
        p = self._proc
        if p is None:
            return
        try:
            p.stdin.close()            # the child exits on EOF
            p.wait(2)
        except Exception:
            pass
        if p.poll() is None:
            p.kill()

    def summary(self) -> dict:
        return {"alive": self.alive(), "starts": self.n_starts, "last_exit": self.last_exit,
                "batches": self.n_batches, "books": self.n_books, "dropped_frames": self.n_dropped}


if __name__ == "__main__":
    _child_main(json.loads(sys.argv[1]))


__all__ = ["VenueProcess", "SHIP_S"]
