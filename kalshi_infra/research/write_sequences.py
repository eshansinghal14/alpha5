"""
Reconstruct the real write-budget spend of a live run from the decision
journal, as sequences of writes per quote slot (ticker, side, role).

The decision log records, per decision, the price each of our quotes rests at
(quote_bid/ask, lip_bid/ask; None = no quote). A change between consecutive
rows of a slot is a write:

    None -> p          create      10 tokens
    p -> None          cancel       2
    p -> q (edge)      reprice     10 if amended, 12 if cancel + create
    p -> q (LIP)       cancel + create, 12 (LIP never reprices in place)

Each transition is labelled with the rule in Quoter._manage_quotes /
_manage_lip that produced it, by recomputing that rule's inputs from the same
row. Costs: KalshiOrderClient.COST (GET /account/endpoint_costs, 2026-10-09).

Approximate in two known ways: amends vs cancel+create on an edge price
change can't be told apart from the log (counted as an amend, the cheaper
case), and a quote refused by the rate limiter shows as a create that never
reached Kalshi -- so this is spend the quoter ATTEMPTED.

    python -m kalshi_infra.research.write_sequences [--date 2026-10-10] [--real-only]
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

from kalshi_infra.runtime.journal import read_journal

TICK = 0.01
COST = {"create": 10, "cancel": 2, "reprice": 10, "move": 12}


def _own(side, yes):
    return yes if side == "yes" else round(1 - yes, 2)


def _first_outside(side, res):
    if side == "yes":
        return round(math.floor(res / TICK + 1e-9) * TICK, 2)
    return round(1.0 - math.ceil(res / TICK - 1e-9) * TICK, 2)


def _blank(v):
    return v is None or (isinstance(v, float) and np.isnan(v))


def real_windows(fills: pd.DataFrame, decisions: pd.DataFrame) -> list[tuple]:
    """(start, end) of each event that had real fills -- armed stretches. The
    log doesn't carry the arm flag, so an event counts as real from its first
    to its last real fill (conservative: misses quoting before the first)."""
    r = fills[fills["mode"] == "real"]
    if r.empty:
        return []
    ev = r.ticker.str.rsplit("-", n=1).str[0]
    out = []
    for e, g in r.groupby(ev):
        d = decisions[decisions.ticker.str.startswith(e + "-")]
        out.append((e, d.time.min(), d.time.max()))
    return out


def transitions(d: pd.DataFrame) -> pd.DataFrame:
    """One row per write the quoter made, with the rule that caused it."""
    out = []
    for tk, g in d.groupby("ticker", sort=False):
        g = g.reset_index(drop=True)
        coin = tk.rsplit("-", 1)[-1]
        for col, side, role in (("quote_bid", "yes", "edge"), ("quote_ask", "no", "edge"),
                                ("lip_bid", "yes", "lip"), ("lip_ask", "no", "lip")):
            rcol = "quote_bid_reservation" if side == "yes" else "quote_ask_reservation"
            bcol = "block_bid" if side == "yes" else "block_ask"
            vals = g[col].to_numpy(dtype=float)
            prev, prev_t = None, None
            for i in range(len(g)):
                cur = None if np.isnan(vals[i]) else _own(side, round(vals[i], 2))
                if cur == prev:
                    continue
                r = g.iloc[i]
                res = r[rcol]
                mb, ma = r.market_bid, r.market_ask
                near = (mb if side == "yes" else (None if pd.isna(ma) else round(1 - ma, 2)))
                near = None if _blank(near) else round(near, 2)
                blk = r[bcol]
                fo = None if pd.isna(res) else _first_outside(side, res)
                if prev is not None and cur is not None:
                    kind = "reprice" if role == "edge" else "move"
                    if fo is not None and prev > fo + 1e-9:
                        why = "edge moved through the quote"
                    elif near is not None and abs(near - prev) < 1e-9:
                        why = "chase while WE are alone at the touch (+1 over ourselves)"
                    elif near is not None and near > prev + 1e-9:
                        why = "touch stepped ahead of us; jump back over"
                    elif near is not None and cur < prev:
                        why = "moved back from touch (deeper)"
                    else:
                        why = "other price change"
                elif prev is not None:
                    kind = "cancel"
                    if not _blank(blk):
                        why = f"blocked: {blk}"
                    elif pd.isna(res):
                        why = "no reservation (stale/no fair upstream)"
                    elif role == "lip":
                        why = "LIP: no level qualified (depth/prop/cap)"
                    elif fo is not None and prev > fo + 1e-9:
                        why = "edge moved through quote, amend refused -> cancel"
                    elif near is not None and abs(near - prev) < 1e-9:
                        why = "chase while alone at touch, amend refused -> cancel"
                    elif near is not None and near > prev + 1e-9:
                        why = "touch stepped ahead, amend refused -> cancel"
                    else:
                        why = "room/cap, deep-skip or requote delay"
                else:
                    kind = "create"
                    why = "first" if prev_t is None else "refill"
                out.append(dict(time=r.time, ticker=tk, coin=coin, side=side, role=role, kind=kind,
                                why=why, prev=prev, cur=cur, near=near, evt=r.event_kind,
                                gap_s=None if prev_t is None else (r.time - prev_t).total_seconds()))
                prev, prev_t = cur, r.time
    t = pd.DataFrame(out)
    if t.empty:
        return t
    t["tokens"] = t.kind.map(COST)
    return t.sort_values("time", kind="stable").reset_index(drop=True)


def link_refills(t: pd.DataFrame) -> pd.DataFrame:
    """Attach to each create the cancel that emptied its slot, and whether it
    came back at the same price (a cancel + create that changed nothing)."""
    t = t.copy()
    t["after"] = None
    t["same_px"] = False
    t["bare_s"] = np.nan
    for _, g in t.groupby(["ticker", "side", "role"], sort=False):
        last = None
        for i in g.index:
            if t.at[i, "kind"] == "cancel":
                last = i
            elif t.at[i, "kind"] == "create" and last is not None:
                t.at[i, "after"] = t.at[last, "why"]
                t.at[i, "same_px"] = t.at[last, "prev"] == t.at[i, "cur"]
                t.at[i, "bare_s"] = (t.at[i, "time"] - t.at[last, "time"]).total_seconds()
                last = None
    return t


def sequences(t: pd.DataFrame, window_s: float = 3.0, max_len: int = 4) -> pd.Series:
    """Most common write sequences per slot: consecutive writes within
    window_s of each other, as a string like 'create > cancel > create'."""
    seqs = []
    for _, g in t.groupby(["ticker", "side", "role"], sort=False):
        cur, last_t = [], None
        for r in g.itertuples():
            if last_t is not None and (r.time - last_t).total_seconds() > window_s:
                if len(cur) > 1:
                    seqs.append(" > ".join(cur[:max_len]))
                cur = []
            cur.append(r.kind)
            last_t = r.time
        if len(cur) > 1:
            seqs.append(" > ".join(cur[:max_len]))
    return pd.Series(seqs).value_counts()


def report(t: pd.DataFrame, elapsed_s: float, label: str):
    tot = t.tokens.sum()
    print(f"\n==== {label}: {len(t)} writes, {tot} tokens over {elapsed_s:.0f}s = {tot / elapsed_s:.1f} tok/s ====")
    by = t.groupby(["role", "kind"]).agg(n=("tokens", "size"), tokens=("tokens", "sum"))
    by["share"] = (by.tokens / tot).round(3)
    print(by.sort_values("tokens", ascending=False).to_string())
    print("\n-- tokens by trigger --")
    w = t.groupby(["role", "kind", "why"]).agg(n=("tokens", "size"), tokens=("tokens", "sum"))
    w["share"] = (w.tokens / tot).round(3)
    print(w.sort_values("tokens", ascending=False).head(25).to_string())
    cr = t[(t.kind == "create") & t.after.notna()]
    if len(cr):
        print("\n-- refills: create after a cancel --")
        a = cr.groupby(["role", "after"]).agg(n=("tokens", "size"), bare_med_s=("bare_s", "median"),
                                              same_price=("same_px", "mean"))
        a["same_price"] = a.same_price.round(2)
        print(a.sort_values("n", ascending=False).to_string())
        same = cr[cr.same_px]
        print(f"\ncancel + create at the SAME price: {len(same)} pairs = {len(same) * 12} tokens "
              f"({len(same) * 12 / tot:.0%} of spend) -- pure waste")
    print("\n-- most common sequences (writes on one slot within 3s) --")
    print(sequences(t).head(15).to_string())
    print("\n-- tokens by coin --")
    print(t.groupby("coin").tokens.sum().sort_values(ascending=False).to_string())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXCRYPTOLEAD15M")
    ap.add_argument("--date", default=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
    ap.add_argument("--real-only", action="store_true")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args(argv)
    d = read_journal(a.series, a.date, "decisions")
    f = read_journal(a.series, a.date, "fills")
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.sort_values("time", kind="stable")
    wins = real_windows(f, d) if a.real_only else [("all", d.time.min(), d.time.max())]
    parts, el = [], 0.0
    for e, t0, t1 in wins:
        sub = d[(d.time >= t0) & (d.time <= t1)]
        if e != "all":
            sub = sub[sub.ticker.str.startswith(e + "-")]
        parts.append(transitions(sub))
        el += (t1 - t0).total_seconds()
    t = link_refills(pd.concat(parts, ignore_index=True))
    report(t, el, f"{a.series} {a.date} {'real events' if a.real_only else 'all'} ({len(wins)} window(s))")
    if a.csv:
        t.to_csv(a.csv, index=False)


if __name__ == "__main__":
    main()
