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

from strategies.ARBS import replay

LAGS = (0.02, 0.05, 0.1, 0.25)
LAG = 0.02
OTHER = {"yes": "no", "no": "yes"}
BINS = {
    "top_r_min": [0, 1, 1.5, 3, 10, 50, math.inf],
    "aged_frac": [-0.01, 0, 0.5, 0.99, 1.0],
    "depth2_r": [0, 1, 2, 5, 20, math.inf],
    # Deep sweeps get their own bins: with one bin past 6.5c (edge past 10c),
    # a 30c sweep was predicted to cost what a 7c one did. Pre-game realized
    # exit loss rose 2.0 -> 3.6 -> 5.8 -> 8.5 -> 14.7c per lot over reach
    # 6.5-10 / 10-15 / 15-20 / 20-30 / 30+c (11-14..16), and the flat
    # prediction put most pre-game picks at the widest edge.
    "reach_c": [-math.inf, 0.5, 1.5, 3.5, 6.5, 10, 15, 20, 30, math.inf],
    "edge_c": [-math.inf, 0, 2, 5, 10, 15, 20, math.inf],
    "spread_c": [-math.inf, 1.5, 3.5, 10, math.inf],
    "n": [0, 5, 20, 50, math.inf],
    "unwind_top_r": [-0.01, 0.5, 1, 3, math.inf],
    "n_other": [0, 1, 2, math.inf],
    "stable": [-0.01, 0.5, 1.0],
    # In play and hours to settlement (-1 when unknown, its own bin): the
    # factor study's two strongest, here beside the book (11-11).
    "live": [-0.01, 0.5, 1.0],
    "tte_h": [-math.inf, -0.5, 1, 6, 24, math.inf],
}
WIDTH = 1 + sum(len(e) - 2 for e in BINS.values())
RIDGE = 50.0
CLIP_C = (-10.0, 50.0)
MAX_REACH = 30                      # ticks a sweep's depth is counted to, for fill odds
# Print volume bins (contracts), for the lots a filled quote of a given size can expect.
VOL_EDGES = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0, math.inf)


def _fee(p: float, rate: float = 0.07) -> float:
    return rate * p * (1.0 - p)


