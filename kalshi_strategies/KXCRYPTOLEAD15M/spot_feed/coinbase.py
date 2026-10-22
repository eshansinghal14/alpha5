"""
Coinbase Advanced Trade `level2` adapter.

Phase 1 of the spec: ONE venue, every coin. Public endpoint
`wss://advanced-trade-ws.coinbase.com`, `level2` channel. No auth is
required for market data (Coinbase recommends authenticating with a CDP
key for connection stability but accepts anonymous market-data
subscriptions). This adapter supports both -- `cdp_api_key_name` /
`cdp_api_key_private` are optional constructor args.

Protocol summary (public docs):
  - Subscribe:  {"type":"subscribe","channel":"level2",
                 "product_ids":["BTC-USD",...]}
  - First message per product is `type:"snapshot"` with full
    `pricebook.bids`/`asks` arrays.
  - Then `type:"update"` with `changes:[{side,price_level,new_quantity}]`.
    `new_quantity == 0` means remove the level.
  - Each message carries `sequence_num` -- strictly monotonic PER
    CONNECTION, not per product. A jump -> resync (reconnect).

Reconnect strategy: on `OSError` / `ConnectionClosed` / sequence gap,
wait RECONNECT_DELAY_S then reopen. On reopen we lose all books and get a
fresh snapshot -- during the gap `latest()` returns a VenueBook flagged
`is_resyncing=True` with the stale levels still populated, so a caller
inspecting the ladder can see what it used to look like.

Logging convention: `print("[spot/coinbase] ...")` for operational events
(connect, reconnect, gap), matching the project's `print("[kalshi-ws]...")`
style in kalshi_infra/kalshi/ws_feed.py. Not using stdlib logging because this adapter
runs inside the same asyncio event loop as the Kalshi feed, and consistent
operator-visible output beats structured logging that lands in a separate
file.
"""
from __future__ import annotations

import asyncio
import collections
import json
import time
from typing import Callable, Awaitable

import websockets
from sortedcontainers import SortedDict

from .base import VenueBook, VenueAdapter, OrderBookLevel
from ..series import COINBASE_PRODUCTS, DEPTH_PRICED

EMIT_REFRESH_S = 0.5

WS_URL = "wss://advanced-trade-ws.coinbase.com"
# Coinbase Exchange public feed: same matching engine, delivered sooner. Same
# trade ids on both, us-east-2 box 10-20 02:27 UTC, 5 min: Coinbase trade time
# -> receipt p50 SOL 10ms / BTC 12 / HYPE 9 here vs 69 / 70 / 56 on Advanced
# Trade, i.e. each trade ~55ms sooner (p10 17ms, p90 98ms). Its book channels
# (level2_batch) were no faster than Advanced Trade's level2, and full level2
# needs auth, so the book stays on Advanced Trade; this feed carries prints
# (matches), which drive the held through-the-touch print and on_print.
EX_WS_URL = "wss://ws-feed.exchange.coinbase.com"
# Book from the Exchange feed's level2_batch (top 50 levels) while it is
# synced, Advanced Trade level2 as the fallback; plus its ticker. Advanced
# Trade level2 is batched too (~50ms) and later: same best-price change, us-east-2
# 10-21 17:51-18:22 UTC, ~19k changes, Exchange first on 75%, p50 20ms sooner
# (BTC 21, ETH 24, XRP 20, HYPE 14, SOL 11); research/latency/cb_book_probe.py.
# The 10-20 trial that turned this off read Exchange's batch age as queueing
# (see EXB_BATCH_S), which Advanced Trade's send-time stamp never showed.
EX_BOOK = True
# Exchange l2update `time` is the batch's content time, so (wall - time) above
# its rolling minimum is batch age plus our queueing; Advanced Trade's message
# `timestamp` is its send time (a flat ~7ms before receipt), so only queueing
# shows there. Excess up to this much is batching, not counted as queueing, so
# both books are aged alike. 10-21 probe: Exchange time -> receipt p10 8ms,
# p99 63-70ms, batches every ~60ms.
EXB_BATCH_S = 0.06
# How far a coin's l2update `time` can trail the newest exchange time already
# seen on the book connection: a coin with no newer message is taken as
# complete up to (connection clock - this). 10-21 20:04-20:24 UTC probe
# (cb_book_probe.py live, section 3): p99 44ms, p99.9 55ms, max 131ms.
EX_CLOCK_LAG_S = 0.06
# Same for the Advanced Trade fallback book, on its update event_times. Not
# measured; set wide since that book is only used while Exchange is down.
AT_CLOCK_LAG_S = 0.1
RECONNECT_DELAY_S = 1.0
# The heartbeats channel sends one message per second on the connection,
# so a silent socket is dead well before this.
RECV_TIMEOUT_S = 5.0
# Coinbase ships the FULL level2 book in the first `snapshot` message per
# product. For a 5-product subscription including BTC-USD, that first
# frame has been observed at ~1.03 MB (2026-10-06) -- over the
# `websockets` library default max_size of 1 MiB, which closes the
# socket with code 1009 before the snapshot is parsed. 8 MiB covers the
# observed size with headroom for both deeper books (HYPE listings expand
# over time) and a worst-case all-5-products batched snapshot after a
# resync.
MAX_FRAME_SIZE = 8 * 1024 * 1024

