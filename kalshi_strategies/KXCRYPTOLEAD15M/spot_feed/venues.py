"""
CF constituent venues other than Coinbase: one websocket client per venue,
each maintaining `_ProductBook`s and emitting `VenueBook`s through the same
callback shape as CoinbaseAdapter.

Constituents per index are in series.CF_CONSTITUENTS (CF/CME CF constituent
lists, 05-Oct-26 / 24-Aug-26). LMAX Digital is a constituent of BTC/ETH/SOL/XRP
but needs an institutional account, so it is the one venue we cannot see.

Every venue here shares one design: parse a message into level changes, apply
them to a per-product book, and emit a thin view. Each class owns only its
protocol (subscribe, snapshot vs delta, sequencing, heartbeats).
"""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from decimal import Decimal
from typing import Awaitable, Callable

import websockets

from .base import VenueBook
from .coinbase import PER_MSG_LEVELS, _ProductBook

RECONNECT_DELAY_S = 1.0
# Longest silence on an open socket before reconnecting. Every venue below
# sends heartbeats or data well inside this.
RECV_TIMEOUT_S = 10.0
MAX_FRAME = 32 << 20

OnUpdate = Callable[[VenueBook], Awaitable[None] | None]


class _Venue:
    venue = ""
    url = ""
    symbols: dict[str, str] = {}

    def __init__(self, coins):
        self.coins = [c for c in coins if c in self.symbols]
        self._coin_by_symbol = {self.symbols[c]: c for c in self.coins}
        self._books = {c: _ProductBook() for c in self.coins}
        self.n_resyncs = 0
        self.n_msgs = 0
        self.last_error: str | None = None

    def latest(self, coin: str) -> VenueBook | None:
        pb = self._books.get(coin)
        if pb is None or pb.last_receive_ts == 0.0:
            return None
        return pb.to_venue_book(self.venue, self.symbols[coin], coin)

    def _emit(self, coin, on_update, as_of, exch_ts=None):
        pb = self._books[coin]
        pb.last_receive_ts = as_of
        pb.last_exchange_ts = exch_ts
        vb = pb.to_venue_book(self.venue, self.symbols[coin], coin, max_levels=PER_MSG_LEVELS)
        r = on_update(vb)
        return r

    def _mark_fresh(self, as_of: float):
        """Any message on the connection proves every synced book is current
        (same reasoning as CoinbaseAdapter._mark_fresh)."""
        for pb in self._books.values():
            if not pb.is_resyncing and pb.last_receive_ts > 0.0:
                pb.last_receive_ts = as_of

    async def _subscribe(self, ws):
        raise NotImplementedError

    async def _handle(self, ws, msg: dict, as_of: float, on_update: OnUpdate):
        raise NotImplementedError

    async def run(self, on_update: OnUpdate):
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=15, ping_interval=20,
                                              ping_timeout=15, max_size=MAX_FRAME) as ws:
                    for pb in self._books.values():
                        pb.clear()
                    if hasattr(self, "_last_touch"):
                        self._last_touch.clear()      # first book after a resubscribe always goes out
                    await self._subscribe(ws)
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S)
                        as_of = time.monotonic()
                        self.n_msgs += 1
                        msg = self._decode(raw)
                        r = self._handle(ws, msg, as_of, on_update)
                        if asyncio.iscoroutine(r):
                            r = await r
                        self._mark_fresh(as_of)
            except asyncio.TimeoutError:
                self.last_error = "recv timeout"
            except (OSError, websockets.ConnectionClosed) as e:
                self.last_error = repr(e)
            except Exception as e:
                # One bad message must not take the venue down for the session.
                self.last_error = repr(e)
                print(f"[spot/{self.venue}] unexpected error: {e!r}; reconnecting")
            for pb in self._books.values():
                pb.is_resyncing = True
            self.n_resyncs += 1
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _decode(self, raw):
        return json.loads(raw)

    async def _call(self, r):
        if asyncio.iscoroutine(r):
            await r


