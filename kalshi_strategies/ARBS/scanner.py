"""Kalshi-wide basket scan: every open event Kalshi flags as MECNET (at most
one market resolves YES) or DIRECNET (a ladder of "above X" strikes) becomes
relationships in the hand spec's format, written to scanned_spec.json.

  MECNET     NO on the k likeliest markets pays at least k-1, for k = 2 up to
             scan.mecnet_max_legs. Any subset is safe; ranked by YES price at
             scan time so the subsets that can clear are the ones listed.
  coverage   MECNET buckets running from a "below" tail through between ranges
             to an "above" tail are exhaustive as well: a complete set (both
             directions). Only for series in scan.complete_set_series, since a
             gap at a bucket edge pays zero; others are listed in
             scan_review.json.
  DIRECNET   for strikes a < b, YES >=a and NO >=b pays at least 1; each
             strike is paired with the next scan.ladder_span above it (a
             200-strike KXBTCD ladder is 20k pairs otherwise).

Events past trading.days_till_expiry are skipped, as are series in
watch.exclude and series another strategy trades (the position audit would
read its contracts as ours). The most traded events are kept until
scan.max_markets markets are listed.

    python -m kalshi_strategies.ARBS.scanner      # one scan, prints a summary
"""
from __future__ import annotations

import json
import os

import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import atomic_write_json

UP = ("greater", "greater_or_equal")
DOWN = ("less", "less_or_equal")


def fetch_events() -> list[dict]:
    """Blocking (~80 pages, ~20s). Every open event with its markets."""
    out, cursor = [], None
    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = kalshi_get("/trade-api/v2/events", params=params)
        out += d.get("events") or []
        cursor = d.get("cursor")
        if not cursor or not d.get("events"):
            return out


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def month_end(month: str) -> pd.Timestamp:
    return pd.Period(month, freq="M").end_time.tz_localize("UTC")


def settles(event: dict, overrides: dict[str, str]) -> pd.Timestamp | None:
    """Latest expected settlement over the event's markets. `overrides` maps a
    series to a settlement month read from its rules: election markets carry
    a placeholder expected_expiration_time a year out."""
    if event.get("series_ticker") in overrides:
        return month_end(overrides[event["series_ticker"]])
    ts = [m.get("expected_expiration_time") or m.get("close_time") for m in event["markets"]]
    ts = [pd.Timestamp(t) for t in ts if t]
    return max(ts) if ts else None


def yes_price(m: dict) -> float:
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    return (bid + ask) / 2 if bid > 0 and ask > 0 else bid or _f(m.get("last_price_dollars"))


def _legs(*pairs) -> list[dict]:
    return [{"ticker": t, "side": s} for t, s in pairs]


def _sfx(event: str, ticker: str) -> str:
    return ticker[len(event) + 1:] if ticker.startswith(event + "-") else ticker


def coverage_step(markets: list[dict]) -> float | None:
    """The widest gap between neighbouring bucket edges if the markets run
    below-tail, contiguous betweens, above-tail with no overlap; else None.
    0 is exact coverage; a gap of 1 or 0.1 is fine when the value is reported
    at that resolution, which is what the review is for."""
    lo = [m for m in markets if m.get("strike_type") in DOWN]
    hi = [m for m in markets if m.get("strike_type") in UP]
    mid = [m for m in markets if m.get("strike_type") == "between"]
    if len(lo) != 1 or len(hi) != 1 or len(lo) + len(hi) + len(mid) != len(markets):
        return None
    try:
        mid.sort(key=lambda m: float(m["floor_strike"]))
        edges = [float(lo[0]["cap_strike"])]
        for m in mid:
            edges += [float(m["floor_strike"]), float(m["cap_strike"])]
        edges.append(float(hi[0]["floor_strike"]))
    except (KeyError, TypeError, ValueError):
        return None
    gaps = [round(edges[i + 1] - edges[i], 9) for i in range(0, len(edges), 2)]
    return max(gaps) if min(gaps) >= 0 else None