# Depth cap on the emitted VenueBook. Coinbase `level2` ships the ENTIRE
# book -- 24k bids / 18k asks on BTC (2026-10-07) -- but the only
# downstream consumers are the CF composite (whose utilized-depth region
# is a few hundred levels either side of the touch -- 416/241 within 0.5%
# of spot on that same BTC book) and the feed-health BBO check. Sorting
# and depth-weighting the full book cost ~180 ms PER MESSAGE at ~13 msg/s
# per product, which pegged the asyncio loop (ping timeouts, reconnect
# storms, DOWN flapping -- the "no decisions" bug). 1000 levels/side is
# ~2x the deepest utilized region seen and keeps to_venue_book under a few
# ms. The raw dict still holds the full book, so a later genuine update at
# a pruned-away price still lands; only the emitted VIEW is capped.
MAX_BOOK_LEVELS = 1000
# Levels per side in the per-message view (mid, health, cross pruning).
PER_MSG_LEVELS = 25
# Window for the clock-offset minimum in CoinbaseAdapter._data_age.
OFFSET_WINDOW_S = 120.0
# Longest a through-the-touch print stands in for the mid if the book never
# reaches it. Not calibrated: a guess at "longer than level2 lags a sweep".
TRADE_HOLD_S = 1.0


class _ProductBook:
    """Single-product L2 state. Dict-backed for O(1) level updates; a
    sorted snapshot is materialized only on `to_venue_book()`. Deliberately
    dumb -- the composite pipeline is what cares about crossed/one-sided
    books; this adapter just reports what the venue said."""

    __slots__ = ("bids", "asks", "last_receive_ts", "last_exchange_ts",
                 "last_seq", "is_resyncing", "last_wire_ts", "trade_px", "trade_dir", "trade_ts",
                 "content_ts", "ovr", "ovr_mono", "data_ts")

    def __init__(self):
        # Price-sorted dicts: best bid = bids.peekitem(-1), best ask =
        # asks.peekitem(0), and the top-K view is an O(K) slice instead of
        # an O(n) scan of a 24k-level book on every message. The scan was
        # ~5-8ms/message on BTC and, at ~85 msg/s, most of a CPU core.
        self.bids: SortedDict = SortedDict()
        self.asks: SortedDict = SortedDict()
        self.last_receive_ts: float = 0.0
        self.last_exchange_ts: float | None = None
        self.last_seq: int | None = None
        self.is_resyncing: bool = True   # until first snapshot
        # Monotonic time the websocket frame carrying the newest applied
        # change was RECEIVED (before parsing/queueing). Staleness is
        # measured from this, not from when we got round to applying it.
        self.last_wire_ts: float = 0.0
        # Last trade that printed through the touch (+1 above the ask, -1
        # below the bid) and when; cleared once the book catches up to it.
        self.trade_px: float | None = None
        self.trade_dir: int = 0
        self.trade_ts: float = 0.0
        # Exchange time (epoch s) of the newest change applied to the levels.
        self.content_ts: float | None = None
        # A touch newer than the levels (ticker): (exch_ts, bid, bid_sz, ask,
        # ask_sz), and when it was set (monotonic). Cleared by the first book
        # change at or after its time.
        self.ovr: tuple | None = None
        self.ovr_mono: float = 0.0
        # Exchange time (epoch s) the emitted view is known complete up to:
        # what the signal age is measured from (see CoinbaseAdapter._known).
        self.data_ts: float | None = None

    def clear(self):
        self.bids.clear()
        self.asks.clear()
        self.is_resyncing = True
        self.trade_px = None
        self.content_ts = None
        self.ovr = None
        self.data_ts = None

    def book_time(self, ts: float | None):
        """The levels now include every change up to exchange time `ts`."""
        if ts is None:
            return
        if self.content_ts is None or ts > self.content_ts:
            self.content_ts = ts
        if self.ovr is not None and ts >= self.ovr[0]:
            self.ovr = None

    def set_override(self, ts: float, bid: float, bid_sz: float, ask: float, ask_sz: float, now: float):
        self.ovr = (ts, bid, bid_sz, ask, ask_sz)
        self.ovr_mono = now

    def live_override(self) -> tuple | None:
        """The ticker touch while it is newer than the levels. Expires after
        TRADE_HOLD_S in case no later book change ever arrives."""
        o = self.ovr
        if o is not None and time.monotonic() - self.ovr_mono > TRADE_HOLD_S:
            self.ovr = o = None
        return o

    def _touch(self) -> tuple[float | None, float | None]:
        o = self.live_override()
        if o is not None:
            return o[1], o[3]
        return (self.bids.peekitem(-1)[0] if self.bids else None,
                self.asks.peekitem(0)[0] if self.asks else None)

    def apply_trade(self, price: float, now: float):
        """A print outside the bid/ask means the book is behind the matching
        engine; hold the print as the mid until the book reaches it. A print
        inside the spread says the book is current."""
        bb, ba = self._touch()
        if ba is not None and price > ba:
            self.trade_px, self.trade_dir, self.trade_ts = price, 1, now
        elif bb is not None and price < bb:
            self.trade_px, self.trade_dir, self.trade_ts = price, -1, now
        else:
            self.trade_px = None

    def settle_trade(self, now: float):
        """Drop the held print once the book has moved to it, or after
        TRADE_HOLD_S in case the book never does (a one-off print)."""
        if self.trade_px is None:
            return
        bb, ba = self._touch()
        caught_up = ((self.trade_dir > 0 and ba is not None and ba >= self.trade_px)
                     or (self.trade_dir < 0 and bb is not None and bb <= self.trade_px))
        if caught_up or now - self.trade_ts > TRADE_HOLD_S:
            self.trade_px = None

    def apply_snapshot(self, bids: list[tuple[float, float]],
                       asks: list[tuple[float, float]]):
        self.bids = SortedDict((p, q) for p, q in bids if q > 0)
        self.asks = SortedDict((p, q) for p, q in asks if q > 0)
        self.is_resyncing = False

    def apply_change(self, side: str, price: float, new_qty: float):
        book = self.bids if side == "bid" else self.asks
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def to_venue_book(self, venue: str, product: str, coin: str,
                      max_levels: int = MAX_BOOK_LEVELS) -> VenueBook:
        # Top `max_levels` per side straight off the sorted keys.
        nb = len(self.bids)
        bids_sorted = [(p, self.bids[p]) for p in
                       self.bids.islice(max(0, nb - max_levels), nb, reverse=True)]
        asks_sorted = [(p, self.asks[p]) for p in self.asks.islice(0, max_levels)]
        o = self.live_override()
        if o is not None:
            _, tb, tbs, ta, tas = o
            bids_sorted = [(tb, tbs)] + [x for x in bids_sorted if x[0] < tb]
            asks_sorted = [(ta, tas)] + [x for x in asks_sorted if x[0] > ta]
        # Self-heal phantom crossed levels. Coinbase `level2` sends
        # ABSOLUTE sizes and explicit new_quantity=0 removals; if we miss
        # even one removal (a dropped frame on a seq gap), a stale level
        # sits on the wrong side of the touch and -- because sizes are
        # absolute, not additive -- it NEVER clears until that exact
        # price trades again, which can be minutes. That phantom crosses
        # the book and compute_cf_spot used to blank the whole coin for
        # the duration (observed 2026-10-06: BTC composite gaps of
        # 230-278s while Coinbase itself was sending 13 msgs/s with max
        # gap 0.5s -- the silence was entirely ours).
        #
        # We only need a clean BBO + nearby depth for the composite, so
        # drop any bid >= best ask and any ask <= best bid before
        # emitting. Prune AGAINST THE OTHER SIDE'S BEST, dropping the
        # fewest, highest-suspicion levels. This is purely a read-time
        # view fix; the raw dict keeps whatever Coinbase sent so a later
        # genuine update at a pruned price still lands.
        # Walk both tops inward while they cross, dropping the SMALLER of
        # the two touching levels each step. A phantom left by a missed
        # removal is almost always a lone tiny resting size (the real
        # liquidity moved on), so dropping the smaller crossing level
        # removes the phantom and leaves the genuine touch -- the same
        # rule exchanges use to reconcile a crossed book, and the same
        # one cf_rate._uncross applies to the consolidated book. One or
        # two steps clears a single phantom; a pathological book just
        # reports a touch or two thinner, which the composite tolerates.
        bi = ai = 0
        while (bi < len(bids_sorted) and ai < len(asks_sorted)
               and bids_sorted[bi][0] >= asks_sorted[ai][0]):
            if bids_sorted[bi][1] <= asks_sorted[ai][1]:
                bi += 1   # drop the crossing bid (smaller or equal)
            else:
                ai += 1   # drop the crossing ask
        bids_sorted = bids_sorted[bi:]
        asks_sorted = asks_sorted[ai:]
        bids = tuple(OrderBookLevel(p, q) for p, q in bids_sorted)
        asks = tuple(OrderBookLevel(p, q) for p, q in asks_sorted)
        return VenueBook(
            venue=venue, product=product, coin=coin,
            bids=bids, asks=asks,
            receive_ts=self.last_receive_ts,
            exchange_ts=self.last_exchange_ts,
            is_resyncing=self.is_resyncing,
            seq=self.last_seq,
            trade_px=self.trade_px,
            data_ts=self.data_ts,
        )