def _apply(pb: _ProductBook, side: str, price: float, qty: float):
    pb.apply_change("bid" if side == "bid" else "ask", price, qty)


# ---------------------------------------------------------------------------
# Kraken v2 `book`, depth 25, CRC32 over the top 10 each side.
# ---------------------------------------------------------------------------

class KrakenAdapter(_Venue):
    venue = "kraken"
    url = "wss://ws.kraken.com/v2"
    symbols = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "XRP": "XRP/USD", "HYPE": "HYPE/USD"}
    # 10, not 25: the mid needs one level and the checksum covers ten. Depth 25
    # sent 147 msg/s for BTC+ETH vs 99 at depth 10 (2026-10-11).
    DEPTH = 10
    # Kraken's checksum formats price and qty at the pair's own precision
    # (AssetPairs pair_decimals / lot_decimals, read 2026-10-11).
    PRECISION = {"BTC": (1, 8), "ETH": (2, 8), "SOL": (2, 8), "XRP": (5, 8), "HYPE": (2, 8)}

    def __init__(self, coins):
        super().__init__(coins)
        # Decimal shadow books for the checksum: float formatting would not
        # reproduce Kraken's string digits.
        self._dec = {c: ({}, {}) for c in self.coins}
        self.n_checksum_fail = 0

    def _decode(self, raw):
        return json.loads(raw, parse_float=Decimal)

    async def _subscribe(self, ws):
        await ws.send(json.dumps({"method": "subscribe", "params": {
            "channel": "book", "symbol": [self.symbols[c] for c in self.coins], "depth": self.DEPTH}}))

    def _crc(self, coin) -> int:
        pp, qp = self.PRECISION[coin]
        bids, asks = self._dec[coin]

        def f(x: Decimal, dp: int) -> str:
            return f"{x:.{dp}f}".replace(".", "").lstrip("0") or "0"
        s = "".join(f(p, pp) + f(q, qp) for p, q in sorted(asks.items())[:10])
        s += "".join(f(p, pp) + f(q, qp) for p, q in sorted(bids.items(), reverse=True)[:10])
        return zlib.crc32(s.encode()) & 0xFFFFFFFF

    async def _handle(self, ws, msg, as_of, on_update):
        if msg.get("channel") != "book":
            return
        typ = msg.get("type")
        for d in msg.get("data", []):
            coin = self._coin_by_symbol.get(d.get("symbol"))
            if coin is None:
                continue
            pb, (db, da) = self._books[coin], self._dec[coin]
            if typ == "snapshot":
                db.clear(); da.clear()
                pb.apply_snapshot([], [])
            elif pb.is_resyncing:
                continue
            for side, dec, name in (("bids", db, "bid"), ("asks", da, "ask")):
                for lv in d.get(side, []):
                    p, q = lv["price"], lv["qty"]
                    if q == 0:
                        dec.pop(p, None)
                    else:
                        dec[p] = q
                    pb.apply_change(name, float(p), float(q))
            # Kraken stops sending deletes for levels pushed past the
            # subscribed depth; truncate or the checksum drifts.
            for dec, name, rev in ((db, "bid", True), (da, "ask", False)):
                if len(dec) > self.DEPTH:
                    for p in sorted(dec, reverse=rev)[self.DEPTH:]:
                        dec.pop(p)
                        pb.apply_change(name, float(p), 0.0)
            if "checksum" in d and self._crc(coin) != int(d["checksum"]):
                self.n_checksum_fail += 1
                pb.is_resyncing = True
                await ws.send(json.dumps({"method": "unsubscribe", "params": {
                    "channel": "book", "symbol": [d["symbol"]], "depth": self.DEPTH}}))
                await ws.send(json.dumps({"method": "subscribe", "params": {
                    "channel": "book", "symbol": [d["symbol"]], "depth": self.DEPTH}}))
                continue
            await self._call(self._emit(coin, on_update, as_of))


