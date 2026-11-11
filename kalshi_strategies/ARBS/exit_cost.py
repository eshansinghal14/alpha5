"""Predicted cost of exiting a maker fill, from the exit legs' books just
before the print that fills it. Research model, fitted and scored by the
daily shadow check (shadow_check.py); no order uses it.

Loss: c per lot, vs completing the other legs at their tops SHADOW_LAG_S
before the print (taker fee included), when the exit takes the cheaper of
completing or unwinding on the book `when` after the print. The model is
additive: each input cut into BINS, one-hot, ridge least squares, so a day's
fit is just its X'X and X'y and days add up.

11-08..11-11 (1.68M prints near an arb): trained without a market group it
predicted that group's exit cost with correlation 0.25-0.69, where the
group's own average had ~0.05; on 712 of our real fills it was within 0.5c a
lot of their mean exit loss. The largest input is the edge showing at the
fill: a big apparent arb is mostly other legs about to reprice.
"""
from __future__ import annotations

import math

import numpy as np

from kalshi_strategies.ARBS import replay

LAGS = (0.02, 0.05, 0.1, 0.25)
LAG = 0.02
OTHER = {"yes": "no", "no": "yes"}
BINS = {
    "top_r_min": [0, 1, 1.5, 3, 10, 50, math.inf],
    "aged_frac": [-0.01, 0, 0.5, 0.99, 1.0],
    "depth2_r": [0, 1, 2, 5, 20, math.inf],
    "reach_c": [-math.inf, 0.5, 1.5, 3.5, 6.5, math.inf],
    "edge_c": [-math.inf, 0, 2, 5, 10, math.inf],
    "spread_c": [-math.inf, 1.5, 3.5, 10, math.inf],
    "n": [0, 5, 20, 50, math.inf],
    "unwind_top_r": [-0.01, 0.5, 1, 3, math.inf],
    "n_other": [0, 1, 2, math.inf],
    "stable": [-0.01, 0.5, 1.0],
}
WIDTH = 1 + sum(len(e) - 2 for e in BINS.values())
RIDGE = 50.0
CLIP_C = (-10.0, 50.0)
MAX_REACH = 30                      # ticks a sweep's depth is counted to, for fill odds


def _fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def ask_at(pre_rows, side: str, lag: float):
    """(price, others' size) to buy `side` at the top `lag` s before, or None."""
    for row in pre_rows or ():
        if abs(row[0] - lag) < 1e-9:
            _, bid, bq, ask, aq = row
            if side == "yes":
                return None if ask is None else (ask, aq or 0.0)
            return None if bid is None else (round(1.0 - bid, 4), bq or 0.0)
    return None


def touch_bid(pre_rows, side: str) -> float | None:
    """Best bid for `side` LAG s before."""
    for row in pre_rows or ():
        if abs(row[0] - LAG) < 1e-9:
            _, bid, _, ask, _ = row
            return bid if side == "yes" else (None if ask is None else round(1.0 - ask, 4))
    return None


def features(r: dict, bid: str, payout: float, n: float) -> dict | None:
    """The exit legs' books before print row `r`, for a fill of n on its leg of
    `bid`, plus what pricing needs (touch, c0 = the other legs' tops, plan =
    the same with taker fees). reach_c and edge_c are the print's own; a rule
    pricing its own quote sets them for that price."""
    pre, books = r.get("pre") or {}, r["books"]
    legs = replay.legs_of(bid, books)
    if not legs or (r["ticker"], r["side"]) not in legs:
        return None
    others = [l for l in legs if l[0] != r["ticker"]]
    tops = [ask_at(pre.get(t), s, LAG) for t, s in others]
    tb = touch_bid(pre.get(r["ticker"]), r["side"])
    if not others or tb is None or any(x is None for x in tops):
        return None
    top, aged, stable, depth2, spread = [], [], [], [], []
    for (t, s), (p0, q0) in zip(others, tops):
        top.append(q0 / n)
        aged.append(min((x[1] if x is not None and abs(x[0] - p0) < 1e-9 else 0.0)
                        for x in (ask_at(pre.get(t), s, lag) for lag in LAGS)) / n)
        q25 = ask_at(pre.get(t), s, LAGS[-1])
        stable.append(1.0 if q25 is not None and abs(q25[0] - p0) < 1e-9 else 0.0)
        lv = replay.asks(books, t, s) or []
        depth2.append(sum(q for p, q in lv if p <= p0 + 0.02 + 1e-9) / n)
        b = touch_bid(pre.get(t), OTHER[s])
        spread.append(100 * (p0 - (1.0 - b)) if b is not None else math.inf)
    j = int(np.argmin(top))
    pmin = min(p for _, p, _ in r["prints"])
    uw = ask_at(pre.get(r["ticker"]), OTHER[r["side"]], LAG)
    c0 = sum(p for p, _ in tops)
    return {"top_r_min": min(top), "aged_frac": min(aged) / min(top) if min(top) > 0 else 0.0,
            "depth2_r": depth2[j], "reach_c": 100 * (tb - pmin), "edge_c": 100 * (payout - pmin - c0),
            "spread_c": spread[j], "n": n, "unwind_top_r": (uw[1] / n) if uw else 0.0, "n_other": len(others),
            "stable": min(stable), "touch": tb, "c0": c0, "plan": sum(p + _fee(p) for p, _ in tops),
            "payout": payout, "pmin": pmin, "others": others}


