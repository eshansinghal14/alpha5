"""
Append-only day journal for a live session: Kalshi BBO, strategy decisions
and fills, written as immutable part files under
data/live/{series}/{date}/journal/{stream}/.

The snapshot (runtime.persist) is a view of the CURRENT session and is
rewritten wholesale, so a restart or an arm (which resets the ledger)
replaces what came before. Journal parts are written once, numbered from
whatever is on disk, so the day survives both. Snapshot = live view;
journal = record.

BBO is journaled unconditionally, so a quiet stretch reads as "market, no
action" rather than missing data. Buffers drain on the event loop (capture)
and serialise on a worker thread (write).
"""
import glob
import os
import warnings

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

STREAMS = ("bbo", "decisions", "fills")


def journal_dir(series: str, date: str, out_root: str = None) -> str:
    root = LIVE_ROOT if out_root is None else out_root
    return os.path.join(root, series, date, "journal")


class JournalingList(list):
    """A list that also hands each appended item to `sink`.

    `strategy.debug_log` is wrapped in one of these so decisions reach the
    journal at append time. Both of the things that mutate that list --
    the session's history pruning, which rewrites the oldest half, and
    LiveEngine.reset_session, which clears it -- would otherwise silently
    drop rows that were never journaled.
    """

    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def append(self, item):
        super().append(item)
        self._sink(item)


class DayJournal:
    """One day's append-only record for one series.

    Not thread-safe: record_*/capture all run on the event loop. Only
    `write` is safe off it, and it touches nothing but the dict handed to it.
    """

    def __init__(self, series: str, date: str, out_root: str = None):
        self.series = series
        self.date = date
        self.dir = journal_dir(series, date, out_root)
        self._buf: dict[str, list] = {s: [] for s in STREAMS}
        self._last_bbo: dict[str, tuple] = {}
        self._seen_fills: set = set()
        self._seq = self._next_seq()
        self.n_written: dict[str, int] = {s: 0 for s in STREAMS}

    def _next_seq(self) -> int:
        """Continue the numbering already on disk, so a restart appends to
        today rather than overwriting the parts a previous process wrote."""
        seq = 0
        for stream in STREAMS:
            for path in glob.glob(os.path.join(self.dir, stream, "part-*.parquet")):
                try:
                    seq = max(seq, int(os.path.basename(path)[5:-8]))
                except ValueError:
                    continue
        return seq + 1

    def record_bbo(self, book) -> None:
        """Journal a BookView, if its touch actually moved.

        On the hot path (every Kalshi book update), so it does a dict lookup
        and a tuple compare and nothing else. The dedupe is what keeps this
        proportional to market activity instead of to message volume -- the
        feed re-sends a ticker's book on depth changes that leave the touch
        where it was."""
        key = (book.bid, book.ask, book.bid_depth, book.ask_depth)
        if self._last_bbo.get(book.ticker) == key:
            return
        self._last_bbo[book.ticker] = key
        self._buf["bbo"].append({
            "time": book.time, "ticker": book.ticker,
            "bid": book.bid, "ask": book.ask,
            "bid_depth": book.bid_depth, "ask_depth": book.ask_depth,
        })

    def record_decision(self, row: dict) -> None:
        self._buf["decisions"].append(row)

    def record_fills(self, fills, live_trading: bool) -> None:
        """Journal any fill not already journaled.

        Keyed rather than indexed because the fill list is not append-only
        from this module's point of view: reset_session swaps in a fresh
        Portfolio, and seed_fills_from_kalshi then replays the event's
        earlier real fills into it. Both would re-present fills already
        recorded, and an index would also silently skip the new ones."""
        for f in fills:
            key = (f.order_id, str(f.time), f.ticker, f.side, f.price, f.size)
            if key in self._seen_fills:
                continue
            self._seen_fills.add(key)
            self._buf["fills"].append({
                "time": f.time, "ticker": f.ticker, "side": f.side, "price": f.price,
                "size": f.size, "fee": f.fee, "liquidity": f.liquidity,
                "order_id": f.order_id,
                # Which ledger this fill belongs to. A day can contain both,
                # and averaging shadow fills into a real session's PnL is
                # exactly the splice reset_session exists to prevent.
                "mode": "real" if live_trading else "shadow",
            })

    def capture(self) -> dict | None:
        """Drain the buffers. Cheap, and must run on the event loop."""
        rows = {s: buf for s, buf in self._buf.items() if buf}
        if not rows:
            return None
        self._buf = {s: [] for s in STREAMS}
        seq, self._seq = self._seq, self._seq + 1
        for s, r in rows.items():
            self.n_written[s] += len(r)
        return {"dir": self.dir, "seq": seq, "rows": rows}


def write(captured: dict | None) -> None:
    """Serialise one capture to part files. Safe on a worker thread.

    Written directly rather than through a tmp+replace: a part file is
    created once and never updated, so a torn write can only be a trailing
    file that a reader skips -- whereas the snapshot's files are overwritten
    in place, where a torn write would destroy the previous good copy."""
    if not captured:
        return
    for stream, rows in captured["rows"].items():
        out_dir = os.path.join(captured["dir"], stream)
        os.makedirs(out_dir, exist_ok=True)
        df = pd.DataFrame(rows)
        path = os.path.join(out_dir, f"part-{captured['seq']:06d}.parquet")
        df.to_parquet(path)


def read_journal(series: str, date: str, stream: str, out_root: str = None) -> pd.DataFrame:
    """The whole day's `stream`, across every process that wrote it today.

    Returns an empty frame when nothing has been journaled. A part that
    fails to read is skipped rather than raised on -- the newest one can be
    mid-write, and the day's history is worth more than its last 30s."""
    parts = sorted(glob.glob(os.path.join(journal_dir(series, date, out_root), stream, "part-*.parquet")))
    frames = []
    for path in parts:
        try:
            frames.append(pd.read_parquet(path))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    # A decisions part whose `bid_final`/`ask_final` are all None is an
    # all-NA column, which pandas warns it will one day type differently.
    # Harmless here -- the column is only ever read through notna() -- and
    # the alternative is this warning on every dashboard poll.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(frames, ignore_index=True)
    if "time" in df.columns:
        df = df.sort_values("time").reset_index(drop=True)
    return df
