"""
Snapshot/journal writer in its own process.

The trading loop only appends rows to small buffers and ships the new ones
once a second (`flush`). Everything that costs CPU -- DataFrames, parquet,
chart streams, the day journal -- happens in the child, so it cannot hold
the trading process's GIL. On a thread it did: a heavy flush took ~1.5s of
pandas work and stalled the event loop up to 2.1s (2026-10-09 crypto
shadow), which is longer than risk.max_signal_age_s.

Messages are applied in order, so a session reset or event start can never
be overtaken by a late write from the previous one.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import time
import traceback

HEAVY_INTERVAL_S = 3.0      # parquets + chart streams; the dashboard chart polls every 1.5s
JOURNAL_INTERVAL_S = 30.0   # one immutable part file per stream per this
MAX_DEBUG_ROWS = 20_000     # heavy-write cost scales with this; the oldest half is thinned past it
MAX_HISTORY_ROWS = 20_000
STOP_TIMEOUT_S = 15.0


def _thin(seq: list, cap: int):
    """Halve the oldest half past `cap`: keeps the full span at reduced
    resolution (the chart decimates anyway)."""
    if len(seq) > cap:
        half = len(seq) // 2
        seq[:half] = seq[:half:2]


class _Event:
    def __init__(self, series: str, date: str):
        from kalshi_infra.runtime import journal as jr
        self.series, self.date = series, date
        self.journal = jr.DayJournal(series, date)
        self.decisions: list[dict] = []
        self.fills: list = []
        self.fair: list[dict] = []
        self.pnl: list[tuple] = []
        self.pnl_by_ticker: dict[str, list] = {}
        self.summary: dict | None = None
        self.dirty = True

    def reset(self):
        self.decisions.clear()
        self.fills.clear()
        self.fair.clear()
        self.pnl.clear()
        self.pnl_by_ticker.clear()
        self.dirty = True

    def apply(self, d: dict):
        self.decisions.extend(d["decisions"])
        for r in d["decisions"]:
            self.journal.record_decision(r)
        for r in d["bbo"]:
            self.journal._buf["bbo"].append(r)
        self.fair.extend(d["fair"])
        self.pnl.extend(d["pnl"])
        for t, rows in d["pnl_by_ticker"].items():
            self.pnl_by_ticker.setdefault(t, []).extend(rows)
        if d["fills"]:
            self.fills.extend(d["fills"])
            self.journal.record_fills(d["fills"], d["live_trading"])
        _thin(self.decisions, MAX_DEBUG_ROWS)
        _thin(self.fair, MAX_HISTORY_ROWS)
        _thin(self.pnl, MAX_HISTORY_ROWS)
        for rows in self.pnl_by_ticker.values():
            _thin(rows, 4000)
        self.summary = d["summary"]
        if d["decisions"] or d["fills"] or d["fair"] or d["pnl"]:
            self.dirty = True

    def state(self, heavy: bool) -> dict:
        return {"series": self.series, "date": self.date, "heavy": heavy, "summary": self.summary,
                "debug_log": self.decisions, "fills": self.fills, "pnl_history": self.pnl,
                "fair_history": self.fair, "pnl_by_ticker": self.pnl_by_ticker}


_HEAVY_PHASES: dict = {}


def _write_summary(ev: _Event):
    from kalshi_infra.runtime import persist
    if ev.summary is None:
        return
    ev.summary.setdefault("timings", {})["write_phases_ms"] = dict(_HEAVY_PHASES)
    persist.write(ev.state(heavy=False))


def _write_heavy(ev: _Event):
    from kalshi_infra.runtime import persist
    if ev.summary is None:
        return
    ev.summary.setdefault("timings", {})["write_phases_ms"] = dict(_HEAVY_PHASES)
    persist.write(ev.state(heavy=True))
    _HEAVY_PHASES.clear()
    _HEAVY_PHASES.update(persist.WRITE_PHASES)
    ev.dirty = False


def _write_journal(ev: _Event):
    from kalshi_infra.runtime import journal as jr
    jr.write(ev.journal.capture())


def _main(q: mp.Queue, series: str):
    from kalshi_infra.runtime import persist
    ev: _Event | None = None
    last_heavy = last_journal = 0.0

    def guarded(fn, what):
        try:
            fn(ev)
        except Exception:
            print(f"[{series}] WARN: {what} failed:", flush=True)
            traceback.print_exc()

    parent = mp.parent_process()
    while True:
        # A force-killed trading process never sends "stop"; without this the
        # writer outlives it (seen 2026-10-09 when the sessions were killed).
        if parent is not None and not parent.is_alive():
            return
        try:
            msgs = [q.get(timeout=0.25)]
        except queue.Empty:
            msgs = []
        while True:   # drain: only the newest summary matters
            try:
                msgs.append(q.get_nowait())
            except queue.Empty:
                break
        stop = False
        summary_due = False
        for kind, payload in msgs:
            if kind == "start":
                if ev is not None:
                    guarded(_write_heavy, "final snapshot")
                    guarded(_write_journal, "journal write")
                persist.reset_session_files(series)
                ev = _Event(series, payload["date"])
                last_heavy = 0.0
                last_journal = time.monotonic()
            elif ev is None:
                continue
            elif kind == "data":
                ev.apply(payload)
                summary_due = True
            elif kind == "reset":
                ev.reset()
                persist.reset_session_files(series)
            elif kind == "end":
                guarded(_write_heavy, "final snapshot")
                guarded(_write_journal, "journal write")
            elif kind == "stop":
                stop = True
        if ev is not None:
            now = time.monotonic()
            if ev.dirty and now - last_heavy >= HEAVY_INTERVAL_S:
                guarded(_write_heavy, "snapshot write")
                last_heavy = time.monotonic()
            elif summary_due:
                guarded(_write_summary, "summary write")
            if now - last_journal >= JOURNAL_INTERVAL_S:
                guarded(_write_journal, "journal write")
                last_journal = now
        if stop:
            if ev is not None:
                guarded(_write_heavy, "final snapshot")
                guarded(_write_journal, "journal write")
            return


class SnapshotWriter:
    """Trading-side handle. Every method is a cheap append or a queue put."""

    def __init__(self, series: str):
        self.series = series
        self._ctx = mp.get_context("spawn")
        self._q = None
        self._proc = None
        self._date = None
        self._spawn()
        self._clear_buffers()
        self._last_bbo: dict[str, tuple] = {}
        self._last_decision: dict[str, tuple] = {}
        self._fills_sent = 0

    def _spawn(self):
        self._q = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_main, args=(self._q, self.series), daemon=True,
                                       name=f"writer-{self.series}")
        self._proc.start()

    def _clear_buffers(self):
        self._decisions, self._bbo, self._fair, self._pnl = [], [], [], []
        self._pnl_by_ticker: dict[str, list] = {}

    def _put(self, msg):
        if not self._proc.is_alive():
            # The writer only affects what the dashboard sees; trading goes
            # on, but say so and bring it back.
            print(f"[{self.series}] WARN: writer process died (exit {self._proc.exitcode}); restarting",
                  flush=True)
            self._spawn()
            if self._date is not None:
                self._q.put(("start", {"date": self._date}))
        self._q.put(msg)

    # --- per event ---
    def start_event(self, date: str):
        self._date = date
        self._clear_buffers()
        self._last_bbo.clear()
        self._last_decision.clear()
        self._fills_sent = 0
        self._put(("start", {"date": date}))

    def reset_session(self):
        self._clear_buffers()
        self._last_decision.clear()
        self._fills_sent = 0
        self._put(("reset", None))

    def end_event(self):
        self._put(("end", None))

    def stop(self):
        try:
            self._put(("stop", None))
            self._proc.join(STOP_TIMEOUT_S)
        except Exception:
            traceback.print_exc()

    # --- hot path ---
    def decision(self, row: dict, key: tuple):
        """Drops a row identical (on `key`) to the previous one for its
        ticker: 61% of crypto rows were Kalshi depth changes that moved
        nothing we decide on (2026-10-09)."""
        tk = row.get("ticker")
        if self._last_decision.get(tk) == key:
            return
        self._last_decision[tk] = key
        self._decisions.append(row)

    def bbo(self, book):
        key = (book.bid, book.ask, book.bid_depth, book.ask_depth)
        if self._last_bbo.get(book.ticker) == key:
            return
        self._last_bbo[book.ticker] = key
        self._bbo.append({"time": book.time, "ticker": book.ticker, "bid": book.bid, "ask": book.ask,
                          "bid_depth": book.bid_depth, "ask_depth": book.ask_depth})

    def fair(self, row: dict):
        self._fair.append(row)

    def pnl(self, now, value: float):
        self._pnl.append((now, value))

    def pnl_ticker(self, ticker: str, now, value: float):
        self._pnl_by_ticker.setdefault(ticker, []).append((now, value))

    def flush(self, summary: dict, fills: list, live_trading: bool):
        if len(fills) < self._fills_sent:
            self._fills_sent = 0
        new_fills = list(fills[self._fills_sent:])
        self._fills_sent = len(fills)
        self._put(("data", {"summary": summary, "decisions": self._decisions, "bbo": self._bbo,
                            "fair": self._fair, "pnl": self._pnl, "pnl_by_ticker": self._pnl_by_ticker,
                            "fills": new_fills, "live_trading": live_trading}))
        self._clear_buffers()


class DecisionLog(list):
    """Stands in for Quoter.debug_log in a live session: forwards each row to
    the writer and keeps nothing (the writer owns the session's history)."""

    def __init__(self, writer: SnapshotWriter, key_fn):
        super().__init__()
        self._writer, self._key_fn = writer, key_fn

    def append(self, row):
        self._writer.decision(row, self._key_fn(row))

    def clear(self):
        self._writer._last_decision.clear()
