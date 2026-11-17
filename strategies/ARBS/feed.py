"""Kalshi websocket for every basket leg on one connection, subscribed in
batches beside the reader. Seq is checked per sid."""
from __future__ import annotations

import asyncio
import json
import math
import time

import pandas as pd
import websockets

from backtest.book import L2Book
from infra.kalshi import auth
from infra.kalshi.ws_feed import (OPEN_TIMEOUT_S, RECONNECT_DELAY_S, RECV_TIMEOUT_S, RESYNC_INTERVAL_S,
                                  WS_URL, LiveKalshiFeed, _loads)

# Same batching as ws_raw_ob_collector (SUBSCRIBE_BATCH / _PACING_S): one
# command with ~1000 tickers was the suspect in a 12h silent gap (2026-08-26).
SUBSCRIBE_BATCH = 50
SUBSCRIBE_PACING_S = 0.2
# Every market gets a fresh snapshot once per RESYNC_INTERVAL_S, a batch at a
# time on the open connection: removing a market from the orderbook sid and
# adding it back sends a new snapshot (probe 10-29; a repeat subscribe sends
# none). A full reconnect instead took ~6 s to resubscribe 1450 markets, past
# feed_down_pull_s, so every quote was pulled every 15 min.
REFRESH_BATCH = 50

_NOW: list = [0.0, None]


def _now_utc():
    """UTC now, one Timestamp per ms: built per book message it was ~2.5% of
    a busy ARBS loop (py-spy 11-17), and messages arrive in bursts."""
    t = time.time()
    if _NOW[1] is None or abs(t - _NOW[0]) > 0.001:
        _NOW[0], _NOW[1] = t, pd.Timestamp(t, unit="s", tz="UTC")
    return _NOW[1]


class MultiFeed(LiveKalshiFeed):
    _now = staticmethod(_now_utc)

    def __init__(self, tickers, on_quote, on_trade, *, label, on_fill=None, on_error=None):
        super().__init__(tickers, on_quote, on_trade, label=label, on_fill=on_fill, price_dp=4)
        self._on_error = on_error
        self.n_errors = 0
        self.connected_at: float | None = None
        # Subscribed and reading. Down since `down_since` (monotonic) otherwise.
        self.up = False
        self.down_since: float | None = time.monotonic()
        self._new: list[str] = []
        self.n_refreshes = 0
        self._sub_id = 1

    def add(self, tickers):
        """Subscribe to more markets: on the open connection, and on every
        reconnect after."""
        new = [t for t in tickers if t not in self._books]
        for t in new:
            self._books[t] = L2Book(t, 4)
        self._tickers += new
        self._new += new

    def _next_id(self) -> int:
        self._sub_id += 1
        return self._sub_id - 1

    async def _subscribe(self, ws, tickers):
        for i in range(0, len(tickers), SUBSCRIBE_BATCH):
            batch = tickers[i:i + SUBSCRIBE_BATCH]
            for ch in (["orderbook_delta"], ["trade"]):
                await ws.send(json.dumps({"id": self._next_id(), "cmd": "subscribe",
                                          "params": {"channels": ch, "market_tickers": batch}}))
            await asyncio.sleep(SUBSCRIBE_PACING_S)

    async def _subscriber(self, ws, first: list[str]):
        """Subscribes `first`, then marks the feed up and subscribes whatever
        add() queues. Runs beside the reader: subscribing 12-23k markets before
        reading took 50-90 s, the unread snapshots stalled the 20 s keepalive,
        and the connection closed 1011 and resubscribed in a loop (11-17)."""
        await self._subscribe(ws, first)
        if self._on_fill:
            # `fill` is account-wide; one subscription covers every market.
            await ws.send(json.dumps({"id": self._next_id(), "cmd": "subscribe", "params": {"channels": ["fill"]}}))
        self._set_up(True)
        while True:
            if self._new:
                new, self._new = self._new, []
                await self._subscribe(ws, new)
            else:
                await asyncio.sleep(SUBSCRIBE_PACING_S)

    def _refresh_gap(self) -> float:
        return RESYNC_INTERVAL_S / max(1, math.ceil(len(self._tickers) / REFRESH_BATCH))

    def ladder(self, ticker: str):
        return self._books.get(ticker)

    def _set_up(self, up: bool):
        if up and not self.up:
            self.down_since = None
        elif not up and self.up:
            self.down_since = time.monotonic()
        self.up = up

    async def run(self):
        # A new process (ARBS3 11-11) starts with none until its first scan
        # lands; raising here left its feed down for good.
        while not self._tickers:
            await asyncio.sleep(1.0)
        while True:
            sub = None
            try:
                async with websockets.connect(WS_URL, additional_headers=auth.ws_headers(),
                                              open_timeout=OPEN_TIMEOUT_S, max_size=None) as ws:
                    self.connected_at = time.monotonic()
                    self._new = []
                    sub = asyncio.create_task(self._subscriber(ws, list(self._tickers)))
                    last_seq: dict = {}
                    ob_sid, refresh_i = None, 0
                    refresh_at = time.monotonic() + self._refresh_gap()
                    while True:
                        if sub.done():
                            sub.result()          # a failed subscribe ends the connection
                        if self.up and ob_sid is not None and time.monotonic() >= refresh_at:
                            if refresh_i >= len(self._tickers):
                                refresh_i = 0
                            batch = self._tickers[refresh_i:refresh_i + REFRESH_BATCH]
                            refresh_i += REFRESH_BATCH
                            for action in ("delete_markets", "add_markets"):
                                await ws.send(json.dumps({"id": self._next_id(), "cmd": "update_subscription",
                                                          "params": {"sids": [ob_sid], "market_tickers": batch,
                                                                     "action": action}}))
                            self.n_refreshes += 1
                            refresh_at = time.monotonic() + self._refresh_gap()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_mono = time.monotonic()
                        d = _loads(raw)
                        mtype = d.get("type", "")
                        if mtype == "subscribed" and (d.get("msg") or {}).get("channel") == "orderbook_delta":
                            ob_sid = d["msg"].get("sid")
                        if mtype in ("error", "unsubscribed"):
                            # e.g. code 25 "Subscription buffer overflow" ends that
                            # subscription; a reconnect resubscribes everything.
                            self.n_errors += 1
                            print(f"[{self.label}] kalshi ws {mtype}: {d.get('msg')}; reconnecting", flush=True)
                            if self._on_error:
                                self._on_error(f"ws {mtype}: {d.get('msg')}")
                            break
                        # Later orderbook subscribes join sid 1 and are acked by an
                        # "ok" that takes a seq number too (probe 2026-10-24), so
                        # every message with a seq counts, not just orderbook ones.
                        if d.get("seq") is not None:
                            sid, seq = d.get("sid"), d["seq"]
                            prev = last_seq.get(sid)
                            if prev is not None and seq != prev + 1:
                                self.n_seq_gaps += 1
                                print(f"[{self.label}] kalshi ws seq gap sid {sid} {prev} -> {seq}; reconnecting",
                                      flush=True)
                                break
                            last_seq[sid] = seq
                        self._handle_message(d)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                self._set_up(False)
                print(f"[{self.label}] kalshi ws connection error ({e!r}); reconnecting", flush=True)
                await asyncio.sleep(RECONNECT_DELAY_S)
            finally:
                if sub is not None:
                    sub.cancel()
            self._set_up(False)
            self.n_reconnects += 1
