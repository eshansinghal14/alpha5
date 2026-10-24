"""Kalshi websocket for every basket leg (~400 markets) on one connection,
subscribed in batches. Seq is checked per sid."""
from __future__ import annotations

import asyncio
import json
import time

import websockets

from kalshi_infra.kalshi import auth
from kalshi_infra.kalshi.ws_feed import (OPEN_TIMEOUT_S, RECONNECT_DELAY_S, RECV_TIMEOUT_S, RESYNC_INTERVAL_S,
                                         WS_URL, LiveKalshiFeed)

# Same batching as ws_raw_ob_collector (SUBSCRIBE_BATCH / _PACING_S): one
# command with ~1000 tickers was the suspect in a 12h silent gap (2026-08-26).
SUBSCRIBE_BATCH = 50
SUBSCRIBE_PACING_S = 0.2


class MultiFeed(LiveKalshiFeed):
    def __init__(self, tickers, on_quote, on_trade, *, label, on_fill=None, on_error=None):
        super().__init__(tickers, on_quote, on_trade, label=label, on_fill=on_fill)
        self._on_error = on_error
        self.n_errors = 0
        self.connected_at: float | None = None
        # Subscribed and reading. Down since `down_since` (monotonic) otherwise.
        self.up = False
        self.down_since: float | None = time.monotonic()

    def ladder(self, ticker: str):
        return self._books.get(ticker)

    def _set_up(self, up: bool):
        if up and not self.up:
            self.down_since = None
        elif not up and self.up:
            self.down_since = time.monotonic()
        self.up = up

    async def run(self):
        if not self._tickers:
            raise ValueError(f"[{self.label}] no Kalshi markets to subscribe to")
        sub_id = 1
        while True:
            try:
                async with websockets.connect(WS_URL, additional_headers=auth.ws_headers(),
                                              open_timeout=OPEN_TIMEOUT_S, max_size=None) as ws:
                    self.connected_at = time.monotonic()
                    channels = [["orderbook_delta"], ["trade"]]
                    for i in range(0, len(self._tickers), SUBSCRIBE_BATCH):
                        batch = self._tickers[i:i + SUBSCRIBE_BATCH]
                        for ch in channels:
                            await ws.send(json.dumps({"id": sub_id, "cmd": "subscribe",
                                                      "params": {"channels": ch, "market_tickers": batch}}))
                            sub_id += 1
                        await asyncio.sleep(SUBSCRIBE_PACING_S)
                    if self._on_fill:
                        # `fill` is account-wide; one subscription covers every market.
                        await ws.send(json.dumps({"id": sub_id, "cmd": "subscribe",
                                                  "params": {"channels": ["fill"]}}))
                        sub_id += 1
                    resync_at = time.monotonic() + RESYNC_INTERVAL_S
                    last_seq: dict = {}
                    self._set_up(True)
                    while time.monotonic() <= resync_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_mono = time.monotonic()
                        d = json.loads(raw)
                        mtype = d.get("type", "")
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
            self._set_up(False)
            self.n_reconnects += 1
