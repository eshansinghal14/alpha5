"""Live-event quote edge chosen per market from replayed flow.

Each recorded print is scored at every candidate edge (what a quote there
would have filled and made once completed); this keeps decayed sums of those
scores per node of a path such as (sport, series, event) under one root, and
picks the edge whose value per print is best at the leaf.

Guards against fitting noise:
- each node's per-contract pnl and fill rate are shrunk toward its parent's,
  with a prior worth `n0` prints at that level, so a thin series or a new
  event starts from its sport's numbers;
- an edge is chosen by its lower confidence bound (z standard errors below),
  with "off" (no quote, worth 0) always a candidate;
- a leaf only switches when the new choice beats its current one by
  `hysteresis` of the current's value.
Prints within a sweep or one game are correlated, so the standard errors are
too small and z is set wider than its nominal meaning.
"""
from __future__ import annotations

import dataclasses
import math

EDGES = (0.02, 0.03, 0.04, 0.05, 0.075, 0.10)
OFF = None


@dataclasses.dataclass(frozen=True)
class Level:
    n0: float             # prior strength toward the parent, in prints
    half_life: float      # in this node's own prints, or seconds when by_time
    by_time: bool = False


# (sport or category, series, event). Picked by walk-forward on replayed live
# prints, settings chosen on 11-01..02 and scored on 11-03..04: lighter
# shrinkage (n0 200) ranked first on both halves; a game-phase level and
# n0 1000/5000 did worse out of sample.
DEFAULT_LEVELS = (Level(200.0, 20000.0), Level(200.0, 5000.0), Level(100.0, 1800.0, by_time=True))
DEFAULT_Z = 1.0
DEFAULT_HYSTERESIS = 0.2


class _Node:
    __slots__ = ("w", "f", "p", "x2", "xf", "f2", "t")

    def __init__(self, n: int):
        self.w = 0.0
        self.f, self.p, self.x2, self.xf, self.f2 = ([0.0] * n for _ in range(5))
        self.t = None

    def decay(self, k: float):
        self.w *= k
        for a in (self.f, self.p, self.x2, self.xf, self.f2):
            for i in range(len(a)):
                a[i] *= k


class LiveParams:
    def __init__(self, levels: list[Level], edges=EDGES, z: float = 2.0, hysteresis: float = 0.2,
                 root_half_life: float = 50000.0):
        self.levels, self.edges, self.z, self.hyst = levels, tuple(edges), z, hysteresis
        self.root_hl = root_half_life
        self.nodes: dict[tuple, _Node] = {}
        self.choice: dict[tuple, float | None] = {}

    def _factor(self, node: _Node, lv: Level | None, t: float) -> float:
        """Decay to apply before one more observation at time t."""
        if lv is not None and lv.by_time:
            return 1.0 if node.t is None else 0.5 ** (max(0.0, t - node.t) / lv.half_life)
        hl = self.root_hl if lv is None else lv.half_life
        return 0.5 ** (1.0 / hl)

    def observe(self, path: tuple, t: float, fills, pnl):
        """One print: contracts a quote at each edge would have filled, and
        what each made completed (dollars)."""
        for d in range(len(path) + 1):
            key = tuple(path[:d])
            n = self.nodes.get(key)
            if n is None:
                n = self.nodes[key] = _Node(len(self.edges))
            n.decay(self._factor(n, self.levels[d - 1] if d else None, t))
            n.t = t
            n.w += 1.0
            for i, (f, x) in enumerate(zip(fills, pnl)):
                n.f[i] += f
                n.p[i] += x
                n.x2[i] += x * x
                n.xf[i] += x * f
                n.f2[i] += f * f

    def estimate(self, path: tuple, t: float) -> list[tuple[float, float]]:
        """(value, lower bound) per edge, in dollars per print, at the leaf."""
        k = len(self.edges)
        r, m, v = [0.0] * k, [0.0] * k, [0.0] * k
        for d in range(len(path) + 1):
            n = self.nodes.get(tuple(path[:d]))
            lv = self.levels[d - 1] if d else None
            if n is None:
                continue                      # an unseen node is its parent
            g = 1.0
            if lv is not None and lv.by_time and n.t is not None:
                g = 0.5 ** (max(0.0, t - n.t) / lv.half_life)
            w = n.w * g
            for i in range(k):
                f, p = n.f[i] * g, n.p[i] * g
                if d == 0:
                    r[i] = f / w if w > 0 else 0.0
                    m[i] = p / f if f > 0 else 0.0
                    kf = 0.0
                else:
                    kf = lv.n0 * r[i]         # the prior's prints, in contracts at the parent's rate
                    r[i] = (f + lv.n0 * r[i]) / (w + lv.n0)
                    m[i] = (p + kf * m[i]) / (f + kf) if f + kf > 0 else m[i]
                s = max(0.0, n.x2[i] * g - 2 * m[i] * n.xf[i] * g + m[i] * m[i] * n.f2[i] * g)
                own = s / (f + kf) ** 2 if f + kf > 0 else 0.0
                v[i] = own + (kf / (f + kf)) ** 2 * v[i] if f + kf > 0 else v[i]
        return [(r[i] * m[i], r[i] * (m[i] - self.z * math.sqrt(v[i]))) for i in range(k)]

    def prune(self, t: float, max_age: float) -> int:
        """Drop nodes of time-decayed levels (events) idle for max_age, and
        the choices made under them. Returns how many nodes went."""
        gone = [k for k, n in self.nodes.items() if k and self.levels[len(k) - 1].by_time
                and n.t is not None and t - n.t > max_age]
        for k in gone:
            del self.nodes[k]
        dead = set(gone)
        for leaf in [c for c in self.choice if any(c[:d] in dead for d in range(1, len(c) + 1))]:
            del self.choice[leaf]
        return len(gone)

    def _cands(self, path: tuple, t: float) -> list[tuple]:
        est = self.estimate(path, t)
        return [(OFF, 0.0, 0.0)] + [(e, val, lcb) for e, (val, lcb) in zip(self.edges, est)]

    def best(self, path: tuple, t: float) -> tuple:
        """(edge or OFF, value, lower bound) with the best lower bound at
        `path`, without hysteresis or remembering it."""
        return max(self._cands(path, t), key=lambda c: c[2])

    def choose(self, path: tuple, t: float) -> float | None:
        """The leaf's edge, or OFF (don't quote). Sticky: see hysteresis."""
        cands = self._cands(path, t)
        best = max(cands, key=lambda c: c[2])
        leaf = tuple(path)
        cur = self.choice.get(leaf, best[0])
        now = next(c for c in cands if c[0] == cur)
        if best[2] > now[2] + self.hyst * abs(now[1]):
            cur = best[0]
        self.choice[leaf] = cur
        return cur
