"""Polymarket US orders, positions and balance, behind KalshiOrderClient's
interface so ARBS's Gateway can hold one for the Polymarket "shard".

Wire format (docs.polymarket.us, 2026-11-12): every price is the YES price,
a NO order is ORDER_INTENT_BUY_SHORT at 1 - its own price; quantities are
decimals. Fills of resting orders come only on the private websocket
(PolyFeed) and from reconcile(); a create answers with the order id alone.

Our core.Order prices and RealFill prices stay in the order's own side terms.
"""
from __future__ import annotations

import collections
import threading
import time
import uuid

import requests

from backtest.core import Order
from infra.kalshi.orders import KalshiOrderClient, RealFill, RestingAck, WriteBudget, _raise_with_body
from infra.polymarket import API, auth

PM_SHARD = 100            # ARBS keys order clients and cash by shard; Polymarket is one
ORDER_TIMEOUT_S = 10.0
# Requests per second for one key across every endpoint (orders overview and
# changelog, 11-12; the rate-limits page says 25/s per IP). ARBS's token units
# are 10 per write, so 20/s is 200 tokens/s; reads come out of the same 20.
KEY_REQUESTS_PER_S = 20.0
# An IOC waits this long for its executions in the create response (max 5).
IOC_BLOCK_S = "2"
INTENT = {"yes": "ORDER_INTENT_BUY_LONG", "no": "ORDER_INTENT_BUY_SHORT"}
# Order states whose quantity can't fill any more.
DONE = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED",
        "ORDER_STATE_REPLACED")


def _amt(a) -> float | None:
    if isinstance(a, dict):
        a = a.get("value")
    try:
        return None if a is None else float(a)
    except (TypeError, ValueError):
        return None


def order_side(o: dict) -> str:
    """"yes" or "no": the contract an order buys (BUY_SHORT buys NO)."""
    intent = o.get("intent") or ""
    if intent.endswith("_SHORT") or o.get("outcomeSide") == "OUTCOME_SIDE_NO":
        return "no"
    return "yes"


def parse_execution(ex: dict) -> dict | None:
    """A fill execution as ARBS's fill dict: price in the order's own side
    terms, `cum` the order's filled quantity after it (fills are booked up to
    it, so a websocket fill and a reconcile can't both book the same lots)."""
    if ex.get("type") not in ("EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"):
        return None
    o = ex.get("order") or {}
    size, px = _amt(ex.get("lastShares")), _amt(ex.get("lastPx"))
    if not size or px is None or not o.get("id"):
        return None
    side = order_side(o)
    t = ex.get("transactTime")
    return {"ticker": o.get("marketSlug"), "side": side, "price": px if side == "yes" else round(1.0 - px, 4),
            "size": size, "fee": _amt(ex.get("commissionNotionalCollected")) or 0.0,
            "time": t, "liquidity": "taker" if ex.get("aggressor") else "maker",
            "order_id": o["id"], "trade_id": ex.get("tradeId") or ex.get("id"),
            "cum": _amt(o.get("cumQuantity"))}


