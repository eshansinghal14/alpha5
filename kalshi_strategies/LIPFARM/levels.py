"""
Guess the orders behind each book level from the websocket's level changes
(Kalshi shows only each price's total, never its orders).

A level is a FIFO list of chunks [size, added_at, tag]. A size increase is a
new order joining the back. A decrease is classified once NEG_WAIT_S has
passed, so the trade that caused it has had time to arrive: a trade at that
price within TRADE_MATCH_S makes it a fill, taken from the front (Kalshi
fills in time order); otherwise it's a cancel, matched to a chunk of exactly
that size if there is one (newest first), else taken from an untagged
snapshot block, else from the back. A snapshot that disagrees with what we
have adds or removes a "snap" chunk of the difference.

Our own orders are tagged when their create is answered (matched to an
untagged chunk of the same size added in the last OURS_MATCH_S, or to the
next one that arrives), so the size ahead of one of our orders can be
checked against Kalshi's queue_positions -- the accuracy test.
"""
from __future__ import annotations

NEG_WAIT_S = 0.3
TRADE_MATCH_S = 1.0
OURS_MATCH_S = 5.0
EPS = 1e-6


class Levels:
    def __init__(self):
        self.q: dict[tuple, list] = {}            # (ticker, side, price) -> [[size, added_at, tag], ...]
        self.trades: dict[tuple, float] = {}      # (ticker, side, price) -> last print there (maker side)
        self.neg: list[tuple] = []                # (at, key, size) decreases awaiting classification
        self.pending_ours: list[tuple] = []       # (at, key, size, oid) acked, not seen in the book yet
        self.n = {"fill": 0, "cancel_exact": 0, "cancel_snap": 0, "cancel_back": 0, "ours_tagged": 0}

    @staticmethod
    def key(t: str, side: str, price: float) -> tuple:
        return (t, side, round(price, 4))

    # ---- feed ----
    def snapshot(self, t: str, side: str, levels, now: float):
        seen = set()
        for p, size in levels:
            k = self.key(t, side, p)
            seen.add(k)
            have = self.size(k)
            if size > have + EPS:
                self.q.setdefault(k, []).append([size - have, now, "snap"])
            elif size < have - EPS:
                self._take_back(k, have - size)
        for k in [k for k in self.q if k[0] == t and k[1] == side and k not in seen]:
            del self.q[k]

    def delta(self, t: str, side: str, price: float, d: float, now: float):
        self.flush(now)
        k = self.key(t, side, price)
        if d > 0:
            for i, (at, kk, size, oid) in enumerate(self.pending_ours):
                if kk == k and abs(size - d) < EPS and now - at <= OURS_MATCH_S:
                    self.q.setdefault(k, []).append([d, now, oid])
                    del self.pending_ours[i]
                    self.n["ours_tagged"] += 1
                    return
            self.q.setdefault(k, []).append([d, now, None])
        elif d < 0:
            self.neg.append((now, k, -d))

    def trade(self, t: str, maker_side: str, price: float, now: float):
        self.trades[self.key(t, maker_side, price)] = now

    def flush(self, now: float):
        """Classify decreases old enough for their trade to have arrived."""
        keep = []
        for at, k, size in self.neg:
            if now - at < NEG_WAIT_S:
                keep.append((at, k, size))
                continue
            last = self.trades.get(k)
            if last is not None and abs(last - at) <= TRADE_MATCH_S:
                self._take_front(k, size)
                self.n["fill"] += 1
            else:
                self._cancel(k, size)
        self.neg = keep

    # ---- ours ----
    def ours(self, t: str, side: str, price: float, size: float, oid: str, now: float):
        k = self.key(t, side, price)
        for c in reversed(self.q.get(k, [])):
            if c[2] is None and abs(c[0] - size) < EPS and now - c[1] <= OURS_MATCH_S:
                c[2] = oid
                self.n["ours_tagged"] += 1
                return
        self.pending_ours = [x for x in self.pending_ours if now - x[0] <= OURS_MATCH_S] + [(now, k, size, oid)]

    def ahead_of(self, t: str, side: str, price: float, oid: str) -> float | None:
        ahead = 0.0
        for size, _, tag in self.q.get(self.key(t, side, price), []):
            if tag == oid:
                return ahead
            ahead += size
        return None

    # ---- reading ----
    def size(self, k) -> float:
        return sum(c[0] for c in self.q.get(k, []))

    def view(self, t: str, side: str, price: float) -> dict:
        """Chunks at a level: count, sizes, and how many are an unknown snapshot block or ours."""
        cs = self.q.get(self.key(t, side, price), [])
        return {"n": len(cs), "sizes": [round(c[0], 2) for c in cs], "snap": sum(c[2] == "snap" for c in cs),
                "ours": sum(c[2] not in (None, "snap") for c in cs)}

    # ---- internals ----
    def _take_front(self, k, size):
        cs = self.q.get(k, [])
        while size > EPS and cs:
            take = min(size, cs[0][0])
            cs[0][0] -= take
            size -= take
            if cs[0][0] <= EPS:
                cs.pop(0)

    def _take_back(self, k, size):
        cs = self.q.get(k, [])
        while size > EPS and cs:
            take = min(size, cs[-1][0])
            cs[-1][0] -= take
            size -= take
            if cs[-1][0] <= EPS:
                cs.pop()

    def _cancel(self, k, size):
        cs = self.q.get(k, [])
        for i in range(len(cs) - 1, -1, -1):
            if abs(cs[i][0] - size) < EPS:
                del cs[i]
                self.n["cancel_exact"] += 1
                return
        for i in range(len(cs) - 1, -1, -1):
            if cs[i][2] == "snap":
                take = min(size, cs[i][0])
                cs[i][0] -= take
                size -= take
                if cs[i][0] <= EPS:
                    del cs[i]
                self.n["cancel_snap"] += 1
                if size <= EPS:
                    return
                break
        self._take_back(k, size)
        self.n["cancel_back"] += 1
