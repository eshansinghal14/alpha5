"""Basket scan for the Polymarket US process (scan.shards holds PM_SHARD):
relationships in the scanner's format, so the runner trades them unchanged.

  pm soccer   a game's home / draw / away markets (90 minutes plus stoppage):
              complete set both ways, and NO on any two pays 1.
  pm weather  a city's daily high or low in 2F buckets from a "<= x" tail to
              a ">= y" tail: complete set both ways, NO on any two pays 1.
  cross       the same outcome on both exchanges: Polymarket YES + Kalshi NO,
              and Polymarket NO + Kalshi YES, each pays 1. Soccer games paired
              by both teams' names and the date; weather by station
              (Polymarket's rules name the NWS station Kalshi's CLI report is).

Polymarket's endDate is a deadline about two weeks after a game, so a game
"settles" (for days_till_expiry) a few hours after kickoff.
"""
from __future__ import annotations

import collections
import json
import os
import re
import unicodedata

import pandas as pd

from infra.config import atomic_write_json
from infra.polymarket.recorder import list_open

GAME_SETTLE = pd.Timedelta(hours=3)
# Kalshi series -> Polymarket weather slug city. Same station on both: KNYC is
# Kalshi's CLINYC, KMDW CLIMDW, KMIA CLIMIA, KLAX CLILAX, KSFO CLISFO, KPHL CLIPHL.
WEATHER = {"KXHIGHNY": "nychigh", "KXLOWTNYC": "nyclow", "KXHIGHMIA": "miahigh", "KXHIGHCHI": "mdwhigh",
           "KXLOWTCHI": "mdwlow", "KXHIGHLAX": "laxhigh", "KXHIGHTSFO": "sfohigh", "KXHIGHPHIL": "phlhigh"}
# A day's reading is known before the day ends: the whole local day is live
# (Kalshi's scan does the same with the series' daily period). 05:00 UTC is
# midnight Eastern, the earliest of these cities.
WEATHER_LIVE_UTC_H = 5
WEATHER_SETTLE_UTC_H = 14
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                   "DEC"], 1)}
STOP = {"fc", "cf", "sc", "afc", "club", "de", "the", "team", "of", "and", "u", "city", "united", "real", "ac",
        "w", "women", "womens", "cd", "ad", "sd", "ud", "cs", "sv", "fk", "nk", "sk", "if", "bk"}
ALIAS = {"saint": "st", "state": "st"}


# ------------------------------------------------------------------ names

