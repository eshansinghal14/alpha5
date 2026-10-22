"""
Real Kalshi order placement -- the ONLY module that sends money-moving
requests. The trading engine is a pure shadow simulation otherwise, and
reaches this module exclusively when its `live_trading` flag is on (see
kalshi_infra.trading.engine.LiveEngine.set_live_trading).

Two order shapes, matching the strategy's two intents (see core.Order):

  TAKE  -- priced AT the touch it intends to cross, sent as one Kalshi v2
    create-order with time_in_force="immediate_or_cancel": it takes on
    arrival or is cancelled outright, never resting. So the live and shadow
    paths model the same thing -- resolve_marketable's "filled at the touch,
    or nothing" is what an IOC actually does at the exchange.
  QUOTE -- meant to rest (place_resting), and therefore the only thing here
    that outlives its own request. It has to be cancelled explicitly
    (cancel), it fills with nothing to respond to (poll `fills`), and it is
    still on the book if this process dies -- which is what resting_orders
    and cancel_resting exist to clean up.

Uses the **v2** endpoint (POST /trade-api/v2/portfolio/events/orders); the
v1 one (/trade-api/v2/portfolio/orders) returns HTTP 410
`deprecated_v1_order_endpoint` and cannot place orders at all. Same
endpoint and body shape kalshi_infra/measure_order_latency.py already
places real orders through.

Two things this does that api_functions deliberately doesn't, both because
they are on the order-latency critical path (see execution.MEASURED_SEND_S:
34ms warm vs 105ms over a fresh TCP+TLS connection, and the RSA key parse
is per-call there):
  * one persistent requests.Session, reused for every order,
  * the private key parsed once at construction, not per request.
"""
import contextlib
import dataclasses
import mmap
import os
import struct
import threading
import time
import uuid

import requests
import requests.adapters

from kalshi_backtest.core import Order
from kalshi_infra.kalshi import auth

# Kalshi's dedicated Trade API host, recommended for API traders (docs
# getting_started/api_environments). Measured from the EC2 box 2026-10-19:
# authenticated GET p50 19.7ms here vs 28.6ms on api.elections.kalshi.com.
# Same keys and signed paths; public market data stays on the shared host.
BASE_URL = "https://external-api.kalshi.com"

ORDERS_PATH = "/trade-api/v2/portfolio/events/orders"


def _raise_with_body(resp) -> None:
    """raise_for_status, but with Kalshi's error body in the message. A bare
    requests HTTPError says only "400 Bad Request", which hid WHY every
    resting quote was rejected (2026-10-08)."""
    if resp.status_code < 400:
        return
    try:
        detail = resp.text[:500]
    except Exception:
        detail = ""
    raise requests.HTTPError(f"{resp.status_code} {resp.reason} for {resp.url}: {detail}",
                             response=resp)


LIMITS_PATH = "/trade-api/v2/account/limits"

# An order that hasn't returned in this long is priced off a book that no
# longer exists; holding the executor thread just delays the next one.
ORDER_TIMEOUT_S = 5.0
# Env var through which kalshi_strategies.run gives each child its share of
# the account's write budget (fraction in (0, 1]).
WRITE_SHARE_ENV = "KALSHI_WRITE_SHARE"
# Share of the write budget we actually spend. 0.8 was for the Basic tier,
# whose Kalshi bucket held only one second; on Advanced (300/s, 900-token
# bucket, 2026-10-18) our one-second bucket can't outrun Kalshi's, and no
# process logged a 429 10-14..18 at 0.8.
WRITE_HEADROOM = 1.0


def _client_id(order: Order) -> str:
    cid = getattr(order, "client_order_id", None)
    if cid is None:
        cid = str(uuid.uuid4())
        order.client_order_id = cid
    return cid


def http_status(exc) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def error_code(exc) -> str | None:
    """Kalshi's error code from a rejected request ("trading_is_paused",
    "insufficient_balance", ...), or None if the body has none."""
    try:
        body = exc.response.json()
    except Exception:
        try:
            import json
            text = str(exc)
            body = json.loads(text[text.index("{"):])
        except Exception:
            return None
    err = body.get("error") if isinstance(body, dict) else None
    return err.get("code") if isinstance(err, dict) else None


# Kalshi refusing orders on a market for now -- the market, not our request.
# KXINXDUD 2026-10-22 03:00:05 ET: five 409 trading_is_paused in 3s tripped the
# order-error breaker, and it stayed off ~10h after Kalshi reopened.
PAUSE_CODES = ("trading_is_paused", "market_closed", "market_not_open")


def is_market_paused(exc) -> bool:
    return error_code(exc) in PAUSE_CODES


