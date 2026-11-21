"""Kalshi-wide basket scan: every open event Kalshi flags as MECNET (at most
one market resolves YES) or DIRECNET (a ladder of "above X" strikes) becomes
relationships in spec.py's format, written to scanned_spec.json.

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
most traded events per market until the runner's CPU-held market count.

    python -m strategies.ARBS.scanner [root]   # one scan + selection, as the runner runs it
"""
from __future__ import annotations

import json
import os
import re
import time

import pandas as pd

from infra.kalshi.api import kalshi_get
from infra.config import atomic_write_json
from strategies.ARBS import parts, watch

UP = ("greater", "greater_or_equal")
DOWN = ("less", "less_or_equal")
# Sports spread/total ladders and watch.py's baskets, which Kalshi doesn't net
# (cash stays locked to settlement): recorded, never traded, until a replay says
# they pay. They sort last in select, so they only take the budget traded
# markets leave: none on shard 0, whose processes sit at their CPU-held count
# (11-21: 0 recorded there; building its ~110k such baskets added ~20 MB to
# every scan doc).
SPREAD_PREFIX = "spread:"
RECORD_PREFIXES = (SPREAD_PREFIX, watch.WATCH_PREFIX)
RECORD_SHARDS = (3,)
SPREAD_CROSS = 3          # each team's lowest strikes paired across teams


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


def spread_event(e: dict, skip_series) -> bool:
    s = e.get("series_ticker") or ""
    return (not e.get("collateral_return_type") and e.get("category") == "Sports"
            and s.endswith(("SPREAD", "TOTAL")) and not s.endswith("TEAMTOTAL")
            and e.get("exchange_index") in RECORD_SHARDS and s not in skip_series)


