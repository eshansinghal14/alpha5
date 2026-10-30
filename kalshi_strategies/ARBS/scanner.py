"""Kalshi-wide basket scan: every open event Kalshi flags as MECNET (at most
one market resolves YES) or DIRECNET (a ladder of "above X" strikes) becomes
relationships in the hand spec's format, written to scanned_spec.json.

  MECNET     NO on the k likeliest markets pays at least k-1, for k = 2 up to
             scan.mecnet_max_legs. Any subset is safe; ranked by YES price at
             scan time so the subsets that can clear are the ones listed.
  coverage   MECNET buckets running from a "below" tail through between ranges
             to an "above" tail are exhaustive as well: a complete set (both
             directions), unless the series is in scan.complete_set_exclude.
             Of the 3,600 such events the API still returns as settled
             (10-29 check, ~68 days, 59 series), 3,598 paid exactly one YES;
             INX-24OCT25 and NASDAQ100-24OCT25 settled every bucket NO with
             no value (voided), which no rule here can see coming.
  live       live_from: when a sports or esports event starts (Kalshi's milestones:
             kickoff, first serve); for a daily or hourly series (temperature,
             gas, index ladders) the start of the day or hour its strike_date
             closes; or its earliest occurrence_datetime if sooner. Quoting
             past it is trading.quote_live_events.
  DIRECNET   for strikes a < b, YES >=a and NO >=b pays at least 1; each
             strike is paired with the next scan.ladder_span above it (a
             200-strike KXBTCD ladder is 20k pairs otherwise).

Every such event is listed except those off scan.shards and series another
strategy trades (the position audit would read its contracts as ours).
`select` picks what the runner trades: within trading.days_till_expiry, the
most traded events until scan.max_markets markets.

    python -m kalshi_strategies.ARBS.scanner [root]   # one scan + selection, as the runner runs it
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


def fetch_starts(now: pd.Timestamp) -> dict[str, str]:
    """Blocking (~10 pages). event_ticker -> earliest start_date of a sports
    or esports milestone it belongs to, for milestones started in the last
    week or not yet (a Test match runs five days). Other categories carry
    placeholder starts (elections: 2020-01-01) and are left out."""
    out, cursor = {}, None
    since = (now - pd.Timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        params = {"minimum_start_date": since, "limit": 500}
        if cursor:
            params["cursor"] = cursor
        d = kalshi_get("/trade-api/v2/milestones", params=params)
        for m in d.get("milestones") or []:
            if m.get("category") not in ("Sports", "Esports") or not m.get("start_date"):
                continue
            for et in m.get("related_event_tickers") or []:
                if et not in out or m["start_date"] < out[et]:
                    out[et] = m["start_date"]
        cursor = d.get("cursor")
        if not cursor or not d.get("milestones"):
            return out


# A series' measurement period (Kalshi's series frequency): its events are
# live from strike_date minus this. A day's temperature is known hours before
# Kalshi settles it, and late traders know it (KXHIGHTSFO 10-29 22:02 PT took a
# 50-lot NO for -$7.16 while quoted as not live).
PERIODS = {"daily": pd.Timedelta(days=1), "hourly": pd.Timedelta(hours=1),
           "fifteen_min": pd.Timedelta(minutes=15)}


def fetch_frequencies() -> dict[str, str]:
    """Blocking (one paged call, ~0.2 s). series_ticker -> frequency."""
    out, cursor = {}, None
    while True:
        d = kalshi_get("/trade-api/v2/series", params={"limit": 1000, **({"cursor": cursor} if cursor else {})})
        for s in d.get("series") or []:
            out[s["ticker"]] = s.get("frequency")
        cursor = d.get("cursor")
        if not cursor or not d.get("series"):
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
    at that resolution."""
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
        # Legs sorted in the id: the same set keeps its id when prices reorder.
        baskets.append({"basket_id": f"mecnet:{et}:no:" + "+".join(sorted(_sfx(et, t) for t in ts)), "payout": k - 1,
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


def scan(events: list[dict], *, now: pd.Timestamp, mecnet_max_legs: int, ladder_span: int,
         complete_set_exclude: list[str], overrides: dict[str, str], shards=(0,),
         skip_series: set[str] = frozenset(), starts: dict[str, str] | None = None,
         freqs: dict[str, str] | None = None) -> dict:
    """Every relationship found, each with its event's expected settlement
    and 24h volume for `select`. `starts`: event -> when it goes live
    (fetch_starts); `freqs`: series -> frequency (fetch_frequencies)."""
    rels, tickers = [], set()
    for e in events:
        if e.get("collateral_return_type") not in ("MECNET", "DIRECNET") or e.get("series_ticker") in skip_series \
                or e.get("exchange_index") not in shards:
            continue
        ms = [m for m in e.get("markets") or []
              if m.get("status") == "active" and m.get("market_type", "binary") == "binary"]
        s = settles(e, overrides)
        if len(ms) < 2 or s is None:
            continue
        if e["collateral_return_type"] == "MECNET":
            complete = len(ms) == len(e["markets"]) and coverage_step(ms) is not None \
                and e["series_ticker"] not in complete_set_exclude
            got = mecnet(e, ms, mecnet_max_legs, complete)
        else:
            got = ladder(e, ms, ladder_span)
        vol = round(sum(_f(m.get("volume_24h_fp")) for m in ms), 2)
        # A resting quote on a live game is picked off the moment the score
        # changes (CFCDLG 10-29 00:42). occurrence_datetime is a game's
        # expected END, not its kickoff, hence the milestone start.
        live = [pd.Timestamp(t) for t in [m.get("occurrence_datetime") for m in ms]
                + [(starts or {}).get(e["event_ticker"])] if t]
        # Not for an event with a kickoff: Kalshi files CS2 maps as daily, which
        # made 61 matches live up to a day before they started (10-30).
        period = PERIODS.get((freqs or {}).get(e.get("series_ticker")))
        if period is not None and e.get("strike_date") and not (starts or {}).get(e["event_ticker"]):
            live.append(pd.Timestamp(e["strike_date"]) - period)
        for r in got:
            r.update(expected_settlement_utc=s.isoformat(), quote_until_utc=None, resume_quoting_utc=None,
                     live_from_utc=min(live).isoformat() if live else None, volume_24h=vol)
            tickers |= {l["ticker"] for b in r["baskets"] for l in b["legs"]}
        rels += got
    return {"generated_at_utc": now.isoformat(), "source": "kalshi_strategies.ARBS.scanner",
            "n_events": len({r["relationship_id"].split(":", 1)[1] for r in rels}), "n_markets": len(tickers),
            "relationships": rels}


def select(doc: dict, *, now: pd.Timestamp, days: float, max_markets: int, known_tickers: set[str] = frozenset(),
           held: set[str] = frozenset(), exclude: set[str] = frozenset()) -> dict:
    """The part of a scan to trade: relationships settling within `days`, the
    most traded first, until max_markets markets beyond `known_tickers`.
    Relationships holding a basket in `held` are always kept."""
    horizon = now + pd.Timedelta(days=days)
    out, used = [], set()
    for r in sorted(doc["relationships"], key=lambda r: -(r.get("volume_24h") or 0.0)):
        ts = {l["ticker"] for b in r["baskets"] for l in b["legs"]} - known_tickers
        if any(b["basket_id"] in held for b in r["baskets"]):
            out.append(r)
            used |= ts
            continue
        s = r.get("expected_settlement_utc")
        if (s and pd.Timestamp(s) > horizon) or r.get("cluster") in exclude:
            continue
        if len(used | ts) <= max_markets:
            out.append(r)
            used |= ts
    return {**doc, "relationships": out}


def other_series() -> set[str]:
    from kalshi_strategies.run import registry
    return {t for t, s in registry().items() if t != "ARBS"}


def run(cfg, hand_doc: dict, spec_path: str, held: set[str] = frozenset()) -> dict:
    """Blocking: fetch, scan, write the file; returns the doc. Relationships
    of the previous file holding a basket in `held` are carried over, so a
    position the scan no longer lists survives a restart."""
    now = pd.Timestamp.now(tz="UTC")
    doc = scan(fetch_events(), now=now, mecnet_max_legs=cfg.scan.mecnet_max_legs,
               ladder_span=cfg.scan.ladder_span, complete_set_exclude=cfg.scan.complete_set_exclude,
               shards=set(cfg.scan.shards), skip_series=other_series(), starts=fetch_starts(now),
               freqs=fetch_frequencies(),
               overrides={s: v["expected_settlement_month"]
                          for s, v in (hand_doc.get("settlement_basis_by_series") or {}).items()})
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
    return doc


SELECTED = "scanned_selected.json"


def scan_and_select(root: str) -> dict:
    """Blocking, and meant for a child process: a scan holds ~1 GB of
    temporary objects that Python keeps after freeing them, so scanning in
    the trading process grew it ~430 MB a scan until the OOM killer took it
    (10-29 03:45 and 04:12). Writes the full scan and the part to trade
    (`select` under today's config, held baskets kept) to SELECTED, which is
    all the runner reads. Returns counts."""
    from kalshi_infra.config import ConfigStore
    from kalshi_strategies.ARBS.config import CONFIG_PATH, SPEC_PATH, ArbsConfig
    from kalshi_strategies.ARBS.spec import load_spec

    cfg = ConfigStore(ArbsConfig, CONFIG_PATH, None).config
    with open(SPEC_PATH, encoding="utf-8") as fh:
        hand = json.load(fh)
    held = set()
    for name in ("ledger.json", "ledger_shadow.json"):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                held |= set(json.load(fh).get("baskets") or {})
        except (OSError, ValueError):
            pass
    doc = run(cfg, hand, os.path.join(root, "scanned_spec.json"), held)
    sel = select(doc, now=pd.Timestamp.now(tz="UTC"), days=cfg.trading.days_till_expiry,
                 max_markets=cfg.scan.max_markets, known_tickers={t for b in load_spec(SPEC_PATH) for t in b.tickers},
                 held=held, exclude=set(cfg.watch.exclude))
    atomic_write_json(os.path.join(root, SELECTED), sel, indent=None)
    return {"listed_markets": doc["n_markets"], "selected_relationships": len(sel["relationships"])}


if __name__ == "__main__":
    import sys

    from kalshi_infra.runtime.persist import LIVE_ROOT

    # The scan peaks ~1 GB and isn't trading: it gives up the -900 inherited
    # from alpha5-arbs so the OOM killer takes it before any trading process.
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(LIVE_ROOT, "ARBS")
    os.makedirs(root, exist_ok=True)
    print(json.dumps(scan_and_select(root)))