def is_post_only_reject(exc) -> bool:
    """A resting create refused because it would have crossed (post_only).
    Matched on Kalshi's error body: the exact code isn't documented, so any
    4xx whose body mentions post-only / cross counts."""
    st = http_status(exc)
    if st is None or not (400 <= st < 500) or st == 429:
        return False
    try:
        body = (exc.response.text or "").lower()
    except Exception:
        body = str(exc).lower()
    return "post" in body and ("only" in body or "cross" in body)


def write_budget_tokens_per_s() -> float:
    """The account's write-token refill rate from Kalshi (tier-dependent:
    100/s on Basic, 2026-10-09). Read at session start, so a tier upgrade
    takes effect at the next event."""
    from kalshi_infra.api_functions import kalshi_get
    return float(kalshi_get(LIMITS_PATH, auth=True)["write"]["refill_rate"])


def process_write_tokens_per_s() -> float:
    """This process's share (WRITE_SHARE_ENV) of the account's write budget."""
    import os
    return write_budget_tokens_per_s() * float(os.environ.get(WRITE_SHARE_ENV, "1"))


POSITIONS_PATH = "/trade-api/v2/portfolio/positions"
BALANCE_PATH = "/trade-api/v2/portfolio/balance"
FILLS_PATH = "/trade-api/v2/portfolio/fills"
ALLOCATION_PATH = "/trade-api/v2/portfolio/target_balance_allocation"


@dataclasses.dataclass
class RealFill:
    """What Kalshi actually gave us for one order. `price` is in the
    ORDER'S OWN side terms (no orders in no-price terms), matching
    core.Fill, not the yes-side price the API reports."""
    price: float
    size: float
    fee: float
    exchange_order_id: str | None


@dataclasses.dataclass
class RestingAck:
    """What came back from a RESTING create-order.

    Three parts because they fail independently, and the caller has to react
    to each differently:

      exchange_order_id -- what a later cancel needs. Read before the fill is
        parsed, so an unreadable fill can never cost us the ability to pull
        the order back.
      fill              -- whatever crossed on arrival. A quote priced inside
        a book that moved during the round trip fills like a take.
      parse_error       -- the response carried a fill we could not read.
        NOT the same as no fill: the caller must treat it as an unaccounted
        position, because that is what it might be.
    """
    exchange_order_id: str | None
    fill: RealFill | None
    parse_error: Exception | None = None