# ---------------------------------------------------------------------------
# Bitstamp `order_book_{pair}`: full top-100 snapshots, nothing to sequence.
# ---------------------------------------------------------------------------

class BitstampAdapter(_Venue):
    venue = "bitstamp"
    url = "wss://ws.bitstamp.net"
    symbols = {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd", "HYPE": "hypeusd"}

    async def _subscribe(self, ws):
        for c in self.coins:
            await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": f"order_book_{self.symbols[c]}"}}))

    async def _handle(self, ws, msg, as_of, on_update):
        ev = msg.get("event")
        if ev == "bts:request_reconnect":
            raise websockets.ConnectionClosed(None, None)
        if ev != "data":
            return
        coin = self._coin_by_symbol.get(msg.get("channel", "").rsplit("_", 1)[-1])
        if coin is None:
            return
        d = msg["data"]
        self._books[coin].apply_snapshot([(float(p), float(q)) for p, q in d.get("bids", [])],
                                         [(float(p), float(q)) for p, q in d.get("asks", [])])
        ts = d.get("microtimestamp")
        await self._call(self._emit(coin, on_update, as_of, None if ts is None else int(ts) / 1e6))


# ---------------------------------------------------------------------------
# Gemini v1 market data with top_of_book=true: one connection per symbol, each
# message is the new best bid or ask. The v2 `l2` feed sent 179 msg/s for
# BTC+ETH against ~12 for this (2026-10-11), and the mid is all we price on.
# ---------------------------------------------------------------------------

class GeminiAdapter(_Venue):
    venue = "gemini"
    symbols = {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "HYPE": "HYPEUSD"}

    def __init__(self, coins):
        super().__init__(coins)
        self._top = {c: {"bid": None, "ask": None} for c in self.coins}

    @staticmethod
    def _url(sym: str) -> str:
        return f"wss://api.gemini.com/v1/marketdata/{sym}?top_of_book=true&trades=false&heartbeat=true"

    async def run(self, on_update: OnUpdate):
        await asyncio.gather(*(self._run_one(c, on_update) for c in self.coins))

    async def _run_one(self, coin: str, on_update: OnUpdate):
        pb = self._books[coin]
        while True:
            try:
                async with websockets.connect(self._url(self.symbols[coin]), open_timeout=15, ping_interval=20,
                                              ping_timeout=15, max_size=MAX_FRAME) as ws:
                    pb.clear()
                    self._top[coin] = {"bid": None, "ask": None}
                    last_seq = None
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S))
                        as_of = time.monotonic()
                        self.n_msgs += 1
                        seq = msg.get("socket_sequence")
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                raise websockets.ConnectionClosed(None, None)   # gap: resubscribe
                            last_seq = seq
                        if msg.get("type") == "update":
                            top = self._top[coin]
                            for e in msg.get("events", []):
                                if e.get("type") == "change" and e.get("side") in top:
                                    q = float(e.get("remaining", 0))
                                    top[e["side"]] = (float(e["price"]), q) if q > 0 else None
                            if top["bid"] and top["ask"]:
                                pb.apply_snapshot([top["bid"]], [top["ask"]])
                                ts = msg.get("timestampms")
                                await self._call(self._emit(coin, on_update, as_of,
                                                            None if ts is None else ts / 1e3))
                        if not pb.is_resyncing and pb.last_receive_ts > 0.0:
                            pb.last_receive_ts = as_of      # heartbeat or update: this book is current
            except asyncio.TimeoutError:
                self.last_error = "recv timeout"
            except (OSError, websockets.ConnectionClosed) as e:
                self.last_error = repr(e)
            except Exception as e:
                self.last_error = repr(e)
                print(f"[spot/{self.venue}] unexpected error on {coin}: {e!r}; reconnecting")
            pb.is_resyncing = True
            self.n_resyncs += 1
            await asyncio.sleep(RECONNECT_DELAY_S)


