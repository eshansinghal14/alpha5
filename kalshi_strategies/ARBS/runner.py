"""ARBS live loop: baskets from the spec across ~70 events, one process.

Per pass over baskets whose books changed:
  1. a broken basket (legs uneven, after a quote fill or a partial take) is
     completed: IOCs on the missing legs, each limited by its cap_j
     (pricing.completion), retried every completion.retry_s;
  2. a whole-basket take if (payout - sum(ask + fee)) / sum(ask + fee) >= min_edge
     (a return on the bundle's cost, see edge_dollars);
  3. one resting quote per hot basket (top watch.max_quoted_baskets by that
     return), priced so a fill completed at today's asks still returns
     min_edge + quote_edge_add.

Shadow (default) runs the same decisions against the live book: takes fill
at the levels they would cross, quotes fill on a print at or through their
price (no queue, so optimistic). It has its own ledger file.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import os
import time
import traceback
import uuid

import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import ConfigStore, atomic_write_json
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.kalshi.orders import RealFill, error_code, http_status, is_market_paused, is_post_only_reject
from kalshi_infra.runtime.control import StopWatcher, TradingModeWatcher, resumable_arm
from kalshi_infra.runtime.persist import LIVE_ROOT, day_dir
from kalshi_strategies.ARBS import pricing, scanner
from kalshi_strategies.ARBS.config import SPEC_PATH
from kalshi_strategies.ARBS.execution import Gateway
from kalshi_strategies.ARBS.feed import MultiFeed
from kalshi_strategies.ARBS.ledger import Ledger
from kalshi_strategies.ARBS.liquidity import Liquidity
from kalshi_strategies.ARBS.spec import Basket, load_spec, merge, parse_spec

_EPS = 1e-9
TRADEABLE = ("active", "open")
SETTLED = ("determined", "settled", "finalized")
SUMMARY_S = 1.0
MAX_ALERTS = 300
# A quote whose create got no response may still rest at Kalshi; look for it
# by client id after this long.
LOST_LOOKUP_S = 1.0
ORPHAN_FILL_S = 30.0


def _log(msg: str):
    print(f"[ARBS] {msg}", flush=True)


def _utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


@dataclasses.dataclass
class Quote:
    basket_id: str
    leg: int
    ticker: str
    side: str
    price: float
    size: float
    cid: str
    edge: float
    max_price: float
    filled: float = 0.0
    exchange_id: str | None = None
    placed_at: float = 0.0
    moved_at: float = 0.0
    pending: bool = True        # create in flight

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def key(self) -> tuple[str, int]:
        return self.basket_id, self.leg


@dataclasses.dataclass
class Meta:
    ticker: str
    event: str
    status: str = "unknown"
    result: str = ""
    series: str | None = None
    shard: int | None = None
    taker_rate: float | None = None
    maker_rate: float | None = None
    ticks: pricing.Ticks = pricing.CENT


class ArbsRunner:
    def __init__(self, series, *, live_trading: bool):
        self.S = series.ticker
        self.store = ConfigStore(series.config_cls, series.config_path, series.config_key)
        self.cfg = self.store.config
        self.root = os.path.join(LIVE_ROOT, self.S)
        os.makedirs(self.root, exist_ok=True)
        self.meta: dict[str, Meta] = {}
        self.real_ledger = Ledger(os.path.join(self.root, "ledger.json"))
        self.shadow_ledger = Ledger(os.path.join(self.root, "ledger_shadow.json"))
        self.alerts: list[dict] = []
        self.live = False
        self.journal_dir = os.path.join(self.root, "journal")
        self.quotes: dict[tuple[str, int], Quote] = {}   # (basket_id, leg index) -> its quote
        self.busy: set[str] = set()
        self.baskets: dict[str, Basket] = {}
        self.tickers: list[str] = []
        self.dirty: set[str] = set()
        self.hot: list[str] = []
        self.ranking: list[dict] = []
        with open(SPEC_PATH, encoding="utf-8") as fh:
            self.hand_doc = json.load(fh)
        self.hand = load_spec(SPEC_PATH)
        self.scan_path = os.path.join(self.root, "scanned_spec.json")
        self.scan_status: dict = {}
        self.set_baskets(merge(self.hand, self.scanned()))
        self.arm_want = live_trading
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="arbs")
        self.gw = Gateway(self.pool)
        self.feed = MultiFeed(self.tickers, self._on_quote, self._on_trade, label=self.S,
                              on_fill=self._on_fill, on_error=lambda m: self.alert("warn", None, m))
        # (ticker, side) -> contracts our IOCs in flight are buying. Held from
        # the decision, not the send: baskets sharing a leg are evaluated in
        # one pass, and each sized against the same touch (10-28 16:24, the
        # TXSENR P2 and P4 ladders each came up one leg short).
        self.inflight: dict[tuple[str, str], float] = {}
        self.last_take: dict[str, float] = {}
        self.last_completion: dict[str, float] = {}
        self.completion_note: dict[str, dict] = {}
        self.order_errors = 0
        self.halt: str | None = None                     # until restart
        self.audit_strikes = 0
        self.audit_diff: dict | None = None
        self.orphans: list[tuple[float, dict]] = []
        self.tasks: set[asyncio.Task] = set()
        self.n = {"takes": 0, "take_bundles": 0.0, "quotes_placed": 0, "quote_moves": 0, "quote_cancels": 0,
                  "quote_fills": 0, "completions": 0, "completion_short": 0, "post_only_rejects": 0}
        self.started = _utc()
        self.liq = Liquidity(self.root)

    # ------------------------------------------------------------------ util

    @property
    def ledger(self) -> Ledger:
        return self.real_ledger if self.live else self.shadow_ledger

    def alert(self, level: str, basket_id: str | None, msg: str):
        row = {"time": _utc().isoformat(), "level": level, "basket_id": basket_id, "msg": msg,
               "live": self.live}
        self.alerts.append(row)
        del self.alerts[:-MAX_ALERTS]
        self.journal({"kind": "alert", **row})
        _log(f"{level.upper()} {basket_id or ''} {msg}")

    def journal(self, row: dict):
        os.makedirs(self.journal_dir, exist_ok=True)
        row = {"t": time.time(), "live": self.live, **row}
        with open(os.path.join(self.journal_dir, f"{_utc():%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def spawn(self, coro, basket_id: str | None = None):
        if basket_id is not None:
            self.busy.add(basket_id)

        async def wrap():
            try:
                await coro
            except Exception:
                _log(f"task error ({basket_id}):")
                traceback.print_exc()
                self.alert("error", basket_id, f"internal error: {traceback.format_exc(limit=2)[-300:]}")
            finally:
                if basket_id is not None:
                    self.busy.discard(basket_id)
                    self.dirty.add(basket_id)
        t = asyncio.get_running_loop().create_task(wrap())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def blocked(self) -> str | None:
        """Why no new takes or quotes may start (completions still run)."""
        if self.halt:
            return f"halted: {self.halt}"
        if self.order_errors >= self.cfg.runtime.max_order_errors:
            return "order_error_breaker"
        if not self.feed.up:
            return "feed_down"
        broken = sum(1 for b in self.ledger.baskets.values() if b.broken_since is not None)
        if broken >= self.cfg.completion.max_broken_baskets:
            return f"{broken} broken baskets"
        return None

    def _order_ok(self):
        self.order_errors = 0

    def _order_err(self, basket_id, what, exc):
        st, code = http_status(exc), error_code(exc)
        if st == 429:
            return
        if is_market_paused(exc):
            self.alert("warn", basket_id, f"{what}: market paused/closed ({code})")
            return
        self.order_errors += 1
        self.alert("error", basket_id, f"{what} failed: {st} {code} {str(exc)[:200]}")

    # ------------------------------------------------------------------ spec

    def scanned(self) -> list[Basket]:
        """Blocking (a full scan is ~25 MB). The part of the last scan to trade."""
        if not self.cfg.scan.enable or not os.path.exists(self.scan_path):
            return []
        try:
            with open(self.scan_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            return parse_spec(scanner.select(
                doc, now=_utc(), days=self.cfg.trading.days_till_expiry, max_markets=self.cfg.scan.max_markets,
                known_tickers={t for b in self.hand for t in b.tickers},
                held=set(self.real_ledger.baskets) | set(self.shadow_ledger.baskets),
                exclude=set(self.cfg.watch.exclude)))
        except Exception as e:
            self.alert("warn", None, f"scanned spec unreadable ({e!r}); hand spec only")
            return []

    def set_baskets(self, baskets: list[Basket]) -> list[str]:
        """Trade `baskets` from now on; returns the tickers new to us. A
        basket dropped from the list is kept while it has a position, quote
        or task: completion, settlement and cancels look it up by id."""
        new = {b.basket_id: b for b in baskets}
        keep = set(self.real_ledger.baskets) | set(self.shadow_ledger.baskets) | self.busy | \
            {q.basket_id for q in self.quotes.values()}
        for bid in keep:
            if bid not in new and bid in self.baskets:
                new[bid] = self.baskets[bid]
        added = new.keys() - self.baskets.keys()
        old = set(self.tickers)
        self.all_baskets = list(new.values())
        self.baskets = new
        self.tickers = sorted({t for b in self.all_baskets for t in b.tickers})
        self.flip = flips(self.all_baskets)
        self.by_ticker: dict[str, list[str]] = {}
        for b in self.all_baskets:
            for t in b.tickers:
                self.by_ticker.setdefault(t, []).append(b.basket_id)
        self.dirty = {d for d in self.dirty if d in new} | added
        self.hot = [h for h in self.hot if h in new]
        self.ranking = [r for r in self.ranking if r["basket_id"] in new]
        return [t for t in self.tickers if t not in old]

    async def reload_spec(self):
        loop = asyncio.get_running_loop()
        new = self.set_baskets(merge(self.hand, await loop.run_in_executor(self.pool, self.scanned)))
        if not new:
            return
        await loop.run_in_executor(self.pool, self.load_meta)
        shards = {t: m.shard for t, m in self.meta.items() if m.shard is not None}
        try:
            await loop.run_in_executor(self.pool, self.gw.setup, shards,
                                       lambda: self.cfg.runtime.max_write_tokens_per_s)
        except Exception as e:
            self.alert("error", None, f"order client setup failed ({e!r})")
        self.feed.add(new)
        _log(f"spec reloaded: {len(self.all_baskets)} baskets over {len(self.tickers)} markets (+{len(new)})")

    async def liquidity_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.cfg.runtime.liquidity_log_s)
            try:
                rows = self.liq.flush(time.monotonic(), time.time())
                await loop.run_in_executor(self.pool, self.liq.write, rows, f"{_utc():%Y-%m-%d}")
            except Exception as e:
                _log(f"WARN: liquidity log failed ({e!r})")

    async def scan_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            sc = self.cfg.scan
            try:
                if sc.enable:
                    t0 = time.monotonic()
                    held = set(self.real_ledger.baskets) | set(self.shadow_ledger.baskets)
                    doc = await loop.run_in_executor(self.pool, scanner.run, self.cfg, self.hand_doc,
                                                     self.scan_path, held)
                    await self.reload_spec()
                    self.scan_status = {"at": _utc().isoformat(), "secs": round(time.monotonic() - t0, 1),
                                        "listed_markets": doc["n_markets"],
                                        "baskets": len(self.all_baskets) - len(self.hand), "error": None}
                elif len(self.all_baskets) > len(self.hand):
                    await self.reload_spec()
            except Exception as e:
                self.scan_status["error"] = repr(e)
                self.alert("warn", None, f"scan failed ({e!r})")
            await asyncio.sleep(sc.interval_s)

    # ------------------------------------------------------------------ meta

    def load_meta(self):
        """Blocking. Market status/result for every leg; shard and fees per event."""
        got: dict[str, dict] = {}
        for i in range(0, len(self.tickers), 100):
            batch = self.tickers[i:i + 100]
            for m in kalshi_get("/trade-api/v2/markets", params={"tickers": ",".join(batch), "limit": 1000}) \
                    .get("markets", []):
                got[m["ticker"]] = m
        events = {}
        for t in self.tickers:
            m = got.get(t)
            if m is None:
                if t not in self.meta:
                    self.meta[t] = Meta(ticker=t, event="", status="missing")
                    self.alert("warn", None, f"{t}: not listed by Kalshi; its baskets are not traded")
                continue
            mt = self.meta.get(t) or Meta(ticker=t, event=m["event_ticker"])
            if mt.status != m.get("status") and mt.status != "unknown":
                self.journal({"kind": "status", "ticker": t, "from": mt.status, "to": m.get("status")})
            mt.status, mt.result = m.get("status") or "", m.get("result") or ""
            mt.ticks = pricing.Ticks.from_market(m.get("price_ranges"))
            self.meta[t] = mt
            events.setdefault(mt.event, []).append(mt)
        for et, ms in events.items():
            if ms[0].shard is not None:
                continue
            try:
                ev = kalshi_get(f"/trade-api/v2/events/{et}", auth=True)["event"]
                sched = kfees.for_event(ev["series_ticker"], et)
            except Exception as e:
                self.alert("warn", None, f"{et}: event/fee lookup failed ({e!r}); not traded")
                continue
            for mt in ms:
                mt.series, mt.shard = ev["series_ticker"], int(ev["exchange_index"])
                mt.taker_rate, mt.maker_rate = sched.taker_rate, sched.maker_rate

    async def meta_loop(self):
        while True:
            await asyncio.sleep(self.cfg.runtime.meta_refresh_s)
            try:
                await asyncio.get_running_loop().run_in_executor(self.pool, self.load_meta)
                self.dirty |= set(self.baskets)
                self.settle()
            except Exception as e:
                _log(f"WARN: meta refresh failed ({e!r})")

    def tradeable(self, b: Basket) -> bool:
        for t in b.tickers:
            m = self.meta.get(t)
            if m is None or m.status not in TRADEABLE or m.shard is None or m.taker_rate is None:
                return False
        return True

    def excluded(self, b: Basket) -> bool:
        if {b.basket_id, b.relationship_id, b.cluster} & set(self.cfg.watch.exclude):
            return True
        return b.settles is not None and b.settles > _utc() + pd.Timedelta(days=self.cfg.trading.days_till_expiry)

    # ------------------------------------------------------------------ books

    def _own(self, ticker: str, side: str) -> dict[float, float]:
        out: dict[float, float] = {}
        for q in self.quotes.values():
            if q.ticker == ticker and q.side == side and q.remaining > 0:
                out[round(q.price, 4)] = out.get(round(q.price, 4), 0.0) + q.remaining
        return out

    def leg_view(self, ticker: str, side: str) -> pricing.LegView | None:
        """None if the book can't be trusted (no snapshot yet, or crossed: a
        reconstruction drift, see L2Book)."""
        bk = self.feed.ladder(ticker)
        m = self.meta.get(ticker)
        if bk is None or m is None or m.taker_rate is None:
            return None
        if bk.last_snapshot_time["yes"] is None or bk.last_snapshot_time["no"] is None:
            return None
        yb, nb = dict(bk.yes_bids), dict(bk.no_bids)
        for s, book in (("yes", yb), ("no", nb)):
            for p, q in self._own(ticker, s).items():
                left = book.get(p, 0.0) - q
                if left > _EPS:
                    book[p] = left
                else:
                    book.pop(p, None)
        if yb and nb and max(yb) + max(nb) > 1.0 + _EPS:
            return None
        opp, mine = (nb, yb) if side == "yes" else (yb, nb)
        asks = sorted((round(1.0 - p, 4), q) for p, q in opp.items() if q > _EPS)
        held = self.inflight.get((ticker, side), 0.0)
        while held > _EPS and asks:
            p, q = asks[0]
            if q <= held + _EPS:
                held -= q
                asks.pop(0)
            else:
                asks[0], held = (p, q - held), 0.0
        return pricing.LegView(asks=asks, bid=max(mine) if mine else None,
                               taker_rate=m.taker_rate, maker_rate=m.maker_rate, ticks=m.ticks)

    def hold(self, legs: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
        for t, s, n in legs:
            self.inflight[(t, s)] = self.inflight.get((t, s), 0.0) + n
        return legs

    def release(self, legs: list[tuple[str, str, float]]):
        for t, s, n in legs:
            left = self.inflight.get((t, s), 0.0) - n
            if left > _EPS:
                self.inflight[(t, s)] = left
            else:
                self.inflight.pop((t, s), None)

    def views(self, b: Basket) -> list[pricing.LegView] | None:
        vs = [self.leg_view(l.ticker, l.side) for l in b.legs]
        return None if any(v is None for v in vs) else vs

    def _on_quote(self, book):
        self.liq.on_book(book.ticker, time.monotonic(), book.ask, book.ask_depth, book.bid, book.bid_depth)
        for bid in self.by_ticker.get(book.ticker, ()):
            self.dirty.add(bid)

    def _on_trade(self, ticker, trade):
        self.liq.on_trade(ticker, trade.size)
        if self.live:
            return
        maker = {"yes": "no", "no": "yes"}.get(trade.taker_side)
        for q in list(self.quotes.values()):
            if (q.ticker == ticker and q.side == maker and not q.pending and q.remaining > 0
                    and trade.price <= q.price + _EPS):
                n = min(q.remaining, trade.size)
                m = self.meta[ticker]
                self._quote_fill(q, n, n * q.price + kfees.fee(q.price, n, m.maker_rate or 0.0), None, "shadow")

    # ------------------------------------------------------------------ fills

    def _book(self, basket_id: str, ticker: str, side: str, size: float, cost: float, kind: str, **kw):
        b = self.baskets[basket_id]
        self.ledger.record(basket_id, b.tickers, ticker, side, size, cost, time.time())
        self.journal({"kind": "fill", "fill_kind": kind, "basket_id": basket_id, "ticker": ticker, "side": side,
                      "size": size, "cost": round(cost, 4), "price": round((cost / size) if size else 0, 4), **kw})
        self.dirty.add(basket_id)
        if basket_id in self.flip:
            self.dirty.add(self.flip[basket_id])

    def _quote_fill(self, q: Quote, size: float, cost: float, trade_id, source: str):
        q.filled += size
        self.n["quote_fills"] += 1
        self._book(q.basket_id, q.ticker, q.side, size, cost, "quote", source=source, order_id=q.exchange_id,
                   trade_id=trade_id)
        if q.remaining <= _EPS and self.quotes.get(q.key) is q:
            self._retire(q)

    def basket_quotes(self, basket_id: str) -> list[Quote]:
        b = self.baskets[basket_id]
        return [q for j in range(len(b.legs)) if (q := self.quotes.get((basket_id, j))) is not None]

    def _retire(self, q: Quote):
        if self.quotes.get(q.key) is q:
            del self.quotes[q.key]
        if q.exchange_id and q.exchange_id in self.real_ledger.quote_ids:
            self.real_ledger.quote_ids[q.exchange_id]["retired_at"] = time.time()

    def apply_real_fill(self, f: dict, source: str) -> bool:
        """A Kalshi fill dict (ws or REST). Booked only if it is one of our
        quotes and not seen before. False if its order id is unknown yet."""
        oid, tid = f.get("order_id"), f.get("trade_id")
        info = self.real_ledger.quote_ids.get(oid)
        if info is None:
            return False
        key = f"{oid}:{tid}"
        if key in self.real_ledger.seen_trades:
            return True
        self.real_ledger.seen_trades.add(key)
        size, cost = float(f["size"]), float(f["price"]) * float(f["size"]) + float(f.get("fee") or 0.0)
        b = self.baskets.get(info["basket_id"])
        leg = b.tickers.index(info["ticker"]) if b is not None and info["ticker"] in b.tickers else -1
        q = self.quotes.get((info["basket_id"], leg))
        if q is not None and q.exchange_id == oid:
            self._quote_fill(q, size, cost, tid, source)
        else:
            self.n["quote_fills"] += 1
            self._book(info["basket_id"], info["ticker"], info["side"], size, cost, "quote", source=source,
                       order_id=oid, trade_id=tid, late=True)
        return True

    def _on_fill(self, f: dict):
        if not self.live or f.get("liquidity") != "maker":
            return
        if not self.apply_real_fill(f, "ws") and f.get("ticker") in self.meta:
            # Fill can beat the create's ack; replayed once the id is known.
            self.orphans.append((time.monotonic(), f))

    def _replay_orphans(self):
        now = time.monotonic()
        keep = []
        for t, f in self.orphans:
            if self.apply_real_fill(f, "ws_late"):
                continue
            if now - t < ORPHAN_FILL_S:
                keep.append((t, f))
        self.orphans = keep

    async def fill_poll_loop(self):
        while True:
            await asyncio.sleep(self.cfg.runtime.fill_poll_s)
            if self.live:
                try:
                    await self.poll_fills()
                except Exception as e:
                    _log(f"WARN: fill poll failed ({e!r})")

    async def poll_fills(self):
        c = self.gw.any_client()
        if c is None:
            return
        since = self.real_ledger.last_fill_ts or (time.time() - 3600)
        fills = await self.gw.call(c.fills, "", min_ts=int(since) - 5)
        newest = since
        for f in fills:
            if f.get("liquidity") == "maker":
                self.apply_real_fill(f, "rest")
            ts = f.get("time")
            if ts:
                newest = max(newest, pd.Timestamp(ts).timestamp())
        self.real_ledger.last_fill_ts = newest
        self.real_ledger.save(time.time())

    # ------------------------------------------------------------------ limits

    def worst_bundle_cost(self, b: Basket) -> float:
        """Most one bundle can cost once a quote fills and is completed: the
        completion's cap_j allows up to payout + max_loss_per_bundle."""
        return b.payout + self.cfg.completion.max_loss_per_bundle

    def quote_commit(self, basket_id: str) -> float:
        """Dollars a basket's resting quotes can cost, counted ONCE per basket:
        a fill on one leg pulls the others and completes, several legs filling
        leaves less to take, so the basket never needs more than its largest
        quote's bundles."""
        qs = self.basket_quotes(basket_id)
        if not qs:
            return 0.0
        return max(q.remaining for q in qs) * self.worst_bundle_cost(self.baskets[basket_id])

    def committed(self) -> float:
        held = sum(b.cost() for b in self.ledger.baskets.values())
        # Kalshi credits $1 per YES/NO pair at once, so bundles held in both a
        # basket and its flip are cash already back, not capital tied up.
        for bid, fid in self.flip.items():
            pa, pb = self.ledger.baskets.get(bid), self.ledger.baskets.get(fid)
            if bid < fid and pa and pb:
                b, f = self.baskets[bid], self.baskets[fid]
                held -= min(pa.bundles(b.tickers), pb.bundles(f.tickers)) * len(b.legs)
        return held + sum(self.quote_commit(bid) for bid in {q.basket_id for q in self.quotes.values()})

    def room_bundles(self, b: Basket, leg: int | None = None) -> float:
        """Bundles the basket may still add. For a quote on `leg`, net of what
        already rests on that leg; for a take (leg None), net of every quote."""
        pos = self.ledger.baskets.get(b.basket_id)
        held = max(pos.held(b.tickers)) if pos else 0.0
        if leg is None:
            resting = max((q.remaining for q in self.basket_quotes(b.basket_id)), default=0.0)
        else:
            q = self.quotes.get((b.basket_id, leg))
            resting = 0.0 if q is None else q.remaining
        return max(0.0, self.cfg.trading.max_bundles_per_basket - held - resting)

    def exit_room(self, b: Basket) -> int:
        """Whole bundles of `b` that would only close bundles held in its flip."""
        fid = self.flip.get(b.basket_id)
        held = self.ledger.baskets.get(fid) if fid else None
        if held is None:
            return 0
        pos = self.ledger.baskets.get(b.basket_id)
        mine = max(pos.held(b.tickers)) if pos else 0.0
        return int(max(0.0, held.bundles(self.baskets[fid].tickers) - mine) + _EPS)

    def room_dollars(self) -> float:
        return self.cfg.trading.max_dollars - self.committed()

    def quote_size_by_dollars(self, b: Basket) -> int:
        """Largest quote size any leg of `b` may have under max_dollars. The
        basket's own quotes are added back, since a leg at or below the
        basket's largest quote adds nothing to what it can cost."""
        budget = self.room_dollars() + self.quote_commit(b.basket_id)
        return int(max(0.0, budget) // max(self.worst_bundle_cost(b), 0.01))

    # ------------------------------------------------------------------ eval

    async def eval_loop(self):
        while True:
            await asyncio.sleep(self.cfg.watch.eval_interval_s)
            self._replay_orphans()
            todo, self.dirty = self.dirty, set()
            for bid in todo:
                if bid in self.busy:
                    self.dirty.add(bid)
                    continue
                b = self.baskets.get(bid)
                if b is None:
                    continue
                try:
                    self.evaluate(b)
                except Exception:
                    _log(f"eval error {bid}:")
                    traceback.print_exc()

    def evaluate(self, b: Basket):
        now = time.time()
        pos = self.ledger.baskets.get(b.basket_id)
        qs = self.basket_quotes(b.basket_id)
        live_qs = [q for q in qs if not q.pending]
        if pos is not None and pos.unhedged(b.tickers):
            if live_qs:
                # Pull every leg's quote while completing: another leg filling
                # now would buy what the completion IOCs are buying. Complete
                # alongside, not after: the asks the fill was priced against
                # only get worse.
                async def both(qs=live_qs):
                    await asyncio.gather(*(self.cancel_quote(q, "filled") for q in qs), self.complete(b))
                self.spawn(both(), b.basket_id)
                return
            if now - self.last_completion.get(b.basket_id, 0.0) >= self.cfg.completion.retry_s:
                self.spawn(self.complete(b), b.basket_id)
            return
        vs = self.views(b) if self.tradeable(b) else None
        blocked = self.blocked()
        if vs is None or blocked or self.excluded(b):
            if live_qs:
                self.spawn(self.cancel_quotes(live_qs, blocked or ("excluded" if self.excluded(b) else "untradeable")),
                           b.basket_id)
            return
        t = self.cfg.trading
        if t.enable_takes and now - self.last_take.get(b.basket_id, 0.0) >= t.take_cooldown_s:
            g = pricing.gap(vs, b.payout)
            need = edge_dollars(b.payout, t.min_edge)
            ex = self.exit_room(b)
            plan = None
            if ex >= 1 and g is not None and g >= t.exit_edge - _EPS:
                plan = pricing.take_plan(vs, b.payout, t.exit_edge, ex)
            if plan is None and g is not None and g >= need - _EPS:
                per = b.payout - g
                n = int(min(self.room_bundles(b), max(0.0, self.room_dollars()) // max(per, 0.01)))
                plan = pricing.take_plan(vs, b.payout, need, n) if n >= 1 else None
            if plan is not None:
                if live_qs:
                    # Our own quotes are on legs we're about to take around.
                    self.spawn(self.cancel_quotes(live_qs, "take"), b.basket_id)
                    return
                if not qs:
                    held = self.hold([(l.ticker, l.side, plan.size) for l in b.legs])
                    self.spawn(self.take(b, plan, held), b.basket_id)
                    return
        self.manage_quotes(b, vs, now)

    def manage_quotes(self, b: Basket, vs, now: float):
        """One quote per leg, each priced on its own: a fill on leg j is
        completed by taking the others, so each leg's limit only depends on the
        other legs' asks. Several legs filling together just leaves less to take."""
        t = self.cfg.trading
        ex = self.exit_room(b)
        want = t.enable_quoting and (b.basket_id in self.hot or ex >= 1) and b.quoting_allowed(_utc())
        reserve = self.cfg.runtime.write_reserve_tokens
        acts = []
        for j, leg in enumerate(b.legs):
            q = self.quotes.get((b.basket_id, j))
            if q is not None and q.pending:
                continue
            plan, size = None, 0.0
            if want and ex >= 1:
                size = min(float(t.quote_size), float(ex))
                if q is None or q.remaining <= size + _EPS:
                    plan = pricing.quote_leg(vs, j, b.payout, t.exit_edge, size)
            elif want:
                size = min(float(t.quote_size), self.room_bundles(b, j), float(self.quote_size_by_dollars(b)))
                if size >= 1:
                    plan = pricing.quote_leg(vs, j, b.payout, edge_dollars(b.payout, t.min_edge + t.quote_edge_add),
                                             size)
            if plan is None:
                if q is not None:
                    acts.append(self.cancel_quote(q, "no_edge" if want else "not_hot"))
                continue
            if q is None:
                if self.live and not self.gw.can_afford(leg.ticker, "order", reserve):
                    continue
                # Registered now, not when the coroutine runs, so the next
                # basket's dollar room in this same pass already counts it.
                acts.append(self.place_quote(self.new_quote(b, plan, size)))
                continue
            past_limit = q.price > plan.max_price + _EPS
            ticks = vs[j].ticks.steps(plan.price, q.price)
            cooled = now - max(q.moved_at, q.placed_at) >= t.requote_cooldown_s
            if past_limit or (ticks >= t.requote_min_ticks and cooled):
                if self.live and not past_limit and not self.gw.can_afford(leg.ticker, "amend", reserve):
                    continue
                acts.append(self.move_quote(q, plan))
            else:
                q.edge, q.max_price = plan.edge, plan.max_price
        if acts:
            async def run(acts=acts):
                await asyncio.gather(*acts)
            self.spawn(run(), b.basket_id)

    async def cancel_quotes(self, qs: list[Quote], reason: str):
        await asyncio.gather(*(self.cancel_quote(q, reason) for q in qs))

    # ------------------------------------------------------------------ orders

    async def _ioc(self, ticker, side, limit, size, reason, view: pricing.LegView):
        """(RealFill | None, exc). Shadow fills at the book levels <= limit."""
        if not self.live:
            got, cost = 0.0, 0.0
            for p, qty in view.asks:
                if p > limit + _EPS or got >= size - _EPS:
                    break
                n = min(qty, size - got)
                got += n
                cost += n * p
            if got <= _EPS:
                return None, None
            fee = kfees.fee(cost / got, got, view.taker_rate)
            return RealFill(price=cost / got, size=got, fee=fee, exchange_order_id=None), None
        fill, exc, sent = await self.gw.ioc(ticker, side, limit, size, force=True, reason=reason)
        if not sent:
            return None, RuntimeError("no order client")
        if exc is None:
            self._order_ok()
            if fill is not None and fill.price > limit + 0.01 + _EPS:
                self.halt = f"{ticker} IOC filled at {fill.price:.4f} above limit {limit:.4f} (units?)"
                self.alert("error", None, self.halt)
        return fill, exc

    async def take(self, b: Basket, plan: pricing.TakePlan, held: list):
        try:
            await self._take(b, plan, held)
        finally:
            self.release(held)

    async def _take(self, b: Basket, plan: pricing.TakePlan, held: list):
        self.last_take[b.basket_id] = time.time()
        # Our own hold off the views: the shadow IOC fills against them.
        self.release(held)
        vs = self.views(b)
        self.hold(held)
        if vs is None:
            return
        if self.live:
            for l in b.legs:
                c = self.gw.client(l.ticker)
                if c is None or not c.can_afford("order", reserve=10 * (len(b.legs) - 1)):
                    return
        self.n["takes"] += 1
        self.journal({"kind": "take", "basket_id": b.basket_id, "size": plan.size, "limits": plan.limits,
                      "edge": round(plan.edge, 4)})
        res = await asyncio.gather(*(self._ioc(l.ticker, l.side, lim, plan.size, "take", v)
                                     for l, lim, v in zip(b.legs, plan.limits, vs)))
        got = []
        for l, (fill, exc) in zip(b.legs, res):
            if exc is not None:
                self._ioc_exc(b, l.ticker, exc, "take")
            if fill is not None and fill.size > 0:
                self._book(b.basket_id, l.ticker, l.side, fill.size, fill.price * fill.size + fill.fee, "take",
                           order_id=fill.exchange_order_id)
            got.append(0.0 if fill is None else fill.size)
        self.n["take_bundles"] += min(got)
        if max(got) - min(got) > _EPS:
            self.alert("warn", b.basket_id, f"take filled unevenly {dict(zip(b.tickers, got))}; completing")

    def _ioc_exc(self, b, ticker, exc, what):
        if isinstance(exc, ValueError):
            # place_ioc raises ValueError on a fill it can't read: the position
            # is unknown until the audit says otherwise.
            self.halt = f"unreadable {what} fill on {ticker}: {exc}"
            self.alert("error", b.basket_id, self.halt)
        else:
            self._order_err(b.basket_id, f"{what} IOC {ticker}", exc)

    async def complete(self, b: Basket):
        self.last_completion[b.basket_id] = time.time()
        pos = self.ledger.baskets[b.basket_id]
        held = pos.held(b.tickers)
        done = min(held)
        target = max(held) - done
        filled = [h - done for h in held]
        spent = pos.broken_spent(b.tickers)
        vs = [self.leg_view(l.ticker, l.side) or pricing.LegView([], None, self.meta[l.ticker].taker_rate or 0.07,
                                                                 0.0) for l in b.legs]
        plan = pricing.completion(b.payout, self.cfg.completion.max_loss_per_bundle, target, filled, spent, vs)
        missing = {b.tickers[j]: plan.need[j] for j in range(len(b.legs)) if plan.need[j] > _EPS}
        closed = [l.ticker for j, l in enumerate(b.legs) if plan.need[j] > _EPS
                  and (self.meta.get(l.ticker) is None or self.meta[l.ticker].status not in TRADEABLE)]
        if closed:
            # Buying the open legs would only add exposure the closed one can't hedge.
            plan.limits = [None] * len(b.legs)
            plan.short = [b.tickers.index(t) for t in closed]
        self.completion_note[b.basket_id] = {
            "time": _utc().isoformat(), "need": missing,
            "caps": {b.tickers[j]: round(c, 4) for j, c in enumerate(plan.caps) if c is not None},
            "asks": {b.tickers[j]: vs[j].best_ask for j in range(len(b.legs)) if plan.need[j] > _EPS},
            "short": [b.tickers[j] for j in plan.short]}
        if not any(p is not None for p in plan.limits):
            self.n["completion_short"] += 1
            age = time.time() - (pos.broken_since or time.time())
            # One alert per minute per basket: retries are every retry_s.
            if int(age) // 60 != int(age - self.cfg.completion.retry_s) // 60 or age < self.cfg.completion.retry_s:
                self.alert("error", b.basket_id,
                           f"cannot complete within max_loss {self.cfg.completion.max_loss_per_bundle:g}/bundle: "
                           f"need {missing}, caps {self.completion_note[b.basket_id]['caps']}, "
                           f"asks {self.completion_note[b.basket_id]['asks']} (broken {age:.0f}s)")
            return
        self.n["completions"] += 1
        legs = [(j, b.legs[j]) for j in range(len(b.legs)) if plan.limits[j] is not None]
        self.journal({"kind": "complete", "basket_id": b.basket_id, "need": missing,
                      "limits": {l.ticker: plan.limits[j] for j, l in legs}})
        held = self.hold([(l.ticker, l.side, plan.need[j]) for j, l in legs])
        try:
            res = await asyncio.gather(*(self._ioc(l.ticker, l.side, plan.limits[j], plan.need[j], "complete",
                                                   vs[j]) for j, l in legs))
        finally:
            self.release(held)
        for (j, l), (fill, exc) in zip(legs, res):
            if exc is not None:
                self._ioc_exc(b, l.ticker, exc, "completion")
            if fill is not None and fill.size > 0:
                self._book(b.basket_id, l.ticker, l.side, fill.size, fill.price * fill.size + fill.fee,
                           "complete", order_id=fill.exchange_order_id)
        if not self.ledger.baskets[b.basket_id].unhedged(b.tickers):
            self.completion_note.pop(b.basket_id, None)
            self.journal({"kind": "completed", "basket_id": b.basket_id})

    def new_quote(self, b: Basket, plan: pricing.QuotePlan, size: float) -> Quote:
        leg = b.legs[plan.leg]
        q = Quote(basket_id=b.basket_id, leg=plan.leg, ticker=leg.ticker, side=leg.side, price=plan.price,
                  size=size, cid=str(uuid.uuid4()), edge=plan.edge, max_price=plan.max_price,
                  placed_at=time.time())
        self.quotes[q.key] = q
        return q

    async def place_quote(self, q: Quote):
        b = self.baskets[q.basket_id]
        size = q.size
        if not self.live:
            q.pending = False
            self.n["quotes_placed"] += 1
            self.journal({"kind": "quote", "basket_id": b.basket_id, "ticker": q.ticker, "side": q.side,
                          "price": q.price, "size": size, "edge": round(q.edge, 4)})
            return
        ack, exc, sent = await self.gw.rest(q.ticker, q.side, q.price, size, q.cid)
        if not sent:
            self.quotes.pop(q.key, None)
            return
        self.n["quotes_placed"] += 1
        if exc is not None:
            if is_post_only_reject(exc):
                self.n["post_only_rejects"] += 1
                self.quotes.pop(q.key, None)
                return
            if http_status(exc) is None:
                await self._find_lost(q)
                return
            self.quotes.pop(q.key, None)
            self._order_err(b.basket_id, f"quote {q.ticker}", exc)
            return
        self._order_ok()
        self._register(q, ack.exchange_order_id)
        if ack.parse_error is not None:
            self.halt = f"unreadable fill on quote create {q.ticker}: {ack.parse_error}"
            self.alert("error", b.basket_id, self.halt)
        elif ack.fill is not None and ack.fill.size > 0:
            self._quote_fill(q, ack.fill.size, ack.fill.price * ack.fill.size + ack.fill.fee, None, "create")
        self.journal({"kind": "quote", "basket_id": b.basket_id, "ticker": q.ticker, "side": q.side,
                          "price": q.price, "size": size, "edge": round(q.edge, 4), "order_id": q.exchange_id})

    def _register(self, q: Quote, oid: str | None):
        q.exchange_id, q.pending = oid, False
        if oid:
            self.real_ledger.quote_ids[oid] = {"basket_id": q.basket_id, "ticker": q.ticker, "side": q.side,
                                               "retired_at": None}
            self.real_ledger.save()
        self._replay_orphans()

    async def _find_lost(self, q: Quote):
        """A create with no response may be resting: find it by client id and pull it."""
        await asyncio.sleep(LOST_LOOKUP_S)
        c = self.gw.client(q.ticker)
        try:
            hit = await self.gw.call(c.find_by_client_id, q.ticker, q.cid)
        except Exception as e:
            self.alert("error", q.basket_id, f"quote {q.ticker} create got no response and lookup failed "
                                             f"({e!r}) -- CHECK KALSHI for a resting order")
            self.halt = f"lost quote on {q.ticker}"
            return
        if hit is None:
            self.quotes.pop(q.key, None)
            return
        self._register(q, hit[0])
        await self.cancel_quote(q, "lost_create")

    async def move_quote(self, q: Quote, plan: pricing.QuotePlan):
        b = self.baskets[q.basket_id]
        q.moved_at = time.time()
        self.n["quote_moves"] += 1
        old = q.price
        if self.live:
            resp, exc, sent = await self.gw.amend(q.ticker, q.side, q.exchange_id, plan.price, q.remaining, q.filled)
            if not sent:
                return
            if exc is not None:
                if http_status(exc) == 404:
                    self._retire(q)           # filled or gone; fills arrive on their own
                    return
                self._order_err(q.basket_id, f"move {q.ticker}", exc)
                if q.price > plan.max_price + _EPS:
                    await self.cancel_quote(q, "move_failed")
                return
            self._order_ok()
            o = (resp or {}).get("order") or {}
            new_id = o.get("order_id")
            if new_id and new_id != q.exchange_id:
                self._retire(q)
                self.quotes[q.key] = q
                self._register(q, new_id)
        q.price, q.edge, q.max_price = plan.price, plan.edge, plan.max_price
        self.journal({"kind": "move", "basket_id": q.basket_id, "ticker": q.ticker, "from": old, "to": plan.price})

    async def cancel_quote(self, q: Quote, reason: str):
        self.n["quote_cancels"] += 1
        if self.live and q.exchange_id:
            _, exc, _ = await self.gw.cancel(q.ticker, q.exchange_id, reason)
            if exc is not None:
                self._order_err(q.basket_id, f"cancel {q.ticker}", exc)
                return            # stays tracked; the next pass retries
        self._retire(q)
        self.journal({"kind": "cancel", "basket_id": q.basket_id, "ticker": q.ticker, "reason": reason})

    async def pull_all(self, reason: str):
        qs = [q for q in self.quotes.values() if not q.pending]
        await asyncio.gather(*(self.cancel_quote(q, reason) for q in qs), return_exceptions=True)
        if self.live:
            await self.sweep(reason)

    async def sweep(self, reason: str):
        """Cancel anything resting on our tickers: the ground truth after a restart."""
        c = self.gw.any_client()
        if c is None:
            return
        ours = set(self.tickers)
        resting = await self.gw.call(c._resting, "")
        for oid, ticker, _ in resting:
            if ticker in ours:
                _, exc, _ = await self.gw.cancel(ticker, oid, f"sweep/{reason}")
                if exc is not None:
                    self.alert("error", None, f"sweep cancel {oid} on {ticker} failed ({exc!r})")
                elif oid in self.real_ledger.quote_ids:
                    self.real_ledger.quote_ids[oid]["retired_at"] = time.time()

    # ------------------------------------------------------------------ ranking

    async def rank_loop(self):
        while True:
            try:
                rows = []
                for b in self.all_baskets:
                    if not self.tradeable(b) or self.excluded(b):
                        continue
                    vs = self.views(b)
                    g = None if vs is None else pricing.gap(vs, b.payout)
                    if g is None:
                        continue
                    rows.append({"basket_id": b.basket_id, "type": b.type, "ret": round(edge_return(b.payout, g), 4),
                                 "gap": round(g, 4), "payout": b.payout, "asks": [v.best_ask for v in vs],
                                 "legs": len(b.legs), "depth": round(min(v.depth() for v in vs), 1)})
                rows.sort(key=lambda r: -r["ret"])
                self.ranking = rows
                quotable = [r["basket_id"] for r in rows if self.baskets[r["basket_id"]].quoting_allowed(_utc())]
                hot = quotable[:self.cfg.watch.max_quoted_baskets]
                if hot != self.hot:
                    self.dirty |= set(hot) ^ set(self.hot)
                    self.hot = hot
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(self.cfg.watch.rank_interval_s)

    # ------------------------------------------------------------------ guards

    async def guard_loop(self):
        while True:
            await asyncio.sleep(1.0)
            ds = self.feed.down_since
            if ds is not None and time.monotonic() - ds > self.cfg.runtime.feed_down_pull_s and self.quotes:
                self.alert("warn", None, "Kalshi websocket down; pulling every quote")
                await self.pull_all("feed_down")

    async def audit_loop(self):
        while True:
            await asyncio.sleep(self.cfg.runtime.audit_s)
            if self.live:
                try:
                    await self.audit()
                except Exception as e:
                    _log(f"WARN: audit failed ({e!r})")

    async def audit(self) -> bool:
        """Ledger vs Kalshi on our tickers. Two misses in a row halt new
        entries; a ledger fix is by hand (the ledger is the only record of
        which basket a contract belongs to)."""
        c = self.gw.any_client()
        real = await self.gw.call(c.positions_all)
        mine = self.real_ledger.net_by_ticker()
        diff = {t: {"kalshi": real.get(t, 0.0), "ledger": round(mine.get(t, 0.0), 4)}
                for t in self.tickers if abs(real.get(t, 0.0) - mine.get(t, 0.0)) > 1e-6}
        if diff and any(not q.pending for q in self.quotes.values()):
            # A fill in flight can show at Kalshi first; confirm after a fills poll.
            await self.poll_fills()
            mine = self.real_ledger.net_by_ticker()
            diff = {t: d for t, d in diff.items() if abs(real.get(t, 0.0) - mine.get(t, 0.0)) > 1e-6}
        if any(self.meta.get(t) is None or self.meta[t].status not in TRADEABLE for t in diff):
            # Kalshi drops a position the moment it settles; we book settlement
            # only on the next meta refresh (FLUINT 10-29 00:36 halted us for it).
            await asyncio.get_running_loop().run_in_executor(self.pool, self.load_meta)
            self.settle()
            mine = self.real_ledger.net_by_ticker()
            diff = {t: d for t, d in diff.items() if abs(real.get(t, 0.0) - mine.get(t, 0.0)) > 1e-6}
        self.audit_diff = diff or None
        if not diff:
            self.audit_strikes = 0
            if self.halt and self.halt.startswith("position audit"):
                # Only this halt clears itself: it means "ledger unknown", and a
                # clean audit is exactly the proof it isn't any more.
                self.alert("info", None, f"cleared halt ({self.halt}): ledger matches Kalshi again")
                self.halt = None
            return True
        self.audit_strikes += 1
        if self.audit_strikes >= 2 and not self.halt:
            self.halt = f"position audit: ledger != Kalshi on {len(diff)} markets"
            self.alert("error", None, f"{self.halt}: {diff}")
        return False

    # ------------------------------------------------------------------ settlement

    def settle(self):
        for led in (self.real_ledger, self.shadow_ledger):
            for bid, pos in list(led.baskets.items()):
                legs = pos.legs
                res = {t: self.meta.get(t).result if self.meta.get(t) else "" for t in legs}
                if not all(self.meta.get(t) and self.meta[t].status in SETTLED for t in legs):
                    continue
                if any(r not in ("yes", "no") for r in res.values()):
                    self.alert("error", bid, f"settled with result {res}; not booked, check by hand")
                    continue
                b = self.baskets.get(bid)
                paid = sum(lp.qty for t, lp in legs.items() if res[t] == lp.side)
                cost = pos.cost()
                row = {"basket_id": bid, "type": b.type if b else "", "settled_at": _utc().isoformat(),
                       "bundles": pos.bundles(list(legs)), "payout": round(paid, 4), "cost": round(cost, 4),
                       "pnl": round(paid - cost, 4), "results": res, "live": led is self.real_ledger}
                led.close(bid, row)
                self.journal({"kind": "settled", **row})

    # ------------------------------------------------------------------ arm / control

    async def arm(self) -> bool:
        if not self.gw.clients:
            self.alert("error", None, "arm refused: no order clients")
            return False
        try:
            bal = {s: await self.gw.call(c.balance, s) for s, c in self.gw.clients.items()}
        except Exception as e:
            self.alert("error", None, f"arm refused: balance read failed ({e!r})")
            return False
        if any(v <= 0 for v in bal.values()):
            self.alert("error", None, f"arm refused: zero balance on shard(s) {bal}")
            return False
        await self.pull_all("arm")        # shadow quotes
        self.live = True
        try:
            await self.poll_fills()
            await self.sweep("arm")
            await self.poll_fills()
            ok = await self.audit()
        except Exception as e:
            self.live = False
            self.alert("error", None, f"arm failed ({e!r})")
            return False
        if not ok:
            self.live = False
            self.alert("error", None, f"arm refused: ledger disagrees with Kalshi {self.audit_diff}")
            return False
        self.order_errors = 0
        self.dirty |= set(self.baskets)
        self.alert("info", None, f"REAL TRADING ON (balances {bal})")
        return True

    async def disarm(self, reason: str):
        await self.pull_all(reason)
        if self.live:
            try:
                await self.poll_fills()
            except Exception:
                pass
        self.live = False
        self.quotes.clear()
        self.alert("info", None, f"real trading off ({reason})")

    async def control_loop(self, trading: TradingModeWatcher, stopper: StopWatcher, stop: asyncio.Event):
        retry_at = 0.0
        while True:
            await asyncio.sleep(self.cfg.runtime.control_poll_s)
            for c in self.store.poll():
                _log(f"config: {c}")
                self.dirty |= set(self.baskets)
            for c in self.store.apply_pending():
                _log(f"config: {c}")
            if (reason := stopper.poll()) is not None:
                _log(f"STOP: {reason}")
                stop.set()
                return
            want = trading.poll()
            if want is not None:
                self.arm_want, retry_at = want, 0.0
                _log(f"trading mode requested live={want} {trading.origin()}")
            if (self.arm_want and not self.live and time.monotonic() >= retry_at
                    and (not self.halt or self.halt.startswith("position audit"))):
                if not await self.arm():
                    retry_at = time.monotonic() + 30.0
            elif not self.arm_want and self.live:
                await self.disarm("dashboard")

    # ------------------------------------------------------------------ summary

    def summary(self) -> dict:
        led = self.ledger
        holdings = []
        for bid, pos in led.baskets.items():
            b = self.baskets.get(bid)
            tickers = b.tickers if b else list(pos.legs)
            n = pos.bundles(tickers)
            locked = (n * b.payout - (pos.cost() - pos.broken_spent(tickers))) if b else None
            holdings.append({"basket_id": bid, "type": b.type if b else "", "payout": b.payout if b else None,
                             "bundles": n, "held": dict(zip(tickers, pos.held(tickers))),
                             "cost": round(pos.cost(), 4), "locked_pnl": None if locked is None else round(locked, 4),
                             "unhedged": pos.unhedged(tickers),
                             "broken_s": None if pos.broken_since is None else round(time.time() - pos.broken_since),
                             "completion": self.completion_note.get(bid)})
        settled = [r for r in led.settled]
        quotes = [{"basket_id": q.basket_id, "ticker": q.ticker, "side": q.side, "price": q.price,
                   "size": q.size, "filled": q.filled, "edge": round(q.edge, 4),
                   "ret": round(edge_return(self.baskets[q.basket_id].payout, q.edge), 4), "max_price": q.max_price,
                   "age_s": round(time.time() - q.placed_at), "pending": q.pending, "order_id": q.exchange_id}
                  for q in self.quotes.values()]
        return {
            "series": self.S, "event_ticker": "ARBS", "last_update": _utc().isoformat(),
            "session_started": self.started.isoformat(), "live_trading": self.live, "arm_requested": self.arm_want,
            "halted": self.halt is not None, "halt_reason": self.halt, "blocked_reason": self.blocked(),
            "feed": {"up": self.feed.up, "reconnects": self.feed.n_reconnects, "seq_gaps": self.feed.n_seq_gaps,
                     "ws_errors": self.feed.n_errors, "last_msg_s": self.feed.seconds_since_message()},
            "n_baskets": len(self.all_baskets), "n_tickers": len(self.tickers),
            "n_tradeable": sum(1 for b in self.all_baskets if self.tradeable(b)),
            "market_status": _count(m.status for m in self.meta.values()),
            "pnl": {"settled": round(sum(r["pnl"] for r in settled), 4),
                    "locked": round(sum(h["locked_pnl"] or 0.0 for h in holdings), 4),
                    "held_cost": round(sum(h["cost"] for h in holdings), 4),
                    "committed": round(self.committed(), 4), "max_dollars": self.cfg.trading.max_dollars},
            "holdings": holdings, "quotes": quotes, "hot": self.hot, "ranking": self.ranking[:60],
            "settled": settled[-200:], "alerts": self.alerts[-100:], "audit_diff": self.audit_diff,
            "order_errors": self.order_errors, "counts": self.n, "write_usage": self.gw.usage(),
            "config_status": self.store.status(), "scan": self.scan_status,
        }

    async def summary_loop(self):
        while True:
            await asyncio.sleep(SUMMARY_S)
            try:
                s = self.summary()
                atomic_write_json(os.path.join(day_dir(self.S, f"{_utc():%Y-%m-%d}"), "summary.json"), s,
                                  indent=None)
            except Exception:
                traceback.print_exc()

    # ------------------------------------------------------------------ main

    async def run(self, stop_requested: bool = False):
        loop = asyncio.get_running_loop()
        _log(f"{len(self.all_baskets)} baskets over {len(self.tickers)} markets")
        await loop.run_in_executor(self.pool, self.load_meta)
        shards = {t: m.shard for t, m in self.meta.items() if m.shard is not None}
        _log(f"markets by status {_count(m.status for m in self.meta.values())}; shards {sorted(set(shards.values()))}")
        try:
            await loop.run_in_executor(self.pool, self.gw.setup, shards,
                                       lambda: self.cfg.runtime.max_write_tokens_per_s)
        except Exception as e:
            self.alert("error", None, f"no order clients ({e!r}); shadow only")
        self.settle()
        trading, stopper, stop = TradingModeWatcher(self.S), StopWatcher(), asyncio.Event()
        coros = [self.feed.run(), self.eval_loop(), self.rank_loop(), self.meta_loop(), self.fill_poll_loop(),
                 self.audit_loop(), self.guard_loop(), self.summary_loop(), self.scan_loop(),
                 self.liquidity_loop(),
                 self.control_loop(trading, stopper, stop)]
        tasks = [asyncio.create_task(c) for c in coros]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            try:
                await self.disarm("shutdown")
            except Exception:
                _log("WARN: shutdown cleanup failed -- CHECK KALSHI BY HAND for resting orders:")
                traceback.print_exc()
            await asyncio.gather(*tasks, return_exceptions=True)
            atomic_write_json(os.path.join(day_dir(self.S, f"{_utc():%Y-%m-%d}"), "summary.json"), self.summary(),
                              indent=None)
            self.pool.shutdown(wait=False)


def flips(baskets: list[Basket]) -> dict[str, str]:
    """basket_id -> its flip's basket_id: the basket buying every leg's other
    side. Only pairs whose payouts add to the leg count, so a bundle of each
    nets to exactly what the pair pays; tie-case payouts (exact: false) don't."""
    by_legs = {frozenset((l.ticker, l.side) for l in b.legs): b for b in baskets}
    out = {}
    for b in baskets:
        f = by_legs.get(frozenset((l.ticker, "no" if l.side == "yes" else "yes") for l in b.legs))
        if f is not None and abs(b.payout + f.payout - len(b.legs)) < _EPS:
            out[b.basket_id] = f.basket_id
    return out


def edge_dollars(payout: float, rate: float) -> float:
    """Dollar edge per bundle that returns `rate` on the bundle's cost. Cost
    is payout - edge, so edge / (payout - edge) >= rate is edge >= rate *
    payout / (1 + rate)."""
    return rate * payout / (1.0 + rate)


def edge_return(payout: float, edge: float) -> float:
    """Edge as a fraction of what the bundle costs (payout - edge)."""
    cost = payout - edge
    return edge / cost if cost > _EPS else float("-inf") if edge <= 0 else float("inf")


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


async def run_arbs(series, *, live_trading: bool, resume_arm: bool = False):
    if not live_trading and resume_arm:
        why = resumable_arm(series.ticker)
        if why is not None:
            _log(f"resuming REAL TRADING after restart: {why}")
            live_trading = True
    r = ArbsRunner(series, live_trading=live_trading)
    try:
        await r.run()
    except asyncio.CancelledError:
        # Ctrl-C: run()'s finally already pulled quotes.
        raise