def _dollars(payload: dict, base: str) -> float | None:
    """Read `{base}_dollars` as a float. ONLY that field -- never the bare
    `{base}`, and never a magnitude heuristic.

    An earlier version fell back to `payload[base] / 100`, on the assumption
    that the bare field is always integer cents. It isn't: the create-order
    response reports price and fee in DOLLARS under the bare name, so every
    real fill was booked at 1/100 of its true price and fee (a yes sale at
    $0.52 recorded as $0.0052). Positions and balance were unaffected --
    they do carry `_dollars` variants -- which is exactly why the bug was
    invisible outside the fill ledger.

    Every quantity this module reads has a `_dollars` form, so returning
    None when it's missing is strictly better than guessing at units.
    """
    v = payload.get(base + "_dollars")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    """Plain float, no unit conversion. For fields already known to be in
    dollars under a bare name -- see _parse_fill's create-response branch."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _count(payload: dict, base: str) -> float | None:
    """Read a contract count: `{base}_fp` (decimal string) or bare `{base}`.

    BOTH spellings, unlike _dollars, because a count carries no unit
    ambiguity -- 1 contract is 1 either way, so accepting whichever the
    endpoint happens to send cannot produce a wrong number, only a right one
    or None. The two really do differ by endpoint: GET /portfolio/orders
    returns `fill_count_fp`, the create-order response returns `fill_count`.
    Reading only the `_fp` form made every real fill parse as size 0, i.e.
    as a no-take, which froze the position ledger and disabled the strategy's
    own cap -- 86 orders filled at Kalshi while we believed we held nothing.
    """
    for key in (base + "_fp", base):
        v = payload.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


class WriteBudget:
    """The write-token bucket. One per order client by default; several
    clients in one process (a write group's series) can share one, so they
    draw on a single budget instead of fixed slices of it. Thread-safe."""

    def __init__(self, tokens_per_s: float):
        self.tokens_per_s = tokens_per_s
        self.rate = tokens_per_s * WRITE_HEADROOM
        # Basic tier holds one second of budget (higher tiers three; one is
        # the safe floor for either).
        self.capacity = self.rate
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float):
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def tokens_now(self) -> float:
        with self._lock:
            self._refill(time.monotonic())
            return self._tokens

    def take(self, cost: float, force: bool = False) -> bool:
        """Spend `cost`, or refuse. force=True always spends (may go negative)."""
        with self._lock:
            self._refill(time.monotonic())
            if self._tokens < cost and not force:
                return False
            self._tokens -= cost
            return True

    def empty(self):
        """Kalshi said 429: its bucket is emptier than ours thinks."""
        with self._lock:
            self._tokens = min(self._tokens, 0.0)
            self._last = time.monotonic()


class SharedWriteBudget(WriteBudget):
    """One write bucket for every process on this machine trading `shard`:
    the token count lives in a small shared file, updated under a file lock,
    so the race and the single coins draw on the shard's whole budget
    together instead of fixed slices of it. Fixed slices left the shard
    oversubscribed (300 + 60 + 30 against Kalshi's 300, 45 race 429s on
    10-20) while most of each slice sat unused (59% of 300 used overall).

    Monotonic time is system-wide on Linux and Windows, so every process
    refills off one clock. Same interface as WriteBudget."""

    _SIZE = struct.Struct("<dd")          # tokens, last refill (time.monotonic)

    def __init__(self, tokens_per_s: float, shard: int, path: str | None = None):
        self.tokens_per_s = tokens_per_s
        self.rate = tokens_per_s * WRITE_HEADROOM
        self.capacity = self.rate
        self.path = path or _shared_budget_path(shard)
        self._lock = threading.Lock()     # threads of this process; the file lock covers processes
        new = not os.path.exists(self.path)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        if os.fstat(self._fd).st_size < self._SIZE.size:
            os.write(self._fd, self._SIZE.pack(self.capacity, time.monotonic()))
            new = True
        self._mm = mmap.mmap(self._fd, self._SIZE.size)
        if new:
            with self._locked():
                self._mm[:] = self._SIZE.pack(self.capacity, time.monotonic())

    @contextlib.contextmanager
    def _locked(self):
        with self._lock:
            _flock(self._fd, True)
            try:
                yield
            finally:
                _flock(self._fd, False)

    def _update(self, fn):
        """fn(tokens after refill) -> (new tokens, result), under the lock."""
        with self._locked():
            tokens, last = self._SIZE.unpack(self._mm[:])
            now = time.monotonic()
            # After a reboot the stored clock is from the old boot.
            elapsed = max(0.0, now - last) if last <= now else 0.0
            tokens = min(self.capacity, tokens + elapsed * self.rate)
            tokens, out = fn(tokens)
            self._mm[:] = self._SIZE.pack(tokens, now)
            return out

    def tokens_now(self) -> float:
        """Read without the file lock. The order manager reads this dozens of
        times per pump on the loop, and each flock released the GIL and waited
        to win it back: 150-500ms loop stalls while armed (py-spy, race
        10-22 00:14-00:17 UTC). Advisory only -- take() still locks -- so a
        torn read just costs one refused or deferred write."""
        tokens, last = self._SIZE.unpack(self._mm[:])
        now = time.monotonic()
        elapsed = max(0.0, now - last) if last <= now else 0.0
        return min(self.capacity, tokens + elapsed * self.rate)

    def take(self, cost: float, force: bool = False) -> bool:
        return self._update(lambda t: (t, False) if t < cost and not force else (t - cost, True))

    def empty(self):
        self._update(lambda t: (min(t, 0.0), None))


def _shared_budget_path(shard: int) -> str:
    import tempfile
    root = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    return os.path.join(root, f"alpha5-write-shard{int(shard)}.bucket")


def _flock(fd: int, on: bool):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX if on else fcntl.LOCK_UN)


_SHARED: dict[int, SharedWriteBudget] = {}
_SHARED_LOCK = threading.Lock()


def shard_budget(shard: int) -> WriteBudget:
    """The machine-wide bucket for `shard` at the account's full refill rate
    (one per process). KALSHI_WRITE_SHARE no longer splits it."""
    with _SHARED_LOCK:
        b = _SHARED.get(shard)
        if b is None:
            b = _SHARED[shard] = SharedWriteBudget(write_budget_tokens_per_s(), shard)
        return b


class KalshiOrderClient:
    """Places real IOC orders and reads real positions. Every method here
    does blocking network I/O and MUST be called off the asyncio event loop
    (LiveEngine runs them on an executor thread) -- a ~40ms POST on the loop
    stalls both feeds and every scheduled order resolution."""

    def __init__(self, tokens_per_s: float, timeout_s: float = ORDER_TIMEOUT_S,
                 budget: WriteBudget | None = None):
        """`tokens_per_s`: this process's share of the account's write budget
        (see write_budget_tokens_per_s). `budget`: a bucket shared with other
        clients; tokens_per_s is then taken from it."""
        self._session = requests.Session()
        # Cancels get their own connection pool: on the shared one a burst of
        # creates (12 in flight, 10-20) could hold every pooled connection.
        self._cancel_session = requests.Session()
        # One pooled connection per cancel worker (engine.CANCEL_WORKERS); past
        # the default 10, urllib3 opens and discards a fresh TLS connection
        # (~105ms vs ~34ms warm, see execution.MEASURED_SEND_S).
        self._cancel_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=16))
        # No proxy/netrc/CA env lookups per request (a netrc stat on every
        # write: 2% of the race process's samples, py-spy 10-21 21:45 UTC).
        # Nothing in the environment sets them.
        for s in (self._session, self._cancel_session):
            s.trust_env = False
        self._key = auth.load_private_key()
        self._lock = threading.Lock()
        self.budget = budget if budget is not None else WriteBudget(tokens_per_s)
        self.tokens_per_s = self.budget.tokens_per_s
        self._rate = self.budget.rate
        self.timeout_s = timeout_s
        self.n_rate_limited = 0
        # Write-budget accounting by (kind, reason): spent tokens and call
        # counts, refusals, and 429s. See usage().
        self._spent: dict[tuple, list] = {}
        self._refused: dict[tuple, int] = {}
        self._429: dict[str, int] = {}
        self._t0 = time.monotonic()

    @classmethod
    def from_account(cls, shard: int | None = None, budget: WriteBudget | None = None) -> "KalshiOrderClient":
        """`shard`: the exchange_index every write is pinned to (see
        _shard_field). A pinned client draws on the machine-wide bucket for
        that shard (shard_budget) unless a `budget` is passed; unpinned falls
        back to this process's WRITE_SHARE_ENV slice."""
        if budget is None and shard is not None:
            budget = shard_budget(shard)
        c = cls(tokens_per_s=process_write_tokens_per_s() if budget is None else budget.tokens_per_s,
                budget=budget)
        c.shard = shard
        c.warm()
        return c

    def warm(self):
        """Open the cancel session's connection now, so the first cancel of an
        event isn't the one paying for TCP+TLS. A public GET: no write tokens."""
        try:
            self._cancel_session.get(BASE_URL + "/trade-api/v2/exchange/status", timeout=self.timeout_s)
        except Exception:
            pass

    # Exchange shard every order write is pinned to, or None to auto-route.
    # Per GET /getting_started/rate_limits (2026-10-10): a single create,
    # amend or cancel with exchange_index >= 1 bills only that shard's write
    # bucket, each carrying the full tier budget; exchange_index 0 bills the
    # unscoped bucket; an auto-routed write (no index) bills EVERY shard's
    # bucket. Batch writes always bill the unscoped bucket.
    shard: int | None = None

    def _shard_field(self) -> dict:
        return {} if self.shard is None else {"exchange_index": int(self.shard)}

    @property
    def max_orders_per_s(self) -> float:
        return self.tokens_per_s / self.COST["order"]

    def _headers(self, method: str, path: str) -> dict:
        return {**auth.rest_headers(method, path, self._key), "Content-Type": "application/json"}

    # Kalshi write-token costs (GET /account/endpoint_costs, 2026-10-09):
    # create/amend default 10, cancel 2. The bucket refills at budget/s and
    # holds one second of budget on the Basic tier.
    COST = {"order": 10, "amend": 10, "cancel": 2}

    def note_rate_limited(self, reason: str = "unknown"):
        """Kalshi said 429: its bucket is emptier than ours thinks. Empty
        ours so the next writes wait for real refill."""
        self.budget.empty()
        with self._lock:
            self.n_rate_limited += 1
            self._429[reason] = self._429.get(reason, 0) + 1

    def usage(self) -> dict:
        """Write-budget spend since construction (or reset_usage): per
        "kind:reason", calls, tokens and tokens/s, plus refusals and 429s.
        `share` is of the budget we let ourselves spend (WRITE_HEADROOM x
        this process's share)."""
        with self._lock:
            el = max(1e-9, time.monotonic() - self._t0)
            spent = {f"{k}:{r}": {"calls": n, "tokens": t, "tokens_per_s": round(t / el, 2),
                                  "share": round(t / el / self._rate, 4) if self._rate else None}
                     for (k, r), (n, t) in sorted(self._spent.items(), key=lambda kv: -kv[1][1])}
            total = sum(t for _, t in self._spent.values())
            return {"elapsed_s": round(el, 1), "budget_tokens_per_s": round(self._rate, 2),
                    "total_tokens": total, "total_tokens_per_s": round(total / el, 2),
                    "utilization": round(total / el / self._rate, 4) if self._rate else None,
                    "by_reason": spent,
                    "refused": {f"{k}:{r}": n for (k, r), n in sorted(self._refused.items(), key=lambda kv: -kv[1])},
                    "http_429": dict(self._429)}

    def reset_usage(self):
        with self._lock:
            self._spent, self._refused, self._429 = {}, {}, {}
            self._t0 = time.monotonic()

    # Optional cap on THIS client's spend, tokens/s, on top of the shared
    # bucket (quoting.max_write_tokens_per_s, read live). None = uncapped.
    cap_fn = None
    _cap: "WriteBudget | None" = None

    def _cap_bucket(self) -> "WriteBudget | None":
        rate = self.cap_fn() if self.cap_fn is not None else None
        if not rate:
            self._cap = None
            return None
        if self._cap is None or abs(self._cap.tokens_per_s - rate) > 1e-9:
            self._cap = WriteBudget(rate)
        return self._cap

    def tokens_now(self) -> float:
        """Tokens this client may spend now: the shared bucket, or its own cap
        if that's lower (may be negative after a forced cancel)."""
        t = self.budget.tokens_now()
        cap = self._cap_bucket()
        return t if cap is None else min(t, cap.tokens_now())

    def can_afford(self, kind: str = "order", reserve: float = 0.0) -> bool:
        """Would `kind` be allowed now with `reserve` tokens left over?
        Spends nothing. Lets the quoter skip building an order the bucket
        would refuse."""
        return self.tokens_now() >= self.COST[kind] + reserve

    def allow_now(self, kind: str = "order", force: bool = False, reason: str = "untagged") -> bool:
        """Spend `kind`'s tokens from the bucket, or refuse.

        Refusing rather than sleeping is deliberate: a queued order arriving
        late would cross a book nobody looked at, and the quoter re-decides on
        the next tick anyway.

        force=True always goes through (the bucket may go negative, which
        throttles everything after it): for cancels that MUST land -- a
        refused cancel leaves a real order resting where we no longer want
        it, which is strictly worse than a 429."""
        cost = self.COST[kind]
        cap = self._cap_bucket()
        if cap is not None and not force and cap.tokens_now() < cost:
            ok = False
        else:
            ok = self.budget.take(cost, force=force)
            if ok and cap is not None:
                cap.take(cost, force=True)
        with self._lock:
            if not ok:
                self._refused[(kind, reason)] = self._refused.get((kind, reason), 0) + 1
                return False
            rec = self._spent.setdefault((kind, reason), [0, 0.0])
            rec[0] += 1
            rec[1] += cost
            return True

    def amend(self, exchange_order_id: str, order: Order, new_size: float, filled: float,
              price: float | None = None) -> dict:
        """Change a resting order's size and/or price (`price` in the order's
        own side terms; None keeps it). Kalshi keeps queue position only on a
        pure size decrease; a price change goes to the back of the queue.
        One amend costs 10 tokens, the same as one create -- so a reprice is
        half the cost of cancel (2) + create (10) and never leaves the side
        unquoted in between."""
        px = order.price if price is None else price
        yes_price = px if order.side == "yes" else 1.0 - px
        path = f"{ORDERS_PATH}/{exchange_order_id}/amend"
        body = {"ticker": order.ticker, "side": "bid" if order.side == "yes" else "ask",
                "price": f"{round(yes_price, 2):.4f}", "count": f"{filled + new_size:.2f}",
                **self._shard_field()}
        resp = self._session.post(BASE_URL + path, headers=self._headers("POST", path), json=body,
                                  timeout=self.timeout_s)
        _raise_with_body(resp)
        return resp.json()

    def place_ioc(self, order: Order) -> RealFill | None:
        """Send `order` as an immediate-or-cancel take. Returns the fill, or
        None if the IOC took nothing (the touch moved inside the round trip
        -- nothing is left resting, so there is nothing to clean up).

        The v2 API's `side` names which side of the YES book the order joins,
        so our core.Order's yes/no side becomes bid/ask and a no order's
        price is converted to its yes-side equivalent. Buying no at q and
        selling yes at 1-q are the same order to Kalshi's engine; core.py
        keeps the no-side representation because that's what settles.
        """
        return self._parse_fill(order, self._create(order, time_in_force="immediate_or_cancel"))

    def place_resting(self, order: Order) -> RestingAck:
        """Send `order` as a RESTING limit order: it joins the book and stays
        there until it fills or `cancel` pulls it.

        Same body and side convention as place_ioc, with
        time_in_force="good_till_canceled". The v2 create-order REQUIRES
        time_in_force (CreateOrderV2Request.required in the API spec); an
        earlier version omitted it to "rest by default", and every resting
        quote came back 400 Bad Request until the circuit breaker opened
        (2026-10-08, KXCRYPTOLEAD15M).

        Post-only: an order that would cross is rejected instead of taking.
        It only crosses when the book moved through it in flight, and those
        takes paid the taker fee (~1.4c/lot) on edge that wasn't there:
        2026-10-14/15, 539 crossed fills netted -0.5 to -1.1c/lot at 10s
        after fees. The next decision re-quotes against the new book. `fill`
        on the ack stays for safety but should never be set.
        """
        payload = self._create(order, time_in_force="good_till_canceled", post_only=True)
        o = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        # Read BEFORE parsing the fill: an unparseable fill must not also cost
        # us the id that pulls the order back off the book.
        exchange_order_id = o.get("order_id")
        try:
            fill = self._parse_fill(order, payload)
        except Exception as exc:
            return RestingAck(exchange_order_id=exchange_order_id, fill=None, parse_error=exc)
        return RestingAck(exchange_order_id=exchange_order_id, fill=fill)

    def _create(self, order: Order, time_in_force: str, post_only: bool = False) -> dict:
        """POST one create-order and return its JSON. `time_in_force` is
        required by the API: "immediate_or_cancel" for takes,
        "good_till_canceled" for resting quotes."""
        yes_price = order.price if order.side == "yes" else 1.0 - order.price
        body = {
            "ticker": order.ticker,
            "side": "bid" if order.side == "yes" else "ask",
            "count": f"{order.size:.2f}",
            "price": f"{round(yes_price, 2):.4f}",
            "self_trade_prevention_type": "taker_at_cross",
            # Kept on the order: a create that timed out on our side may still
            # rest at Kalshi, and this id is the only way to find it again.
            "client_order_id": _client_id(order),
            "time_in_force": time_in_force,
            **self._shard_field(),
        }
        if post_only:
            body["post_only"] = True
        path = ORDERS_PATH
        resp = self._session.post(BASE_URL + path, headers=self._headers("POST", path),
                                  json=body, timeout=self.timeout_s)
        _raise_with_body(resp)
        return resp.json()

    def cancel(self, exchange_order_id: str, market_ticker: str | None = None) -> bool:
        """Pull one resting order. True if Kalshi cancelled it, False if it
        was already gone (404) -- an order that filled or expired between our
        decision to cancel and the request arriving is not an error, and
        raising on it would trip the consecutive-error breaker for something
        entirely normal.

        V2 cancel (DELETE /portfolio/events/orders/{id}), the pair of the v2
        create. With the client's shard set, it's sent with that
        exchange_index (billed to that shard's own bucket); otherwise
        `market_ticker` auto-routes it, which bills EVERY shard's bucket."""
        if not market_ticker:
            # The v1 path costs 10 tokens to the v2 path's 2 (endpoint_costs,
            # 2026-10-10) and can't route to a non-zero shard. Every caller has
            # the ticker; fail loudly rather than silently pay 5x.
            raise ValueError(f"cancel {exchange_order_id}: market_ticker is required")
        path = f"{ORDERS_PATH}/{exchange_order_id}"
        params = {"market_ticker": market_ticker, **self._shard_field()}
        resp = self._cancel_session.delete(BASE_URL + path, headers=self._headers("DELETE", path),
                                           params=params, timeout=self.timeout_s)
        if resp.status_code == 404:
            return False
        _raise_with_body(resp)
        return True

    def queue_positions(self, event_ticker: str) -> dict[str, float]:
        """exchange order id -> contracts ahead of it AT ITS OWN PRICE (price-
        time priority) for every resting order in the event. Verified live
        2026-10-11: excludes better-priced levels (a 0.84 order behind 151 at
        0.85 reported 120 = the queue at 0.84 only). A GET, so read bucket."""
        path = "/trade-api/v2/portfolio/orders/queue_positions"
        resp = self._session.get(BASE_URL + path, headers=self._headers("GET", path),
                                 params={"event_ticker": event_ticker}, timeout=self.timeout_s)
        resp.raise_for_status()
        # Kalshi sends "queue_positions": null, not [], with nothing resting
        # (894 TypeErrors in run.err.log by 2026-10-16 11:04 UTC).
        return {q["order_id"]: float(q.get("queue_position_fp") or 0.0)
                for q in resp.json().get("queue_positions") or [] if q.get("order_id")}

    def find_by_client_id(self, event_ticker: str, client_order_id: str) -> tuple[str, str] | None:
        """(exchange order id, ticker) of our RESTING order with this client
        id, or None. For a create whose response never came back."""
        for oid, ticker, cid in self._resting(event_ticker):
            if cid == client_order_id:
                return oid, ticker
        return None

    def resting_orders(self, event_ticker: str, max_pages: int = 10) -> list[tuple[str, str]]:
        return [(oid, t) for oid, t, _ in self._resting(event_ticker, max_pages)]

    def _resting(self, event_ticker: str, max_pages: int = 10) -> list[tuple[str, str, str | None]]:
        """(exchange order id, market ticker, client order id) for every order
        this account still has resting in `event_ticker`, oldest first.

        Ground truth for "is anything of ours left on the book", which our
        own map of live quotes cannot answer after a crash or a restart --
        the process that placed them is the one that forgot them. Filtered
        CLIENT-side on the ticker prefix, same as `fills`: this endpoint's
        own event scoping is not trusted here for the same reason.
        """
        out, cursor = [], None
        for _ in range(max_pages):
            params = {"status": "resting", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            resp = self._session.get(BASE_URL + "/trade-api/v2/portfolio/orders",
                                     headers=self._headers("GET", "/trade-api/v2/portfolio/orders"),
                                     params=params, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("orders", [])
            for o in batch:
                ticker = o.get("ticker") or o.get("market_ticker") or ""
                if ticker.startswith(event_ticker) and o.get("order_id"):
                    out.append((o["order_id"], ticker, o.get("client_order_id")))
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return out

    def _parse_fill(self, order: Order, payload: dict) -> RealFill | None:
        """Turn a create-order response into what actually traded.

        Derived from FILL COST rather than any average-price field. The
        response's `*_fill_cost_dollars` is the total dollars we paid for
        what we acquired, already in the order's own side terms, so
        cost / count is the fill price core.Fill wants with no yes/no
        conversion and no unit ambiguity. `average_fill_price` -- which an
        earlier version used -- carries no `_dollars` variant and is quoted
        in dollars under a bare name, which is precisely how every fill came
        to be booked 100x too small.

        Both taker and maker legs are summed: an IOC is all-taker in
        practice, but a partial cross could in principle rest for an instant,
        and silently dropping the maker leg would understate the fill.
        """
        o = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        order_id = o.get("order_id")
        size = _count(o, "fill_count")
        if size is None:
            # NEVER fall through to "no fill" here. A missing count is an
            # unreadable response, not a confirmed zero, and treating the two
            # alike is exactly what let a position run past its cap.
            raise ValueError(f"cannot read fill count for {order.ticker} from order response "
                             f"(keys: {sorted(o)})")
        if size <= 0:
            return None

        # Two response shapes, both fully determined -- no unit guessing and,
        # deliberately, no second request. An earlier version re-read the
        # order from GET /portfolio/orders/{id} whenever the rich fields were
        # absent; that 404s, because we ask microseconds after creating the
        # order and the read side hasn't caught up. Four such orders filled at
        # Kalshi and errored here, leaving the ledger 4 contracts short.
        #
        #   GET shape    -- *_fill_cost_dollars: total dollars for what we
        #                   acquired, already in the order's own side terms.
        #   create shape -- average_fill_price / average_fee_paid: per
        #                   contract, YES-side, and in DOLLARS despite the
        #                   bare name. Reading them as cents is what booked
        #                   every fill at 1/100 of its price; the fix is to
        #                   not divide, not to avoid the fields.
        cost = (_dollars(o, "taker_fill_cost") or 0.0) + (_dollars(o, "maker_fill_cost") or 0.0)
        fee = (_dollars(o, "taker_fees") or 0.0) + (_dollars(o, "maker_fees") or 0.0)
        if cost > 0:
            price = cost / size
        else:
            yes_price = _float(o.get("average_fill_price"))
            if yes_price is None:
                raise ValueError(f"order {order_id} on {order.ticker} filled {size} but carries "
                                 f"neither *_fill_cost_dollars nor average_fill_price "
                                 f"(keys: {sorted(o)})")
            price = yes_price if order.side == "yes" else 1.0 - yes_price
            fee = (_float(o.get("average_fee_paid")) or 0.0) * size
        # LiveEngine cross-checks price against the order's own limit and
        # halts if it's outside -- a units mistake here cannot go quiet.
        return RealFill(price=price, size=size, fee=fee, exchange_order_id=order_id)

    def order(self, order_id: str) -> dict:
        """One order as Kalshi currently sees it -- the authoritative record
        of what filled, in fully unit-qualified fields."""
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        resp = self._session.get(BASE_URL + path, headers=self._headers("GET", path),
                                 timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return data.get("order") if isinstance(data.get("order"), dict) else data

    def balance(self, exchange_index: int | None = None) -> float:
        """Available balance in dollars, on one exchange shard if
        `exchange_index` is given, else summed across all of them.

        Pass the shard the traded event lives on. Collateral is checked
        per-shard, so an account-wide total can be positive while the
        trading shard is empty -- every order then 404s
        `insufficient_shard_balance` (2026-10-08: $165 on shard 0, $0 on
        crypto's shard 2)."""
        params = {"exchange_index": exchange_index} if exchange_index is not None else None
        resp = self._session.get(BASE_URL + BALANCE_PATH,
                                 headers=self._headers("GET", BALANCE_PATH),
                                 params=params, timeout=self.timeout_s)
        resp.raise_for_status()
        return _dollars(resp.json(), "balance") or 0.0

    def target_allocation(self) -> dict:
        """Kalshi's auto-rebalance target: {"allocations": {shard: percent},
        "resting_margin_reservation": ...}. Empty allocations = rebalancing off."""
        resp = self._session.get(BASE_URL + ALLOCATION_PATH,
                                 headers=self._headers("GET", ALLOCATION_PATH),
                                 timeout=self.timeout_s)
        _raise_with_body(resp)
        data = resp.json()
        return {"allocations": {int(a["exchange_index"]): int(a["percent"])
                                for a in data.get("allocations", [])},
                "resting_margin_reservation": data.get("resting_margin_reservation")}

    def set_target_allocation(self, allocations: dict[int, int],
                              resting_margin_reservation: str = "sum") -> None:
        """Replace the auto-rebalance target with {shard: percent}. Kalshi
        sweeps toward it every ~10s. Percents must total 100; `{}` turns
        rebalancing off.

        "sum" leaves every resting order's collateral where it is, so a
        rebalance can't strand a quote we already have up on a shard."""
        if allocations and sum(allocations.values()) != 100:
            raise ValueError(f"allocation percents must total 100, got {allocations}")
        body = {"allocations": [{"exchange_index": int(s), "percent": int(p)}
                                for s, p in sorted(allocations.items())],
                "resting_margin_reservation": resting_margin_reservation}
        resp = self._session.post(BASE_URL + ALLOCATION_PATH,
                                  headers=self._headers("POST", ALLOCATION_PATH),
                                  json=body, timeout=self.timeout_s)
        _raise_with_body(resp)

    def exchange_index(self, event_ticker: str) -> int:
        """Exchange shard `event_ticker` trades on (all its markets share it)."""
        path = f"/trade-api/v2/events/{event_ticker}"
        resp = self._session.get(BASE_URL + path, headers=self._headers("GET", path),
                                 timeout=self.timeout_s)
        resp.raise_for_status()
        return int(resp.json()["event"]["exchange_index"])

    def fills(self, event_ticker: str, max_pages: int = 20, min_ts: int | None = None) -> list[dict]:
        """Every fill this account has in `event_ticker`, oldest first.

        `min_ts` (unix seconds) narrows the server-side window. Used by the
        quote-fill poll, which runs every second and only cares about what
        just happened; without it that poll would page the whole day's fills
        once a second. Unlike the event scoping below, a min_ts the server
        ignored can only return MORE than asked for, never less, so trusting
        it costs nothing.

        Filtered CLIENT-side on the ticker prefix: /portfolio/fills accepts
        an `event_ticker` param but ignores it -- confirmed directly, a
        request scoped to a KXINXU event came back carrying KXINX fills too.
        Passing it and trusting the server would silently import another
        series' trades into this session's ledger.

        `outcome_side` is the side the fill LEFT US HOLDING (a sell-yes
        that opens a short reports outcome_side="no"), which is the
        convention core.Fill.side uses, so it maps across directly. That
        reading is checked, not assumed: seed_fills_from_kalshi replays
        these and compares the implied position against the positions
        endpoint.
        """
        out, cursor = [], None
        for _ in range(max_pages):
            params = {"limit": 1000}
            if min_ts is not None:
                params["min_ts"] = int(min_ts)
            if cursor:
                params["cursor"] = cursor
            resp = self._session.get(BASE_URL + FILLS_PATH,
                                     headers=self._headers("GET", FILLS_PATH),
                                     params=params, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("fills", [])
            for f in batch:
                ticker = f.get("ticker") or f.get("market_ticker")
                if not ticker or not ticker.startswith(event_ticker):
                    continue
                side = f.get("outcome_side")
                price = _dollars(f, "yes_price" if side == "yes" else "no_price")
                size = _count(f, "count")
                if side not in ("yes", "no") or price is None or not size:
                    continue
                out.append({
                    "ticker": ticker, "side": side, "price": price, "size": size,
                    "fee": float(f.get("fee_cost") or 0.0),
                    "time": f.get("created_time"),
                    "liquidity": "taker" if f.get("is_taker") else "maker",
                    "order_id": f.get("order_id"),
                    # Identifies THIS execution, not the order it belongs to.
                    # One resting order can fill in several pieces, so the
                    # quote-fill poll dedupes on (order_id, trade_id) -- an
                    # order_id alone would drop every partial after the first.
                    "trade_id": f.get("trade_id"),
                })
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        out.sort(key=lambda f: f["time"] or "")
        return out

    def positions(self, event_ticker: str) -> dict[str, dict]:
        """Kalshi's own record of what we hold in `event_ticker`, keyed by
        market ticker. `position` is signed YES contracts (negative = long
        no) and `market_exposure` is that position's cost basis in dollars.
        Markets we're flat in are omitted by the endpoint, so a missing key
        means zero."""
        params = {"event_ticker": event_ticker, "limit": 1000}
        resp = self._session.get(BASE_URL + POSITIONS_PATH,
                                 headers=self._headers("GET", POSITIONS_PATH),
                                 params=params, timeout=self.timeout_s)
        resp.raise_for_status()
        out = {}
        for p in resp.json().get("market_positions", []):
            ticker = p.get("ticker")
            if ticker is None:
                continue
            out[ticker] = {
                "position": float(p.get("position_fp", p.get("position", 0)) or 0),
                "market_exposure": _dollars(p, "market_exposure"),
                "fees_paid": _dollars(p, "fees_paid"),
            }
        return out