def spreads(event: dict, markets: list[dict], span: int) -> list[dict]:
    """A spread event: per team, "wins by over b" implies "over a" for a < b
    (YES a + NO b pays >= 1), and the two teams can't both cover a positive
    spread (NO on each pays >= 1). A total: the over ladder."""
    et, series = event["event_ticker"], event["series_ticker"]
    groups: dict[str, list[tuple[float, str]]] = {}
    for m in markets:
        if m.get("strike_type") not in UP or m.get("floor_strike") is None:
            continue
        team = re.fullmatch(r"([A-Z]+)\d+", _sfx(et, m["ticker"])) if series.endswith("SPREAD") else None
        key = team.group(1) if team else ("" if not series.endswith("SPREAD") else None)
        if key is not None:
            groups.setdefault(key, []).append((float(m["floor_strike"]), m["ticker"]))
    baskets = []
    for rungs in groups.values():
        rungs.sort()
        for i, (ka, a) in enumerate(rungs):
            for kb, b in rungs[i + 1:i + 1 + span]:
                if kb - ka > 1e-9:
                    baskets.append({"basket_id": f"{SPREAD_PREFIX}{et}:{_sfx(et, b)}=>{_sfx(et, a)}", "payout": 1,
                                    "legs": _legs((a, "yes"), (b, "no"))})
    if len(groups) == 2 and "" not in groups:
        lows = [[t for k, t in sorted(g) if k > 0][:SPREAD_CROSS] for g in groups.values()]
        for a in lows[0]:
            for b in lows[1]:
                baskets.append({"basket_id": f"{SPREAD_PREFIX}{et}:no:" + "+".join(sorted((_sfx(et, a), _sfx(et, b)))),
                                "payout": 1, "legs": _legs((a, "no"), (b, "no"))})
    if not baskets:
        return []
    return [{"relationship_id": f"{SPREAD_PREFIX}{et}", "type": "implication", "cluster": series,
             "description": f"{et}: spread/total ladder (not netted by Kalshi); recorded only.",
             "baskets": baskets}]


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
         freqs: dict[str, str] | None = None, live_windows: dict[str, list[str]] | None = None,
         settle_lead: pd.Timedelta | None = None, early_h: dict[str, float] | None = None,
         owns=None, locked_days: float | None = None) -> dict:
    """Every relationship found, each with its event's expected settlement,
    24h volume for `select`. `owns(event)`: only the events it accepts (shard
    0's split, parts.py). `starts`: event -> when it goes live
    (fetch_starts); `freqs`: series -> frequency (fetch_frequencies);
    `live_windows`: Kalshi category -> [from, until], its events live from
    `from` while the scan runs before `until`; `settle_lead`: an event
    without a kickoff is live from this long before its expected settlement;
    `early_h`: series -> hours before its start an event turns live;
    `locked_days`: record-only baskets (RECORD_PREFIXES) come only from events
    settling within this many days, None for no limit."""
    rels, tickers = [], set()
    locked_until = now + pd.Timedelta(days=locked_days) if locked_days is not None else None

    def active(e):
        return [m for m in e.get("markets") or []
                if m.get("status") == "active" and m.get("market_type", "binary") == "binary"]

    for e in events:
        spread = spread_event(e, skip_series) and e.get("exchange_index") in shards
        if not scanned_event(e, shards, skip_series) or (
                owns is not None and not owns(e.get("event_ticker") or "")):
            continue
        ms = active(e)
        s = settles(e, overrides)
        if len(ms) < 2 or s is None or (spread and locked_until is not None and s > locked_until):
            continue
        if spread:
            got = spreads(e, ms, ladder_span)
        elif e["collateral_return_type"] == "MECNET":
            complete = len(ms) == len(e["markets"]) and coverage_step(ms) is not None \
                and e["series_ticker"] not in complete_set_exclude
            got = mecnet(e, ms, mecnet_max_legs, complete)
        else:
            got = ladder(e, ms, ladder_span)
        s, live_from, vol = _timing(e, ms, s, now, starts, freqs, live_windows, settle_lead, early_h)
        for r in got:
            r.update(expected_settlement_utc=s.isoformat(), quote_until_utc=None, resume_quoting_utc=None,
                     live_from_utc=live_from, volume_24h=vol)
            tickers |= {l["ticker"] for b in r["baskets"] for l in b["legs"]}
        rels += got
    # Record-only baskets across events (watch.py): settled when the last of
    # their events is, live once the first one is.
    pool = watch_pool(events, shards, overrides, now, locked_days)
    for anchor, r, evs_in in watch.build([e for e, _ in pool.values()], RECORD_SHARDS, skip_series,
                                         recorded=lambda e: spread_event(e, skip_series)):
        if owns is not None and not owns(anchor):
            continue
        ts = [_timing(pool[et][0], active(pool[et][0]), pool[et][1], now, starts, freqs, live_windows,
                      settle_lead, early_h) for et in evs_in]
        lives = [t[1] for t in ts if t[1]]
        r.update(expected_settlement_utc=max(t[0] for t in ts).isoformat(), quote_until_utc=None,
                 resume_quoting_utc=None, live_from_utc=min(lives) if lives else None,
                 volume_24h=round(sum(t[2] for t in ts), 2))
        tickers |= {l["ticker"] for b in r["baskets"] for l in b["legs"]}
        rels.append(r)
    return {"generated_at_utc": now.isoformat(), "source": "strategies.ARBS.scanner",
            "n_events": len({r["relationship_id"].split(":", 1)[1] for r in rels}), "n_markets": len(tickers),
            "relationships": rels}


def watch_pool(events: list[dict], shards, overrides: dict[str, str], now: pd.Timestamp,
               locked_days: float | None) -> dict[str, tuple]:
    """event -> (event, expected settlement) for the events watch.build gets:
    on `shards` and RECORD_SHARDS, settling within `locked_days` (None: any)."""
    until = now + pd.Timedelta(days=locked_days) if locked_days is not None else None
    out = {}
    for e in events:
        if (e.get("exchange_index") in shards and e.get("exchange_index") in RECORD_SHARDS
                and (s := settles(e, overrides)) is not None and (until is None or s <= until)):
            out[e["event_ticker"]] = (e, s)
    return out


