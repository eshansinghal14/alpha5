"""
Append-only day journal for a live session: Kalshi BBO, strategy decisions,
our fills and the public trade tape, written as immutable part files under
data/live/{series}/{date}/journal/{stream}/.

The snapshot (runtime.persist) is a view of the CURRENT session and is
rewritten wholesale, so a restart or an arm (which resets the ledger)
replaces what came before. Journal parts are written once, numbered from
whatever is on disk, so the day survives both. Snapshot = live view;
journal = record.

BBO is journaled unconditionally, so a quiet stretch reads as "market, no
action" rather than missing data. Lives in the writer process
(runtime.writer), off the trading loop.
"""
import glob
import os
import warnings

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

STREAMS = ("bbo", "decisions", "fills", "trades")


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
        self._seen_orders: set = set()
        self._seen_trades: set = set()
        self._seen_prints: set = set()
        self._load_seen_exchange_ids()
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

    def record_trades(self, rows) -> None:
        """Public prints, once per trade_id within this process."""
        for r in rows:
            tid = r.get("trade_id")
            if tid is not None:
                if tid in self._seen_prints:
                    continue
                self._seen_prints.add(tid)
            self._buf["trades"].append(r)

    def _load_seen_exchange_ids(self) -> None:
        """Ids already journaled today, so a restarted process's replayed
        fills are recognised: every trade id, plus the order ids of fills
        that had no trade id. Parts written before these columns existed
        contribute nothing."""
        for path in glob.glob(os.path.join(self.dir, "fills", "part-*.parquet")):
            try:
                df = pd.read_parquet(path)
            except Exception:
                continue
            if "trade_id" in df.columns:
                self._seen_trades.update(df["trade_id"].dropna())
                if "exchange_order_id" in df.columns:
                    self._seen_orders.update(df.loc[df["trade_id"].isna(), "exchange_order_id"].dropna())

    def record_fills(self, fills, live_trading: bool) -> None:
        """Journal any fill not already journaled.

        Real fills are keyed on Kalshi's ids. Our own order_id is not usable:
        seed_fills_from_kalshi replays an event's fills on every arm under
        fresh local ids and Kalshi's timestamps, so a local key journaled
        each replay as a new fill (2026-10-09/10: 577 journal rows vs 495
        real fills, $103 vs $32 PnL)."""
        for f in fills:
            xo, tid = getattr(f, "exchange_order_id", None), getattr(f, "trade_id", None)
            if tid is not None and tid in self._seen_trades:
                continue
            if xo is not None and xo in self._seen_orders and (tid is None or getattr(f, "seeded", False)):
                # An order booked off its create response carries no trade id,
                # and a sweep comes back as one summed fill but replays as one
                # row per level -- so its replay can only match on the order.
                continue
            if xo is None and tid is None:
                key = (f.order_id, str(f.time), f.ticker, f.side, f.price, f.size)
                if key in self._seen_fills:
                    continue
                self._seen_fills.add(key)
            if tid is not None:
                self._seen_trades.add(tid)
            elif xo is not None:
                self._seen_orders.add(xo)
            self._buf["fills"].append({
                "time": f.time, "ticker": f.ticker, "side": f.side, "price": f.price,
                "size": f.size, "fee": f.fee, "liquidity": f.liquidity,
                "order_id": f.order_id, "exchange_order_id": xo, "trade_id": tid,
                "seeded": bool(getattr(f, "seeded", False)),
                "role": getattr(f, "role", None),
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


def read_journal(series: str, date: str, stream: str, out_root: str = None,
                 columns: list[str] | None = None, ticker_prefix: str | None = None) -> pd.DataFrame:
    """The whole day's `stream`, across every process that wrote it today.

    Pass `columns` and `ticker_prefix` when reading on the live box: a full
    day of decisions is ~2.5 GB in memory, and on EC2 (3.8 GB, trading uses
    ~1.4 GB) that got the reader OOM-killed (2026-10-12).

    Returns an empty frame when nothing has been journaled. A part that
    fails to read is skipped rather than raised on -- the newest one can be
    mid-write, and the day's history is worth more than its last 30s."""
    parts = sorted(glob.glob(os.path.join(journal_dir(series, date, out_root), stream, "part-*.parquet")))
    if not parts:
        return pd.DataFrame()

    def _filter(df):
        if ticker_prefix is not None and "ticker" in df.columns:
            df = df[df["ticker"].str.startswith(ticker_prefix)]
        return df

    # One dataset read is ~10x faster than a read_parquet per part (106
    # fills parts: 1.1s -> ~0.1s); fall back to per-part only when one part
    # is unreadable or the parts' schemas disagree.
    try:
        import pyarrow.compute as pc
        import pyarrow.dataset as ds
        flt = None if ticker_prefix is None else pc.starts_with(pc.field("ticker"), ticker_prefix)
        df = ds.dataset(parts, format="parquet").to_table(columns=columns, filter=flt).to_pandas()
        if "time" in df.columns:
            df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception:
        pass
    frames = []
    for path in parts:
        try:
            frames.append(_filter(pd.read_parquet(path, columns=columns)))
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
