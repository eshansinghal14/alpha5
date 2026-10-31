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


def basket_settles(root: str) -> dict[str, pd.Timestamp]:
    """basket_id -> expected settlement, from the same sources as basket_info."""
    from kalshi_strategies.ARBS.config import SPEC_PATH
    from kalshi_strategies.ARBS.spec import load_spec
    out = {b.basket_id: b.settles for b in load_spec(SPEC_PATH) if b.settles is not None}
    try:
        with open(os.path.join(root, "scanned_spec.json"), encoding="utf-8") as fh:
            for r in json.load(fh)["relationships"]:
                if r.get("expected_settlement_utc"):
                    ts = pd.Timestamp(r["expected_settlement_utc"])
                    for b in r["baskets"]:
                        out.setdefault(b["basket_id"], ts)
    except (OSError, ValueError, KeyError):
        pass
    try:
        with open(os.path.join(root, "ledger.json"), encoding="utf-8") as fh:
            for bid, p in (json.load(fh).get("baskets") or {}).items():
                if (p.get("spec") or {}).get("settles"):
                    out.setdefault(bid, pd.Timestamp(p["spec"]["settles"]))
    except (OSError, ValueError):
        pass
    return out


def _tte(seconds: float) -> str:
    """Time left as '3d 4h', '5h 20m' or '12m'."""
    m = max(0, int(seconds // 60))
    if m >= 1440:
        return f"{m // 1440}d {m % 1440 // 60}h"
    if m >= 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m}m"


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
    """(time, settled, total, capital) after every live fill or settlement.
    capital: cost of the contracts held, less $1 per YES/NO pair of a market
    held across baskets (Kalshi pays a pair out at once), as the runner's
    max_dollars counts it; resting quotes aren't in it."""
    legs: dict[str, dict[str, list]] = {}       # basket -> ticker -> [side, qty, cost]
    locked: dict[str, float] = {}
    settled, out = 0.0, []
    held: dict[tuple[str, str], float] = {}     # (ticker, side) -> contracts, all baskets
    spent, paired = 0.0, 0.0

    def move(ticker: str, side: str, dq: float, dc: float):
        nonlocal spent, paired
        other = held.get((ticker, "no" if side == "yes" else "yes"), 0.0)
        before = min(held.get((ticker, side), 0.0), other)
        held[(ticker, side)] = held.get((ticker, side), 0.0) + dq
        paired += min(held[(ticker, side)], other) - before
        spent += dc

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
                basis = v[2] / v[1] * n if v[1] > _EPS else 0.0
                v[2] -= basis
                v[1] -= n
                move(t, v[0], -n, -basis)
                if v[1] <= _EPS:
                    del ls[t]
            else:
                v = ls.setdefault(t, [side, 0.0, 0.0])
                v[1] += size
                v[2] += cost
                move(t, side, size, cost)
            locked[bid] = lock(bid)
        elif r.get("kind") == "settled":
            settled += float(r.get("pnl") or 0.0)
            if not any(str(v).startswith(("unwound", "sold by hand")) for v in (r.get("results") or {}).values()):
                for t, (side, q, c) in legs.pop(bid, {}).items():
                    move(t, side, -q, -c)
                locked.pop(bid, None)
        else:
            continue
        out.append((pd.Timestamp(r["t"], unit="s", tz="UTC"), settled, settled + sum(locked.values()),
                    max(0.0, spent - paired)))
    return pd.DataFrame(out, columns=["time", "settled", "total", "capital"])


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


def _net(legs: dict, meta) -> tuple[float, float, float | None]:
    """(unwound pairs, whole bundles, pnl-less-cost or None) for a row's legs.
    Both sides of a market are an unwind, paid out $1 a pair at once; what's
    left after netting is whole bundles, or an unfinished basket (None)."""
    pairs, left = 0.0, {}
    for t in {t for t, _ in legs}:
        y, n = legs.get((t, "yes"), 0.0), legs.get((t, "no"), 0.0)
        pairs += min(y, n)
        if abs(y - n) > _EPS:
            left[t] = abs(y - n)
    if not left:
        return pairs, 0.0, pairs
    if meta is None or len(left) < meta[1] or len({round(q, 6) for q in left.values()}) != 1:
        return pairs, 0.0, None
    b = next(iter(left.values()))
    return pairs, b, pairs + b * meta[0]


def bundle_log(series: str, days: int = 2) -> pd.DataFrame:
    """Live fills, one row per arb (a basket's fills from flat until flat
    again: whole bundles or all unwound), newest first. A row still open
    leaves the basket incomplete: bundles and pnl None."""
    root = os.path.join(LIVE_ROOT, series)
    rows = []
    for f in sorted(glob.glob(os.path.join(root, "journal", "*.jsonl")))[-days:]:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if '"fill"' in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("kind") == "fill" and r.get("live"):
                        rows.append(r)
    info = basket_info(root)
    settles = basket_settles(root)
    arbs: list[dict] = []
    open_: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["t"]):
        bid = r["basket_id"]
        cur = open_.get(bid)
        if cur is None:
            cur = open_[bid] = {"t": r["t"], "basket_id": bid, "how": set(), "legs": {}, "cost": 0.0,
                                "settles": r.get("settles"), "meta": info.get(bid) or guess_info(bid)}
            arbs.append(cur)
        cur["how"].add(r.get("fill_kind") or "")
        key = (r["ticker"], r["side"])
        cur["legs"][key] = cur["legs"].get(key, 0.0) + float(r["size"])
        cur["cost"] += float(r["cost"])
        if _net(cur["legs"], cur["meta"])[2] is not None:
            del open_[bid]
    out = []
    for c in reversed(arbs):
        _, bundles, back = _net(c["legs"], c["meta"])
        pnl = None if back is None else back - c["cost"]
        st = pd.Timestamp(c["settles"]) if c.get("settles") else settles.get(c["basket_id"])
        out.append({"time": pd.Timestamp(c["t"], unit="s", tz="UTC"),
                    # Time to expected settlement when the arb began.
                    "tte": None if st is None else _tte((st - pd.Timestamp(c["t"], unit="s", tz="UTC")).total_seconds()),
                    # Series tickers, the part of a market ticker Kalshi's search finds.
                    "series": " ".join(dict.fromkeys(t.split("-", 1)[0] for t, _ in c["legs"])),
                    "basket_id": c["basket_id"], "how": "+".join(sorted(h for h in c["how"] if h)),
                    "bundles": bundles or None, "cost": round(c["cost"], 2),
                    "pnl": None if pnl is None else round(pnl, 2),
                    "return": None if pnl is None or c["cost"] <= 0 else f"{pnl / c['cost']:.2%}",
                    "fills": ", ".join(f"{t.split('-', 1)[-1]} {s} {q:g}" for (t, s), q in c["legs"].items())})
    return pd.DataFrame(out)
