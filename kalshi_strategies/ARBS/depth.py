"""Top-of-book history per market, for telling whether other makers pull
ahead of the fills that lose us money.

Each market keeps HIST_S of (time, yes bid, others' yes bid depth, yes ask,
others' depth at the yes ask) -- our own resting size taken out -- one
sample per COALESCE_S. At every real quote fill the history of the
basket's markets is logged; at each print-log flush (10 s) one resting
quote's basket is logged the same way with no fill, the base rate a pull
signal is judged against. Rows go to data/live/ARBS/fill_context/<day>.jsonl.gz.
"""
from __future__ import annotations

import collections
import gzip
import json
import os

HIST_S = 10.0
COALESCE_S = 0.05


class TopHistory:
    def __init__(self, root: str):
        self.dir = os.path.join(root, "fill_context")
        self.h: dict[str, collections.deque] = {}
        self.rows: list[dict] = []

    def on_book(self, ticker: str, now: float, bid, bid_depth, ask, ask_depth):
        d = self.h.get(ticker)
        if d is None:
            d = self.h[ticker] = collections.deque()
        s = (now, bid, round(bid_depth or 0.0, 2), ask, round(ask_depth or 0.0, 2))
        if d and now - d[-1][0] < COALESCE_S:
            d[-1] = s
        else:
            d.append(s)
        while d and now - d[0][0] > HIST_S:
            d.popleft()

    def context(self, tickers, now: float) -> dict:
        """ticker -> [[seconds before now, bid, bid depth, ask, ask depth], ...], oldest first."""
        out = {}
        for t in tickers:
            d = self.h.get(t)
            if d:
                out[t] = [[round(now - s[0], 3), *s[1:]] for s in d if now - s[0] <= HIST_S]
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
