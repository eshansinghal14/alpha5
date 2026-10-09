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
style in live_kalshi_ws.py. Not using stdlib logging because this adapter
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
from ..series import COINBASE_PRODUCTS

WS_URL = "wss://advanced-trade-ws.coinbase.com"
RECONNECT_DELAY_S = 2.0
# Coinbase pings keep the socket alive; this is just a receive timeout so
# a wedged socket is noticed. 20s because the level2 channel has quiet
# minutes on low-volume products (notably HYPE).
RECV_TIMEOUT_S = 20.0
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


class _ProductBook:
    """Single-product L2 state. Dict-backed for O(1) level updates; a
    sorted snapshot is materialized only on `to_venue_book()`. Deliberately
    dumb -- the composite pipeline is what cares about crossed/one-sided
    books; this adapter just reports what the venue said."""

    __slots__ = ("bids", "asks", "last_receive_ts", "last_exchange_ts",
                 "last_seq", "is_resyncing", "last_wire_ts")

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

    def clear(self):
        self.bids.clear()
        self.asks.clear()
        self.is_resyncing = True

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
        self._offsets: collections.deque[tuple[float, float]] = collections.deque()

    def latest(self, coin: str) -> VenueBook | None:
        pb = self._books.get(coin)
        if pb is None or pb.last_receive_ts == 0.0:
            return None
        return pb.to_venue_book(self.venue, COINBASE_PRODUCTS[coin], coin)

    def deep_book(self, coin: str) -> VenueBook | None:
        """Full MAX_BOOK_LEVELS view for the once-a-second CF replica."""
        return self.latest(coin)

    async def run(self, on_update: Callable[[VenueBook], Awaitable[None] | None]):
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
                    print(f"[spot/coinbase] subscribed level2 to {product_ids}")
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
                        # Coinbase wraps l2 payloads inside {"channel":"l2_data","events":[...]}.
                        # Subscribe confirms arrive as {"channel":"subscriptions"} -- skip.
                        if d.get("channel") == "subscriptions":
                            continue
                        if d.get("channel") != "l2_data":
                            continue
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
                        for event in d.get("events", []) or []:
                            await self._handle_event(event, now, d.get("timestamp"), seq, on_update)
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
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _data_age(self, exch_ts: float | None, mono_now: float) -> float:
        """Seconds this message has spent queued on our side: received but
        not yet processed. `ws.recv()` hands back frames websockets already
        buffered, so after an event-loop stall a backlog all reads as
        "just received" if we stamp it with time.monotonic() -- which is
        why the 2026-10-08 ...1300 stalls (2-9s with no processing) didn't
        trip the staleness kill switch. Coinbase's own message timestamp
        doesn't lie about that. Returns 0 when it is unavailable."""
        if exch_ts is None:
            return 0.0
        off = time.time() - exch_ts
        q = self._offsets
        while q and q[0][0] < mono_now - OFFSET_WINDOW_S:
            q.popleft()
        while q and q[-1][1] >= off:      # monotonic deque -> O(1) running min
            q.pop()
        q.append((mono_now, off))
        return max(0.0, off - q[0][1])

    async def _handle_event(self, event: dict, now: float, msg_ts: str | None,
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
        pb.last_exchange_ts = _parse_iso_ts(msg_ts) if msg_ts else None
        # receive_ts is the monotonic time the data was FRESH at our edge:
        # processing time minus however long it sat queued. Feed health,
        # the theo kill switch and the cross-coin skew guard all read it.
        mono = time.monotonic()
        pb.last_receive_ts = mono - self._data_age(pb.last_exchange_ts, mono)
        pb.last_seq = seq
        # Per message: a THIN view (top PER_MSG_LEVELS) -- enough for the
        # mid, feed health and phantom-cross pruning. The 1000-level view
        # the CF replica wants is built once a second via deep_book().
        result = on_update(pb.to_venue_book(self.venue, product_id, coin,
                                            max_levels=PER_MSG_LEVELS))
        if asyncio.iscoroutine(result):
            await result


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
