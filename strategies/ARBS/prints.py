"""Trade-time book snapshots for replaying quote settings offline.

For each print on a basket leg that a quote could have made edge on (the
print's price plus the other legs' asks within PRINT_SLACK of the payout),
one row: the prints (maker-side price and size; a sweep's prints within
MERGE_S on one market share a row), the baskets that qualified, the top
DEPTH bid levels of every market in them at the print and AFTER_S later
(when a completion would land, and a markout), and our own resting size
there (the simulator takes it back out of the book).

Written to data/live/ARBS/prints/<day>.jsonl.gz.
"""
from __future__ import annotations

import gzip
import heapq
import json
import os

DEPTH = 5
MERGE_S = 0.05
# 0.1: about when our completion lands (fill -> exit decision 35/62 ms p50/p90
# plus ~10 ms order RTT, measured 10-30).
AFTER_S = (0.03, 0.1, 1.0)
# A row is written once its last snapshot is in.
WRITE_AFTER_S = max(AFTER_S) + 0.5
# Rows held unwritten at most: past it new prints are dropped (and counted).
MAX_PENDING = 50000


def top(bids: dict[float, float]) -> list[list[float]]:
    return [[p, round(q, 2)] for p, q in heapq.nlargest(DEPTH, bids.items()) if q > 0]


class Prints:
    def __init__(self, root: str):
        self.dir = os.path.join(root, "prints")
        self.pending: list[dict] = []
        self.open: dict[tuple[str, str], dict] = {}
        self.dropped = 0
        self.rows = 0

    def merge(self, now: float, ticker: str, side: str, price: float, size: float) -> bool:
        """Adds the print to its market's row from the last MERGE_S, if any."""
        row = self.open.get((ticker, side))
        if row is None or now - row["t"] > MERGE_S:
            return False
        row["prints"].append([round(now - row["t"], 4), price, size])
        return True

    def start(self, now: float, ticker: str, side: str, price: float, size: float, baskets: list,
              books: dict, own: dict) -> dict | None:
        if len(self.pending) >= MAX_PENDING:
            self.dropped += 1
            return None
        row = {"t": now, "ticker": ticker, "side": side, "prints": [[0.0, price, size]], "baskets": baskets,
               "books": books, "own": own, "after": {}}
        self.open[(ticker, side)] = row
        self.pending.append(row)
        self.rows += 1
        return row

    def take(self, now: float) -> list[dict]:
        """Rows complete by `now`, in order; the rest stay pending."""
        n = 0
        while n < len(self.pending) and now - self.pending[n]["t"] >= WRITE_AFTER_S:
            n += 1
        done, self.pending = self.pending[:n], self.pending[n:]
        for r in done:
            if self.open.get((r["ticker"], r["side"])) is r:
                del self.open[(r["ticker"], r["side"])]
        return done

    def write(self, rows: list[dict], day: str):
        if not rows:
            return
        os.makedirs(self.dir, exist_ok=True)
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
        with gzip.open(os.path.join(self.dir, f"{day}.jsonl.gz"), "at", encoding="utf-8") as fh:
            fh.write(text)
