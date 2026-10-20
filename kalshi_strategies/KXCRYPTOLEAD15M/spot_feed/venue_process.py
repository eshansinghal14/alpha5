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
# A book whose touch (best bid/ask or resync state) changed goes out on its
# own frame at once and wakes the parent's drain; size-only changes, prints
# and keep-alives still ride the SHIP_S batch.
IMMEDIATE = True
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


def _read_exact(inp, n: int) -> bytes | None:
    """`n` bytes, or None at EOF. The pipes are unbuffered (bufsize=0), and an
    unbuffered read returns at most one pipe read's worth (4096 bytes per
    piece on Linux). A big frame read in one call came back short and was
    taken as EOF: the reader thread exited, the child blocked on a full pipe,
    and every non-Coinbase venue dropped out of the median for 11h44m
    (2026-10-19 07:34-19:18 UTC, after a 24k prints/s Binance sweep)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = inp.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_frame(inp):
    head = _read_exact(inp, _LEN.size)
    if head is None:
        return None
    (n,) = _LEN.unpack(head)
    data = _read_exact(inp, n)
    if data is None:
        return None
    return pickle.loads(data)


# ---------------------------------------------------------------------------
# Child
# ---------------------------------------------------------------------------

def _child_main(plan: dict[str, list[str]]):
    import asyncio
    import signal
    from .venues import ADAPTERS, LEADING_VENUES
    from ..prints import (PERP_BURST_USD_1S, PERP_SOURCES, REFRESH_S, SAMPLE_S, BurstCalibrator, LeadTrigger,
                          seed_move_rows)

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

    control: list[dict] = []

    def watch_parent():
        # stdin carries the parent's control frames (trigger thresholds); EOF
        # means the parent closed it or died.
        inp = sys.stdin.buffer
        while True:
            try:
                msg = _read_frame(inp)
            except Exception:
                msg = None
            if msg is None:
                os._exit(0)
            control.append(msg)
    threading.Thread(target=watch_parent, daemon=True).start()

    latest: dict[tuple[str, str], tuple] = {}
    prints: list[tuple] = []
    adapters = []
    coins_all = sorted({c for cs in plan.values() for c in cs})
    perps = tuple(v for v in PERP_SOURCES if v in plan)
    trigger = LeadTrigger(coins_all, perps)
    move_cal = {v: BurstCalibrator(coins_all, None) for v in perps}
    move_cal_ready = [False]

    def apply_control():
        while control:
            trigger.configure(vol_thr=control.pop(0).get("vol"))

    def lead_print(coin, t, as_of, exch_ts, kind):
        """Trigger check on every leading-venue print; a trip goes out on its
        own frame at once instead of riding the next SHIP_S batch."""
        apply_control()
        try:
            rows = trigger.on_print(kind, coin, float(t["price"]), float(t["size"]), as_of)
        except (KeyError, TypeError, ValueError):
            return
        if not rows:
            return
        recv_wall = time.time() - (time.monotonic() - as_of)
        try:
            _write_frame(out, ([], [], [], {"trips": [r + (recv_wall, exch_ts) for r in rows]}))
        except (BrokenPipeError, OSError):
            os._exit(0)

    async def move_calibrate():
        """Rolling MOVE_QUANTILE of each perp venue's |move over window_s|,
        same cadence as the volume calibrators, seeded from perp_prints/."""
        loop = asyncio.get_running_loop()
        for v, cal in move_cal.items():
            try:
                rows = await loop.run_in_executor(None, seed_move_rows, v, coins_all, time.time(),
                                                  trigger.trail.window_s)
                cal.seed(rows)
            except Exception as e:
                print(f"[spot/venues] move seed failed for {v} ({e!r})", flush=True)
        n = 0
        while True:
            now = time.monotonic()
            for v, cal in move_cal.items():
                cal.sample({c: abs(trigger.trail.move_bp((v, c), now) or 0.0) for c in coins_all})
            n += 1
            if n * SAMPLE_S >= REFRESH_S or not move_cal_ready[0]:
                n = 0
                thr = {}
                for v, cal in move_cal.items():
                    if cal.ready:
                        cal.adopt(await loop.run_in_executor(None, _quantile, cal.snapshot()))
                        thr[v] = dict(cal.live)
                if thr:
                    move_cal_ready[0] = True
                    trigger.configure(move_thr=thr)
                    status["move_thr"] = thr
            await asyncio.sleep(SAMPLE_S)

    status: dict = {}

    last_touch: dict[tuple[str, str], tuple] = {}

    def on_update(vb: VenueBook):
        k = (vb.venue, vb.coin)
        row = (vb.venue, vb.coin, vb.product,
               tuple((l.price, l.size) for l in vb.bids[:SHIP_LEVELS]),
               tuple((l.price, l.size) for l in vb.asks[:SHIP_LEVELS]),
               time.time() - (time.monotonic() - vb.receive_ts), vb.exchange_ts, vb.is_resyncing)
        touch = (vb.best_bid(), vb.best_ask(), vb.is_resyncing)
        if not IMMEDIATE or last_touch.get(k) == touch:
            latest[k] = row            # size-only change: rides the next batch
            return
        # Touch moved: send it now. Waiting for the batch cost 0-50ms here plus
        # 0-50ms on the parent's drain tick, about the size of the perps' lead.
        last_touch[k] = touch
        latest.pop(k, None)
        try:
            _write_frame(out, ([row], [], [], {}))
        except (BrokenPipeError, OSError):
            os._exit(0)

    perp_rows: list[dict] = []
    perp_usd: dict[tuple[str, str], tuple] = {}

    def on_print(coin, t, as_of, exch_ts, kind):
        if kind in PERP_BURST_USD_1S or kind.startswith("hl_"):
            lead_print(coin, t, as_of, exch_ts, kind)
        if kind in PERP_BURST_USD_1S:
            try:
                perp_rows.append({"recv_ts": time.time() - (time.monotonic() - as_of), "exch_ts": exch_ts,
                                  "venue": kind, "coin": coin, "price": float(t["price"]),
                                  "size": float(t["size"]), "side": t.get("side"),
                                  "trade_id": str(t.get("trade_id"))})
            except (KeyError, TypeError, ValueError):
                return
            # Shipped as one notional sum per (venue, coin) per ship, not per
            # print: Binance sweeps run to thousands of prints/s, and the burst
            # detector only needs the dollar total.
            k = (kind, coin)
            agg = perp_usd.get(k)
            wall = time.time() - (time.monotonic() - as_of)
            perp_usd[k] = (wall, (agg[1] if agg else 0.0) + perp_rows[-1]["price"] * perp_rows[-1]["size"], exch_ts)
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
            apply_control()
            books, ps = list(latest.values()), prints
            latest.clear()
            prints = []
            # Same 5-tuple shape as a print, with the batch's notional as a
            # size-1 "print" at price=usd. The parent only calibrates on these
            # now; the trip itself came from lead_print.
            for (kind, coin), (wall, usd, exch_ts) in perp_usd.items():
                ps.append((kind, coin, {"price": usd, "size": 1.0, "trade_id": None, "side": None}, wall, exch_ts))
            perp_usd.clear()
            extra = {"status": dict(status)} if status else {}
            try:
                _write_frame(out, (books, ps, alive(), extra))
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
        await asyncio.gather(ship(), record_perps(), move_calibrate(), *tasks)

    asyncio.run(main())


def _quantile(snap):
    from ..prints import MOVE_QUANTILE
    import numpy as np
    return np.quantile(snap, MOVE_QUANTILE, axis=1) if snap.shape[1] else np.zeros(snap.shape[0])


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
        # Called from the reader thread when a frame carries trips or an
        # immediate book, so the trading loop drains at once instead of on its
        # next tick.
        self.on_urgent = None
        self._control: dict | None = None
        self.status: dict = {}
        self._reader_thread: threading.Thread | None = None
        self.n_reader_restarts = 0

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
        self._reader_thread = threading.Thread(target=self._reader, args=(self._proc,), daemon=True,
                                               name="venue-reader")
        self._reader_thread.start()
        if self._control is not None:
            self.send_control(self._control)      # a restarted child knows nothing

    def send_control(self, msg: dict):
        """Trigger thresholds for the child ({"vol": {source: {coin: usd}}}).
        Kept and re-sent to every new child."""
        self._control = msg
        p = self._proc
        if p is None or p.poll() is not None:
            return
        try:
            _write_frame(p.stdin, msg)
        except (BrokenPipeError, OSError, ValueError):
            pass

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
            urgent = len(frame) > 3 and (frame[3].get("trips") or (frame[0] and not frame[1] and not frame[2]))
            if urgent and self.on_urgent is not None:
                try:
                    self.on_urgent()
                except Exception:
                    pass

    def ensure_running(self):
        """Start the child, or restart a dead one with backoff. Safe to call
        every drain. A child whose reader thread has ended is killed and
        restarted too: nothing would ever read its pipe again."""
        if self.alive() and self._reader_thread is not None and not self._reader_thread.is_alive():
            self.n_reader_restarts += 1
            print("[spot/venues] reader stopped while the venue process was alive; restarting it", flush=True)
            self._proc.kill()
            try:
                self._proc.wait(2)
            except Exception:
                pass
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

    def drain(self, on_book, on_print=None, on_alive=None, max_batches: int = 50, on_trip=None) -> int:
        """Apply what the child has shipped. Wall-clock receipt times become
        this process's monotonic clock here. on_trip(kind, source, coin,
        level, thr, onset, as_of, exch_ts) per trip row."""
        with self._lock:
            frames = [self._frames.popleft() for _ in range(min(max_batches, len(self._frames)))]
        off = time.monotonic() - time.time()
        for fr in frames:
            books, ps, alive = fr[:3]
            extra = fr[3] if len(fr) > 3 else {}
            if extra.get("status"):
                self.status = extra["status"]
            if on_trip is not None:
                for kind, source, coin, level, thr, onset, recv_wall, exch_ts in extra.get("trips") or ():
                    on_trip(kind, source, coin, level, thr, onset, recv_wall + off, exch_ts)
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
                "reader_alive": self._reader_thread is not None and self._reader_thread.is_alive(),
                "reader_restarts": self.n_reader_restarts,
                "batches": self.n_batches, "books": self.n_books, "dropped_frames": self.n_dropped,
                "move_thr_bp": self.status.get("move_thr")}


if __name__ == "__main__":
    _child_main(json.loads(sys.argv[1]))


__all__ = ["VenueProcess", "SHIP_S"]
