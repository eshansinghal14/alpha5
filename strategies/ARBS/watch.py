"""Baskets on structures Kalshi doesn't net, traded only with
trading.quote_unnetted (recorded otherwise).
Each pays at least its payout whatever happens, from the markets' own rules:

  same      sports markets whose rules read the same but for the strike measure
            one quantity (a team's points, a player's yards, a game's total),
            across events too (TOTAL vs TOTALPRECISE): a higher "over" implies
            a lower one; equal strikes are the same outcome.
  deadline  "Before <date>" markets of one event: an earlier deadline implies
            every later one.
  margin    a spread "wins by over k-0.5" is exactly the margin buckets from
            k up (WINMARGIN, overtime included on both).
  winner    covering a positive spread means winning, so the other team didn't.
  sum       halves (2H with overtime) and team totals add up to the game total:
            NO on both parts + YES total, or the reverse, at strikes integer
            scores can't fall between.
  props     points, rebounds or assists <= PRA; 3 x threes <= points; scoring
            the first touchdown means scoring one.

Every basket's id is WATCH_PREFIX + its anchor event + kind, so parts.event_of
gives the anchor and a split shard's owner of that event records it.
"""
from __future__ import annotations

import math
import re

WATCH_PREFIX = "watch:"
UP = ("greater", "greater_or_equal")
DOWN = ("less", "less_or_equal")
SPAN = 3
# Derivative markets are watch-only by rule (11-17) and would only take scan budget.
SKIP_CATEGORIES = ("Financials", "Crypto", "Commodities")
SUM_NEAR = 3                  # each part's strikes nearest an even price, for the sum baskets
DEADLINE = re.compile(r"^\s*(before|by|on or before|prior to)\b", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mid(m: dict) -> float:
    b, a = _f(m.get("yes_bid_dollars")) or 0.0, _f(m.get("yes_ask_dollars")) or 0.0
    return (b + a) / 2 if b > 0 and a > 0 else (b or a or 0.5)


def _sfx(et: str, ticker: str) -> str:
    return ticker[len(et) + 1:] if ticker.startswith(et + "-") else ticker


def _legs(*pairs) -> list[dict]:
    return [{"ticker": t, "side": s} for t, s in pairs]


def _active(e: dict) -> list[dict]:
    return [m for m in e.get("markets") or []
            if m.get("status") == "active" and m.get("market_type", "binary") == "binary"]


def _game(et: str) -> str:
    """An event ticker less its series: the game (26NOV21BOSSAC)."""
    return et.split("-", 1)[1] if "-" in et else ""


def same_key(m: dict) -> tuple | None:
    """What the market measures: its rules (both parts) with the strike taken
    out, its direction and day. None if the strike can't be found in the rules
    (no way to tell it apart from another quantity)."""
    t = m.get("strike_type")
    k = _f(m.get("floor_strike") if t in UP else m.get("cap_strike") if t in DOWN else None)
    if k is None or not m.get("rules_primary"):
        return None
    # The secondary rules too: a PROT prop reads like its plain twin in the
    # primary, but a player hurt in the 1st half settles it at the pre-game
    # fair price while the plain one pays on stats (11-20 dump).
    rules = m["rules_primary"] + "\n" + (m.get("rules_secondary") or "")
    hit = [False]

    def sub(x):
        v = _f(x.group(0).replace(",", ""))
        if v is not None and min(abs(v - k), abs(v - k - 0.5), abs(v - k + 0.5)) < 1e-9:
            hit[0] = True
            return "N"
        return x.group(0)
    text = _NUM.sub(sub, rules)
    if not hit[0]:
        return None
    day = (m.get("occurrence_datetime") or m.get("expected_expiration_time") or m.get("close_time") or "")[:10]
    return text, "up" if t in UP else "down", day


def _strike(m: dict) -> float:
    return float(m["floor_strike"] if m.get("strike_type") in UP else m["cap_strike"])


def same(events: list[dict], recorded=lambda e: False) -> list[tuple[str, str, list[dict]]]:
    """(anchor event, kind, baskets) for same-quantity ladders. Netted events'
    ladders are traded already (DIRECNET); a group wholly inside one event
    `recorded` lists already (scanner.spreads) is left to it."""
    groups: dict[tuple, list[tuple[float, str, str]]] = {}
    rec = {e["event_ticker"] for e in events if recorded(e)}
    for e in events:
        if e.get("collateral_return_type") or e.get("category") != "Sports":
            continue
        for m in _active(e):
            k = same_key(m)
            if k is not None:
                groups.setdefault(k, []).append((_strike(m), m["ticker"], e["event_ticker"]))
    out: dict[str, list[dict]] = {}
    for (_, way, _), g in groups.items():
        if len(g) < 2 or (len({x[2] for x in g}) == 1 and g[0][2] in rec):
            continue
        sign = 1.0 if way == "up" else -1.0
        g.sort(key=lambda x: (sign * x[0], x[1]))
        anchor = g[0][2]
        for i, (ka, a, _) in enumerate(g):
            for kb, b, _ in g[i + 1:i + 1 + SPAN]:
                if abs(ka - kb) < 1e-9:
                    # Same strike in two events: the same outcome, either way round.
                    for x, y in ((a, b), (b, a)):
                        out.setdefault(anchor, []).append(
                            {"basket_id": f"{WATCH_PREFIX}{anchor}:same:{x}=>{y}", "payout": 1,
                             "legs": _legs((x, "yes"), (y, "no"))})
                else:
                    out.setdefault(anchor, []).append(
                        {"basket_id": f"{WATCH_PREFIX}{anchor}:same:{b}=>{a}", "payout": 1,
                         "legs": _legs((a, "yes"), (b, "no"))})
    return [(a, "same", bs) for a, bs in out.items()]


def deadlines(events: list[dict]) -> list[tuple[str, str, list[dict]]]:
    out = []
    for e in events:
        if e.get("collateral_return_type"):
            continue
        ms = [m for m in _active(e) if DEADLINE.match(m.get("yes_sub_title") or "")
              and (m.get("close_time") or m.get("expected_expiration_time"))]
        if len(ms) < 2:
            continue
        ms.sort(key=lambda m: m.get("expected_expiration_time") or m.get("close_time"))
        et = e["event_ticker"]
        bs = []
        for i, early in enumerate(ms):
            for late in ms[i + 1:i + 1 + SPAN]:
                bs.append({"basket_id": f"{WATCH_PREFIX}{et}:deadline:{_sfx(et, early['ticker'])}=>{_sfx(et, late['ticker'])}",
                           "payout": 1, "legs": _legs((late["ticker"], "yes"), (early["ticker"], "no"))})
        out.append((et, "deadline", bs))
    return out


def _scope(m: dict) -> str:
    """The rules' "originally scheduled ..." clause, normalized: when the game is
    and what part of it counts (90 minutes + stoppage, overtime). Markets paired
    across events must agree on it."""
    t = (m.get("rules_primary") or "").lower()
    i = t.find("originally scheduled")
    return re.sub(r"[^a-z0-9]", "", t[i:]) if i >= 0 else ""


def _same_scope(*ms: dict) -> bool:
    sc = {_scope(m) for m in ms}
    return len(sc) == 1 and "" not in sc


def _team(m: dict) -> str:
    """The team a spread/margin/moneyline market is about, from its subtitle."""
    t = (m.get("yes_sub_title") or "").strip()
    return re.split(r"\s+(?:wins\b|by\b|over\b)", t, maxsplit=1)[0].strip().lower()


_BUCKET = re.compile(r"([A-Z]+?)(\d+)(PLUS|TO(\d+))$")


def margins(by_et: dict[str, dict]) -> list[tuple[str, str, list[dict]]]:
    out = []
    for et, e in by_et.items():
        s = e.get("series_ticker") or ""
        if not s.endswith("WINMARGIN"):
            continue
        sp = by_et.get(s[:-len("WINMARGIN")] + "SPREAD-" + _game(et))
        if sp is None:
            continue
        buckets: dict[str, list[tuple[int, float, dict]]] = {}
        for m in _active(e):
            x = _BUCKET.match(_sfx(et, m["ticker"]))
            if x:
                hi = math.inf if x.group(3) == "PLUS" else int(x.group(4))
                buckets.setdefault(_team(m), []).append((int(x.group(2)), hi, m))
        bs = []
        for m in _active(sp):
            if m.get("strike_type") not in UP or _f(m.get("floor_strike")) is None:
                continue
            k = _f(m["floor_strike"]) + 0.5
            team = buckets.get(_team(m))
            if not team or abs(k - round(k)) > 1e-9:
                continue
            team.sort(key=lambda x: x[0])
            up = [x for x in team if x[0] >= k]
            # The buckets from k must start at k, run without gaps and end open.
            if (not up or up[0][0] != k or up[-1][1] != math.inf
                    or any(up[i + 1][0] != up[i][1] + 1 for i in range(len(up) - 1))):
                continue
            if not _same_scope(m, *(x[2] for x in up)):
                continue
            ts = [x[2]["ticker"] for x in up]
            sid = _sfx(sp["event_ticker"], m["ticker"])
            bs.append({"basket_id": f"{WATCH_PREFIX}{sp['event_ticker']}:margin:no:{sid}", "payout": 1,
                       "legs": _legs((m["ticker"], "no"), *((t, "yes") for t in ts))})
            bs.append({"basket_id": f"{WATCH_PREFIX}{sp['event_ticker']}:margin:yes:{sid}", "payout": len(ts),
                       "legs": _legs((m["ticker"], "yes"), *((t, "no") for t in ts))})
        if bs:
            out.append((sp["event_ticker"], "margin", bs))
    return out


def winners(by_et: dict[str, dict]) -> list[tuple[str, str, list[dict]]]:
    out = []
    for et, e in by_et.items():
        s = e.get("series_ticker") or ""
        if not s.endswith("SPREAD") or e.get("collateral_return_type"):
            continue
        g = by_et.get(s[:-len("SPREAD")] + "GAME-" + _game(et))
        if g is None:
            continue
        teams = {_team(m): m for m in _active(g)}
        if len(teams) < 2:
            continue
        bs = []
        for m in _active(e):
            if m.get("strike_type") not in UP or (_f(m.get("floor_strike")) or 0) <= 0:
                continue
            win = teams.get(_team(m))
            if win is None or not _same_scope(m, win):
                continue
            sid = _sfx(et, m["ticker"])
            bs.append({"basket_id": f"{WATCH_PREFIX}{et}:winner:{sid}", "payout": 1,
                       "legs": _legs((win["ticker"], "yes"), (m["ticker"], "no"))})
            for name, other in teams.items():
                if other is not win and name not in ("tie", "draw") and _same_scope(m, other):
                    bs.append({"basket_id": f"{WATCH_PREFIX}{et}:winner:{sid}+{_sfx(g['event_ticker'], other['ticker'])}",
                               "payout": 1, "legs": _legs((m["ticker"], "no"), (other["ticker"], "no"))})
        if bs:
            out.append((et, "winner", bs))
    return out


def _near(ms: list[dict], n: int = SUM_NEAR) -> list[dict]:
    return sorted(ms, key=lambda m: abs(_mid(m) - 0.5))[:n]


def _sum_baskets(anchor: str, tag: str, a_ms, b_ms, tot_ms) -> list[dict]:
    """Integer scores: A > a means A >= floor(a) + 1. NO A>a, NO B>b, YES T>c
    pays >= 1 when c < floor(a) + floor(b) + 2; YES, YES, NO T>c when
    c >= floor(a) + floor(b)."""
    tots = sorted((_f(m["floor_strike"]), m["ticker"]) for m in tot_ms)
    by_t = {m["ticker"]: m for m in tot_ms}
    bs = []
    for ma in _near(a_ms):
        for mb in _near(b_ms):
            fa, fb = math.floor(_f(ma["floor_strike"])), math.floor(_f(mb["floor_strike"]))
            lo = [x for x in tots if x[0] < fa + fb + 2]
            hi = [x for x in tots if x[0] >= fa + fb]
            ids = f"{ma['ticker'].rsplit('-', 1)[-1]}+{mb['ticker'].rsplit('-', 1)[-1]}"
            scope_ok = lambda t: _same_scope(ma, mb, by_t[t])  # noqa: E731
            if lo and scope_ok(lo[-1][1]):
                t = lo[-1][1]
                bs.append({"basket_id": f"{WATCH_PREFIX}{anchor}:sum{tag}:no:{ids}>{t.rsplit('-', 1)[-1]}",
                           "payout": 1, "legs": _legs((ma["ticker"], "no"), (mb["ticker"], "no"), (t, "yes"))})
            if hi and scope_ok(hi[0][1]):
                t = hi[0][1]
                bs.append({"basket_id": f"{WATCH_PREFIX}{anchor}:sum{tag}:yes:{ids}>{t.rsplit('-', 1)[-1]}",
                           "payout": 1, "legs": _legs((ma["ticker"], "yes"), (mb["ticker"], "yes"), (t, "no"))})
    return bs


def _ups(e: dict | None) -> list[dict]:
    return [m for m in _active(e or {}) if m.get("strike_type") in UP and _f(m.get("floor_strike")) is not None]


def sums(by_et: dict[str, dict]) -> list[tuple[str, str, list[dict]]]:
    out = []
    for et, e in by_et.items():
        s = e.get("series_ticker") or ""
        if not s.endswith("TOTAL") or s.endswith(("TEAMTOTAL", "1HTOTAL", "2HTOTAL")) or e.get("collateral_return_type"):
            continue
        lg, game = s[:-len("TOTAL")], _game(et)
        tot = _ups(e)
        if not tot:
            continue
        bs = []
        h1, h2 = by_et.get(f"{lg}1HTOTAL-{game}"), by_et.get(f"{lg}2HTOTAL-{game}")
        # The halves add up to the game only if the 2nd half counts overtime.
        if h1 and h2 and all("including overtime" in (m.get("rules_primary") or "") for m in _ups(h2)):
            bs += _sum_baskets(et, "h", _ups(h1), _ups(h2), tot)
        tt = by_et.get(f"{lg}TEAMTOTAL-{game}")
        if tt:
            teams: dict[str, list[dict]] = {}
            for m in _ups(tt):
                teams.setdefault(_team(m), []).append(m)
            if len(teams) == 2:
                a, b = teams.values()
                bs += _sum_baskets(et, "t", a, b, tot)
        if bs:
            out.append((et, "sum", bs))
    return out


_PROPS = (("PTS", "PRA", 1), ("REB", "PRA", 1), ("AST", "PRA", 1), ("3PT", "PTS", 3))


def _player(et: str, m: dict) -> tuple[str, str]:
    return _sfx(et, m["ticker"]).rsplit("-", 1)[0], (m.get("yes_sub_title") or "").split(":")[0].strip().lower()


def props(by_et: dict[str, dict]) -> list[tuple[str, str, list[dict]]]:
    out = []
    for et, e in by_et.items():
        s = e.get("series_ticker") or ""
        game = _game(et)
        bs = []
        for lo, hi, mult in _PROPS:
            if not s.endswith(lo) or s.endswith("PROT"):
                continue
            y = by_et.get(s[:-len(lo)] + hi + "-" + game)
            if y is None:
                continue
            ys: dict[tuple, list[dict]] = {}
            for m in _ups(y):
                ys.setdefault(_player(y["event_ticker"], m), []).append(m)
            for m in _ups(e):
                cands = ys.get(_player(et, m))
                if not cands:
                    continue
                least = mult * (math.floor(_f(m["floor_strike"])) + 1)      # what X > x guarantees of Y
                ok = [c for c in cands if _f(c["floor_strike"]) < least]
                if not ok:
                    continue
                c = max(ok, key=lambda c: _f(c["floor_strike"]))
                if not _same_scope(m, c):
                    continue
                bs.append({"basket_id": f"{WATCH_PREFIX}{et}:props:{_sfx(et, m['ticker'])}=>{hi}{c['floor_strike']}",
                           "payout": 1, "legs": _legs((c["ticker"], "yes"), (m["ticker"], "no"))})
        if s.endswith("FIRSTTD") and not s.endswith("TEAMFIRSTTD"):
            td = by_et.get(s[:-len("FIRSTTD")] + "TD-" + game)
            if td is not None:
                ones = {_player(td["event_ticker"], m)[0]: m for m in _ups(td) if abs(_f(m["floor_strike"]) - 0.5) < 1e-9}
                for m in _active(e):
                    t = ones.get(_sfx(et, m["ticker"]))
                    if (t is not None and _player(td["event_ticker"], t)[1] == (m.get("yes_sub_title") or "").strip().lower()
                            and _same_scope(m, t)):
                        bs.append({"basket_id": f"{WATCH_PREFIX}{et}:props:{_sfx(et, m['ticker'])}=>TD",
                                   "payout": 1, "legs": _legs((t["ticker"], "yes"), (m["ticker"], "no"))})
        if bs:
            out.append((et, "props", bs))
    return out


def build(events: list[dict], shards, skip_series=frozenset(), recorded=lambda e: False) -> list[tuple[str, dict, list[str]]]:
    """(anchor event, relationship, events its legs are in) for every family,
    over the events on `shards` (a basket's legs share one shard). `recorded`:
    events scanner.spreads lists already."""
    evs = [e for e in events if e.get("exchange_index") in shards and e.get("category") not in SKIP_CATEGORIES
           and e.get("series_ticker") not in skip_series]
    by_et = {e["event_ticker"]: e for e in evs}
    ev_of = {m["ticker"]: e["event_ticker"] for e in evs for m in e.get("markets") or []}
    shard_of = {m["ticker"]: e.get("exchange_index") for e in evs for m in e.get("markets") or []}
    out = []
    for anchor, kind, bs in same(evs, recorded) + deadlines(evs) + margins(by_et) + winners(by_et) + sums(by_et) + props(by_et):
        bs = [b for b in bs if len({shard_of.get(l["ticker"]) for l in b["legs"]}) == 1]
        if not bs:
            continue
        evs_in = sorted({ev_of[l["ticker"]] for b in bs for l in b["legs"]})
        out.append((anchor, {"relationship_id": f"{WATCH_PREFIX}{anchor}:{kind}", "type": "implication",
                             "cluster": by_et[anchor]["series_ticker"],
                             "description": f"{anchor}: {kind} (not netted by Kalshi).",
                             "baskets": bs}, evs_in))
    return out