class CoinbaseAdapter(VenueAdapter):
    venue = "coinbase"

    def __init__(self, coins: list[str] | None = None):
        self.coins = list(coins) if coins is not None else list(COINBASE_PRODUCTS.keys())
        self._coin_by_product = {COINBASE_PRODUCTS[c]: c for c in self.coins}
        self._books: dict[str, _ProductBook] = {c: _ProductBook() for c in self.coins}
        # Last message receive time regardless of product -- used as the
        # connection liveness clock. A per-product receive_ts can lag
        # arbitrarily on quiet products (HYPE), so a connection-level timer
        # is what detects a dead socket.
        self._last_any_msg_ts: float = 0.0
        # Rolling minimum of (local wall clock - Coinbase message timestamp)
        # over the last OFFSET_WINDOW_S. That minimum is network latency +
        # clock skew (this box ran 0.9s slow on 2026-10-08), so
        # (wall - exch - offset_min) is how long a message sat in OUR
        # buffers. See _data_age.
        # One window per feed: the Exchange feed's offsets run ~55ms lower, and
        # a shared minimum would age every Advanced Trade book by that much.
        # The Exchange book ("exb", level2_batch) gets its own too: against the
        # matches minimum its batching read as up to ~60ms of queueing.
        self._offsets: dict[str, collections.deque[tuple[float, float]]] = {
            "at": collections.deque(), "ex": collections.deque(), "exb": collections.deque(),
            "ext": collections.deque()}
        # Last view handed out per coin; its receive_ts is advanced in place
        # by _mark_fresh, since the composite holds on to these objects.
        self._views: dict[str, VenueBook] = {}
        # Optional: callable(coin, trade_dict, as_of, exch_ts) for every live
        # print (not the subscribe snapshot). Sync; must be cheap.
        self.on_print = None
        # Prints come from the Exchange feed while it is connected (it is
        # ~55ms sooner); Advanced Trade's market_trades are then ignored so
        # nothing is counted twice. Falls back to them if the Exchange feed drops.
        self.ex_live = False
        self._ex_seen: collections.deque = collections.deque(maxlen=4096)
        self._ex_seen_set: set = set()
        # Exchange level2_batch books (top 50 levels). While one is synced and
        # its connection is up, views are built from it instead of the
        # Advanced Trade book, which keeps running as the fallback.
        self._ex_books: dict[str, _ProductBook] = {c: _ProductBook() for c in self.coins}
        self.ex_book_live = False
        # Newest exchange time seen on each book connection (epoch s).
        self._ex_clock = 0.0
        self._at_clock = 0.0
        # coin -> (touch key, monotonic, source book) of the last view emitted.
        self._last_emit: dict[str, tuple] = {}

    def _known_ts(self, coin: str, pb: _ProductBook) -> float | None:
        """Exchange time `pb` is known complete up to: its newest applied
        change, a newer ticker touch, or its connection's clock less the
        most a pending update for it can trail that clock."""
        if self._ex_books.get(coin) is pb:
            clock, lag = self._ex_clock, EX_CLOCK_LAG_S
        else:
            clock, lag = self._at_clock, AT_CLOCK_LAG_S
        o = pb.live_override()
        vals = [v for v in (pb.content_ts, o[0] if o is not None else None,
                            clock - lag if clock > 0 else None) if v is not None]
        return max(vals) if vals else None

    def _refresh_known(self, books: dict):
        """Advance data_ts on every synced book and the view handed out for it."""
        for coin, pb in books.items():
            if pb.is_resyncing:
                continue
            pb.data_ts = self._known_ts(coin, pb)
            vb = self._views.get(coin)
            if vb is not None and self._src(coin) is pb:
                vb.data_ts = pb.data_ts

    def _src(self, coin: str) -> _ProductBook:
        """The book views are built from: Exchange when usable, else Advanced Trade."""
        if EX_BOOK and self.ex_book_live:
            eb = self._ex_books[coin]
            if not eb.is_resyncing and eb.last_receive_ts > 0.0:
                return eb
        return self._books[coin]

    def latest(self, coin: str) -> VenueBook | None:
        if coin not in self._books:
            return None
        pb = self._src(coin)
        if pb.last_receive_ts == 0.0:
            return None
        vb = pb.to_venue_book(self.venue, COINBASE_PRODUCTS[coin], coin)
        self._views[coin] = vb
        return vb

    def _mark_fresh(self, as_of: float):
        """Every product shares one in-order connection, so a message on
        ANY of them proves every synced book is current as of `as_of`. A
        per-product clock read a quiet HYPE book (>1s between changes, ~4x
        a minute on 2026-10-09) as stale and tripped the cross-coin skew
        withhold while its book was in fact current."""
        self._mark_books_fresh(self._books, as_of, not (EX_BOOK and self.ex_book_live))

    def _mark_books_fresh(self, books: dict, as_of: float, owns_views: bool):
        """`owns_views`: these books are what the emitted views come from, so
        the views' freshness follows this connection."""
        for coin, pb in books.items():
            if pb.is_resyncing or pb.last_receive_ts == 0.0:
                continue
            pb.last_receive_ts = as_of
            vb = self._views.get(coin)
            if vb is not None and (owns_views or self._src(coin) is pb):
                vb.receive_ts = as_of

    def deep_book(self, coin: str) -> VenueBook | None:
        """Full MAX_BOOK_LEVELS view for the once-a-second CF replica."""
        return self.latest(coin)

    async def run(self, on_update: Callable[[VenueBook], Awaitable[None] | None]):
        tasks = [self._run_at(on_update), self._run_ex_trades(on_update)]
        if EX_BOOK:
            tasks.append(self._run_ex_book(on_update))
        await asyncio.gather(*tasks)

    async def _ex_socket(self, channels, on_msg, on_up, on_down):
        product_ids = [COINBASE_PRODUCTS[c] for c in self.coins]
        name = " + ".join(channels)
        while True:
            try:
                async with websockets.connect(EX_WS_URL, open_timeout=15, ping_interval=20,
                                              ping_timeout=15, max_size=MAX_FRAME_SIZE) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": product_ids,
                                              "channels": channels}))
                    print(f"[spot/coinbase] exchange feed: {name} to {product_ids}")
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            print(f"[spot/coinbase] exchange {name} recv timeout -- reconnecting")
                            break
                        d = json.loads(raw)
                        ty = d.get("type")
                        if ty == "subscriptions":
                            on_up()
                            continue
                        if ty == "error":
                            print(f"[spot/coinbase] exchange feed error: {d.get('message')} {d.get('reason')}")
                            break
                        coin = self._coin_by_product.get(d.get("product_id"))
                        if coin is None:
                            continue
                        exch_ts = _parse_iso_ts(d["time"]) if d.get("time") else None
                        await on_msg(ty, coin, d, exch_ts, time.monotonic())
            except (OSError, websockets.ConnectionClosed) as e:
                print(f"[spot/coinbase] exchange {name} connection error ({e!r}); retrying in {RECONNECT_DELAY_S}s")
            except Exception as e:
                print(f"[spot/coinbase] exchange {name} unexpected error: {e!r}; reconnecting")
            on_down()
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _run_ex_trades(self, on_update):
        """Exchange `matches` on its own socket: sharing one with level2_batch
        delayed each print p50 1.9ms / p99 18ms (10-21 20:04-20:24 UTC probe)."""
        async def on_msg(ty, coin, d, exch_ts, mono):
            if ty == "match":
                await self._ex_match(coin, d, mono - self._data_age(exch_ts, mono, "ex"), exch_ts, on_update)

        def up():
            self.ex_live = True

        def down():
            self.ex_live = False
        await self._ex_socket(["matches", "heartbeat"], on_msg, up, down)

    async def _run_ex_book(self, on_update):
        """level2_batch (top-50 book, the view source while synced) and ticker
        (the touch after each match) on one in-order socket, so their exchange
        times are comparable: whichever is newer sets the touch."""
        async def on_msg(ty, coin, d, exch_ts, mono):
            if exch_ts is not None and exch_ts > self._ex_clock:
                self._ex_clock = exch_ts
            as_of = mono
            if ty == "snapshot":
                self._ex_snapshot(coin, d, mono, exch_ts)
            elif ty == "l2update":
                as_of = mono - self._data_age(exch_ts, mono, "exb", EXB_BATCH_S)
                await self._ex_l2update(coin, d, as_of, exch_ts, on_update)
            elif ty == "ticker":
                as_of = mono - self._data_age(exch_ts, mono, "ext")
                await self._ex_ticker(coin, d, as_of, exch_ts, on_update)
            elif ty != "heartbeat":
                return
            # One in-order connection: any message proves every synced
            # Exchange book current (see _mark_fresh).
            self._mark_books_fresh(self._ex_books, as_of, True)
            self._refresh_known(self._ex_books)

        def up():
            self.ex_book_live = True

        def down():
            self.ex_book_live = False
            self._ex_clock = 0.0
            for eb in self._ex_books.values():
                eb.clear()
                eb.last_receive_ts = 0.0
        await self._ex_socket(["heartbeat", "level2_batch", "ticker"], on_msg, up, down)

    def _ex_snapshot(self, coin, d, mono, exch_ts=None):
        eb = self._ex_books[coin]
        eb.clear()
        eb.apply_snapshot([(float(p), float(q)) for p, q in d.get("bids", [])],
                          [(float(p), float(q)) for p, q in d.get("asks", [])])
        eb.book_time(exch_ts)
        eb.last_receive_ts = mono

    async def _ex_l2update(self, coin, d, as_of, exch_ts, on_update):
        eb = self._ex_books[coin]
        if eb.is_resyncing:
            return
        for side, p, q in d.get("changes", []):
            eb.apply_change("bid" if side == "buy" else "ask", float(p), float(q))
        # A batch at or after a ticker's time already includes that touch
        # (never earlier in 2,422 matched touches, 10-21 probe), so it ends the override.
        eb.book_time(exch_ts)
        eb.settle_trade(as_of)
        eb.last_exchange_ts = exch_ts
        eb.last_receive_ts = as_of
        if self._src(coin) is eb:
            await self._emit(coin, on_update)

    async def _ex_ticker(self, coin, d, as_of, exch_ts, on_update):
        """The touch right after a match. It sets the book's best bid/ask while
        it is newer than the last level2_batch change applied: it showed a
        best-price change first on 16% of the book's changes (p50 43ms sooner),
        and 70% of its touches never appear in a batch at all (10-21 probe)."""
        eb = self._ex_books[coin]
        if eb.is_resyncing or exch_ts is None or (eb.content_ts is not None and exch_ts <= eb.content_ts):
            return
        try:
            bb, ba = float(d["best_bid"]), float(d["best_ask"])
        except (KeyError, TypeError, ValueError):
            return
        if not 0 < bb < ba:
            return
        bs = float(d.get("best_bid_size") or eb.bids.get(bb) or 1e-9)
        asz = float(d.get("best_ask_size") or eb.asks.get(ba) or 1e-9)
        before = eb._touch()
        eb.set_override(exch_ts, bb, bs, ba, asz, time.monotonic())
        eb.settle_trade(as_of)
        eb.last_exchange_ts = exch_ts
        if self._src(coin) is eb and eb._touch() != before:
            await self._emit(coin, on_update)

    def _new_trade(self, coin: str, tid) -> bool:
        k = (coin, str(tid))
        if k in self._ex_seen_set:
            return False
        if len(self._ex_seen) == self._ex_seen.maxlen:
            self._ex_seen_set.discard(self._ex_seen[0])
        self._ex_seen.append(k)
        self._ex_seen_set.add(k)
        return True

    async def _ex_match(self, coin, d, as_of, exch_ts, on_update):
        if not self._new_trade(coin, d.get("trade_id")):
            return
        # Advanced Trade's market_trades shape, so on_print consumers are unchanged
        # (side is the taker's: BUY/SELL on both feeds for the same trade id).
        t = {"product_id": d["product_id"], "trade_id": d.get("trade_id"), "price": d["price"],
             "size": d["size"], "side": str(d.get("side", "")).upper(), "time": d.get("time")}
        if self.on_print is not None:
            self.on_print(coin, t, as_of, exch_ts)
        await self._held(coin, float(t["price"]), as_of, on_update)

    async def _held(self, coin, price: float, as_of: float, on_update):
        """A price newer than the book (a print, or the ticker's touch) held
        on whichever book views come from, until that book reaches it."""
        pb = self._src(coin)
        if pb.is_resyncing or pb.last_receive_ts == 0.0:
            return
        before = pb.trade_px
        pb.apply_trade(price, as_of)
        if pb.trade_px != before:
            await self._emit(coin, on_update)

    async def _emit(self, coin, on_update):
        pb = self._src(coin)
        pb.data_ts = self._known_ts(coin, pb)
        # Touch-priced coins only read the mid off a view, so a batch that moved
        # no best price and no held print is skipped (the held view's freshness
        # and data_ts advance in place). Building and pricing the view was 72%
        # of the Exchange book socket's loop time (py-spy 10-22 02:23 UTC).
        # Depth levels refresh at least every EMIT_REFRESH_S for the 1s bars.
        key = (*pb._touch(), pb.trade_px, pb.is_resyncing)
        now = time.monotonic()
        last = self._last_emit.get(coin)
        if (coin not in DEPTH_PRICED and last is not None and last[0] == key and last[2] is pb
                and now - last[1] < EMIT_REFRESH_S):
            return
        self._last_emit[coin] = (key, now, pb)
        vb = pb.to_venue_book(self.venue, COINBASE_PRODUCTS[coin], coin, max_levels=PER_MSG_LEVELS)
        self._views[coin] = vb
        result = on_update(vb)
        if asyncio.iscoroutine(result):
            await result

    async def _run_at(self, on_update: Callable[[VenueBook], Awaitable[None] | None]):
        product_ids = [COINBASE_PRODUCTS[c] for c in self.coins]
        while True:
            try:
                async with websockets.connect(WS_URL, open_timeout=15,
                                              ping_interval=20, ping_timeout=15,
                                              max_size=MAX_FRAME_SIZE) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": "level2",
                        "product_ids": product_ids,
                    }))
                    # 1/s on the same connection: proves liveness when no
                    # book changes (see _mark_fresh).
                    await ws.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
                    # Trades: in a sweep, prints arrive before level2 has
                    # removed the swept levels (see _ProductBook.apply_trade).
                    await ws.send(json.dumps({"type": "subscribe", "channel": "market_trades",
                                              "product_ids": product_ids}))
                    print(f"[spot/coinbase] subscribed level2 + market_trades + heartbeats to {product_ids}")
                    # Sequence tracking. The level2 channel's own docs
                    # ("guarantees delivery of all updates") PLUS the
                    # overview's warning that TCP can still drop frames
                    # ("WebSocket servers receive market data in a
                    # manner that can result in dropped messages")
                    # together say: gaps are rare but DO happen, and the
                    # protocol self-heals via the per-level `new_quantity`
                    # (not a delta -- an absolute size), so a missed
                    # message just means we have a stale level until the
                    # next update replaces it. Full reconnect on every
                    # gap costs a ~1 MB snapshot per product and starves
                    # the composite for 1-2s on an event timed in minutes;
                    # strictly worse than the stale level.
                    #
                    # Policy: track seq per connection (schema says it's
                    # per-connection), log and keep going on gaps,
                    # reconnect only on a timeout or a hard error.
                    # Observed 2026-10-06: 22 -> 24 right after subscribe,
                    # likely one setup message dropped; the stream
                    # recovered inside a second on its own.
                    last_seq: int | None = None
                    gaps_logged = 0
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            print("[spot/coinbase] recv timeout -- reconnecting")
                            break
                        now = time.monotonic()
                        self._last_any_msg_ts = now
                        d = json.loads(raw)
                        # The counter spans every channel on the connection
                        # (subscriptions, heartbeats, l2_data), so it is
                        # checked before filtering; skipping non-l2 messages
                        # first logged a false gap after every one of them.
                        seq = d.get("sequence_num")
                        if seq is not None and last_seq is not None:
                            if seq <= last_seq:
                                # Out-of-order; per the spec these can
                                # be dropped. Rare.
                                continue
                            if seq != last_seq + 1:
                                if gaps_logged < 5:
                                    print(f"[spot/coinbase] seq gap {last_seq} -> {seq} "
                                          f"(missed {seq - last_seq - 1}); continuing")
                                    gaps_logged += 1
                        if seq is not None:
                            last_seq = seq
                        channel = d.get("channel")
                        if channel not in ("l2_data", "heartbeats", "market_trades"):
                            continue
                        exch_ts = _parse_iso_ts(d["timestamp"]) if d.get("timestamp") else None
                        mono = time.monotonic()
                        as_of = mono - self._data_age(exch_ts, mono)
                        if channel == "l2_data":
                            for event in d.get("events", []) or []:
                                await self._handle_event(event, as_of, exch_ts, seq, on_update)
                        elif channel == "market_trades":
                            for event in d.get("events", []) or []:
                                await self._handle_trades(event, as_of, on_update, exch_ts)
                        self._mark_fresh(as_of)
                        self._refresh_known(self._books)
            except (OSError, websockets.ConnectionClosed) as e:
                print(f"[spot/coinbase] connection error ({e!r}); retrying in {RECONNECT_DELAY_S}s")
            except Exception as e:
                # Don't let one bad message take the adapter down -- but DO
                # surface it. Reconnect loops are quiet by design in the
                # happy path, so unhandled parse errors would otherwise be
                # invisible.
                print(f"[spot/coinbase] unexpected error: {e!r}; reconnecting")
            for pb in self._books.values():
                pb.is_resyncing = True
            self._at_clock = 0.0
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _data_age(self, exch_ts: float | None, mono_now: float, feed: str = "at",
                  allow: float = 0.0) -> float:
        """Seconds this message has spent queued on our side: received but
        not yet processed. `ws.recv()` hands back frames websockets already
        buffered, so after an event-loop stall a backlog all reads as
        "just received" if we stamp it with time.monotonic() -- which is
        why the 2026-10-08 ...1300 stalls (2-9s with no processing) didn't
        trip the staleness kill switch. Coinbase's own message timestamp
        doesn't lie about that. `allow`: excess over the minimum that is the
    feed's own batching, not ours. Returns 0 when exch_ts is unavailable."""
        if exch_ts is None:
            return 0.0
        off = time.time() - exch_ts
        q = self._offsets[feed]
        while q and q[0][0] < mono_now - OFFSET_WINDOW_S:
            q.popleft()
        while q and q[-1][1] >= off:      # monotonic deque -> O(1) running min
            q.pop()
        q.append((mono_now, off))
        return max(0.0, off - q[0][1] - allow)

    async def _handle_event(self, event: dict, as_of: float, exch_ts: float | None,
                            seq: int | None,
                            on_update: Callable[[VenueBook], Awaitable[None] | None]):
        product_id = event.get("product_id")
        coin = self._coin_by_product.get(product_id)
        if coin is None:
            return
        pb = self._books[coin]
        etype = event.get("type")
        if etype == "snapshot":
            bids = [(float(x["price_level"]), float(x["new_quantity"]))
                    for x in event.get("updates", []) if x.get("side") == "bid"]
            asks = [(float(x["price_level"]), float(x["new_quantity"]))
                    for x in event.get("updates", []) if x.get("side") == "offer"]
            pb.apply_snapshot(bids, asks)
        elif etype == "update":
            for change in event.get("updates", []):
                side = "bid" if change.get("side") == "bid" else "ask"
                pb.apply_change(side, float(change["price_level"]),
                                float(change["new_quantity"]))
        else:
            return
        # The message `timestamp` is send time; content time is the newest
        # update's event_time (32-42ms older at p50, 10-21 probe). Same ISO
        # format throughout, so the string max is the time max.
        ets = [u.get("event_time") for u in event.get("updates", []) if u.get("event_time")]
        if ets:
            et = _parse_iso_ts(max(ets))
            pb.book_time(et)
            if et is not None and et > self._at_clock:
                self._at_clock = et
        pb.settle_trade(as_of)
        pb.last_exchange_ts = exch_ts
        # receive_ts is the monotonic time the data was FRESH at our edge:
        # processing time minus however long it sat queued. Feed health,
        # the theo kill switch and the cross-coin skew guard all read it.
        pb.last_receive_ts = as_of
        pb.last_seq = seq
        # Kept current as the fallback, but not emitted while the Exchange
        # book is the source (two books' views would interleave).
        if self._src(coin) is not pb:
            return
        # Per message: a THIN view (top PER_MSG_LEVELS) -- enough for the
        # mid, feed health and phantom-cross pruning. The 1000-level view
        # the CF replica wants is built once a second via deep_book().
        await self._emit(coin, on_update)

    async def _handle_trades(self, event: dict, as_of: float,
                             on_update: Callable[[VenueBook], Awaitable[None] | None],
                             exch_ts: float | None = None):
        # The snapshot is the last 50 trades, already reflected in the book.
        if event.get("type") != "update":
            return
        newest: dict[str, dict] = {}
        for t in event.get("trades", []) or []:
            pid = t.get("product_id")
            if pid not in self._coin_by_product:
                continue
            coin = self._coin_by_product[pid]
            # The Exchange feed delivers the same trade ~55ms sooner; skip what
            # it already sent (or everything while it is connected).
            if self.ex_live or not self._new_trade(coin, t.get("trade_id")):
                continue
            if self.on_print is not None:
                self.on_print(coin, t, as_of, exch_ts)
            if pid in self._coin_by_product and (pid not in newest or t.get("time", "") >= newest[pid].get("time", "")):
                newest[pid] = t
        for pid, t in newest.items():
            await self._held(self._coin_by_product[pid], float(t["price"]), as_of, on_update)


def _parse_iso_ts(s: str) -> float | None:
    """Coinbase stamps messages with an ISO-8601 string. Return a float
    unix seconds, or None on parse failure (not worth tearing the stream
    down over)."""
    try:
        import datetime as _dt
        # 2026-10-06T15:00:00.000000Z form; fromisoformat handles the +00:00
        # form but not trailing Z until 3.11.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None