# ---------------------------------------------------------------------------
# Crypto.com exchange v1 `book.{inst}.50`, snapshot then deltas chained by
# u/pu; a break means resubscribe. Heartbeats must be answered or the server
# drops the connection.
# ---------------------------------------------------------------------------

class CryptoComAdapter(_Venue):
    venue = "crypto_com"
    url = "wss://stream.crypto.com/exchange/v1/market"
    symbols = {"BTC": "BTC_USD", "ETH": "ETH_USD", "SOL": "SOL_USD", "XRP": "XRP_USD", "HYPE": "HYPE_USD"}
    DEPTH = 50

    def __init__(self, coins):
        super().__init__(coins)
        self._last_u: dict[str, int | None] = {c: None for c in self.coins}
        self.n_seq_gaps = 0

    def _sub_msg(self, coins, method="subscribe"):
        return json.dumps({"id": 1, "method": method, "params": {
            "channels": [f"book.{self.symbols[c]}.{self.DEPTH}" for c in coins],
            "book_subscription_type": "SNAPSHOT_AND_UPDATE", "book_update_frequency": 10},
            "nonce": int(time.time() * 1000)})

    async def _subscribe(self, ws):
        # The docs ask for a 1s pause after connect; sooner can be rate-limited.
        await asyncio.sleep(1.0)
        await ws.send(self._sub_msg(self.coins))

    async def _handle(self, ws, msg, as_of, on_update):
        if msg.get("method") == "public/heartbeat":
            await ws.send(json.dumps({"id": msg.get("id"), "method": "public/respond-heartbeat"}))
            return
        res = msg.get("result")
        if not isinstance(res, dict):
            return
        coin = self._coin_by_symbol.get(res.get("instrument_name"))
        if coin is None:
            return
        pb = self._books[coin]
        for d in res.get("data", []):
            if res.get("channel") == "book":
                pb.apply_snapshot([(float(p), float(q)) for p, q, *_ in d.get("bids", [])],
                                  [(float(p), float(q)) for p, q, *_ in d.get("asks", [])])
                self._last_u[coin] = d.get("u")
            elif res.get("channel") == "book.update":
                if pb.is_resyncing or self._last_u[coin] is None:
                    continue
                if d.get("pu") != self._last_u[coin]:
                    self.n_seq_gaps += 1
                    pb.is_resyncing = True
                    self._last_u[coin] = None
                    await ws.send(self._sub_msg([coin], "unsubscribe"))
                    await ws.send(self._sub_msg([coin]))
                    return
                up = d.get("update", {})
                for side, name in (("bids", "bid"), ("asks", "ask")):
                    for p, q, *_ in up.get(side, []):
                        pb.apply_change(name, float(p), float(q))
                self._last_u[coin] = d.get("u")
            else:
                continue
            t = d.get("t")
            await self._call(self._emit(coin, on_update, as_of, None if t is None else t / 1e3))


# ---------------------------------------------------------------------------
# Bullish `l2Orderbook`: full snapshots, flat [price, qty, price, qty, ...].
# ---------------------------------------------------------------------------

