"""Top-of-book history per market, for telling whether other makers pull
ahead of the fills that lose us money.

Each market keeps HIST_S of (time, yes bid, others' yes bid depth, yes ask,
others' depth at the yes ask) -- our own resting size taken out -- one
sample per top price change, depth changes at most one per COALESCE_S. At
every real quote fill the history of the basket's markets is logged, with
Kalshi's own fill time (exch_t) when the fill carried one; at each print-log
flush (10 s) one resting quote's basket is logged the same way with no fill,
the base rate a pull signal is judged against. Rows go to
data/live/ARBS/fill_context/<day>.jsonl.gz.
"""
from __future__ import annotations

import collections
import gzip
import json
import os

HIST_S = 10.0
COALESCE_S = 0.01


class TopHistory:
    def __init__(self, root: str):
        self.dir = os.path.join(root, "fill_context")
        self.h: dict[str, collections.deque] = {}
        self.opened: dict[str, float] = {}
        self.rows: list[dict] = []

    def on_book(self, ticker: str, now: float, bid, bid_depth, ask, ask_depth):
        """A price change always opens a sample; depth-only changes within
        COALESCE_S of the sample's opening replace it."""
        d = self.h.get(ticker)
        if d is None:
            d = self.h[ticker] = collections.deque()
        s = (now, bid, round(bid_depth or 0.0, 2), ask, round(ask_depth or 0.0, 2))
        # Coalescing against the sample's opening, not its last update: chained
        # off the last update, a busy book stayed one sample and a move 30 ms
        # before a fill read as 0 ms.
        if d and (bid, ask) == (d[-1][1], d[-1][3]) and now - self.opened[ticker] < COALESCE_S:
            d[-1] = s
        else:
            d.append(s)
            self.opened[ticker] = now
        # Keep one sample from before the window: the book it opened on.
        while len(d) > 1 and now - d[1][0] > HIST_S:
            d.popleft()

    def context(self, tickers, now: float) -> dict:
        """ticker -> [[seconds before now, bid, bid depth, ask, ask depth], ...],
        oldest first. The first sample can be older than HIST_S: the book as it
        stood when the window opened (a quiet market has nothing newer)."""
        out = {}
        for t in tickers:
            d = self.h.get(t)
            if not d:
                continue
            rows, before = [], None
            for s in d:
                if now - s[0] > HIST_S:
                    before = s
                else:
                    rows.append([round(now - s[0], 3), *s[1:]])
            if before is not None:
                rows.insert(0, [round(now - before[0], 3), *before[1:]])
            out[t] = rows
        return out

    def add(self, row: dict):
        self.rows.append(row)

    def take(self) -> list[dict]:
        rows, self.rows = self.rows, []
        return rows

    def write(self, rows: list[dict], day: str):
        if not rows:
            return
        os.makedirs(self.dir, exist_ok=True)
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
        with gzip.open(os.path.join(self.dir, f"{day}.jsonl.gz"), "at", encoding="utf-8") as fh:
            fh.write(text)