def _timing(e, ms, s, now, starts, freqs, live_windows, settle_lead, early_h):
    """(expected settlement, live_from iso or None, 24h volume) for one event."""
    vol = round(sum(_f(m.get("volume_24h_fp")) for m in ms), 2)
    # A resting quote on a live game is picked off the moment the score
    # changes (CFCDLG 10-29 00:42). occurrence_datetime is a game's
    # expected END, not its kickoff, hence the milestone start.
    live = [pd.Timestamp(t) for t in [m.get("occurrence_datetime") for m in ms]
            + [(starts or {}).get(e["event_ticker"])] if t]
    early = (early_h or {}).get(e.get("series_ticker"))
    if early and (starts or {}).get(e["event_ticker"]):
        live.append(pd.Timestamp(starts[e["event_ticker"]]) - pd.Timedelta(hours=early))
    # Not for an event with a kickoff: Kalshi files CS2 maps as daily, which
    # made 61 matches live up to a day before they started (10-30).
    period = PERIODS.get((freqs or {}).get(e.get("series_ticker")))
    if period is not None and e.get("strike_date") and not (starts or {}).get(e["event_ticker"]):
        live.append(pd.Timestamp(e["strike_date"]) - period)
    # Kalshi dates most election events to the seat's start (Jan 2027), so
    # occurrence_datetime never makes results night live: the window does.
    # Past `until` it drops out, so a later race isn't held at live size.
    w = (live_windows or {}).get(e.get("category"))
    if w and now < pd.Timestamp(w[1]):
        live.append(pd.Timestamp(w[0]))
    # News lands hardest near settlement: a weekly Hormuz/Bab el-Mandeb ladder
    # quoted as not live lost $108 to one headline 1.7h out (11-10 13:18).
    if settle_lead is not None and not (starts or {}).get(e["event_ticker"]):
        live.append(s - settle_lead)
    return s, (min(live).isoformat() if live else None), vol


# An event whose loop CPU stayed under CHEAP_EVENT_S on every recent day (today's
# too) doesn't count against the CPU-held market count. 82-88% of shard 0's
# events took under 1 s a day on 11-18, and cutting the count dropped them first
# while the busy few kept the CPU: ARBS0B went from 10.6k events to 1.1k at the
# same 11-12 core-hours a day (11-19). TOTAL_MARKETS_MAX holds each process's
# scan where shard 0's reads held (~31k across its processes on 11-17), well
# short of where they didn't (ARBS0C alone at 55k couldn't arm, 11-18).
CHEAP_EVENT_S, TOTAL_MARKETS_MAX = 10.0, 15000


def cheap_events(procs: tuple[str, ...], root: str | None = None, days: int = parts.SPEND_DAYS) -> set[str]:
    """Events `procs` timed in their loops on the last `days` UTC days whose
    CPU (summed over the processes) stayed under CHEAP_EVENT_S on each one.
    An event with no record isn't cheap."""
    per: dict[tuple[str, str], float] = {}
    for p in procs:
        try:
            with open(os.path.join(root or parts.LIVE_ROOT, p, parts.CPU), encoding="utf-8") as fh:
                by_day = json.load(fh)
        except (OSError, ValueError):
            continue
        for day in sorted(by_day)[-days:]:
            for ev, x in by_day[day].items():
                per[(ev, day)] = per.get((ev, day), 0.0) + x
    worst: dict[str, float] = {}
    for (ev, _), x in per.items():
        worst[ev] = max(worst.get(ev, 0.0), x)
    return {ev for ev, x in worst.items() if x < CHEAP_EVENT_S}


