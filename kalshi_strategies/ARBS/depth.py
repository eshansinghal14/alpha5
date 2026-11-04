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


class PullWatch:
    """Others' depth at a market side's best bid over the last `window` s.
    fires() when it falls below (1 - drop) of its peak at the same price:
    other makers pulling ahead of a sweep. A price change isn't a pull; the
    peak is only taken over samples at today's price."""

    def __init__(self):
        self.h: dict[tuple[str, str], collections.deque] = {}

    def fires(self, key: tuple[str, str], now: float, price, depth: float, window: float, drop: float) -> bool:
        d = self.h.get(key)
        if d is None:
            d = self.h[key] = collections.deque()
        while d and now - d[0][0] > window:
            d.popleft()
        peak = max((x[2] for x in d if x[1] == price), default=0.0)
        if not d or (d[-1][1], d[-1][2]) != (price, depth):
            d.append((now, price, depth))
        return price is not None and peak > 0 and depth < (1 - drop) * peak

    def forget(self, key: tuple[str, str]):
        self.h.pop(key, None)


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

    def at(self, ticker: str, t: float):
        """(bid, bid depth, ask, ask depth) in effect at time t, or None if the
        history doesn't reach back that far."""
        d = self.h.get(ticker)
        if not d or d[0][0] > t:
            return None
        for s in reversed(d):
            if s[0] <= t:
                return s[1:]
        return None

    def pull_ratio(self, ticker: str, side: str, now: float, window: float) -> float | None:
        """Others' depth at the side's best bid now over its peak at that same
        price within `window` (the pull rule's measure, depth.PullWatch), for
        any market whether quoted or not. None with no bid or no peak."""
        d = self.h.get(ticker)
        if not d:
            return None

        def touch(s):
            if side == "yes":
                return s[1], s[2]
            return (None if s[3] is None else round(1.0 - s[3], 4)), s[4]

        price, depth = touch(d[-1])
        if price is None:
            return None
        peak = max((q for s in d if now - s[0] <= window for p, q in (touch(s),) if p == price), default=0.0)
        return round(depth / peak, 3) if peak > 0 else None

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