def loss(r: dict, f: dict, when: str = "0.1") -> float:
    """Exit loss vs plan, c per lot, clipped to CLIP_C."""
    after = r.get("after") or {}
    end, late = after.get(when) or r["books"], after.get("1.0") or r["books"]
    n = f["n"]
    comp = sum(replay._take(end, late, t, s, n, 0.07) for t, s in f["others"]) / n
    unw = replay._take(end, late, r["ticker"], OTHER[r["side"]], n, 0.07) / n
    # Unwinding gets $1 a pair instead of the bundle's payout.
    c = 100 * min(comp - f["plan"], (f["payout"] - 1.0) + unw - f["plan"])
    return min(CLIP_C[1], max(CLIP_C[0], c))


def row(f: dict) -> np.ndarray:
    x = np.zeros(WIDTH)
    x[0] = 1.0
    k = 1
    for name, edges in BINS.items():
        v = f[name]
        b = next((i for i in range(len(edges) - 1) if v <= edges[i + 1]), len(edges) - 2)
        if b > 0:                                   # bin 0 is the baseline
            x[k + b - 1] = 1.0
        k += len(edges) - 2
    return x


class Stats:
    """A day's sufficient statistics: X'X, X'y and how far prints swept below
    the touch (ticks, by live), which fit() and fill_prob() sum over days."""

    def __init__(self):
        self.xtx = np.zeros((WIDTH, WIDTH))
        self.xty = np.zeros(WIDTH)
        self.n = 0
        self.reach = {"live": np.zeros(MAX_REACH + 1), "not_live": np.zeros(MAX_REACH + 1)}

    def add(self, f: dict, y: float):
        x = row(f)
        self.xtx += np.outer(x, x)
        self.xty += x * y
        self.n += 1

    def add_reach(self, live: bool, ticks: float):
        self.reach["live" if live else "not_live"][int(min(MAX_REACH, max(0, round(ticks))))] += 1

    def merge(self, o: Stats):
        self.xtx += o.xtx
        self.xty += o.xty
        self.n += o.n
        for k in self.reach:
            self.reach[k] += o.reach[k]

    def to_json(self) -> dict:
        return {"xtx": self.xtx.round(3).tolist(), "xty": self.xty.round(3).tolist(), "n": self.n,
                "reach": {k: v.tolist() for k, v in self.reach.items()}}

    @classmethod
    def from_json(cls, d: dict) -> Stats:
        s = cls()
        if len(d.get("xty") or []) == WIDTH:
            s.xtx, s.xty, s.n = np.array(d["xtx"]), np.array(d["xty"]), int(d["n"])
        for k, v in (d.get("reach") or {}).items():
            if k in s.reach and len(v) == MAX_REACH + 1:
                s.reach[k] = np.array(v)
        return s

    def fit(self) -> np.ndarray:
        a = self.xtx + RIDGE * np.eye(WIDTH)
        a[0, 0] -= RIDGE
        return np.linalg.solve(a, self.xty)

    def fill_prob(self, live: bool, ticks: float) -> float:
        """Share of prints that swept at least `ticks` below the touch."""
        h = self.reach["live" if live else "not_live"]
        tot = h.sum()
        if tot <= 0:
            return 0.0
        return float(h[int(min(MAX_REACH, max(0, math.ceil(ticks - 1e-9)))):].sum() / tot)


def predict(w: np.ndarray, f: dict) -> float:
    return float(row(f) @ w)


def at_price(f: dict, x: float) -> dict:
    """f for a fill at our price x: its edge and the sweep it needs."""
    return {**f, "edge_c": 100 * (f["payout"] - x - f["c0"]), "reach_c": max(0.0, 100 * (f["touch"] - x))}


def quote_price(f: dict, edge: float) -> float:
    """Where replay.score rests a quote at `edge` (maker fee 0)."""
    return math.floor((f["payout"] - f["plan"] - edge) / replay.TICK + 1e-6) * replay.TICK