def select(doc: dict, *, now: pd.Timestamp, days: float | None, max_markets: int | None, known_tickers: set[str] = frozenset(),
           held: set[str] = frozenset(), exclude: set[str] = frozenset(), cheap: set[str] = frozenset(),
           max_total: int | None = None) -> dict:
    """The part of a scan to trade: relationships settling within `days` (None: any), the
    most traded per market first (24h volume),
    until max_markets markets beyond `known_tickers` (None: no limit), not
    counting `cheap` events' markets, and max_total markets in all.
    Relationships holding a basket in `held` are always kept."""
    # Per market, not in total: on 11-05 a 400-market Nasdaq ladder and the
    # 14-market election-margin ladders outranked 3-market games trading
    # $40-110k a day, and only 10 games within 36 h made the budget (32 per market).
    def per_market(r):
        n = len({l["ticker"] for b in r["baskets"] for l in b["legs"]})
        return (r.get("volume_24h") or 0.0) / max(n, 1)

    horizon = None if days is None else now + pd.Timedelta(days=days)
    out, used, every = [], set(), set()
    # Recorded-only baskets last: they take only the budget traded markets leave.
    for r in sorted(doc["relationships"], key=lambda r: (r["relationship_id"].startswith(RECORD_PREFIXES), -per_market(r))):
        ts = {l["ticker"] for b in r["baskets"] for l in b["legs"]} - known_tickers
        costly = set() if r["relationship_id"].split(":", 1)[-1] in cheap else ts
        if any(b["basket_id"] in held for b in r["baskets"]):
            out.append(r)
            used |= costly
            every |= ts
            continue
        s = r.get("expected_settlement_utc")
        if (s and horizon is not None and pd.Timestamp(s) > horizon) or r.get("cluster") in exclude                 or r["relationship_id"].split(":", 1)[-1] in exclude:
            continue
        # Adding no new costly market fits however far over the count `used`
        # is: a held basket past the cap otherwise shut out every cheap event
        # after it (11-21: ARBS 13k -> 1.7k markets, ARBS0C 15k -> 1.5k).
        if (max_markets is None or costly <= used or len(used | costly) <= max_markets) and                 (max_total is None or len(every | ts) <= max_total):
            out.append(r)
            used |= costly
            every |= ts
    return {**doc, "relationships": out}


def settlement_overrides() -> dict[str, str]:
    """series -> the month its events settle by (settlement_overrides.json)."""
    from strategies.ARBS.config import OVERRIDES_PATH
    with open(OVERRIDES_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)
    return {s: v["expected_settlement_month"] for s, v in (doc.get("settlement_basis_by_series") or {}).items()}


def other_series() -> set[str]:
    from strategies.run import registry
    return {t for t, s in registry().items() if t != "ARBS"}


def scanned_event(e: dict, shards, skip_series) -> bool:
    """Listed by scan(): a netted event, or a recorded-only spread. The owners
    map must list the spreads too, or no process of a split shard takes them."""
    return tradeable_event(e, shards, skip_series) or (spread_event(e, skip_series)
                                                       and e.get("exchange_index") in shards)


def tradeable_event(e: dict, shards, skip_series) -> bool:
    return (e.get("collateral_return_type") in ("MECNET", "DIRECNET") and e.get("series_ticker") not in skip_series
            and e.get("exchange_index") in shards)


def run(cfg, spec_path: str, held: set[str] = frozenset(), owns=None, events: list[dict] | None = None,
        locked_days: float | None = None) -> dict:
    """Blocking: fetch (unless `events` are given), scan, write the file;
    returns the doc. Relationships of the previous file holding a basket in
    `held` are carried over, so a position the scan no longer lists survives
    a restart."""
    now = pd.Timestamp.now(tz="UTC")
    doc = scan(fetch_events() if events is None else events, now=now, mecnet_max_legs=cfg.scan.mecnet_max_legs,
               owns=owns,
               ladder_span=cfg.scan.ladder_span, complete_set_exclude=cfg.scan.complete_set_exclude,
               shards=set(cfg.scan.shards), skip_series=other_series(), starts=fetch_starts(now),
               freqs=fetch_frequencies(), live_windows=cfg.scan.live_windows,
               settle_lead=pd.Timedelta(hours=cfg.scan.live_before_settle_h) if cfg.scan.live_before_settle_h > 0 else None,
               early_h=cfg.scan.live_early_h, locked_days=locked_days,
               overrides=settlement_overrides())
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


