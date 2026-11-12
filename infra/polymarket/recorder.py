"""Record every open Polymarket US market's book and trades, plus the market
metadata needed to match them to Kalshi markets (rules text, times, outcomes).

For the cross-exchange study: whether ARBS's broken-basket exits, or arbs on
Polymarket itself, would have been cheaper with Polymarket's books.

    python -m infra.polymarket.recorder

Writes under data/polymarket_us/:
  books/{day}/{ms}.parquet   recv_ns, slug, snap, chg, exch_ns. chg is the levels that
                             changed, JSON [["b"|"a", px, qty], ...], qty 0 = level gone
                             (prices are YES; "a" = offers). snap rows list every level:
                             a market's first message, and every book at the first
                             flush of each UTC day, so a day replays on its own
  trades/{day}/{ms}.parquet  recv_ns, slug, px, qty, exch_ns, taker_side,
                             taker_intent, taker_outcome, id
  meta/{day}/{ms}.jsonl.gz   one slim record per market, written when it is new
                             or changed, and in full at the first refresh of each day
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

import pandas as pd
import websockets

from infra import DATA_ROOT
from infra.polymarket import GATEWAY, WS, auth

ROOT = os.path.join(DATA_ROOT, "polymarket_us")
WS_PATH = "/v1/ws/markets"
FLUSH_S = 60.0
REFRESH_S = 600.0
STATS_S = 300.0
# The server rejects an 11th subscription on a connection ("max subscriptions
# per connection reached", 2026-11-12); one subscription took 2,000+ slugs.
# Each slug group costs two: market data and trades.
MAX_SUBS = 10
MAX_SLUGS_PER_CONN = 5000
EVENTS_PAGE = 100


def _get(path: str, query: dict) -> dict:
    url = f"{GATEWAY}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "alpha5-recorder"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _slim(ev: dict, m: dict) -> dict:
    sides = [{"id": s.get("identifier"), "desc": s.get("description"), "long": s.get("long"),
              "team": (s.get("team") or {}).get("name"), "abbr": (s.get("team") or {}).get("abbreviation"),
              "league": (s.get("team") or {}).get("league")} for s in m.get("marketSides") or []]
    return {"slug": m["slug"], "event": ev.get("slug"), "event_title": ev.get("title"),
            "series": ev.get("seriesSlug"), "category": m.get("category"),
            "subcategory": m.get("eventSubcategory"), "question": m.get("question"),
            "title": m.get("title"), "subtitle": m.get("subtitle"), "rules": m.get("description"),
            "outcomes": m.get("outcomes"), "sides": sides, "market_type": m.get("marketType"),
            "sports_type": m.get("sportsMarketTypeV2"), "asset_terms": m.get("assetPriceTerms"),
            "event_start": ev.get("startTime"), "game_start": m.get("gameStartTime"),
            "end": m.get("endDate"), "tick": m.get("orderPriceMinTickSize"),
            "fee_coef": m.get("feeCoefficient"), "status": m.get("status"),
            "updated": m.get("updatedAt")}


def list_open() -> dict[str, dict]:
    """slug -> slim record for every open market. Paged by event, so a page's
    raw JSON (images, team data) is dropped before the next; the full listing
    is ~120 MB raw."""
    out: dict[str, dict] = {}
    off = 0
    while True:
        evs = _get("/v1/events", {"closed": "false", "active": "true",
                                  "limit": EVENTS_PAGE, "offset": off})["events"]
        if not evs:
            return out
        off += len(evs)
        for ev in evs:
            for m in ev.get("markets") or []:
                if m.get("status") == "MARKET_STATUS_OPEN" and not m.get("closed"):
                    out[m["slug"]] = _slim(ev, m)


def _px(a: dict | None) -> float | None:
    return float(a["value"]) if a and a.get("value") is not None else None


def _levels(d: dict) -> dict[tuple[str, float], float]:
    out = {("b", float(x["px"]["value"])): float(x["qty"]) for x in d.get("bids") or []}
    out.update({("a", float(x["px"]["value"])): float(x["qty"]) for x in d.get("offers") or []})
    return out


def _chg(old: dict | None, new: dict) -> list:
    if old is None:
        return [[k[0], k[1], q] for k, q in new.items()]
    return ([[k[0], k[1], q] for k, q in new.items() if old.get(k) != q]
            + [[k[0], k[1], 0.0] for k in old if k not in new])


def _ns(ts: str | None) -> int | None:
    return pd.Timestamp(ts).value if ts else None


class Recorder:
    def __init__(self):
        self.books: list[dict] = []
        self.trades: list[dict] = []
        self.last: dict[str, dict[tuple[str, float], float]] = {}
        self.book_day = time.strftime("%Y-%m-%d", time.gmtime())
        self.meta_hash: dict[str, str] = {}
        self.meta_day = ""
        self.conns: list[list[str]] = []
        self.conn_subs: list[int] = []
        self.wakeups: list[asyncio.Queue] = []
        self.n = {"book_msgs": 0, "book_rows": 0, "trades": 0, "errors": 0, "reconnects": 0}

    def on_message(self, raw: str | bytes, recv: int):
        m = json.loads(raw)
        if (d := m.get("marketData")) is not None:
            self.n["book_msgs"] += 1
            slug = d["marketSlug"]
            new = _levels(d)
            old = self.last.get(slug)
            # Every message is the whole book, resent on stats changes too (a
            # trade, open interest); full books were ~3.2 GB/day, changes ~1.5.
            chg = _chg(old, new)
            if not chg:
                return
            self.last[slug] = new
            self.n["book_rows"] += 1
            self.books.append({"recv_ns": recv, "slug": slug, "snap": old is None,
                               "chg": json.dumps(chg, separators=(",", ":")), "exch_ns": _ns(d.get("transactTime"))})
        elif (t := m.get("trade")) is not None:
            self.n["trades"] += 1
            taker = t.get("taker") or {}
            self.trades.append({"recv_ns": recv, "slug": t.get("marketSlug"), "px": _px(t.get("price")),
                                "qty": _px(t.get("quantity")), "exch_ns": _ns(t.get("tradeTime")),
                                "taker_side": taker.get("side"), "taker_intent": taker.get("intent"),
                                "taker_outcome": taker.get("outcomeSide"), "id": t.get("id")})
        elif "error" in m:
            self.n["errors"] += 1
            print(f"[pm_rec] error {m.get('requestId')}: {m['error']}", flush=True)

    async def conn_loop(self, i: int):
        while True:
            try:
                async with websockets.connect(WS + WS_PATH, additional_headers=auth.headers("GET", WS_PATH),
                                              max_size=None, ping_interval=20, open_timeout=30) as ws:
                    # A reconnect folds this connection's groups into one pair.
                    self.conn_subs[i] = 0
                    sent = 0

                    async def subscribe_new():
                        nonlocal sent
                        slugs = self.conns[i][sent:]
                        if not slugs:
                            return
                        for typ in ("SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"):
                            await ws.send(json.dumps({"subscribe": {
                                "requestId": f"{typ[18:].lower()}-{i}-{sent}", "subscriptionType": typ,
                                "marketSlugs": slugs}}))
                        sent += len(slugs)
                        self.conn_subs[i] += 2

                    async def on_wakeup():
                        while True:
                            await self.wakeups[i].get()
                            await subscribe_new()

                    await subscribe_new()
                    sub_task = asyncio.create_task(on_wakeup())
                    try:
                        async for raw in ws:
                            self.on_message(raw, time.time_ns())
                    finally:
                        sub_task.cancel()
                    raise ConnectionError("closed by server")
            except Exception as e:
                self.n["reconnects"] += 1
                print(f"[pm_rec] conn {i}: {e!r}; reconnecting", flush=True)
                await asyncio.sleep(5.0)

    def add_slugs(self, slugs: list[str]):
        """Put new slugs on the last connection while it has subscriptions and
        slug room left, else on a new one."""
        if not slugs:
            return
        i = len(self.conns) - 1
        if i < 0 or self.conn_subs[i] + 2 > MAX_SUBS or len(self.conns[i]) + len(slugs) > MAX_SLUGS_PER_CONN:
            for k in range(0, len(slugs), MAX_SLUGS_PER_CONN):
                self.conns.append(list(slugs[k:k + MAX_SLUGS_PER_CONN]))
                self.conn_subs.append(0)
                self.wakeups.append(asyncio.Queue())
                asyncio.get_running_loop().create_task(self.conn_loop(len(self.conns) - 1))
            return
        self.conns[i].extend(slugs)
        self.wakeups[i].put_nowait(True)

    def write_meta(self, markets: dict[str, dict]):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        full = day != self.meta_day
        rows = []
        for slug, rec in markets.items():
            h = hashlib.sha1(json.dumps(rec, sort_keys=True).encode()).hexdigest()
            if full or self.meta_hash.get(slug) != h:
                rows.append(rec)
            self.meta_hash[slug] = h
        self.meta_day = day
        if rows:
            d = os.path.join(ROOT, "meta", day)
            os.makedirs(d, exist_ok=True)
            with gzip.open(os.path.join(d, f"{int(time.time() * 1000)}.jsonl.gz"), "wt", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")

    async def refresh_loop(self):
        while True:
            try:
                markets = await asyncio.to_thread(list_open)
                self.write_meta(markets)
                have = {s for c in self.conns for s in c}
                new = [s for s in markets if s not in have]
                self.add_slugs(new)
                print(f"[pm_rec] {len(markets)} open, {len(new)} new, {len(self.conns)} conns", flush=True)
            except Exception as e:
                print(f"[pm_rec] refresh: {e!r}", flush=True)
            await asyncio.sleep(REFRESH_S)

    @staticmethod
    def write(kind: str, rows: list[dict]):
        if not rows:
            return
        df = pd.DataFrame(rows)
        ms = int(time.time() * 1000)
        for day, part in df.groupby(pd.to_datetime(df.recv_ns, unit="ns", utc=True).dt.strftime("%Y-%m-%d")):
            d = os.path.join(ROOT, kind, day)
            os.makedirs(d, exist_ok=True)
            part.to_parquet(os.path.join(d, f"{ms}.parquet"), compression="zstd", index=False)

    async def flush_loop(self):
        last_stats = time.monotonic()
        while True:
            await asyncio.sleep(FLUSH_S)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self.book_day:
                now = time.time_ns()
                self.books.extend({"recv_ns": now, "slug": slug, "snap": True, "chg": json.dumps(_chg(None, lv)),
                                   "exch_ns": None} for slug, lv in self.last.items())
                self.book_day = day
            books, trades = self.books, self.trades
            self.books, self.trades = [], []
            try:
                await asyncio.to_thread(self.write, "books", books)
                await asyncio.to_thread(self.write, "trades", trades)
            except Exception as e:
                print(f"[pm_rec] flush: {e!r}", flush=True)
            if time.monotonic() - last_stats >= STATS_S:
                print(f"[pm_rec] {self.n} markets_seen={len(self.last)}", flush=True)
                last_stats = time.monotonic()

    async def run(self):
        await asyncio.gather(self.refresh_loop(), self.flush_loop())


if __name__ == "__main__":
    try:
        asyncio.run(Recorder().run())
    except KeyboardInterrupt:
        pass
