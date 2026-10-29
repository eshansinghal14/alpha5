"""ARBS cumulative PnL over time, replayed from the journal's live rows:
settled (settlements and unwound contracts) plus locked (complete bundles
held: payout minus what they cost, at each leg's average cost, the ledger's
own rule). Contracts of an unfinished basket count at nothing until they
complete, net out or settle."""
from __future__ import annotations

import glob
import json
import os

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT

_EPS = 1e-9


def basket_info(root: str) -> dict[str, tuple[float, int]]:
    """basket_id -> (payout, legs) from the hand spec, the last scan and the
    ledger's saved definitions."""
    from kalshi_strategies.ARBS.config import SPEC_PATH
    from kalshi_strategies.ARBS.spec import load_spec
    out = {b.basket_id: (b.payout, len(b.legs)) for b in load_spec(SPEC_PATH)}
    try:
        with open(os.path.join(root, "scanned_spec.json"), encoding="utf-8") as fh:
            for r in json.load(fh)["relationships"]:
                for b in r["baskets"]:
                    out.setdefault(b["basket_id"], (float(b["payout"]), len(b["legs"])))
    except (OSError, ValueError, KeyError):
        pass
    try:
        with open(os.path.join(root, "ledger.json"), encoding="utf-8") as fh:
            for bid, p in (json.load(fh).get("baskets") or {}).items():
                if p.get("spec"):
                    out.setdefault(bid, (float(p["spec"]["payout"]), len(p["spec"]["legs"])))
    except (OSError, ValueError):
        pass
    return out


def guess_info(basket_id: str) -> tuple[float, int] | None:
    """A scanned basket no longer listed anywhere, from its id's shape."""
    kind = basket_id.split(":", 1)[0]
    if kind == "mecnet":
        n = len(basket_id.rsplit(":", 1)[-1].split("+"))
        return float(n - 1), n
    if kind == "ladder":
        return 1.0, 2
    return None


def replay(rows: list[dict], info: dict[str, tuple[float, int]]) -> pd.DataFrame:
    """(time, settled, total) after every live fill or settlement."""
    legs: dict[str, dict[str, list]] = {}       # basket -> ticker -> [side, qty, cost]
    locked: dict[str, float] = {}
    settled, out = 0.0, []

    def lock(bid: str) -> float:
        meta = info.get(bid) or guess_info(bid)
        ls = legs.get(bid, {})
        if meta is None or len(ls) < meta[1]:
            return 0.0
        n = min(v[1] for v in ls.values())
        return n * meta[0] - sum(v[2] / v[1] * n for v in ls.values() if v[1] > _EPS)

    for r in sorted((r for r in rows if r.get("live")), key=lambda r: r["t"]):
        bid = r.get("basket_id")
        if r.get("kind") == "fill":
            ls = legs.setdefault(bid, {})
            t, side, size, cost = r["ticker"], r["side"], float(r["size"]), float(r["cost"])
            if t in ls and ls[t][0] != side:
                # The other side of a held leg: nets out at the leg's average cost.
                v = ls[t]
                n = min(size, v[1])
                v[2] -= v[2] / v[1] * n if v[1] > _EPS else 0.0
                v[1] -= n
                if v[1] <= _EPS:
                    del ls[t]
            else:
                v = ls.setdefault(t, [side, 0.0, 0.0])
                v[1] += size
                v[2] += cost
            locked[bid] = lock(bid)
        elif r.get("kind") == "settled":
            settled += float(r.get("pnl") or 0.0)
            if not any(str(v).startswith(("unwound", "sold by hand")) for v in (r.get("results") or {}).values()):
                legs.pop(bid, None)
                locked.pop(bid, None)
        else:
            continue
        out.append((pd.Timestamp(r["t"], unit="s", tz="UTC"), settled, settled + sum(locked.values())))
    return pd.DataFrame(out, columns=["time", "settled", "total"])


def load(series: str) -> pd.DataFrame:
    root = os.path.join(LIVE_ROOT, series)
    rows = []
    for f in sorted(glob.glob(os.path.join(root, "journal", "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if '"fill"' in line or '"settled"' in line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    return replay(rows, basket_info(root))