class BullishAdapter(_Venue):
    venue = "bullish"
    url = "wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook"
    # No HYPE market on Bullish (checked 2026-10-11), and it isn't a HYPE constituent.
    symbols = {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD"}

    async def _subscribe(self, ws):
        for i, c in enumerate(self.coins):
            await ws.send(json.dumps({"jsonrpc": "2.0", "type": "command", "method": "subscribe",
                                      "params": {"topic": "l2Orderbook", "symbol": self.symbols[c]},
                                      "id": str(i + 1)}))

    async def _handle(self, ws, msg, as_of, on_update):
        if msg.get("type") != "snapshot":
            return
        d = msg.get("data", {})
        coin = self._coin_by_symbol.get(d.get("symbol"))
        if coin is None:
            return
        b, a = d.get("bids", []), d.get("asks", [])
        self._books[coin].apply_snapshot([(float(b[i]), float(b[i + 1])) for i in range(0, len(b) - 1, 2)],
                                         [(float(a[i]), float(a[i + 1])) for i in range(0, len(a) - 1, 2)])
        ts = d.get("timestamp")
        await self._call(self._emit(coin, on_update, as_of, None if ts is None else int(ts) / 1e3))


# ---------------------------------------------------------------------------
# Hyperliquid: NOT a CF constituent, so never part of the settlement price.
# HYPE trades mainly here, and its perps lead the spot venues, so it's a
# leading signal only (2026-10-11 04:30:23: we were swept on HYPE ~110 ms
# before the move printed on Coinbase).
# ---------------------------------------------------------------------------

class HyperliquidAdapter(_Venue):
    """`bbo` (top of book, every block) and `trades` for the perps, plus HYPE
    spot. Books are emitted under venue "hl_perp" / "hl_spot" so they can never
    be mistaken for a constituent; prints go to `on_print` like Coinbase's."""
    venue = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"
    symbols = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "HYPE": "HYPE"}
    SPOT = {"HYPE": "@107"}            # HYPE/USDC spot pair index (spotMeta, 2026-10-11)
    # The first trades message per feed after subscribe is a replay of recent
    # history and is dropped whole; this age cap is a backstop for any later
    # stale batch.
    MAX_PRINT_AGE_S = 2.0

    def __init__(self, coins):
        super().__init__(coins)
        self._spot_books = {c: _ProductBook() for c in self.SPOT if c in self.coins}
        self._by_feed = {self.symbols[c]: (c, "hl_perp") for c in self.coins}
        self._by_feed.update({s: (c, "hl_spot") for c, s in self.SPOT.items() if c in self.coins})
        self.on_print = None
        self._seen_tid: set = set()
        self._replayed: set = set()

    def _book(self, coin, kind):
        return self._books[coin] if kind == "hl_perp" else self._spot_books[coin]

    def latest(self, coin: str, kind: str = "hl_perp") -> VenueBook | None:
        pb = (self._books if kind == "hl_perp" else self._spot_books).get(coin)
        if pb is None or pb.last_receive_ts == 0.0:
            return None
        return pb.to_venue_book(kind, self.symbols[coin], coin)

    def _mark_fresh(self, as_of):
        super()._mark_fresh(as_of)
        for pb in self._spot_books.values():
            if not pb.is_resyncing and pb.last_receive_ts > 0.0:
                pb.last_receive_ts = as_of

    async def _subscribe(self, ws):
        for pb in self._spot_books.values():
            pb.clear()
        self._replayed: set = set()
        for feed in self._by_feed:
            for typ in ("bbo", "trades"):
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": typ, "coin": feed}}))

    async def _handle(self, ws, msg, as_of, on_update):
        ch, d = msg.get("channel"), msg.get("data")
        if ch == "bbo":
            hit = self._by_feed.get(d.get("coin"))
            if hit is None:
                return
            coin, kind = hit
            bid, ask = (d.get("bbo") or [None, None])[:2]
            pb = self._book(coin, kind)
            pb.apply_snapshot([(float(bid["px"]), float(bid["sz"]))] if bid else [],
                              [(float(ask["px"]), float(ask["sz"]))] if ask else [])
            pb.last_receive_ts, pb.last_exchange_ts = as_of, d["time"] / 1e3
            await self._call(on_update(pb.to_venue_book(kind, d["coin"], coin, max_levels=1)))
        elif ch == "trades" and self.on_print is not None:
            feed = d[0].get("coin") if d else None
            if feed not in self._replayed:
                self._replayed.add(feed)
                for t in d or []:
                    self._seen_tid.add(t.get("tid"))
                return
            now = time.time()
            for t in d or []:
                hit = self._by_feed.get(t.get("coin"))
                if hit is None or t.get("tid") in self._seen_tid:
                    continue
                self._seen_tid.add(t["tid"])
                if now - t["time"] / 1e3 > self.MAX_PRINT_AGE_S:
                    continue
                coin, kind = hit
                self.on_print(coin, {"price": t["px"], "size": t["sz"], "trade_id": t["tid"],
                                     "side": "BUY" if t["side"] == "B" else "SELL"},
                              as_of, t["time"] / 1e3, kind)
            if len(self._seen_tid) > 200_000:
                self._seen_tid.clear()


