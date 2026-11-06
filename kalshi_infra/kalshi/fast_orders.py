"""IOC creates on one raw keep-alive HTTPS connection, sent from whatever
thread calls them.

The normal take path hands the order to a pool thread that runs it through
requests: KXBTC15M 11-06, the pickup alone was 0.88 ms p50 / 3.2 ms p90 and
requests adds ~0.3 ms of Python before the first byte. Here the caller (the
venue-reader thread, on a CME tick) writes prebuilt bytes itself. The body,
headers and response handling are the OrderClient's own, so a fast take is
the same order to Kalshi and to the engine as a normal one.
"""
from __future__ import annotations

import json
import select
import socket
import ssl
import threading
import time

import requests

from kalshi_infra.kalshi import routing
from kalshi_infra.kalshi.orders import BASE_URL, ORDERS_PATH, _client_id, _raise_with_body

HOST = "external-api.kalshi.com"
PORT = 443
TIMEOUT_S = 5.0
# Kalshi's load balancer drops idle keep-alives; a GET this often keeps the
# connection (and its TLS session) warm so a take never pays a handshake.
KEEPALIVE_S = 15.0
PING_PATH = "/trade-api/v2/exchange/status"


class FastOrderConn:
    """One persistent connection. `place_ioc(order)` is safe from any thread
    (serialized by a lock) and returns what OrderClient.place_ioc would."""

    def __init__(self, client):
        self.client = client
        self._ctx = ssl.create_default_context()
        self._sock: ssl.SSLSocket | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._last_use = 0.0
        self.n_reconnects = 0
        self.n_sent = 0
        threading.Thread(target=self._keepalive, daemon=True, name="fast-order-ka").start()

    # --- connection ------------------------------------------------------

    def _connect(self):
        self._close()
        ips = routing.preferred() or [HOST]
        last = None
        for ip in ips:
            try:
                raw = socket.create_connection((ip, PORT), timeout=TIMEOUT_S)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = self._ctx.wrap_socket(raw, server_hostname=HOST)
                self._buf = b""
                self.n_reconnects += 1
                return
            except OSError as e:
                last = e
        raise last or OSError("no Kalshi address")

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def warm(self):
        with self._lock:
            self._ping()

    def _keepalive(self):
        while True:
            time.sleep(1.0)
            if time.monotonic() - self._last_use < KEEPALIVE_S:
                continue
            with self._lock:
                try:
                    self._ping()
                except Exception:
                    self._close()

    def _ping(self):
        if self._sock is None:
            self._connect()
        self._roundtrip(f"GET {PING_PATH} HTTP/1.1\r\nHost: {HOST}\r\nAccept: application/json\r\n"
                        f"Connection: keep-alive\r\n\r\n".encode())

    # --- HTTP ------------------------------------------------------------

    def _readline(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Kalshi closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Kalshi closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _roundtrip(self, req: bytes) -> tuple[int, dict, bytes]:
        self._sock.sendall(req)
        self._last_use = time.monotonic()
        status = int(self._readline().split(b" ", 2)[1])
        headers = {}
        while True:
            line = self._readline()
            if not line:
                break
            k, _, v = line.partition(b":")
            headers[k.strip().lower().decode()] = v.strip().decode()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(self._readline().split(b";")[0], 16)
                if size == 0:
                    while self._readline():
                        pass
                    break
                body += self._read(size)
                self._read(2)
        else:
            body = self._read(int(headers.get("content-length", "0")))
        if headers.get("connection", "").lower() == "close":
            self._close()
        return status, headers, body

    # --- orders ----------------------------------------------------------

    def _request(self, order) -> bytes:
        c = self.client
        yes_price = order.price if order.side == "yes" else 1.0 - order.price
        # Same body as OrderClient._create for an IOC take.
        body = {
            "ticker": order.ticker,
            "side": "bid" if order.side == "yes" else "ask",
            "count": f"{order.size:.2f}",
            "price": f"{round(yes_price, c.price_dp):.4f}",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": _client_id(order),
            "time_in_force": "immediate_or_cancel",
            **c._shard_field(),
        }
        if c.order_group_id is not None:
            body["order_group_id"] = c.order_group_id
        data = json.dumps(body, separators=(",", ":")).encode()
        hdr = c._headers("POST", ORDERS_PATH)
        lines = [f"POST {ORDERS_PATH} HTTP/1.1", f"Host: {HOST}", f"Content-Length: {len(data)}",
                 "Connection: keep-alive"] + [f"{k}: {v}" for k, v in hdr.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + data

    def place_ioc(self, order):
        """RealFill or None (nothing crossed); raises like OrderClient.place_ioc.
        A connection that fails before the request is written is reopened
        once; after it is written the order may be live, so never resent."""
        req = self._request(order)
        with self._lock:
            # An idle connection with something to read has been closed by
            # Kalshi (FIN / close_notify): reopen before writing the order.
            if self._sock is None or self._buf or select.select([self._sock], [], [], 0)[0]:
                self._connect()
            try:
                status, headers, body = self._roundtrip(req)
            except Exception:
                self._close()
                raise
            self.n_sent += 1
        resp = requests.Response()
        resp.status_code, resp._content, resp.url = status, body, BASE_URL + ORDERS_PATH
        resp.headers.update(headers)
        _raise_with_body(resp)
        return self.client._parse_fill(order, resp.json())


__all__ = ["FastOrderConn"]
