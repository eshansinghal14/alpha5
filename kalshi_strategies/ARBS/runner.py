"""ARBS live loop: baskets from the spec across ~70 events, one process.

Per pass over baskets whose books changed:
  1. a broken basket (legs uneven, after a quote fill or a partial take)
     takes its cheaper exit, completing the missing legs or netting the
     extra ones out (pricing.exits), when it worsens by chase_ticks within
     chase_window_s or the event is live;
     otherwise it rests post-only bids on that exit, re-pegged every pass;
  2. a whole-basket take if (payout - sum(ask + fee)) / sum(ask + fee) >= min_edge
     (a return on the bundle's cost, see edge_dollars);
  3. one resting quote per leg of each hot basket (the best by expected
     quote profit per hour, as many as the write budget covers), priced so a
     fill completed at today's asks still returns min_edge + quote_edge_add.

Shadow (default) runs the same decisions against the live book: takes fill
at the levels they would cross, quotes fill on a print at or through their
price (no queue, so optimistic). It has its own ledger file.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import dataclasses
import gzip
import json
import math
import os
import sys
import time
import traceback
import uuid

import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import ConfigStore, atomic_write_json
from kalshi_infra.kalshi import fees as kfees
from kalshi_infra.kalshi.orders import RealFill, error_code, http_status, is_market_paused, is_post_only_reject
from kalshi_infra.kalshi.orders import write_budget_tokens_per_s
from kalshi_infra.trading.engine import MARKET_PAUSE_RETRY_S
from kalshi_infra.runtime.control import StopWatcher, TradingModeWatcher, resumable_arm
from kalshi_infra.runtime.persist import LIVE_ROOT, day_dir
from kalshi_strategies.ARBS import pricing, scanner
from kalshi_strategies.ARBS.config import SPEC_PATH
from kalshi_strategies.ARBS.execution import Gateway
from kalshi_strategies.ARBS.feed import MultiFeed
from kalshi_strategies.ARBS.ledger import Ledger
from kalshi_strategies.ARBS.liquidity import Liquidity
from kalshi_strategies.ARBS.spec import Basket, basket_from_dict, basket_to_dict, load_spec, merge, parse_spec

_EPS = 1e-9
OTHER = {"yes": "no", "no": "yes"}
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
    kind: str = "arb"           # or "complete" / "unwind": resting on a broken basket's exit

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def key(self) -> tuple[str, int]:
        return self.basket_id, self.leg


class _Dirty(set):
    """Basket ids to evaluate. Adding one wakes the eval loop."""

    def __init__(self, items=(), wake: asyncio.Event | None = None):
        super().__init__(items)
        self.wake = wake

    def add(self, x):
        super().add(x)
        self.wake.set()

    def __ior__(self, other):
        super().__ior__(other)
        if other:
            self.wake.set()
        return self


# Time constant of the per-basket write spend and per-market top-of-book
# change rates the hot list is budgeted on.
WRITE_COST_TAU_S = 300.0
# Write tokens per order or move, per cancel (Kalshi's write budget).
ORDER_TOKENS, CANCEL_TOKENS = 10.0, 2.0


class _Rate:
    """Events per second, exponentially weighted over WRITE_COST_TAU_S."""

    def __init__(self):
        self.v, self.t = 0.0, None

    def at(self, now: float) -> float:
        return 0.0 if self.t is None else self.v * math.exp(-(now - self.t) / WRITE_COST_TAU_S)

    def add(self, x: float, now: float):
        self.v, self.t = self.at(now) + x / WRITE_COST_TAU_S, now


class _Flow:
    """Contracts sold into each market side's bids (a YES bid fills on a
    taker selling YES, i.e. buying NO), by wall time."""

    def __init__(self):
        self.d: dict[tuple[str, str], collections.deque] = {}
        self.total: dict[tuple[str, str], float] = {}

    def add(self, ticker: str, side: str, size: float, t: float):
        k = (ticker, side)
        self.d.setdefault(k, collections.deque()).append((t, size))
        self.total[k] = self.total.get(k, 0.0) + size

    def rate(self, ticker: str, side: str, now: float, window: float) -> float:
        """Contracts per second over the last `window` seconds."""
        k = (ticker, side)
        dq = self.d.get(k)
        if not dq:
            return 0.0
        while dq and dq[0][0] < now - window:
            self.total[k] -= dq.popleft()[1]
        return max(0.0, self.total[k]) / window


class _Quotes(dict):
    """(basket_id, leg) -> Quote, indexed by (ticker, side): leg views and the
    one-quote-per-side check look a market side up on every evaluation."""

    def __init__(self):
        super().__init__()
        self.side: dict[tuple[str, str], dict] = collections.defaultdict(dict)

    def __setitem__(self, k, q):
        old = self.get(k)
        if old is not None:
            self.side[(old.ticker, old.side)].pop(k, None)
        super().__setitem__(k, q)
        self.side[(q.ticker, q.side)][k] = q

    def __delitem__(self, k):
        q = self[k]
        super().__delitem__(k)
        self.side[(q.ticker, q.side)].pop(k, None)

    def pop(self, k, *default):
        if k in self:
            q = self[k]
            del self[k]
            return q
        if default:
            return default[0]
        raise KeyError(k)

    def clear(self):
        super().clear()
        self.side.clear()

    def on(self, ticker: str, side: str):
        return list(self.side.get((ticker, side), {}).values())


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
        self.quotes = _Quotes()                           # (basket_id, leg index) -> its quote
        self.busy: set[str] = set()
        self.baskets: dict[str, Basket] = {}
        self.tickers: list[str] = []
        self.wake = asyncio.Event()
        self._dirty = _Dirty((), self.wake)
        # basket_id -> its ranking row, refreshed whenever the basket is
        # evaluated (its books changed), so the rank pass only sorts.
        self.rank_rows: dict[str, dict] = {}
        self._in_pass = False
        self._commit_cache: float | None = None
        self.hot: list[str] = []
        self.ranking: list[dict] = []
        with open(SPEC_PATH, encoding="utf-8") as fh:
            self.hand_doc = json.load(fh)
        self.hand = load_spec(SPEC_PATH)
        self.scan_path = os.path.join(self.root, scanner.SELECTED)
        self.scan_status: dict = {}
        self.set_baskets(merge(self.hand, self.scanned()))
        for led in (self.real_ledger, self.shadow_ledger):
            for bid, pos in led.baskets.items():
                if pos.spec is None and bid in self.baskets:
                    pos.spec = basket_to_dict(self.baskets[bid])
            led.save()
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
        # ticker -> book updates seen; basket_id -> (its legs' counts, held)
        # at its last exit attempt. An exit is retried only once one changed.
        self.book_seq: dict[str, int] = {}
        self.book_time: dict[str, float] = {}     # ticker -> monotonic time of its last book update
        self.paused_until: dict[str, float] = {}  # ticker -> monotonic time Kalshi's pause is next retried
        self.exchange_paused = False              # Kalshi's exchange-wide trading_active is off
        self.priority_trading: list[str] = []     # write_priority_series in session now
        self.last_exit: dict[str, tuple] = {}
        self.completion_note: dict[str, dict] = {}
        # basket_id -> recent (time, exit cost) of a broken basket: the chase
        # reference. Exit cost is what finishing costs from here, net of the
        # payout, without what the extra contracts already cost: a leg's
        # average cost mixes in older bundles, and it is the same for both
        # paths and over time anyway.
        self.exit_hist: dict[str, collections.deque] = {}
        self.exit_planned: dict[str, float] = {}  # exit cost the fill or take was priced at
        self.order_errors = 0
        self.halt: str | None = None                     # until restart
        self.audit_strikes = 0
        self.audit_diff: dict | None = None
        self.orphans: list[tuple[float, dict]] = []
        self.tasks: set[asyncio.Task] = set()
        self.n = {"takes": 0, "take_bundles": 0.0, "quotes_placed": 0, "quote_moves": 0, "quote_cancels": 0,
                  "quote_fills": 0, "completions": 0, "unwinds": 0, "exit_unpriced": 0, "post_only_rejects": 0,
                  "moves_refused": 0}
        self.started = _utc()
        self.liq = Liquidity(self.root)
        self.flow = _Flow()
        # basket_id -> per leg (price, edge per bundle, size) we'd quote, or None.
        self.quote_px: dict[str, list] = {}
        # basket_id -> prints that would have filled a quote on a basket we didn't quote.
        self.missed: dict[str, dict] = {}
        # Misses count only from 2 min after a start, once the hot list has
        # filled: until then every print looks like one (10-29 restarts, 9-92 s in).
        self.hot_ready = False
        self.rank_at: dict[str, float] = {}       # basket_id -> monotonic time its rank row was last priced
        self.spend: dict[str, _Rate] = {}         # basket_id -> write tokens/s its quotes spent
        self.top_moves: dict[str, _Rate] = {}     # ticker -> best bid/ask changes per second
        self.last_top: dict[str, tuple] = {}
        self.hot_churn: collections.deque = collections.deque()   # (time, entered, left) per hot-list change
        self.hot_cost = 0.0                       # estimated write tokens/s of quoting the hot list
        # Quote ids are saved by ledger_flush_loop, not per placement: a
        # synchronous write per quote held the loop ~200 ms each and queued a
        # live exit 7 s behind a burst of placements (FURIABB-2 10-29). Dirty
        # at start so ids read from a pre-split ledger reach their own file.
        self.ids_dirty = True
        # ticker -> unix time a bad exit flagged it live: an unwind or a fast
        # move means the market reprices like a live game (weather readings,
        # news). Cleared by a clean completion or an even take on it.
        self.flagged: dict[str, float] = {}
        self.unpriced_alert_at: dict[str, float] = {}
        self._horizon: tuple[float, pd.Timestamp] | None = None
        # Live order round trips, flushed with the liquidity log (orders_*.jsonl.gz).
        self.order_times: list[dict] = []

    # ------------------------------------------------------------------ util

    @property
    def dirty(self) -> _Dirty:
        return self._dirty

    @dirty.setter
    def dirty(self, items):
        self._dirty = _Dirty(items, self.wake)
        if items:
            self.wake.set()

    def later(self, basket_id: str, delay: float):
        """Re-evaluate a basket after `delay` without spinning the eval loop."""
        asyncio.get_running_loop().call_later(delay, self.dirty.add, basket_id)

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
        if self.exchange_paused:
            return "Kalshi trading paused"
        broken = sum(1 for b in self.ledger.baskets.values() if b.broken_since is not None)
        if broken >= self.cfg.completion.max_broken_baskets:
            return f"{broken} broken baskets"
        return None

    def _order_ok(self):
        self.order_errors = 0

    def _order_err(self, basket_id, what, exc, ticker: str):
        st, code = http_status(exc), error_code(exc)
        if st == 429:
            return
        if is_market_paused(exc):
            # Held like the engine does: Kalshi's Thursday maintenance pauses
            # every market, and retrying each pass sent 1563 rejected orders
            # in 8 min (10-29 07:00).
            if not self.market_paused(ticker) and not self.exchange_paused:
                self.alert("warn", basket_id, f"{what}: market paused/closed ({code}); "
                                              f"no new orders on it for {MARKET_PAUSE_RETRY_S:.0f}s")
            self.paused_until[ticker] = time.monotonic() + MARKET_PAUSE_RETRY_S
            return
        self.order_errors += 1
        self.alert("error", basket_id, f"{what} failed: {st} {code} {str(exc)[:200]}")

    # ------------------------------------------------------------------ spec

    def scanned(self) -> list[Basket]:
        """The part of the last scan to trade, as the scan process selected it."""
        if not self.cfg.scan.enable or not os.path.exists(self.scan_path):
            return []
        try:
            return load_spec(self.scan_path)
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
            if bid in new:
                continue
            if bid in self.baskets:
                new[bid] = self.baskets[bid]
                continue
            pos = self.real_ledger.baskets.get(bid) or self.shadow_ledger.baskets.get(bid)
            if pos is not None and pos.spec:
                new[bid] = basket_from_dict(pos.spec)
        # A held basket kept under an old id (MECNET ids were renamed) wins
        # over a listed basket with the same legs: both trading one set of
        # legs took and exited against each other (MIACIN 10-29 01:33).
        held_legs = {frozenset(new[bid].legs): bid for bid in keep if bid in new}
        for bid in [k for k, v in new.items() if held_legs.get(frozenset(v.legs), k) != k]:
            del new[bid]
        old = set(self.tickers)
        # Changed ones too: a new quote window has to pull a resting quote.
        changed = {k for k, v in new.items() if self.baskets.get(k) != v}
        self.all_baskets = list(new.values())
        self.baskets = new
        self.tickers = sorted({t for b in self.all_baskets for t in b.tickers})
        self.flip = flips(self.all_baskets)
        self.by_ticker: dict[str, list[str]] = {}
        for b in self.all_baskets:
            for t in b.tickers:
                self.by_ticker.setdefault(t, []).append(b.basket_id)
        self.dirty = {d for d in self.dirty if d in new} | changed
        self.rank_rows = {k: v for k, v in self.rank_rows.items() if k in new}
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
                                       self.write_cap)
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
                orders, self.order_times = self.order_times, []
                day = f"{_utc():%Y-%m-%d}"
                await loop.run_in_executor(self.pool, self.liq.write, rows, day)
                await loop.run_in_executor(self.pool, self.liq.write, orders, f"orders_{day}")
            except Exception as e:
                _log(f"WARN: liquidity log failed ({e!r})")

    async def scan_loop(self):
        while True:
            sc = self.cfg.scan
            try:
                if sc.enable:
                    t0 = time.monotonic()
                    # A child process: the scan's memory goes back when it exits.
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "kalshi_strategies.ARBS.scanner", self.root,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
                    if proc.returncode != 0:
                        raise RuntimeError(f"scan exited {proc.returncode}: {err.decode()[-300:]}")
                    counts = json.loads(out.decode().strip().splitlines()[-1])
                    await self.reload_spec()
                    self.scan_status = {"at": _utc().isoformat(), "secs": round(time.monotonic() - t0, 1),
                                        **counts, "baskets": len(self.all_baskets) - len(self.hand), "error": None}
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
            mt = self.meta.get(t)
            if mt is None or mt.event != m["event_ticker"]:
                # A market missing from an earlier read was stubbed with no
                # event; keeping that stub looked fees up for event "" forever.
                mt = Meta(ticker=t, event=m["event_ticker"], status=mt.status if mt else "unknown")
            if mt.status != m.get("status") and mt.status != "unknown":
                self.journal({"kind": "status", "ticker": t, "from": mt.status, "to": m.get("status")})
            mt.status, mt.result = m.get("status") or "", m.get("result") or ""
            mt.ticks = pricing.Ticks.from_market(m.get("price_ranges"))
            self.meta[t] = mt
            events.setdefault(mt.event, []).append(mt)
        for et, ms in events.items():
            known = next((mt for mt in ms if mt.shard is not None), None)
            if known is not None:
                # A market added to a known event (a new strike or candidate)
                # shares its series, shard and fees.
                for mt in ms:
                    if mt.shard is None:
                        mt.series, mt.shard = known.series, known.shard
                        mt.taker_rate, mt.maker_rate = known.taker_rate, known.maker_rate
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

    def market_paused(self, ticker: str) -> bool:
        return time.monotonic() < self.paused_until.get(ticker, 0.0)

    def tradeable(self, b: Basket) -> bool:
        for t in b.tickers:
            m = self.meta.get(t)
            if (m is None or m.status not in TRADEABLE or m.shard is None or m.taker_rate is None
                    or self.market_paused(t)):
                return False
        return True

    def excluded(self, b: Basket) -> bool:
        ex = self.cfg.watch.exclude
        if ex and (b.basket_id in ex or b.relationship_id in ex or b.cluster in ex):
            return True
        return b.settles is not None and b.settles > self.horizon()

    def horizon(self) -> pd.Timestamp:
        """Latest settlement traded, refreshed once a second: evaluate asks
        for every basket and pd.Timestamp.now() was a tenth of the loop."""
        now = time.monotonic()
        if self._horizon is None or now - self._horizon[0] > 1.0:
            self._horizon = (now, _utc() + pd.Timedelta(days=self.cfg.trading.days_till_expiry))
        return self._horizon[1]

    # ------------------------------------------------------------------ books

    def _own(self, ticker: str, side: str) -> dict[float, float]:
        out: dict[float, float] = {}
        for q in self.quotes.on(ticker, side):
            if q.remaining > 0:
                out[round(q.price, 4)] = out.get(round(q.price, 4), 0.0) + q.remaining
        return out

    def queue_wait(self, ticker: str, side: str, price: float, size: float) -> float:
        """Seconds of recent trading to fill `size` bid at `price`: other
        orders at or above it go first (joining a level puts us at its back).
        inf on a side nothing has sold into lately."""
        bk = self.feed.ladder(ticker)
        if bk is None:
            return float("inf")
        own = self._own(ticker, side)
        ahead = sum(max(0.0, q - own.get(round(p, 4), 0.0))
                    for p, q in (bk.yes_bids if side == "yes" else bk.no_bids).items() if p >= price - _EPS)
        rate = self.flow.rate(ticker, side, time.time(), self.cfg.watch.queue_window_s)
        return (ahead + size) / rate if rate > 0 else float("inf")

    def quote_score(self, b: Basket, vs) -> tuple[float, float]:
        """(expected $/h of quoting every leg, shortest leg wait in s): each
        leg's edge x size over the time trading takes to reach it."""
        t = self.cfg.trading
        need = edge_dollars(b.payout, t.min_edge + t.quote_edge_add) + \
            (t.live_edge_add if self.live_now(b, _utc()) else 0.0)
        score, fastest = 0.0, float("inf")
        px = []
        for j, leg in enumerate(b.legs):
            size = float(max(1, min(t.quote_size, int(pricing.touch_size(vs, j) + _EPS))))
            plan = pricing.quote_leg(vs, j, b.payout, need, size)
            px.append(None if plan is None else (plan.price, plan.edge, size))
            if plan is None:
                continue
            wait = self.queue_wait(leg.ticker, leg.side, plan.price, size)
            fastest = min(fastest, wait)
            if wait < float("inf"):
                score += plan.edge * size * 3600.0 / max(wait, 1.0)
        self.quote_px[b.basket_id] = px
        return score, fastest

    def note_missed(self, ticker: str, side: str, trade):
        """A print through the price we'd have quoted on a basket outside the
        hot list: what the basket cap left on the table."""
        if not self.hot_ready:
            return
        hot = set(self.hot)
        for bid in self.by_ticker.get(ticker, ()):
            px = self.quote_px.get(bid)
            b = self.baskets.get(bid)
            # Not on live or flagged baskets: a print through our price there is
            # mostly the sweep that would have picked us off, not edge missed
            # (CHACUS showed $410 of "edge" in one live game, 10-29).
            if (bid in hot or px is None or b is None or not self.may_quote(b, _utc())
                    or self.live_now(b, _utc())):
                continue
            for leg, p in zip(b.legs, px):
                if p is None or leg.ticker != ticker or leg.side != side or trade.price > p[0] + _EPS:
                    continue
                n = min(trade.size, p[2])
                m = self.missed.setdefault(bid, {"basket_id": bid, "prints": 0, "contracts": 0.0, "usd": 0.0})
                m["prints"] += 1
                m["contracts"] += n
                m["usd"] += n * p[1]
                m["last"] = _utc().isoformat()
                self.journal({"kind": "missed", "basket_id": bid, "ticker": ticker, "side": side,
                              "price": trade.price, "size": n, "quote_price": p[0], "edge": round(p[1], 4)})

    def seed_flow(self):
        """Blocking. Recent fill rates from the liquidity log, so a restart
        doesn't quote blind for a window: each window's contracts, half to
        each side (the log doesn't split them)."""
        since = time.time() - self.cfg.watch.queue_window_s
        days = {f"{pd.Timestamp(since, unit='s', tz='UTC'):%Y-%m-%d}", f"{_utc():%Y-%m-%d}"}
        for day in sorted(days):
            path = os.path.join(self.liq.dir, f"{day}.jsonl.gz")
            if not os.path.exists(path):
                continue
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    if r["t"] >= since and r["contracts"] > 0 and r["ticker"] in self.by_ticker:
                        for s in ("yes", "no"):
                            self.flow.add(r["ticker"], s, r["contracts"] / 2, r["t"])

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
        top = (book.bid, book.ask)
        if self.last_top.get(book.ticker) != top:
            self.last_top[book.ticker] = top
            self.top_moves.setdefault(book.ticker, _Rate()).add(1.0, time.time())
        self.book_seq[book.ticker] = self.book_seq.get(book.ticker, 0) + 1
        self.book_time[book.ticker] = time.monotonic()
        self.liq.on_book(book.ticker, time.monotonic(), book.ask, book.ask_depth, book.bid, book.bid_depth)
        for bid in self.by_ticker.get(book.ticker, ()):
            self.dirty.add(bid)

    def _on_trade(self, ticker, trade):
        self.liq.on_trade(ticker, trade.size)
        maker = {"yes": "no", "no": "yes"}.get(trade.taker_side)
        if maker:
            self.flow.add(ticker, maker, trade.size, time.time())
            self.note_missed(ticker, maker, trade)
        if self.live:
            return
        for q in list(self.quotes.values()):
            if (q.ticker == ticker and q.side == maker and not q.pending and q.remaining > 0
                    and trade.price <= q.price + _EPS):
                n = min(q.remaining, trade.size)
                m = self.meta[ticker]
                self._quote_fill(q, n, n * q.price + kfees.fee(q.price, n, m.maker_rate or 0.0), None, "shadow")

    # ------------------------------------------------------------------ fills

    def _book(self, basket_id: str, ticker: str, side: str, size: float, cost: float, kind: str, **kw):
        b = self.baskets[basket_id]
        if any(l.ticker == ticker and l.side != side for l in b.legs):
            self._book_unwind(b, ticker, side, size, cost, kind, **kw)
            return
        self.ledger.pos(basket_id).spec = basket_to_dict(b)
        self.ledger.record(basket_id, b.tickers, ticker, side, size, cost, time.time())
        self.journal({"kind": "fill", "fill_kind": kind, "basket_id": basket_id, "ticker": ticker, "side": side,
                      "size": size, "cost": round(cost, 4), "price": round((cost / size) if size else 0, 4), **kw})
        self.dirty.add(basket_id)
        if basket_id in self.flip:
            self.dirty.add(self.flip[basket_id])

    def _book_unwind(self, b: Basket, ticker: str, side: str, size: float, cost: float, kind: str, **kw):
        """Bought the other side of a leg we hold: Kalshi pays $1 a pair, so
        the loss is realized now and goes to the settled history."""
        now = time.time()
        basis = self.ledger.unwind(b.basket_id, b.tickers, ticker, size, now)
        row = {"basket_id": b.basket_id, "type": b.type, "settled_at": _utc().isoformat(), "bundles": 0.0,
               "payout": round(size - cost, 4), "cost": round(basis, 4), "pnl": round(size - cost - basis, 4),
               "results": {ticker: f"unwound {size:g} ({kind})"}, "live": self.live}
        self.ledger.settled.append(row)
        self.ledger.save(now)
        self.journal({"kind": "fill", "fill_kind": kind, "basket_id": b.basket_id, "ticker": ticker, "side": side,
                      "size": size, "cost": round(cost, 4), "price": round(cost / size if size else 0, 4), **kw})
        self.journal({"kind": "settled", **row})
        self.dirty.add(b.basket_id)

    def seed_exit(self, basket_id: str, planned: float):
        """Start a basket's chase history at the exit cost it was priced
        against: a fill usually lands in the same burst that moves the other
        legs, so a history begun at the fill would miss that move."""
        if basket_id not in self.exit_hist:
            self.exit_hist[basket_id] = collections.deque([(time.time(), planned)])
            self.exit_planned[basket_id] = planned

    def _quote_fill(self, q: Quote, size: float, cost: float, trade_id, source: str):
        q.filled += size
        if q.kind == "arb":
            # Priced so the other legs cost payout - edge - this leg's cost a bundle.
            m = self.meta.get(q.ticker)
            per = q.edge + pricing.maker_cost(q.price, (m.maker_rate if m else None) or 0.0)
            self.seed_exit(q.basket_id, -per * q.filled)
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
            self.ids_dirty = True

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
        """Commitment per bundle of a resting quote: payout plus what a broken
        basket's exit may move before it is taken (a cent per chase tick)."""
        return b.payout + 0.01 * self.cfg.completion.chase_ticks

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
        """Inside an eval pass the value is cached (a pass runs without
        yielding, and new_quote clears it): summing every basket's quotes per
        leg checked cost ~1 ms each at 200 quotes."""
        if self._commit_cache is not None:
            return self._commit_cache
        v = self._committed()
        if self._in_pass:
            self._commit_cache = v
        return v

    def _committed(self) -> float:
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

    def live_now(self, b: Basket, now: pd.Timestamp) -> bool:
        return b.is_live(now) or any(t in self.flagged for t in b.tickers)

    def flag_live(self, b: Basket, why: str):
        new = [t for t in b.tickers if t not in self.flagged]
        for t in new:
            self.flagged[t] = time.time()
        if new:
            self.alert("info", b.basket_id, f"treated as live after {why}: {', '.join(new)}")

    def unflag_live(self, b: Basket):
        gone = [t for t in b.tickers if self.flagged.pop(t, None) is not None]
        if gone:
            self.alert("info", b.basket_id, f"no longer treated as live after a clean fill: {', '.join(gone)}")

    def exit_need(self, b: Basket, need: float, now: pd.Timestamp) -> float:
        """Edge ($/bundle) to close held bundles through flip basket `b`:
        `need` (the arb edge) within trading.exit_ref_days of settlement,
        need x ref_days / days left beyond, never under min_exit_edge or over
        need. No settlement date counts as far out."""
        t = self.cfg.trading
        if self.live_now(b, now):
            # The same stale-leg risk as a new arb, whichever way the bundle points.
            return need
        days = None if b.settles is None else (b.settles - now).total_seconds() / 86400.0
        scaled = need if days is not None and days <= t.exit_ref_days else \
            (need * t.exit_ref_days / days if days is not None else 0.0)
        return min(need, max(t.min_exit_edge, scaled))

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
        """Runs as soon as a basket is marked: a book change, a fill, a task
        ending. A busy basket is skipped; its task re-marks it when done."""
        while True:
            await self.wake.wait()
            self.wake.clear()
            self._replay_orphans()
            todo, self.dirty = self.dirty, set()
            self._in_pass, self._commit_cache = True, None
            for bid in todo:
                if bid in self.busy:
                    continue
                b = self.baskets.get(bid)
                if b is None:
                    continue
                try:
                    self.evaluate(b)
                except Exception:
                    _log(f"eval error {bid}:")
                    traceback.print_exc()
            self._in_pass, self._commit_cache = False, None
            # Let the websocket reader run before the next pass.
            await asyncio.sleep(0)

    def evaluate(self, b: Basket):
        now = time.time()
        pos = self.ledger.baskets.get(b.basket_id)
        qs = self.basket_quotes(b.basket_id)
        live_qs = [q for q in qs if not q.pending]
        if pos is not None and pos.unhedged(b.tickers):
            self.resolve(b, pos, qs, now)
            return
        self.exit_hist.pop(b.basket_id, None)
        self.exit_planned.pop(b.basket_id, None)
        if any(q.kind != "arb" for q in live_qs):
            self.spawn(self.cancel_quotes([q for q in live_qs if q.kind != "arb"], "whole"), b.basket_id)
            return
        vs = self.views(b) if self.tradeable(b) else None
        # Priced for the ranking once per rank interval, not on every book
        # update: it was a fifth of the loop (py-spy 10-29).
        if vs is None or now - self.rank_at.get(b.basket_id, 0.0) >= self.cfg.watch.rank_interval_s:
            self.rank_at[b.basket_id] = now
            self.rank_row(b, vs)
        blocked = self.blocked()
        if vs is None or blocked or self.excluded(b):
            if live_qs:
                self.spawn(self.cancel_quotes(live_qs, blocked or ("excluded" if self.excluded(b) else "untradeable")),
                           b.basket_id)
            return
        t = self.cfg.trading
        if (t.enable_takes and (t.take_live_events or not self.live_now(b, _utc()))
                and now - self.last_take.get(b.basket_id, 0.0) >= t.take_cooldown_s):
            g = pricing.gap(vs, b.payout)
            need = edge_dollars(b.payout, t.min_edge) + (t.live_edge_add if self.live_now(b, _utc()) else 0.0)
            ex = self.exit_room(b)
            # An apparent arb on a live game is usually one leg not yet
            # repriced after a score, already being taken by faster traders.
            live_cap = t.live_quote_size if self.live_now(b, _utc()) else 10 ** 9
            exit_need = self.exit_need(b, need, _utc())
            plan = None
            if ex >= 1 and g is not None and g >= exit_need - _EPS:
                plan = pricing.take_plan(vs, b.payout, exit_need, min(ex, live_cap))
            if plan is None and g is not None and g >= need - _EPS:
                per = b.payout - g
                n = int(min(self.room_bundles(b), max(0.0, self.room_dollars()) // max(per, 0.01), live_cap))
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
        now_ts = _utc()
        want = t.enable_quoting and (b.basket_id in self.hot or ex >= 1) and self.may_quote(b, now_ts)
        live = self.live_now(b, now_ts)
        cap = float(t.live_quote_size if live else t.quote_size)
        quote_need = edge_dollars(b.payout, t.min_edge + t.quote_edge_add) + (t.live_edge_add if live else 0.0)
        reserve = self.cfg.runtime.write_reserve_tokens
        acts = []
        for j, leg in enumerate(b.legs):
            q = self.quotes.get((b.basket_id, j))
            if q is not None and q.pending:
                continue
            plan, size = None, 0.0
            # No bigger than the other legs' touch: a fill then completes at
            # the prices it was quoted against, not a level further in.
            touch = float(int(pricing.touch_size(vs, j) + _EPS))
            if want and ex >= 1:
                size = min(cap, float(ex), touch)
                if size >= 1 and (q is None or q.remaining <= size + _EPS):
                    plan = pricing.quote_leg(vs, j, b.payout, self.exit_need(b, quote_need, now_ts), size)
            elif want:
                size = min(cap, self.room_bundles(b, j), float(self.quote_size_by_dollars(b)), touch)
                if size >= 1 and (q is None or q.remaining <= size + _EPS):
                    plan = pricing.quote_leg(vs, j, b.payout, quote_need, size)
            if plan is None:
                if q is not None:
                    acts.append(self.cancel_quote(q, "no_edge" if want else "not_hot"))
                continue
            other = self.quoted_elsewhere(b.basket_id, leg.ticker, leg.side)
            if other is not None and (q is None or other.placed_at < q.placed_at):
                # One quote per market side: baskets sharing a leg would rest
                # one bid each and fill (and complete) twice on one print.
                if q is not None:
                    acts.append(self.cancel_quote(q, "duplicate"))
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

    def may_quote(self, b: Basket, now: pd.Timestamp) -> bool:
        return b.quoting_allowed(now) and (self.cfg.trading.quote_live_events or not self.live_now(b, now))

    def quoted_elsewhere(self, basket_id: str, ticker: str, side: str) -> Quote | None:
        """Another basket's arb quote resting on this market side, if any."""
        for q in self.quotes.on(ticker, side):
            if q.basket_id != basket_id and q.kind == "arb":
                return q
        return None

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
        t0 = time.time()
        fill, exc, sent = await self.gw.ioc(ticker, side, limit, size, force=True, reason=reason)
        self.order_times.append({"kind": "ioc", "reason": reason, "ticker": ticker, "sent": t0,
                                 "answered": time.time(), "order_id": None if fill is None else fill.exchange_order_id})
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
        # On a live event: stalest book first, the rest only for what it
        # filled. In a burst the leg that hasn't repriced is the one others are
        # taking: sent together, MSTOR's NO MS filled 310 while NO TOR's stale
        # 0.64 was gone (10-29 02:06, -$27.71). Elsewhere nothing moves in a
        # round trip, so every leg goes at once.
        live = self.live_now(b, _utc())
        first = min(range(len(b.legs)), key=lambda j: self.book_time.get(b.legs[j].ticker, 0.0)) if live else None
        self.n["takes"] += 1
        self.journal({"kind": "take", "basket_id": b.basket_id, "size": plan.size, "limits": plan.limits,
                      "edge": round(plan.edge, 4), "first": None if first is None else b.legs[first].ticker})
        got = [0.0] * len(b.legs)

        async def leg(j: int, size: float):
            l = b.legs[j]
            fill, exc = await self._ioc(l.ticker, l.side, plan.limits[j], size, "take", vs[j])
            if exc is not None:
                self._ioc_exc(b, l.ticker, exc, "take")
            if fill is not None and fill.size > 0:
                self._book(b.basket_id, l.ticker, l.side, fill.size, fill.price * fill.size + fill.fee, "take",
                           order_id=fill.exchange_order_id)
                got[j] = fill.size

        if first is None:
            await asyncio.gather(*(leg(j, plan.size) for j in range(len(b.legs))))
        else:
            await leg(first, plan.size)
            if got[first] > _EPS:
                await asyncio.gather(*(leg(j, got[first]) for j in range(len(b.legs)) if j != first))
        self.n["take_bundles"] += min(got)
        if min(got) > _EPS and max(got) - min(got) <= _EPS:
            self.unflag_live(b)
        if max(got) - min(got) > _EPS:
            hi = max(got)
            self.seed_exit(b.basket_id, sum((hi - g) * pricing.taker_cost(p, v.taker_rate)
                                            for g, p, v in zip(got, plan.limits, vs)) - (hi - min(got)) * b.payout)
            self.alert("warn", b.basket_id, f"take filled unevenly {dict(zip(b.tickers, got))}; completing")

    def _ioc_exc(self, b, ticker, exc, what):
        if isinstance(exc, ValueError):
            # place_ioc raises ValueError on a fill it can't read: the position
            # is unknown until the audit says otherwise.
            self.halt = f"unreadable {what} fill on {ticker}: {exc}"
            self.alert("error", b.basket_id, self.halt)
        else:
            self._order_err(b.basket_id, f"{what} IOC {ticker}", exc, ticker)

    def is_open(self, ticker: str) -> bool:
        m = self.meta.get(ticker)
        return m is not None and m.status in TRADEABLE

    def resolve(self, b: Basket, pos, qs: list[Quote], now: float):
        """A broken basket, on every change to its legs' books. Its cheaper exit
        is taken once it is chase_ticks per bundle worse than its best within
        chase_window_s (the book running away), once it is at least as good as
        planned, or at once on a live event (prices jump, they don't drift).
        Otherwise post-only bids rest on that exit's legs, re-pegged each pass:
        a fill completes or nets out below today's asks."""
        c, bid = self.cfg.completion, b.basket_id
        if any(q.pending for q in qs):
            self.later(bid, 0.05)
            return
        legs = [self.leg_view(l.ticker, l.side) if self.is_open(l.ticker) else None for l in b.legs]
        others = [self.leg_view(l.ticker, OTHER[l.side]) if self.is_open(l.ticker) else None for l in b.legs]
        ex = pricing.exits(b.payout, pos.held(b.tickers), pos.broken_spent(b.tickers), legs, others)
        pick = ex.cheaper()
        age = now - (pos.broken_since or now)
        self.completion_note[bid] = {
            "time": _utc().isoformat(), "excess": {t: e for t, e in zip(b.tickers, ex.excess) if e > _EPS},
            "complete_loss": None if ex.complete is None else round(ex.complete, 4),
            "unwind_loss": None if ex.unwind is None else round(ex.unwind, 4),
            "path": pick and pick[0]}
        if pick is None:
            self.n["exit_unpriced"] += 1
            # Once a minute per basket: every book update re-evaluates, and the
            # first second alone sent 25 (NUMOVI 10-29 15:46 ET, market closing).
            if now - self.unpriced_alert_at.get(bid, 0.0) >= 60.0:
                self.unpriced_alert_at[bid] = now
                self.alert("error", bid, f"no exit priced (a leg closed or too thin): excess "
                                         f"{self.completion_note[bid]['excess']} (broken {age:.0f}s)")
            return
        path, loss = pick
        cost = loss - pos.broken_spent(b.tickers)
        hist = self.exit_hist.setdefault(bid, collections.deque())
        hist.append((now, cost))
        while hist and hist[0][0] < now - c.chase_window_s:
            hist.popleft()
        best = min(v for _, v in hist)
        moved = c.chase_ticks * self.exit_tick(b, path, ex, legs, others) * ex.target
        # Resting only gambles for more: once the exit is at least as good as
        # the fill or take was priced at, or finishing doesn't lose, take it
        # (TXSEND-P4 rested 48 min on thin legs 10-29 with completing worth +$0.54).
        planned = self.exit_planned.get(bid)
        at_plan = loss <= _EPS or (planned is not None and cost <= planned + _EPS)
        why = ("fast move" if cost - best >= moved - _EPS else
               "at plan" if at_plan else
               "live event" if self.live_now(b, _utc()) or not b.quoting_allowed(_utc()) else None)
        self.completion_note[bid]["window_best"] = round(best, 4)
        self.completion_note[bid].update(action="take" if why else "rest", why=why)
        if why is None:
            self.rest_exit(b, path, ex, legs, others, qs, now)
        else:
            # A missed IOC leaves our book showing the level it missed until
            # Kalshi's update lands; retrying before that fires at it again.
            sig = (tuple(self.book_seq.get(t, 0) for t in b.tickers), tuple(pos.held(b.tickers)))
            if self.last_exit.get(bid) != sig:
                self.last_exit[bid] = sig
                self.spawn(self.exit_now(b, path, ex, legs, others, qs, cost, why), bid)

    def exit_tick(self, b: Basket, path: str, ex: pricing.Exits, legs, others) -> float:
        """The coarsest tick among the exit's orders at their prices: a cent,
        or 0.1c on a tapered market's tails."""
        lim = ex.complete_limits if path == "complete" else ex.unwind_limits
        ticks = [v.ticks.up(lim[j]) - lim[j] for j, _, _, _, v in self._exit_orders(b, path, ex, legs, others)
                 if v is not None and lim[j] is not None]
        return max(ticks, default=0.01)

    def _exit_orders(self, b: Basket, path: str, ex: pricing.Exits, legs, others):
        """(leg, ticker, side, size, view) per order the exit needs."""
        if path == "complete":
            return [(j, l.ticker, l.side, ex.need[j], legs[j]) for j, l in enumerate(b.legs) if ex.need[j] > _EPS]
        return [(j, l.ticker, OTHER[l.side], ex.excess[j], others[j]) for j, l in enumerate(b.legs)
                if ex.excess[j] > _EPS]

    async def exit_now(self, b: Basket, path: str, ex: pricing.Exits, legs, others, qs, cost: float, why: str):
        bid = b.basket_id
        live_qs = [q for q in qs if not q.pending]
        if live_qs:
            # A resting exit bid filling alongside these IOCs would overshoot.
            await self.cancel_quotes(live_qs, "exit")
        lim = ex.complete_limits if path == "complete" else ex.unwind_limits
        orders = [(j, t, sd, n, v, lim[j]) for j, t, sd, n, v in self._exit_orders(b, path, ex, legs, others)]
        self.n["completions" if path == "complete" else "unwinds"] += 1
        planned = self.exit_planned.get(bid)
        worse = None if planned is None else cost - planned
        self.journal({"kind": path, "basket_id": bid, "why": why, "cost": round(cost, 4),
                      "vs_plan": None if worse is None else round(worse, 4),
                      "orders": {t: [sd, n, p] for _, t, sd, n, _, p in orders}})
        if worse is not None and worse > _EPS:
            self.alert("warn", bid, f"{path} ${worse:.2f} worse than planned ({why})")
        if path == "unwind" or why == "fast move":
            self.flag_live(b, f"a {why} {path}")
        elif why == "at plan":
            self.unflag_live(b)
        held = self.hold([(t, sd, n) for _, t, sd, n, _, _ in orders])
        try:
            res = await asyncio.gather(*(self._ioc(t, sd, p, n, path, v) for _, t, sd, n, v, p in orders))
        finally:
            self.release(held)
        for (_, t, sd, _, _, _), (fill, exc) in zip(orders, res):
            if exc is not None:
                self._ioc_exc(b, t, exc, path)
            if fill is not None and fill.size > 0:
                self._book(bid, t, sd, fill.size, fill.price * fill.size + fill.fee, path,
                           order_id=fill.exchange_order_id)
        pos = self.ledger.baskets.get(bid)
        if pos is None or not pos.unhedged(b.tickers):
            self.completion_note.pop(bid, None)
            self.exit_hist.pop(bid, None)
            self.exit_planned.pop(bid, None)
            self.last_exit.pop(bid, None)
            self.journal({"kind": "completed", "basket_id": bid, "path": path})

    def rest_exit(self, b: Basket, path: str, ex: pricing.Exits, legs, others, qs, now: float):
        """Post-only bids on every order the exit needs, re-pegged one tick
        over the best bid; quotes for the other path are pulled."""
        want = {j: (t, sd, n, pricing.rest_price(v) if v is not None else None)
                for j, t, sd, n, v in self._exit_orders(b, path, ex, legs, others)}
        reserve = self.cfg.runtime.write_reserve_tokens
        acts = []
        for j, leg in enumerate(b.legs):
            q = self.quotes.get((b.basket_id, j))
            w = want.get(j)
            if w is None or w[3] is None:
                if q is not None:
                    acts.append(self.cancel_quote(q, "exit_path"))
                continue
            t, sd, n, price = w
            if q is not None and (q.kind != path or q.side != sd or abs(q.remaining - n) > _EPS):
                acts.append(self.cancel_quote(q, "exit_resize"))
            elif q is None:
                if self.live and not self.gw.can_afford(t, "order", reserve):
                    continue
                q = Quote(basket_id=b.basket_id, leg=j, ticker=t, side=sd, price=price, size=n,
                          cid=str(uuid.uuid4()), edge=0.0, max_price=price, placed_at=now, kind=path)
                self.quotes[q.key] = q
                self._commit_cache = None
                acts.append(self.place_quote(q))
            elif abs(q.price - price) > _EPS:
                acts.append(self.move_quote(q, pricing.QuotePlan(leg=j, price=price, max_price=price, edge=0.0,
                                                                 others_cost=0.0)))
        if acts:
            async def run(acts=acts):
                await asyncio.gather(*acts)
            self.spawn(run(), b.basket_id)

    def new_quote(self, b: Basket, plan: pricing.QuotePlan, size: float) -> Quote:
        leg = b.legs[plan.leg]
        q = Quote(basket_id=b.basket_id, leg=plan.leg, ticker=leg.ticker, side=leg.side, price=plan.price,
                  size=size, cid=str(uuid.uuid4()), edge=plan.edge, max_price=plan.max_price,
                  placed_at=time.time())
        self.quotes[q.key] = q
        self._commit_cache = None
        return q

    async def place_quote(self, q: Quote):
        b = self.baskets[q.basket_id]
        size = q.size
        self.charge(q.basket_id, ORDER_TOKENS)
        if not self.live:
            q.pending = False
            self.n["quotes_placed"] += 1
            self.journal({"kind": "quote", "basket_id": b.basket_id, "ticker": q.ticker, "side": q.side,
                          "price": q.price, "size": size, "edge": round(q.edge, 4)})
            return
        t0 = time.time()
        ack, exc, sent = await self.gw.rest(q.ticker, q.side, q.price, size, q.cid)
        self.order_times.append({"kind": "rest", "reason": q.kind, "ticker": q.ticker, "sent": t0,
                                 "answered": time.time(), "order_id": None if ack is None else ack.exchange_order_id})
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
            self._order_err(b.basket_id, f"quote {q.ticker}", exc, q.ticker)
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
            self.ids_dirty = True
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
        old = q.price
        if self.live:
            resp, exc, sent = await self.gw.amend(q.ticker, q.side, q.exchange_id, plan.price, q.remaining, q.filled)
            if not sent:
                self.n["moves_refused"] += 1
                if q.price > plan.max_price + _EPS:
                    # Past its edge limit it would fill at a loss; a cancel
                    # costs 2 write tokens to an amend's 10 and always goes out.
                    await self.cancel_quote(q, "move_refused")
                return
            if exc is not None:
                if http_status(exc) == 404:
                    self._retire(q)           # filled or gone; fills arrive on their own
                    return
                self._order_err(q.basket_id, f"move {q.ticker}", exc, q.ticker)
                if q.price > plan.max_price + _EPS:
                    await self.cancel_quote(q, "move_failed")
                return
            self._order_ok()
            o = (resp or {}).get("order") or {}
            new_id = o.get("order_id")
            if new_id and new_id != q.exchange_id:
                self._retire(q)
                self.quotes[q.key] = q
                self._commit_cache = None
                self._register(q, new_id)
        q.price, q.edge, q.max_price = plan.price, plan.edge, plan.max_price
        self.n["quote_moves"] += 1
        self.charge(q.basket_id, ORDER_TOKENS)
        self.journal({"kind": "move", "basket_id": q.basket_id, "ticker": q.ticker, "from": old, "to": plan.price})

    async def cancel_quote(self, q: Quote, reason: str):
        self.n["quote_cancels"] += 1
        self.charge(q.basket_id, CANCEL_TOKENS)
        if self.live and q.exchange_id:
            _, exc, _ = await self.gw.cancel(q.ticker, q.exchange_id, reason)
            if exc is not None:
                self._order_err(q.basket_id, f"cancel {q.ticker}", exc, q.ticker)
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
                    self.ids_dirty = True

    # ------------------------------------------------------------------ ranking

    def rank_row(self, b: Basket, vs):
        g = None if vs is None or self.excluded(b) else pricing.gap(vs, b.payout)
        if g is None:
            self.rank_rows.pop(b.basket_id, None)
            return
        score, wait = self.quote_score(b, vs)
        self.rank_rows[b.basket_id] = {
            "basket_id": b.basket_id, "type": b.type, "quote_usd_h": round(score, 4),
            "wait_s": None if wait == float("inf") else round(wait), "ret": round(edge_return(b.payout, g), 4),
            "gap": round(g, 4),
            "payout": b.payout, "asks": [v.best_ask for v in vs], "legs": len(b.legs),
            "depth": round(min(v.depth() for v in vs), 1)}

    def charge(self, basket_id: str, tokens: float):
        self.spend.setdefault(basket_id, _Rate()).add(tokens, time.time())

    def write_cost(self, b: Basket, now: float) -> float:
        """Write tokens/s quoting `b` takes: what its quotes spent lately, or
        if more, a move per leg each time its best bid/ask changes (at most
        one per requote_cooldown_s), plus placing every leg once per tau."""
        gap = max(self.cfg.trading.requote_cooldown_s, 0.1)
        est = sum(ORDER_TOKENS * (min(self.top_moves[l.ticker].at(now) if l.ticker in self.top_moves else 0.0,
                                      1.0 / gap) + 1.0 / WRITE_COST_TAU_S) for l in b.legs)
        spent = self.spend.get(b.basket_id)
        return max(est, spent.at(now) if spent else 0.0)

    def write_budget(self) -> float:
        cap = self.write_cap()
        return cap if cap is not None else write_budget_tokens_per_s()

    def pick_hot(self, quotable: list[str], budget: float, cost: dict[str, float]) -> list[str]:
        """The best-ranked baskets whose write cost fits `budget` (tokens/s).
        A basket already quoted keeps its place while the quoted ones ranked
        above it fit 1.1x the budget: near the cut scores sit close and
        reorder on small book changes (339 entries in 10 min for 125 slots,
        10-29), each a cancel, a new order and a lost queue place. At 1.5x the
        list planned 186 tokens/s on a 150 budget and 568 placements were
        refused in 4 min (10-29)."""
        out, used = set(), 0.0
        for bid in quotable:
            if bid in self.hot and used + cost[bid] <= 1.1 * budget:
                out.add(bid)
                used += cost[bid]
        for bid in quotable:
            if bid not in out and used + cost[bid] <= budget:
                out.add(bid)
                used += cost[bid]
        return [bid for bid in quotable if bid in out]

    async def rank_loop(self):
        """Sorts the rows evaluate() keeps fresh: re-pricing every basket here
        blocked the event loop ~200 ms per pass at 2,500 baskets."""
        while True:
            try:
                rows = sorted(self.rank_rows.values(), key=lambda r: (-r["quote_usd_h"], -r["ret"]))
                # A quote whose event just went live is pulled on its basket's next pass.
                now_ts = _utc()
                self.dirty |= {q.basket_id for q in self.quotes.values()
                               if q.basket_id in self.baskets and not self.may_quote(self.baskets[q.basket_id], now_ts)}
                self.ranking = rows
                quotable = [r["basket_id"] for r in rows if self.may_quote(self.baskets[r["basket_id"]], now_ts)]
                now = time.time()
                cost = {bid: self.write_cost(self.baskets[bid], now) for bid in quotable}
                for r in rows:
                    r["write_tps"] = round(cost.get(r["basket_id"], 0.0), 2)
                hot = self.pick_hot(quotable, self.write_budget(), cost)
                self.hot_cost = round(sum(cost[bid] for bid in hot), 1)
                if _utc() - self.started > pd.Timedelta(minutes=2):
                    self.hot_ready = True
                if hot != self.hot:
                    entered, left = set(hot) - set(self.hot), set(self.hot) - set(hot)
                    if self.hot_ready and (entered or left):
                        self.hot_churn.append((time.time(), len(entered), len(left)))
                    self.dirty |= entered | left
                    self.hot = hot
                while self.hot_churn and self.hot_churn[0][0] < time.time() - 3600:
                    self.hot_churn.popleft()
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(self.cfg.watch.rank_interval_s)

    # ------------------------------------------------------------------ guards

    async def exchange_loop(self):
        """Tracks Kalshi's exchange-wide trading switch (off in the Thursday
        maintenance window), so a pause is one alert, not one per market."""
        while True:
            try:
                st = await asyncio.get_running_loop().run_in_executor(
                    self.pool, kalshi_get, "/trade-api/v2/exchange/status")
                paused = not st.get("trading_active", True)
                if paused != self.exchange_paused:
                    self.exchange_paused = paused
                    self.alert("warn" if paused else "info", None,
                               "Kalshi trading paused exchange-wide" if paused else "Kalshi trading resumed")
                    if not paused:
                        self.paused_until.clear()
                        self.dirty |= set(self.baskets)
            except Exception as e:
                _log(f"WARN: exchange status failed ({e!r})")
            await asyncio.sleep(10.0)

    def write_cap(self) -> float | None:
        rt = self.cfg.runtime
        if rt.max_write_tokens_per_s_solo is None or self.priority_trading:
            return rt.max_write_tokens_per_s
        return rt.max_write_tokens_per_s_solo

    def series_trading(self, series: str) -> bool:
        """Live and inside its session, by its summary.json (written ~1/s)."""
        try:
            with open(os.path.join(LIVE_ROOT, series, _utc().strftime("%Y-%m-%d"), "summary.json"),
                      encoding="utf-8") as fh:
                s = json.load(fh)
            now = _utc()
            return (bool(s.get("live_trading")) and now - pd.Timestamp(s["last_update"]) < pd.Timedelta(seconds=30)
                    and pd.Timestamp(s["session_started"]) <= now < pd.Timestamp(s["close_time"]))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    async def write_share_loop(self):
        while True:
            busy = [s for s in self.cfg.runtime.write_priority_series if self.series_trading(s)]
            if busy != self.priority_trading:
                self.priority_trading = busy
                _log(f"write cap {self.write_cap()} tokens/s ({', '.join(busy) or 'no priority series'} trading)")
            await asyncio.sleep(5.0)

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
        if diff:
            # Kalshi drops a position the moment it settles; our cached status
            # can still say active until the next meta refresh (FLUINT 00:36,
            # CRBOT 02:28 on 10-29 both halted us), so re-read it first.
            await asyncio.get_running_loop().run_in_executor(self.pool, self.load_meta)
            self.settle()
            mine = self.real_ledger.net_by_ticker()
            # On a determined or settled market we book the payout when the
            # result is known, Kalshi when it pays out, in either order
            # (KENNMTU 10-29 02:52: ours at determined, Kalshi's a minute
            # later). Nothing can be traded there, so it isn't a strike.
            diff = {t: d for t, d in diff.items() if abs(real.get(t, 0.0) - mine.get(t, 0.0)) > 1e-6
                    and not (self.meta.get(t) and self.meta[t].status in SETTLED)}
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

    def sale_value(self, pos, unhedged: dict[str, float]) -> float:
        """What the contracts beyond complete bundles would sell for now: each
        at its side's best bid, less the taker fee; nothing without a bid."""
        out = 0.0
        for t, q in unhedged.items():
            v, m = self.feed.book(t), self.meta.get(t)
            if v is None:
                continue
            bid = v.bid if pos.legs[t].side == "yes" else (None if v.ask is None else 1.0 - v.ask)
            if bid is not None and bid > 0:
                out += q * bid - kfees.fee(bid, q, (m.taker_rate if m else None) or 0.0)
        return out

    def summary(self) -> dict:
        # Real positions stay on screen after a disarm; shadow ones only show
        # when there is no real money held.
        led = self.real_ledger if self.live or self.real_ledger.baskets else self.shadow_ledger
        holdings = []
        for bid, pos in led.baskets.items():
            b = self.baskets.get(bid)
            tickers = b.tickers if b else list(pos.legs)
            n = pos.bundles(tickers)
            locked = (n * b.payout - (pos.cost() - pos.broken_spent(tickers))) if b else None
            un = pos.unhedged(tickers)
            broken = self.sale_value(pos, un) - pos.broken_spent(tickers) if un else 0.0
            # Every contract held, sold at its bid now.
            curr = self.sale_value(pos, {t: q for t, q in zip(tickers, pos.held(tickers)) if q > 0}) - pos.cost()
            holdings.append({"basket_id": bid, "type": b.type if b else "", "payout": b.payout if b else None,
                             "bundles": n, "held": dict(zip(tickers, pos.held(tickers))),
                             "cost": round(pos.cost(), 4), "locked_pnl": None if locked is None else round(locked, 4),
                             "broken_pnl": round(broken, 4), "curr_pnl": round(curr, 4),
                             "unhedged": pos.unhedged(tickers),
                             "broken_s": None if pos.broken_since is None else round(time.time() - pos.broken_since),
                             "completion": self.completion_note.get(bid)})
        settled = [r for r in led.settled]
        quotes = [{"basket_id": q.basket_id, "ticker": q.ticker, "side": q.side, "price": q.price,
                   "size": q.size, "filled": q.filled, "edge": round(q.edge, 4),
                   "ret": round(edge_return(self.baskets[q.basket_id].payout, q.edge), 4), "max_price": q.max_price,
                   "age_s": round(time.time() - q.placed_at), "pending": q.pending, "order_id": q.exchange_id,
                   "kind": q.kind}
                  for q in self.quotes.values()]
        return {
            "series": self.S, "event_ticker": "ARBS", "last_update": _utc().isoformat(),
            "session_started": self.started.isoformat(), "live_trading": self.live, "arm_requested": self.arm_want,
            "ledger": "real" if led is self.real_ledger else "shadow",
            "halted": self.halt is not None, "halt_reason": self.halt, "blocked_reason": self.blocked(),
            "feed": {"up": self.feed.up, "reconnects": self.feed.n_reconnects, "seq_gaps": self.feed.n_seq_gaps,
                     "ws_errors": self.feed.n_errors, "refreshes": self.feed.n_refreshes, "last_msg_s": self.feed.seconds_since_message()},
            "n_baskets": len(self.all_baskets), "n_tickers": len(self.tickers),
            "n_tradeable": sum(1 for b in self.all_baskets if self.tradeable(b)),
            "market_status": _count(m.status for m in self.meta.values()),
            "pnl": {"settled": round(sum(r["pnl"] for r in settled), 4),
                    "locked": round(sum(h["locked_pnl"] or 0.0 for h in holdings), 4),
                    "broken": round(sum(h["broken_pnl"] for h in holdings), 4),
                    "held_cost": round(sum(h["cost"] for h in holdings), 4),
                    "committed": round(self.committed(), 4), "max_dollars": self.cfg.trading.max_dollars},
            "holdings": holdings, "quotes": quotes, "hot": self.hot, "ranking": self.ranking[:60],
            "hot_churn": {f"{m}m": sum(e for t, e, _ in self.hot_churn if t >= time.time() - 60 * m)
                          for m in (1, 10, 60)},
            "hot_cost_tps": self.hot_cost, "write_budget_tps": self.write_budget(),
            "flagged_live": {t: datetime_iso(ts) for t, ts in self.flagged.items()},
            "missed": sorted(({**m, "usd": round(m["usd"], 2)} for m in self.missed.values()),
                             key=lambda m: -m["usd"])[:50],
            "settled": settled[-200:], "alerts": self.alerts[-100:], "audit_diff": self.audit_diff,
            "order_errors": self.order_errors, "counts": self.n, "write_usage": self.gw.usage(), "write_cap": self.write_cap(),
            "config_status": self.store.status(), "scan": self.scan_status,
        }

    async def ledger_flush_loop(self):
        while True:
            await asyncio.sleep(1.0)
            if self.ids_dirty:
                self.ids_dirty = False
                try:
                    self.real_ledger.save_quote_ids(time.time())
                except Exception as e:
                    _log(f"WARN: quote id save failed ({e!r})")

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
                                       self.write_cap)
        except Exception as e:
            self.alert("error", None, f"no order clients ({e!r}); shadow only")
        self.settle()
        try:
            await loop.run_in_executor(self.pool, self.seed_flow)
        except Exception as e:
            _log(f"WARN: fill-rate seed failed ({e!r})")
        trading, stopper, stop = TradingModeWatcher(self.S), StopWatcher(), asyncio.Event()
        coros = [self.feed.run(), self.eval_loop(), self.rank_loop(), self.meta_loop(), self.fill_poll_loop(),
                 self.audit_loop(), self.guard_loop(), self.summary_loop(), self.scan_loop(),
                 self.liquidity_loop(), self.exchange_loop(), self.write_share_loop(), self.ledger_flush_loop(),
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
            self.real_ledger.save_quote_ids(time.time())
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


def datetime_iso(ts: float) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


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