# ---------------------------------------------------------------------------
# Offshore USDT perps (Binance, Bybit, OKX): leading signals only, like
# Hyperliquid. USDT-quoted with a funding basis, so they must never enter a
# constituent median. Top of book + every print.
# ---------------------------------------------------------------------------

# A perp book update whose touch prices are unchanged is dropped, except one
# per this interval per coin (keeps the book's age and size current). Binance
# bookTicker: ~711 msg/s on BTC for ~6 mid changes/s (10-20 recording); the
# perp books only feed the nowcast mid and the recorder.
PERP_SAME_TOUCH_S = 0.25


class _PerpSignal(_Venue):
    """Emits a 1-level book under `venue` and every print to `on_print`
    (same signature as HyperliquidAdapter's, kind = venue)."""

    def __init__(self, coins):
        super().__init__(coins)
        self.on_print = None
        self._last_touch: dict[str, tuple] = {}
        self.n_bbo_dropped = 0

    def _bbo(self, coin, bid, bsz, ask, asz, as_of, exch_ts, on_update):
        touch = (bid, ask)
        last = self._last_touch.get(coin)
        if last is not None and last[0] == touch and as_of - last[1] < PERP_SAME_TOUCH_S:
            self.n_bbo_dropped += 1
            return None
        self._last_touch[coin] = (touch, as_of)
        pb = self._books[coin]
        pb.apply_snapshot([(float(bid), float(bsz))], [(float(ask), float(asz))])
        pb.last_receive_ts, pb.last_exchange_ts = as_of, exch_ts
        return on_update(pb.to_venue_book(self.venue, self.symbols[coin], coin, max_levels=1))

    def _print(self, coin, px, sz, tid, side, as_of, exch_ts):
        if self.on_print is not None:
            self.on_print(coin, {"price": px, "size": sz, "trade_id": tid, "side": side}, as_of, exch_ts, self.venue)


class BinancePerpAdapter(_PerpSignal):
    # fstream.binance.com is reachable from this box; spot binance.com 451s
    # (US block) -- checked 2026-10-11. `aggTrade` on /ws never sent a frame
    # in that check while `trade` did, hence `trade`.
    venue = "binance_perp"
    symbols = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt", "HYPE": "hypeusdt"}

    @property
    def url(self):
        streams = "/".join(f"{self.symbols[c]}@{s}" for c in self.coins for s in ("bookTicker", "trade"))
        return f"wss://fstream.binance.com/stream?streams={streams}"

    async def _subscribe(self, ws):
        return

    async def _handle(self, ws, msg, as_of, on_update):
        d = msg.get("data") or {}
        coin = self._coin_by_symbol.get(str(d.get("s", "")).lower())
        if coin is None:
            return
        if d.get("e") == "bookTicker":
            await self._call(self._bbo(coin, d["b"], d["B"], d["a"], d["A"], as_of, d.get("T", 0) / 1e3, on_update))
        elif d.get("e") == "trade" and d.get("X", "MARKET") == "MARKET":
            # m = buyer is the maker, i.e. the aggressor sold.
            self._print(coin, d["p"], d["q"], d["t"], "SELL" if d.get("m") else "BUY", as_of, d["T"] / 1e3)