def toks(s: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return [ALIAS.get(w, w) for w in re.sub(r"[^a-z0-9 ]", " ", s).split()]


def sig(s: str | None) -> set[str]:
    return {w for w in toks(s) if w not in STOP and len(w) > 1}


def name_match(a: str | None, bs) -> float:
    """1 for the same name; containment of the shorter's words, tie-broken by
    overlap so "Idaho" prefers "U. of Idaho" to "Idaho State"."""
    A, best = sig(a), 0.0
    for b in bs:
        B = sig(b)
        if not A or not B:
            continue
        if A == B:
            return 1.0
        i = len(A & B)
        best = max(best, 0.9 * i / min(len(A), len(B)) + 0.1 * i / len(A | B))
    return best


def kalshi_date(event_ticker: str) -> pd.Timestamp | None:
    m = re.search(r"-(\d\d)([A-Z]{3})(\d\d)", event_ticker)
    if not m or m.group(2) not in MON:
        return None
    try:
        return pd.Timestamp(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


# ------------------------------------------------------------------ polymarket events

def soccer_games(pm: dict[str, dict]) -> list[dict]:
    """Three-way games: {event, teams [home, away], start, markets {home, away, draw}}."""
    by_ev = collections.defaultdict(list)
    for r in pm.values():
        if r.get("sports_type") == "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME":
            by_ev[r["event"]].append(r)
    out = []
    for ev, rs in by_ev.items():
        title = rs[0].get("event_title") or ""
        teams = re.split(r"\s+vs\.?\s+", title)
        start = rs[0].get("game_start") or rs[0].get("event_start")
        if len(rs) != 3 or len(teams) != 2 or not start:
            continue
        mk = {}
        for r in rs:
            if "draw" in (r.get("question") or "").lower():
                mk["draw"] = r["slug"]
                continue
            team = next((s.get("team") for s in r.get("sides") or [] if s.get("team")), None)
            hits = [name_match(team, [t]) for t in teams]
            if max(hits) >= 0.5 and hits[0] != hits[1]:
                mk[0 if hits[0] > hits[1] else 1] = r["slug"]
        if set(mk) == {0, 1, "draw"}:
            abbr = {i: next((s.get("abbr") for s in pm[mk[i]].get("sides") or [] if s.get("abbr")), None)
                    for i in (0, 1)}
            out.append({"event": ev, "teams": teams, "abbr": abbr, "start": pd.Timestamp(start), "markets": mk,
                        "league": pm[mk[0]].get("series")})
    return out


def _bucket(key: str) -> tuple[float, float] | None:
    """Polymarket weather bucket from its slug suffix: lt48f is <= 47,
    gte50lt51f is 50..51, gte60f is >= 60."""
    if m := re.fullmatch(r"lt(-?\d+)f", key):
        return (float("-inf"), float(m.group(1)) - 1)
    if m := re.fullmatch(r"gte(-?\d+)lt(-?\d+)f", key):
        return (float(m.group(1)), float(m.group(2)))
    if m := re.fullmatch(r"gte(-?\d+)f", key):
        return (float(m.group(1)), float("inf"))
    return None


def weather_days(pm: dict[str, dict]) -> list[dict]:
    """{event, city, day, buckets {(lo, hi): slug}, covered} per city and day."""
    by_ev = collections.defaultdict(dict)
    for slug, r in pm.items():
        m = re.fullmatch(r"tc-temp-([a-z]+)-(\d{4}-\d\d-\d\d)-(.+)", slug)
        if m and (b := _bucket(m.group(3))) is not None:
            by_ev[(m.group(1), m.group(2))][b] = slug
    out = []
    for (city, day), bs in by_ev.items():
        edges = sorted(bs)
        covered = (len(edges) >= 3 and edges[0][0] == float("-inf") and edges[-1][1] == float("inf")
                   and all(edges[i + 1][0] == edges[i][1] + 1 for i in range(len(edges) - 1)))
        out.append({"event": f"tc-temp-{city}-{day}", "city": city, "day": pd.Timestamp(day), "buckets": bs,
                    "covered": covered})
    return out


# ------------------------------------------------------------------ relationships

def _legs(*pairs) -> list[dict]:
    return [{"ticker": t, "side": s} for t, s in pairs]


def outcome_set(event: str, cluster: str, slugs: list[str], names: list[str], complete: bool) -> list[dict]:
    """Complete set both ways (when the buckets cover every outcome) and NO on
    every pair, which pays 1 when at most one is YES."""
    rels = []
    if complete:
        rels.append({"relationship_id": f"complete_set:{event}", "type": "complete_set", "cluster": cluster,
                     "description": f"{event}: one of these resolves YES.",
                     "baskets": [{"basket_id": f"complete_set:{event}:buy_all_yes", "payout": 1,
                                  "legs": _legs(*((s, "yes") for s in slugs))},
                                 {"basket_id": f"complete_set:{event}:buy_all_no", "payout": len(slugs) - 1,
                                  "legs": _legs(*((s, "no") for s in slugs))}]})
    pairs = [(i, j) for i in range(len(slugs)) for j in range(i + 1, len(slugs))]
    if complete and len(slugs) == 2:
        pairs = []
    if pairs:
        rels.append({"relationship_id": f"mecnet:{event}", "type": "mutually_exclusive_no", "cluster": cluster,
                     "description": f"{event}: at most one resolves YES; NO on any two pays 1.",
                     "baskets": [{"basket_id": f"mecnet:{event}:no:{'+'.join(sorted((names[i], names[j])))}",
                                  "payout": 1, "legs": _legs((slugs[i], "no"), (slugs[j], "no"))}
                                 for i, j in pairs]})
    return rels


def cross(pm_slug: str, k_ticker: str, cluster: str) -> dict:
    """Both ways across exchanges on one outcome; the Polymarket leg first, so
    the basket's shard is Polymarket's."""
    return {"relationship_id": f"cross:{k_ticker}", "type": "cross_venue", "cluster": cluster,
            "description": f"{k_ticker} on Kalshi pays on the same outcome as {pm_slug} on Polymarket US.",
            "baskets": [{"basket_id": f"cross:{k_ticker}:pm_yes", "payout": 1,
                         "legs": _legs((pm_slug, "yes"), (k_ticker, "no"))},
                        {"basket_id": f"cross:{k_ticker}:pm_no", "payout": 1,
                         "legs": _legs((pm_slug, "no"), (k_ticker, "yes"))}]}


def kalshi_soccer(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        ms = [m for m in e.get("markets") or [] if m.get("status") == "active"]
        tie = [m for m in ms if (m.get("yes_sub_title") or "").lower() in ("tie", "draw")]
        teams = [m for m in ms if m not in tie]
        if not e.get("series_ticker", "").endswith("GAME") or len(tie) != 1 or len(teams) != 2 or len(ms) != 3:
            continue
        d = kalshi_date(e["event_ticker"])
        if d is not None:
            out.append({"event": e["event_ticker"], "date": d, "teams": teams, "tie": tie[0]})
    return out


def pair_soccer(k: dict, games_by_day: dict) -> tuple[dict, dict[str, str]] | None:
    """The Polymarket game for Kalshi game `k` and {kalshi ticker: polymarket
    slug}, or None. Both teams must match, in one orientation."""
    best = None
    for dd in (-1, 0, 1):
        for g in games_by_day.get(k["date"] + pd.Timedelta(days=dd), ()):
            sc = []
            for i, km in enumerate(k["teams"]):
                code = km["ticker"].rsplit("-", 1)[1].lower()
                row = []
                for side in (0, 1):
                    v = name_match(km.get("yes_sub_title"), [g["teams"][side]])
                    if code and code == (g["abbr"].get(side) or "").lower():
                        v = max(v, 1.0)
                    row.append(v)
                sc.append(row)
            straight = sc[0][0] + sc[1][1] if min(sc[0][0], sc[1][1]) >= 0.5 else 0.0
            flipped = sc[0][1] + sc[1][0] if min(sc[0][1], sc[1][0]) >= 0.5 else 0.0
            score = max(straight, flipped) - 0.1 * abs(dd)
            if max(straight, flipped) >= 1.3 and (best is None or score > best[0]):
                best = (score, g, straight >= flipped)
    if best is None:
        return None
    _, g, straight = best
    order = (0, 1) if straight else (1, 0)
    m = {k["teams"][0]["ticker"]: g["markets"][order[0]], k["teams"][1]["ticker"]: g["markets"][order[1]],
         k["tie"]["ticker"]: g["markets"]["draw"]}
    return g, m


def pair_weather(events: list[dict], days: list[dict]) -> dict[str, str]:
    by = {(d["city"], d["day"]): d for d in days}
    out = {}
    for e in events:
        city = WEATHER.get(e.get("series_ticker"))
        d = kalshi_date(e["event_ticker"]) if city else None
        w = by.get((city, d)) if d is not None else None
        if w is None:
            continue
        for m in e.get("markets") or []:
            st, fl, cap = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
            try:
                b = ((float("-inf"), float(cap) - 1) if st == "less" else
                     (float(fl) + 1, float("inf")) if st == "greater" else
                     (float(fl), float(cap)) if st == "between" else None)
            except (TypeError, ValueError):
                b = None
            if b is not None and b in w["buckets"] and m.get("status") == "active":
                out[m["ticker"]] = w["buckets"][b]
    return out


def scan(pm: dict[str, dict], kalshi_events: list[dict], *, now: pd.Timestamp, cross_venue: bool = True) -> dict:
    rels = []
    games = soccer_games(pm)
    for g in games:
        rs = outcome_set(g["event"], f"pm:soccer:{g['league']}",
                         [g["markets"][0], g["markets"]["draw"], g["markets"][1]], ["home", "draw", "away"], True)
        for r in rs:
            r.update(expected_settlement_utc=(g["start"] + GAME_SETTLE).isoformat(),
                     live_from_utc=g["start"].isoformat())
        rels += rs
    days = weather_days(pm)
    for d in days:
        bs = sorted(d["buckets"])
        rs = outcome_set(d["event"], "pm:weather", [d["buckets"][b] for b in bs],
                         [d["buckets"][b].rsplit("-", 1)[1] for b in bs], d["covered"])
        for r in rs:
            r.update(expected_settlement_utc=(d["day"] + pd.Timedelta(days=1, hours=WEATHER_SETTLE_UTC_H)).tz_localize("UTC").isoformat(),
                     live_from_utc=(d["day"] + pd.Timedelta(hours=WEATHER_LIVE_UTC_H)).tz_localize("UTC").isoformat())
        rels += rs
    n_cross = 0
    if cross_venue:
        by_day = collections.defaultdict(list)
        for g in games:
            by_day[g["start"].tz_convert(None).normalize()].append(g)
        for k in kalshi_soccer(kalshi_events):
            got = pair_soccer(k, by_day)
            if got is None:
                continue
            g, m = got
            for kt, slug in m.items():
                r = cross(slug, kt, "cross:soccer")
                r.update(expected_settlement_utc=(g["start"] + GAME_SETTLE).isoformat(),
                         live_from_utc=g["start"].isoformat())
                rels.append(r)
                n_cross += 1
        wdays = {d["event"]: d for d in days}
        for kt, slug in pair_weather(kalshi_events, days).items():
            d = wdays[slug.rsplit("-", 1)[0]]
            r = cross(slug, kt, "cross:weather")
            r.update(expected_settlement_utc=(d["day"] + pd.Timedelta(days=1, hours=WEATHER_SETTLE_UTC_H)).tz_localize("UTC").isoformat(),
                     live_from_utc=(d["day"] + pd.Timedelta(hours=WEATHER_LIVE_UTC_H)).tz_localize("UTC").isoformat())
            rels.append(r)
            n_cross += 1
    for r in rels:
        r.update(quote_until_utc=None, resume_quoting_utc=None, volume_24h=None)
    tickers = {l["ticker"] for r in rels for b in r["baskets"] for l in b["legs"]}
    return {"generated_at_utc": now.isoformat(), "source": "strategies.ARBS.pm_scan",
            "n_events": len({r["relationship_id"].split(":", 1)[1] for r in rels}), "n_markets": len(tickers),
            "n_cross": n_cross, "relationships": rels}


def select(doc: dict, *, now: pd.Timestamp, days: float, max_markets: int, held: set[str],
           exclude: set[str]) -> dict:
    """Soonest first (no volume to rank by), within `days`, until max_markets;
    relationships holding a basket in `held` always."""
    horizon = now + pd.Timedelta(days=days)
    out, used = [], set()

    def soon(r):
        s = r.get("expected_settlement_utc")
        return pd.Timestamp(s) if s else pd.Timestamp.max.tz_localize("UTC")

    for r in sorted(doc["relationships"], key=soon):
        ts = {l["ticker"] for b in r["baskets"] for l in b["legs"]}
        if any(b["basket_id"] in held for b in r["baskets"]):
            out.append(r)
            used |= ts
            continue
        if soon(r) > horizon or soon(r) < now - pd.Timedelta(hours=6) or r.get("cluster") in exclude \
                or r["relationship_id"].split(":", 1)[-1] in exclude or r.get("type") in exclude:
            continue
        if len(used | ts) <= max_markets:
            out.append(r)
            used |= ts
    return {**doc, "relationships": out}


def scan_and_select(root: str, cfg, held: set[str]) -> dict:
    from strategies.ARBS.scanner import SELECTED, fetch_events
    now = pd.Timestamp.now(tz="UTC")
    pm = list_open()
    doc = scan(pm, fetch_events(), now=now)
    path = os.path.join(root, "scanned_spec.json")
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)["relationships"]
    except (OSError, ValueError, KeyError):
        prev = []
    have = {r["relationship_id"] for r in doc["relationships"]}
    doc["relationships"] += [r for r in prev if r["relationship_id"] not in have
                             and any(b["basket_id"] in held for b in r["baskets"])]
    atomic_write_json(path, doc, indent=None)
    sel = select(doc, now=now, days=cfg.trading.days_till_expiry, max_markets=cfg.scan.max_markets, held=held,
                 exclude=set(cfg.watch.exclude))
    atomic_write_json(os.path.join(root, SELECTED), sel, indent=None)
    return {"listed_markets": doc["n_markets"], "cross_pairs": doc["n_cross"],
            "selected_relationships": len(sel["relationships"])}
