"""Score a recorded print (prints.py row) as a maker quote at each edge would
have met it: filled by the prints at or through its price, then exited as our
completion would when it lands: the cheaper of buying the other legs or
unwinding (buying the filled leg's other side, $1 a pair). Shared by the runner's
shadow estimator (live_params.py) and offline studies.

With `lag`, the quote is priced on the books `lag` seconds before the print
(the row's "pre" tops): a quote resting then hadn't seen a move on the other
legs yet, which is how most of our live losses fill. Without it, on the books
at the print.
"""
from __future__ import annotations

import math

TICK = 0.01
# Contracts tried when sizing a quote to what the other legs can complete.
_SIZES = (1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 75, 100, 150, 200, 300, 400)
# Price for contracts the 1 s book can't complete either: its worst level plus this.
SHORT_PENALTY = 0.03


def _fee(p: float, n: float, rate: float) -> float:
    return rate * n * p * (1.0 - p) if n > 0 and rate > 0 else 0.0


def legs_of(basket_id: str, tickers) -> list[tuple[str, str]] | None:
    """Legs from a scanned basket id; a complete set's are its event's markets
    among `tickers`. None for a shape it can't read (hand-spec ids)."""
    kind, _, rest = basket_id.partition(":")
    if kind == "mecnet":
        ev, side, names = rest.rsplit(":", 2)
        return [(f"{ev}-{n}", side) for n in names.split("+")]
    if kind == "ladder":
        ev, pair = rest.rsplit(":", 1)
        a, _, b = pair.partition("=>")
        return [(f"{ev}-{b}", "yes"), (f"{ev}-{a}", "no")]
    if kind == "complete_set":
        ev, how = rest.rsplit(":", 1)
        side = "yes" if how.endswith("yes") else "no"
        return [(t, side) for t in tickers if t.startswith(ev + "-")]
    return None


def asks(books: dict, ticker: str, side: str) -> list[tuple[float, float]] | None:
    """Asks to buy `side` on `ticker`, cheapest first: the other side's bids."""
    b = books.get(ticker)
    if not b:
        return None
    return [(round(1.0 - p, 4), q) for p, q in b["no" if side == "yes" else "yes"]]


def lagged_ask(pre: dict, ticker: str, side: str, lag: float) -> tuple[float, float] | None:
    """(price, others' size) to buy `side` at the top `lag` s before the print."""
    for row in pre.get(ticker) or ():
        if abs(row[0] - lag) < 1e-9:
            _, bid, bid_q, ask, ask_q = row
            if side == "yes":
                return None if ask is None else (ask, ask_q)
            return None if bid is None else (round(1.0 - bid, 4), bid_q)
    return None


_OTHER = {"yes": "no", "no": "yes"}


def _take(end: dict, late: dict, ticker: str, side: str, n: float, rate: float) -> float:
    """Cost to buy n of `side` on `end`'s book, else `late`'s, the rest at its
    worst level plus SHORT_PENALTY."""
    cc, got = _walk(asks(end, ticker, side) or [], n, rate)
    if got < n - 1e-9:
        lv1 = asks(late, ticker, side) or []
        cc, got = _walk(lv1, n, rate)
        if got < n - 1e-9:
            cc += (n - got) * min(0.99, (lv1[-1][0] if lv1 else 0.99) + SHORT_PENALTY)
    return cc


def _walk(lv, n: float, rate: float) -> tuple[float, float]:
    cost = got = 0.0
    for p, q in lv:
        k = min(q, n - got)
        if k <= 0:
            break
        cost += k * p + _fee(p, k, rate)
        got += k
    return cost, got


def score(row: dict, rates, edges, size: float, lag: float | None = None, done: str = "0.1",
          live_only: bool = False) -> dict | None:
    """The best qualifying basket's (by edge at the print) outcome at each
    edge: {"basket_id", "live", "g0", "fills": [...], "pnl": [...]} with pnl in
    dollars for the contracts filled. `rates(ticker)` -> (taker, maker) fee
    rates. None when no basket's legs could be read off the row."""
    books, pre = row["books"], row.get("pre") or {}
    after = row.get("after") or {}
    end = after.get(done) or after.get("0.03") or books
    late = after.get("1.0") or end
    tk, side, pr = row["ticker"], row["side"], row["prints"]
    pmin = min(p for _, p, _ in pr)
    best = None
    for bid, payout, live in row["baskets"]:
        if live_only and not live:
            continue
        legs = legs_of(bid, books)
        if not legs or (tk, side) not in legs:
            continue
        others = [l for l in legs if l[0] != tk]
        s0 = 0.0
        quoted = []
        for ot, os_ in others:
            if lag is not None:
                top = lagged_ask(pre, ot, os_, lag)
                lv = None if top is None else [top]
            else:
                lv = asks(books, ot, os_)
            if not lv:
                break
            s0 += lv[0][0] + _fee(lv[0][0], 1, rates(ot)[0])
            quoted.append(lv)
        else:
            mrate = rates(tk)[1]
            g0 = payout - pmin - _fee(pmin, 1, mrate) - s0
            if best is None or g0 > best[0]:
                best = (g0, bid, payout, live, others, quoted, s0, mrate)
    if best is None:
        return None
    g0, bid, payout, live, others, quoted, s0, mrate = best
    fills, pnl = [], []
    for e in edges:
        x = math.floor((payout - s0 - e - _fee(pr[0][1], 1, mrate)) / TICK + 1e-6) * TICK
        n = 0.0
        if 0 < x < 1:
            # Prints through our price fill us first; ties share the queue.
            avail = sum(q for _, p, q in pr if p < x - 1e-9) + 0.5 * sum(q for _, p, q in pr if abs(p - x) < 1e-9)
            cap = 0.0
            for k in _SIZES:
                c, ok = k * x + _fee(x, k, mrate), True
                for (ot, os_), lv in zip(others, quoted):
                    cc, got = _walk(lv, k, rates(ot)[0])
                    if got < k - 1e-9:
                        ok = False
                        break
                    c += cc
                if not ok or k * payout - c < e * k - 1e-9:
                    break
                cap = k
            n = min(size, avail, cap)
        if n <= 0:
            fills.append(0.0)
            pnl.append(0.0)
            continue
        c = n * x + _fee(x, n, mrate)
        complete = n * payout - c - sum(_take(end, late, ot, os_, n, rates(ot)[0]) for ot, os_ in others)
        # Our live exits took this path for 568 of 2,834 breaks (11-04..10); completing
        # only scored our real fills 1.3c/lot worse than they did.
        unwind = n - c - _take(end, late, tk, _OTHER[side], n, rates(tk)[0])
        fills.append(n)
        pnl.append(max(complete, unwind))
    return {"basket_id": bid, "live": live, "g0": g0, "fills": fills, "pnl": pnl}