def mecnet(event: dict, markets: list[dict], max_legs: int, complete: bool) -> list[dict]:
    et, series = event["event_ticker"], event["series_ticker"]
    ranked = sorted(markets, key=lambda m: -yes_price(m))
    rels = []
    if complete:
        ts = [m["ticker"] for m in markets]
        rels.append({"relationship_id": f"complete_set:{et}", "type": "complete_set", "cluster": series,
                     "description": f"{et}: buckets cover every outcome; exactly one resolves YES.",
                     "baskets": [{"basket_id": f"complete_set:{et}:buy_all_yes", "payout": 1,
                                  "legs": _legs(*((t, "yes") for t in ts))},
                                 {"basket_id": f"complete_set:{et}:buy_all_no", "payout": len(ts) - 1,
                                  "legs": _legs(*((t, "no") for t in ts))}]})
    baskets = []
    for k in range(2, min(len(ranked), max_legs) + 1):
        if complete and k == len(ranked):
            break
        ts = [m["ticker"] for m in ranked[:k]]
        baskets.append({"basket_id": f"mecnet:{et}:no:" + "+".join(_sfx(et, t) for t in ts), "payout": k - 1,
                        "legs": _legs(*((t, "no") for t in ts))})
    if baskets:
        rels.append({"relationship_id": f"mecnet:{et}", "type": "mutually_exclusive_no", "cluster": series,
                     "description": f"{et}: at most one market resolves YES; NO on any k of them pays k-1.",
                     "baskets": baskets})
    return rels


def ladder(event: dict, markets: list[dict], span: int) -> list[dict]:
    """b implies a for strikes ordered by `key`: buy YES a and NO b, pays >= 1.
    Equal strikes (> x vs >= x) are skipped rather than ordered by strictness."""
    et, series = event["event_ticker"], event["series_ticker"]
    baskets = []
    for types, field, key in ((UP, "floor_strike", 1.0), (DOWN, "cap_strike", -1.0)):
        rungs = []
        for m in markets:
            if m.get("strike_type") in types and m.get(field) is not None:
                rungs.append((key * float(m[field]), m["ticker"]))
        rungs.sort()
        for i, (ka, a) in enumerate(rungs):
            for kb, b in rungs[i + 1:i + 1 + span]:
                if kb - ka < 1e-9:
                    continue
                baskets.append({"basket_id": f"ladder:{et}:{_sfx(et, b)}=>{_sfx(et, a)}", "payout": 1,
                                "legs": _legs((a, "yes"), (b, "no"))})
    if not baskets:
        return []
    return [{"relationship_id": f"ladder:{et}", "type": "implication", "cluster": series,
             "description": f"{et}: a higher strike implies every lower one; YES lower + NO higher pays 1.",
             "baskets": baskets}]


def scan(events: list[dict], *, now: pd.Timestamp, days: float, max_markets: int, mecnet_max_legs: int,
         ladder_span: int, complete_set_series: list[str], overrides: dict[str, str],
         skip_series: set[str] = frozenset(), known_tickers: set[str] = frozenset()) -> tuple[dict, dict]:
    """(spec doc, review doc). `known_tickers` (the hand spec's) don't count
    against max_markets."""
    horizon = now + pd.Timedelta(days=days)
    approved = set(complete_set_series)
    cands = []
    for e in events:
        crt = e.get("collateral_return_type")
        if crt not in ("MECNET", "DIRECNET") or e.get("series_ticker") in skip_series:
            continue
        ms = [m for m in e.get("markets") or []
              if m.get("status") == "active" and m.get("market_type", "binary") == "binary"]
        if len(ms) < 2:
            continue
        s = settles(e, overrides)
        if s is None or s > horizon:
            continue
        cands.append((sum(_f(m.get("volume_24h_fp")) for m in ms), sum(_f(m.get("open_interest_fp")) for m in ms),
                      e, ms, s))
    cands.sort(key=lambda c: (-c[0], -c[1]))
    rels, review, used = [], {}, set()
    for _vol, _oi, e, ms, s in cands:
        if e["collateral_return_type"] == "MECNET":
            step = coverage_step(ms) if len(ms) == len(e["markets"]) else None
            ok = step is not None and e["series_ticker"] in approved
            if step is not None and not ok:
                r = review.setdefault(e["series_ticker"], {"events": 0, "max_gap": 0.0, "example": e["event_ticker"],
                                                           "buckets": [m.get("yes_sub_title") for m in
                                                                       sorted(ms, key=_bucket_key)]})
                r["events"] += 1
                r["max_gap"] = max(r["max_gap"], step)
            got = mecnet(e, ms, mecnet_max_legs, ok)
        else:
            got = ladder(e, ms, ladder_span)
        new = {l["ticker"] for r in got for b in r["baskets"] for l in b["legs"]} - known_tickers - used
        if not got or len(used) + len(new) > max_markets:
            continue
        used |= new
        for r in got:
            r["expected_settlement_utc"] = s.isoformat()
            r["quote_until_utc"] = None
            r["resume_quoting_utc"] = None
        rels += got
    doc = {"generated_at_utc": now.isoformat(), "source": "kalshi_strategies.ARBS.scanner",
           "days_till_expiry": days, "n_events": len({r["relationship_id"].split(":", 1)[1] for r in rels}),
           "n_markets": len(used), "relationships": rels}
    return doc, {"generated_at_utc": now.isoformat(),
                 "note": "MECNET bucket series that look exhaustive. Add a series to scan.complete_set_series once "
                         "its rules confirm a gap of max_gap between buckets can't happen.",
                 "series": review}


