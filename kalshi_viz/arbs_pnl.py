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


def bundle_log(series: str, days: int = 2, gap_s: float = 30.0) -> pd.DataFrame:
    """Live fills, one row per burst on a basket (fills within gap_s of the
    last), newest first. pnl: locked in for whole bundles, realized for an
    unwind, None for a burst that left the basket incomplete."""
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
    bursts: list[dict] = []
    last: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["t"]):
        bid = r["basket_id"]
        cur = last.get(bid)
        if cur is None or r["t"] - cur["t_last"] > gap_s:
            cur = last[bid] = {"t": r["t"], "t_last": r["t"], "basket_id": bid, "how": set(), "legs": {}, "cost": 0.0}
            bursts.append(cur)
        cur["t_last"] = r["t"]
        cur["how"].add(r.get("fill_kind") or "")
        key = (r["ticker"], r["side"])
        cur["legs"][key] = cur["legs"].get(key, 0.0) + float(r["size"])
        cur["cost"] += float(r["cost"])
    out = []
    for c in reversed(bursts):
        meta = info.get(c["basket_id"]) or guess_info(c["basket_id"])
        qty = list(c["legs"].values())
        tickers = [t for t, _ in c["legs"]]
        # Both sides of a market: an unwind, the pair paid out $1 each at once.
        pairs = sum(min(c["legs"].get((t, "yes"), 0.0), c["legs"].get((t, "no"), 0.0)) for t in set(tickers))
        whole = (not pairs and meta is not None and len(set(tickers)) >= meta[1]
                 and len({round(q, 6) for q in qty}) == 1)
        pnl = qty[0] * meta[0] - c["cost"] if whole else pairs - c["cost"] if pairs else None
        out.append({"time": pd.Timestamp(c["t"], unit="s", tz="UTC"),
                    # Series tickers, the part of a market ticker Kalshi's search finds.
                    "series": " ".join(dict.fromkeys(t.split("-", 1)[0] for t, _ in c["legs"])),
                    "basket_id": c["basket_id"], "how": "+".join(sorted(h for h in c["how"] if h)),
                    "bundles": qty[0] if whole else None, "cost": round(c["cost"], 2),
                    "pnl": None if pnl is None else round(pnl, 2),
                    "return": None if pnl is None or c["cost"] <= 0 else f"{pnl / c['cost']:.2%}",
                    "fills": ", ".join(f"{t.split('-', 1)[-1]} {s} {q:g}" for (t, s), q in c["legs"].items())})
    return pd.DataFrame(out)