def scan_and_select(root: str, config_path: str | None = None, markets: int | None = None) -> dict:
    """Blocking, and meant for a child process: a scan holds ~1 GB of
    temporary objects that Python keeps after freeing them, so scanning in
    the trading process grew it ~430 MB a scan until the OOM killer took it
    (10-29 03:45 and 04:12). Writes the full scan and the part to trade
    (`select` up to `markets`, the runner's CPU-held count, None for all;
    held baskets kept) to SELECTED, which is all the runner reads. Returns
    counts."""
    from infra.config import ConfigStore, load
    from strategies.ARBS.config import CONFIG_PATH, CONFIGPM_PATH, ArbsConfig

    cfg = ConfigStore(ArbsConfig, config_path or CONFIG_PATH, None).config
    # Record-only baskets aren't netted, so their cash is locked to settlement
    # as on Polymarket: they keep to PM's trading.days_till_expiry.
    locked_days = load(ArbsConfig, CONFIGPM_PATH).trading.days_till_expiry
    held = set()
    for name in ("ledger.json", "ledger_shadow.json"):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                held |= set(json.load(fh).get("baskets") or {})
        except (OSError, ValueError):
            pass
    from infra.polymarket.orders import PM_SHARD
    if PM_SHARD in cfg.scan.shards:
        from strategies.ARBS import pm_scan
        return pm_scan.scan_and_select(root, cfg, held, markets)
    owns, events, info = None, None, {}
    name = os.path.basename(os.path.normpath(root))
    if (group := parts.group_of(name)) is not None:
        events = fetch_events()
        st = parts.load(procs=group)
        if name == group[0]:
            # The one writer of the owners map: assigns new events, rebalances hourly
            # on writes and book updates, and hands on events an owner's scan dropped.
            now = time.time()
            skip = other_series()
            listed = [e["event_ticker"] for e in events if scanned_event(e, set(cfg.scan.shards), skip)]
            pool = watch_pool(events, set(cfg.scan.shards), settlement_overrides(), pd.Timestamp.now(tz="UTC"),
                              locked_days)
            listed += sorted({a for a, _, _ in watch.build([e for e, _ in pool.values()], RECORD_SHARDS, skip,
                                                          recorded=lambda e: spread_event(e, skip))} - set(listed))
            cpu = parts.spend(procs=group, name=parts.CPU)
            if sum(cpu.values()) < parts.CPU_MIN_S:
                cpu = parts.spend(procs=group, name=parts.MSGS)
            moves = parts.update(st, listed, now, parts.spend(procs=group), procs=group,
                                 msgs=cpu, held=parts.held_events(group),
                                 drops=parts.dropped(group, now), spare=parts.room(group, now))
            parts.save(st, parts.owners_path(group))
            info = {"moved": len(moves), "load": st.get("load")}
        idx = group.index(name)
        owns = lambda ev: parts.owner(st, ev) == idx  # noqa: E731
    doc = run(cfg, os.path.join(root, "scanned_spec.json"), held, owns=owns, events=events, locked_days=locked_days)
    cheap = cheap_events(group) if group is not None else set()
    sel = select(doc, now=pd.Timestamp.now(tz="UTC"), days=cfg.trading.days_till_expiry, max_markets=markets,
                 held=held, exclude=set(cfg.watch.exclude), cheap=cheap, max_total=TOTAL_MARKETS_MAX)
    info["cheap_events"] = len(cheap)
    atomic_write_json(os.path.join(root, SELECTED), sel, indent=None)
    if owns is not None:
        # Owned events only the CPU-held count left out (not the horizon or
        # exclude, which would leave them out anywhere), for the group's
        # leader to hand on (parts.update).
        ev_of = lambda rs: {r["relationship_id"].split(":", 1)[-1] for r in rs}  # noqa: E731
        every = select(doc, now=pd.Timestamp.now(tz="UTC"), days=cfg.trading.days_till_expiry, max_markets=None,
                       held=held, exclude=set(cfg.watch.exclude))
        atomic_write_json(os.path.join(root, parts.DROPPED),
                          {"t": time.time(), "events": sorted(ev_of(every["relationships"]) - ev_of(sel["relationships"]))},
                          indent=None)
    return {"listed_markets": doc["n_markets"], "selected_relationships": len(sel["relationships"]), **info}


if __name__ == "__main__":
    import sys

    from infra.runtime.persist import LIVE_ROOT

    # The scan peaks ~1 GB: it gives up the -900 inherited from alpha5-arbs so the OOM
    # killer takes it before a trading process, but stays under the dashboard, perf
    # worker and recorders (900-1000), which must die first. At 1000 it tied them and,
    # being the largest, was the one killed.
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("500")
    except OSError:
        pass
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(LIVE_ROOT, "ARBS")
    os.makedirs(root, exist_ok=True)
    print(json.dumps(scan_and_select(root, sys.argv[2] if len(sys.argv) > 2 else None,
                                     int(sys.argv[3]) if len(sys.argv) > 3 else None)))