def other_series() -> set[str]:
    from kalshi_strategies.run import registry
    return {t for t, s in registry().items() if t != "ARBS"}


def _bucket_key(m: dict) -> float:
    for f in ("floor_strike", "cap_strike"):
        if m.get(f) is not None:
            return float(m[f]) + (0.5 if m.get("strike_type") in UP else 0.0)
    return float("-inf")


def run(cfg, hand_doc: dict, known_tickers: set[str], spec_path: str, review_path: str,
        held: set[str] = frozenset()) -> dict:
    """Blocking: fetch, scan, write both files. Returns the review doc.
    Relationships of the previous file holding a basket in `held` are carried
    over, so a position the scan no longer lists survives a restart."""
    doc, review = scan(fetch_events(), now=pd.Timestamp.now(tz="UTC"), days=cfg.trading.days_till_expiry,
                       max_markets=cfg.scan.max_markets, mecnet_max_legs=cfg.scan.mecnet_max_legs,
                       ladder_span=cfg.scan.ladder_span, complete_set_series=cfg.scan.complete_set_series,
                       skip_series=set(cfg.watch.exclude) | other_series(),
                       overrides={s: v["expected_settlement_month"]
                                  for s, v in (hand_doc.get("settlement_basis_by_series") or {}).items()},
                       known_tickers=known_tickers)
    have = {r["relationship_id"] for r in doc["relationships"]}
    try:
        with open(spec_path, encoding="utf-8") as fh:
            prev = json.load(fh)["relationships"]
    except (OSError, ValueError, KeyError):
        prev = []
    for r in prev:
        if r["relationship_id"] not in have and any(b["basket_id"] in held for b in r["baskets"]):
            doc["relationships"].append(r)
    atomic_write_json(spec_path, doc, indent=None)
    atomic_write_json(review_path, review, indent=1)
    return review


if __name__ == "__main__":
    from kalshi_infra.config import ConfigStore
    from kalshi_infra.runtime.persist import LIVE_ROOT
    from kalshi_strategies.ARBS.config import CONFIG_PATH, SPEC_PATH, ArbsConfig
    from kalshi_strategies.ARBS.spec import load_spec

    root = os.path.join(LIVE_ROOT, "ARBS")
    os.makedirs(root, exist_ok=True)
    with open(SPEC_PATH, encoding="utf-8") as fh:
        hand = json.load(fh)
    led = os.path.join(root, "ledger.json")
    held = set(json.load(open(led, encoding="utf-8")).get("baskets") or {}) if os.path.exists(led) else set()
    rv = run(ConfigStore(ArbsConfig, CONFIG_PATH, None).config, hand,
             {t for b in load_spec(SPEC_PATH) for t in b.tickers},
             os.path.join(root, "scanned_spec.json"), os.path.join(root, "scan_review.json"), held)
    with open(os.path.join(root, "scanned_spec.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    n = {}
    for r in d["relationships"]:
        n[r["type"]] = n.get(r["type"], 0) + len(r["baskets"])
    print(f"{d['n_events']} events, {d['n_markets']} new markets, baskets {n}; "
          f"{len(rv['series'])} series await complete-set review")