class BybitPerpAdapter(_PerpSignal):
    venue = "bybit_perp"
    url = "wss://stream.bybit.com/v5/public/linear"
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "HYPE": "HYPEUSDT"}

    async def _subscribe(self, ws):
        args = [f"orderbook.1.{self.symbols[c]}" for c in self.coins]
        args += [f"publicTrade.{self.symbols[c]}" for c in self.coins]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        asyncio.ensure_future(self._ping(ws))

    @staticmethod
    async def _ping(ws):
        # Bybit drops connections that send nothing for 10 min.
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            return

    async def _handle(self, ws, msg, as_of, on_update):
        topic = msg.get("topic", "")
        if topic.startswith("orderbook.1."):
            d = msg["data"]
            coin = self._coin_by_symbol.get(d.get("s"))
            # depth-1 pushes are snapshots, but a side can arrive empty.
            if coin is None or not d.get("b") or not d.get("a"):
                return
            await self._call(self._bbo(coin, d["b"][0][0], d["b"][0][1], d["a"][0][0], d["a"][0][1],
                                       as_of, msg.get("cts", msg.get("ts", 0)) / 1e3, on_update))
        elif topic.startswith("publicTrade."):
            for t in msg.get("data", []):
                coin = self._coin_by_symbol.get(t.get("s"))
                if coin is not None:
                    self._print(coin, t["p"], t["v"], t["i"], "BUY" if t["S"] == "Buy" else "SELL", as_of, t["T"] / 1e3)


class OkxPerpAdapter(_PerpSignal):
    venue = "okx_perp"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    symbols = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP",
               "HYPE": "HYPE-USDT-SWAP"}
    # Coins per contract (public/instruments ctVal, 2026-10-11); OKX sizes are in contracts.
    CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0, "HYPE": 0.1}

    async def _subscribe(self, ws):
        args = [{"channel": ch, "instId": self.symbols[c]} for c in self.coins for ch in ("bbo-tbt", "trades")]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        asyncio.ensure_future(self._ping(ws))

    @staticmethod
    async def _ping(ws):
        # OKX closes a connection idle for 30 s; it expects a bare "ping".
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send("ping")
        except Exception:
            return

    def _decode(self, raw):
        return {} if raw == "pong" else json.loads(raw)

    async def _handle(self, ws, msg, as_of, on_update):
        arg = msg.get("arg") or {}
        coin = self._coin_by_symbol.get(arg.get("instId"))
        if coin is None or "data" not in msg:
            return
        k = self.CT_VAL[coin]
        if arg.get("channel") == "bbo-tbt":
            for d in msg["data"]:
                if d.get("bids") and d.get("asks"):
                    b, a = d["bids"][0], d["asks"][0]
                    await self._call(self._bbo(coin, b[0], float(b[1]) * k, a[0], float(a[1]) * k,
                                               as_of, int(d["ts"]) / 1e3, on_update))
        elif arg.get("channel") == "trades":
            for t in msg["data"]:
                self._print(coin, t["px"], float(t["sz"]) * k, t["tradeId"],
                            "BUY" if t["side"] == "buy" else "SELL", as_of, int(t["ts"]) / 1e3)


# Venues that are signals only: never in a median, never priced. Hyperliquid
# is no longer run (the adapter stays): its prints reached us p50 ~400ms after
# trade time (us-east-2, 10-20 16:00 UTC) vs Binance ~80ms, its prior-0.5s
# return explained 0.2-0.4% of Coinbase's next 1s move vs 1.3-10.5% for Binance
# (10-19), and it only pulled HYPE. Bybit is no longer run either (10-20):
# slowest perp to us (p50 113-145ms vs Binance ~75, OKX ~80-120), second
# heaviest feed, and OKX alone covers the nowcast check and the move trigger.
LEADING_VENUES = ("binance_perp", "okx_perp")

ADAPTERS = {a.venue: a for a in (KrakenAdapter, BitstampAdapter, GeminiAdapter, CryptoComAdapter, BullishAdapter,
                                 HyperliquidAdapter, BinancePerpAdapter, BybitPerpAdapter, OkxPerpAdapter)}

__all__ = ["KrakenAdapter", "BitstampAdapter", "GeminiAdapter", "CryptoComAdapter", "BullishAdapter",
           "HyperliquidAdapter", "BinancePerpAdapter", "BybitPerpAdapter", "OkxPerpAdapter",
           "LEADING_VENUES", "ADAPTERS"]