def _rate(ticker: str) -> float:
    """Taker rate a training row's leg pays: Polymarket's default or Kalshi's."""
    from strategies.ARBS.venues import PM_TAKER_RATE, is_pm
    return PM_TAKER_RATE if is_pm(ticker) else 0.07


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
    legs = replay.legs_of(bid, books, r["ticker"])
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
    live = next((bool(x[2]) for x in r.get("baskets") or () if x[0] == bid), False)
    tte = (r.get("tte_h") or {}).get(bid)
    return {"top_r_min": min(top), "aged_frac": min(aged) / min(top) if min(top) > 0 else 0.0,
            "depth2_r": depth2[j], "reach_c": 100 * (tb - pmin), "edge_c": 100 * (payout - pmin - c0),
            "spread_c": spread[j], "n": n, "unwind_top_r": (uw[1] / n) if uw else 0.0, "n_other": len(others),
            "stable": min(stable), "live": 1.0 if live else 0.0, "tte_h": -1.0 if tte is None else float(tte),
            "touch": tb, "c0": c0, "plan": sum(p + _fee(p, _rate(t)) for (t, _), (p, _) in zip(others, tops)),
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


def _active(f: dict) -> list[int]:
    """Indices of row(f)'s ones: the intercept and each feature's bin."""
    out, k = [0], 1
    for name, edges in BINS.items():
        v = f[name]
        b = next((i for i in range(len(edges) - 1) if v <= edges[i + 1]), len(edges) - 2)
        if b > 0:                                   # bin 0 is the baseline
            out.append(k + b - 1)
        k += len(edges) - 2
    return out


def row(f: dict) -> np.ndarray:
    x = np.zeros(WIDTH)
    x[_active(f)] = 1.0
    return x


class Stats:
    """A day's sufficient statistics: X'X, X'y, how far prints swept below the
    touch (ticks) and their volume, by live, which fit(), fill_prob() and
    expected_fill() sum over days."""

    def __init__(self):
        self.xtx = np.zeros((WIDTH, WIDTH))
        self.xty = np.zeros(WIDTH)
        self.n = 0
        self.reach = {"live": np.zeros(MAX_REACH + 1), "not_live": np.zeros(MAX_REACH + 1)}
        self.vol = {"live": np.zeros(len(VOL_EDGES) - 1), "not_live": np.zeros(len(VOL_EDGES) - 1)}
        # fill_prob/expected_fill answers: the runner's stats only change at a
        # model reload, and recomputing per leg priced was ~5% of ARBS's busy
        # loop (py-spy 11-16). Cleared by anything that adds to the counts.
        self._memo: dict = {}

    def add(self, f: dict, y: float):
        x = row(f)
        self.xtx += np.outer(x, x)
        self.xty += x * y
        self.n += 1

    def add_reach(self, live: bool, ticks: float):
        self._memo.clear()
        self.reach["live" if live else "not_live"][int(min(MAX_REACH, max(0, round(ticks))))] += 1

    def add_vol(self, live: bool, contracts: float):
        self._memo.clear()
        i = next(i for i in range(len(VOL_EDGES) - 1) if contracts < VOL_EDGES[i + 1])
        self.vol["live" if live else "not_live"][i] += 1

    def merge(self, o: Stats):
        self._memo.clear()
        self.xtx += o.xtx
        self.xty += o.xty
        self.n += o.n
        for k in self.reach:
            self.reach[k] += o.reach[k]
            self.vol[k] += o.vol[k]

    def to_json(self) -> dict:
        return {"xtx": self.xtx.round(3).tolist(), "xty": self.xty.round(3).tolist(), "n": self.n,
                "reach": {k: v.tolist() for k, v in self.reach.items()},
                "vol": {k: v.tolist() for k, v in self.vol.items()}}

    @classmethod
    def from_json(cls, d: dict) -> Stats:
        s = cls()
        if len(d.get("xty") or []) == WIDTH:
            s.xtx, s.xty, s.n = np.array(d["xtx"]), np.array(d["xty"]), int(d["n"])
        for k, v in (d.get("reach") or {}).items():
            if k in s.reach and len(v) == MAX_REACH + 1:
                s.reach[k] = np.array(v)
        for k, v in (d.get("vol") or {}).items():
            if k in s.vol and len(v) == len(VOL_EDGES) - 1:
                s.vol[k] = np.array(v)
        return s

    def fit(self) -> np.ndarray:
        a = self.xtx + RIDGE * np.eye(WIDTH)
        a[0, 0] -= RIDGE
        return np.linalg.solve(a, self.xty)

    def fill_prob(self, live: bool, ticks: float) -> float:
        """Share of prints that swept at least `ticks` below the touch."""
        k = ("p", live, int(min(MAX_REACH, max(0, math.ceil(ticks - 1e-9)))))
        got = self._memo.get(k)
        if got is None:
            h = self.reach["live" if live else "not_live"]
            tot = h.sum()
            got = self._memo[k] = 0.0 if tot <= 0 else float(h[k[2]:].sum() / tot)
        return got

    def expected_fill(self, live: bool, size: float) -> float:
        """Contracts a filled quote of `size` can expect: min(size, the print's
        volume), each bin at its lower edge (1 for the first). `size` itself
        with no volumes recorded, as the replay scored before this existed."""
        k = ("f", live, size)
        got = self._memo.get(k)
        if got is None:
            h = self.vol["live" if live else "not_live"]
            tot = h.sum()
            lo = [max(1.0, VOL_EDGES[i]) for i in range(len(h))]
            got = size if tot <= 0 else float(sum(c * min(size, v) for c, v in zip(h, lo)) / tot)
            if len(self._memo) > 100_000:
                self._memo.clear()
            self._memo[k] = got
        return got


class Model:
    """The fitted model as the runner uses it: weights plus fill odds and fill
    sizes, written by the daily shadow check (MODEL_FILE)."""

    def __init__(self, w: np.ndarray, stats: Stats, trained_on: list[str]):
        self.w, self.stats, self.trained_on = w, stats, trained_on

    def to_json(self) -> dict:
        return {"w": [round(float(x), 6) for x in self.w], "width": WIDTH, "trained_on": self.trained_on,
                "reach": {k: v.tolist() for k, v in self.stats.reach.items()},
                "vol": {k: v.tolist() for k, v in self.stats.vol.items()}}

    @classmethod
    def from_json(cls, d: dict) -> Model | None:
        if d.get("width") != WIDTH or len(d.get("w") or []) != WIDTH:
            return None
        st = Stats.from_json({"reach": d.get("reach"), "vol": d.get("vol")})
        return cls(np.array(d["w"]), st, d.get("trained_on") or [])

    def loss(self, f: dict) -> float:
        return predict(self.w, f)

    def value(self, f: dict, x: float, live: bool, size: float, maker_rate: float = 0.0) -> tuple[float, float]:
        """(expected $-cents of a quote at x for `size`: fill odds x expected
        lots x net, net c per lot after the predicted exit loss)."""
        edge_c, reach_c = 100 * (f["payout"] - x - f["c0"]), max(0.0, 100 * (f["touch"] - x))
        net = 100 * (f["payout"] - x - f["plan"] - maker_rate * x * (1 - x)) - _predict_at(self.w, f, edge_c, reach_c)
        return self.stats.fill_prob(live, reach_c) * self.stats.expected_fill(live, size) * net, net


MODEL_FILE = "exit_model.json"


def live_features(views, j: int, payout: float, n: float, aged: list, unwind_top: float | None,
                  live: bool, tte_h: float | None) -> dict | None:
    """features() from the runner's books instead of a print row: `views` the
    basket's LegViews (our own orders out), `aged` the depth each other leg's
    top has held over the last 0.25 s (None unknown), `unwind_top` the size to
    buy leg j's other side at its top."""
    others = [i for i in range(len(views)) if i != j]
    if not others or any(not views[i].asks for i in others) or views[j].bid is None:
        return None
    tops = [views[i].asks[0] for i in others]
    top = [q / n for _, q in tops]
    k = min(range(len(top)), key=top.__getitem__)    # np.argmin's first minimum, without numpy
    ag = [min(a, q) / n if a is not None else 0.0 for a, (_, q) in zip(aged, tops)]
    p0k = tops[k][0]
    depth2 = sum(q for p, q in views[others[k]].asks if p <= p0k + 0.02 + 1e-9) / n
    c0 = sum(p for p, _ in tops)
    return {"top_r_min": min(top), "aged_frac": min(ag) / min(top) if min(top) > 0 else 0.0, "depth2_r": depth2,
            "reach_c": 0.0, "edge_c": 0.0, "spread_c": 0.0, "n": n,
            "unwind_top_r": (unwind_top / n) if unwind_top else 0.0, "n_other": len(others),
            "stable": 1.0 if all(a is not None and a > 0 for a in aged) else 0.0,
            "live": 1.0 if live else 0.0, "tte_h": -1.0 if tte_h is None else float(tte_h),
            "touch": views[j].bid, "c0": c0, "plan": sum(p + _fee(p) for p, _ in tops), "payout": payout}


_W_LIST: list = [None, None]                        # (w, w as floats), predict's last weights


def predict(w: np.ndarray, f: dict) -> float:
    """row(f) @ w as the sum of the weights row(f) selects: a numpy row and
    dot per leg priced was ~1% of ARBS's busy loop (py-spy 11-17)."""
    if _W_LIST[0] is not w:
        _W_LIST[0], _W_LIST[1] = w, [float(x) for x in w]
    wl = _W_LIST[1]
    return sum(wl[i] for i in _active(f))


_AT_PRICE = ("edge_c", "reach_c")


def _bin(edges: list, v: float) -> int:
    return next((i for i in range(len(edges) - 1) if v <= edges[i + 1]), len(edges) - 2)


def _predict_at(w: np.ndarray, f: dict, edge_c: float, reach_c: float) -> float:
    """predict(w, at_price(f, x)) for the edge_c and reach_c of price x, bit
    for bit (same terms, same order). Only those two features move with the
    price, so the others' weights are looked up once per f and kept in it:
    pricing 28 edge/size pairs a live leg every 0.5 s, Model.value was ~6% of
    ARBS0B's busy loop (py-spy 11-17 19:26)."""
    if _W_LIST[0] is not w:
        _W_LIST[0], _W_LIST[1] = w, [float(x) for x in w]
    wl = _W_LIST[1]
    got = f.get("_terms")
    if got is None or got[0] is not wl:
        terms, k = [], 1
        for name, edges in BINS.items():
            if name in _AT_PRICE:
                terms.append((name, k, edges))
            else:
                b = _bin(edges, f[name])
                if b > 0:
                    terms.append(wl[k + b - 1])
            k += len(edges) - 2
        got = f["_terms"] = (wl, terms)
    at = {"edge_c": edge_c, "reach_c": reach_c}
    vals = [wl[0]]
    for t in got[1]:
        if type(t) is tuple:
            b = _bin(t[2], at[t[0]])
            if b > 0:
                vals.append(wl[t[1] + b - 1])
        else:
            vals.append(t)
    return sum(vals)                                # sum(), as predict: its float summation is compensated


def at_price(f: dict, x: float) -> dict:
    """f for a fill at our price x: its edge and the sweep it needs."""
    return {**f, "edge_c": 100 * (f["payout"] - x - f["c0"]), "reach_c": max(0.0, 100 * (f["touch"] - x))}


def quote_price(f: dict, edge: float) -> float:
    """Where replay.score rests a quote at `edge` (maker fee 0)."""
    return math.floor((f["payout"] - f["plan"] - edge) / replay.TICK + 1e-6) * replay.TICK