class PolymarketOrderClient(KalshiOrderClient):
    """Reuses KalshiOrderClient's token bucket, cap and usage accounting; every
    request is new. Blocking: call off the event loop."""

    price_dp = 4
    # One request each; a move is a cancel and a create (see amend).
    COST = {"order": 10, "amend": 20, "cancel": 10}

    def __init__(self, tokens_per_s: float = KEY_REQUESTS_PER_S * 10 * 0.6, timeout_s: float = ORDER_TIMEOUT_S):
        self._session = requests.Session()
        self._cancel_session = requests.Session()
        for s in (self._session, self._cancel_session):
            s.trust_env = False
            s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=16))
        self._lock = threading.Lock()
        self.budget = WriteBudget(tokens_per_s)
        self.tokens_per_s = self.budget.tokens_per_s
        self._rate = self.budget.rate
        self.timeout_s = timeout_s
        self.n_rate_limited = 0
        self._spent, self._refused, self._429 = {}, {}, {}
        self._t0 = time.monotonic()
        self.shard = PM_SHARD
        self.order_group_id = None
        # client id -> create body, so a create with no answer can be re-sent
        # under the same Idempotency-Key: the exchange returns the first
        # answer instead of placing it twice (kept 24 h).
        self._bodies: collections.OrderedDict[str, dict] = collections.OrderedDict()

    @classmethod
    def from_account(cls, shard=None, budget=None) -> "PolymarketOrderClient":
        return cls()

    def warm(self):
        pass

    def keepalive(self) -> int:
        return 0

    # ---------------------------------------------------------------- http

    def _req(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
             session=None, headers: dict | None = None):
        # Signed over the path alone: a query string isn't part of the message (probe 11-12).
        h = {**auth.headers(method, path), "Content-Type": "application/json", **(headers or {})}
        return (session or self._session).request(method, API + path, headers=h, json=body, params=params,
                                                  timeout=self.timeout_s)

    def _json(self, method: str, path: str, **kw) -> dict:
        r = self._req(method, path, **kw)
        _raise_with_body(r)
        return r.json() if r.content else {}

    # ---------------------------------------------------------------- orders

    def _body(self, order: Order, tif: str, post_only: bool) -> dict:
        yes = order.price if order.side == "yes" else 1.0 - order.price
        body = {"marketSlug": order.ticker, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{round(yes, self.price_dp):.4f}", "currency": "USD"},
                "quantity": round(float(order.size), 2), "tif": tif, "intent": INTENT[order.side],
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
        if post_only:
            body["participateDontInitiate"] = True
        return body

    def _create(self, order: Order, body: dict) -> dict:
        cid = getattr(order, "client_order_id", None) or str(uuid.uuid4())
        self._bodies[cid] = body
        while len(self._bodies) > 5000:
            self._bodies.popitem(last=False)
        return self._json("POST", "/v1/orders", body=body, headers={"Idempotency-Key": cid})

    def place_ioc(self, order: Order) -> RealFill | None:
        body = self._body(order, "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", False)
        body.update(synchronousExecution=True, maxBlockTime=IOC_BLOCK_S)
        resp = self._create(order, body)
        oid = resp.get("id")
        exs = resp.get("executions")
        fills = [f for f in (parse_execution(e) for e in exs or []) if f is not None]
        if exs is None:
            # No executions block at all: read the order rather than call it unfilled.
            o = self.order(oid)
            cum, avg = _amt(o.get("cumQuantity")) or 0.0, _amt(o.get("avgPx"))
            if cum <= 0:
                return None
            if avg is None:
                raise ValueError(f"IOC {oid} on {order.ticker} filled {cum} with no average price")
            px = avg if order.side == "yes" else 1.0 - avg
            fee = _amt(o.get("commissionNotionalTotalCollected")) or 0.0
            return RealFill(price=round(px, 6), size=cum, fee=fee, exchange_order_id=oid)
        size = sum(f["size"] for f in fills)
        if size <= 0:
            return None
        return RealFill(price=round(sum(f["price"] * f["size"] for f in fills) / size, 6), size=size,
                        fee=sum(f["fee"] for f in fills), exchange_order_id=oid)

    def place_resting(self, order: Order, order_group_id=None) -> RestingAck:
        """Post-only GTC. A cross is rejected on the order stream, not here:
        PolyFeed reports it and ARBS retires the quote."""
        resp = self._create(order, self._body(order, "TIME_IN_FORCE_GOOD_TILL_CANCEL", True))
        return RestingAck(exchange_order_id=resp.get("id"), fill=None)

    def amend(self, exchange_order_id: str, order: Order, new_size: float, filled: float,
              price: float | None = None) -> dict:
        """Cancel, then a new post-only order for `new_size` at `price`. The
        docs don't say whether /modify keeps the order id, so a move gets an
        id we know. Returns {"order": {"order_id": new id}} like Kalshi's amend."""
        if not self.cancel(exchange_order_id, order.ticker):
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError(f"order {exchange_order_id} already gone", response=resp)
        o = Order(order_id=order.order_id, ticker=order.ticker, side=order.side, order_type="limit",
                  price=order.price if price is None else price, size=new_size,
                  decision_time=order.decision_time, intent="quote")
        o.client_order_id = str(uuid.uuid4())
        ack = self.place_resting(o)
        return {"order": {"order_id": ack.exchange_order_id}}

    def cancel(self, exchange_order_id: str, market_ticker: str | None = None) -> bool:
        r = self._req("POST", f"/v1/order/{exchange_order_id}/cancel", body={"marketSlug": market_ticker},
                      session=self._cancel_session)
        if r.status_code == 404:
            return False
        _raise_with_body(r)
        return True

    def find_by_client_id(self, event_ticker: str, client_order_id: str) -> tuple[str, str] | None:
        body = self._bodies.get(client_order_id)
        if body is None:
            return None
        resp = self._json("POST", "/v1/orders", body=body, headers={"Idempotency-Key": client_order_id})
        return (resp["id"], body["marketSlug"]) if resp.get("id") else None

    def open_orders(self) -> list[dict]:
        return self._json("GET", "/v1/orders/open").get("orders") or []

    def _resting(self, event_ticker: str = "", max_pages: int = 10) -> list[tuple[str, str, str | None]]:
        return [(o["id"], o.get("marketSlug") or "", None) for o in self.open_orders()
                if o.get("id") and (o.get("marketSlug") or "").startswith(event_ticker)]

    def order(self, order_id: str) -> dict:
        return self._json("GET", f"/v1/order/{order_id}").get("order") or {}

    def reconcile(self, order_ids: list[str]) -> list[dict]:
        """Fill dicts carrying each order's whole filled quantity (`cum`) and
        average price, for ARBS to book what the websocket didn't. One read
        for the open orders, one per order no longer open."""
        open_ = {o["id"]: o for o in self.open_orders() if o.get("id")}
        out = []
        for oid in order_ids:
            o = open_.get(oid)
            if o is None:
                try:
                    o = self.order(oid)
                except requests.HTTPError:
                    continue
            cum, avg = _amt(o.get("cumQuantity")) or 0.0, _amt(o.get("avgPx"))
            if cum <= 0 or avg is None:
                continue
            side = order_side(o)
            out.append({"ticker": o.get("marketSlug"), "side": side,
                        "avg": avg if side == "yes" else round(1.0 - avg, 6), "cum": cum,
                        "fee_total": _amt(o.get("commissionNotionalTotalCollected")) or 0.0,
                        "order_id": oid, "state": o.get("state")})
        return out

    def fills(self, event_ticker: str = "", max_pages: int = 20, min_ts: int | None = None) -> list[dict]:
        """Trades carry no order id here, so fills are booked from the private
        websocket and reconcile(); this backstop has nothing to add."""
        return []

    # ---------------------------------------------------------------- account

    def positions_all(self, max_pages: int = 20) -> dict[str, float]:
        """slug -> signed YES contracts (negative = NO)."""
        out, cursor = {}, None
        for _ in range(max_pages):
            d = self._json("GET", "/v1/portfolio/positions", params={"limit": 100, **({"cursor": cursor} if cursor else {})})
            for slug, p in (d.get("positions") or {}).items():
                v = _amt(p.get("netPositionDecimal"))
                if v is None:
                    v = _amt(p.get("netPosition")) or 0.0
                if abs(v) > 1e-9 and not p.get("expired"):
                    out[slug] = v
            cursor = d.get("nextCursor")
            if d.get("eof") or not cursor:
                break
        return out

    def shard_balances(self, reserve: float = 0.0) -> tuple[dict[int, float], float]:
        """({PM_SHARD: buying power}, value of positions). An account that was
        never funded lists no balance at all: 0."""
        bs = self._json("GET", "/v1/account/balances").get("balances") or []
        b = bs[0] if bs else {}
        cash = _amt(b.get("buyingPower"))
        if cash is None:
            cash = _amt(b.get("currentBalance")) or 0.0
        return {PM_SHARD: cash}, _amt(b.get("assetNotional")) or 0.0
