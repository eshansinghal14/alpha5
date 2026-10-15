"""
Order manager: turns the quoter's DESIRED quotes into exchange writes, in
priority order, inside the account write bucket.

The quoter never sends a quote directly. Each decision it publishes, per
(ticker, side):
    an EDGE ladder   prices it wants resting (`set_desired`), plus the edge
                     limit -- the worst own-side price still outside the quote
                     reservation;
    a LIP quote      one price and size, or none (`set_lip`).
This module diffs that against what is actually live and sends, highest
priority first:

    1  cancels        queued; sent nearest-the-touch first using the WHOLE
                      bucket. While any is waiting, nothing below is sent.
    2  takes          sent by the engine directly, never budgeted here.
    3  best edge level      each side's top ladder price (move or create).
    4  LIP create, then LIP move
    5  edge levels behind the best, nearest the touch first.
    6  trims          cancel edge quotes deeper than the ladder (extra levels
                      left behind as the touch moved up), deepest first, only
                      when nothing above is waiting -- they're resting outside
                      the edge, so they're the cheapest risk to leave a moment.

There is no separate quoting rate: 3-6 may spend whatever is in the bucket
above `cancels_token` (quoting.cancels_token). That reserve is what's kept for
the sparse cancels normal quoting needs (a fair move pushing a level inside
the edge). A full pull (stale signal, halt) needs no special path either:
everything is queued as cancels, quoting stops, and the whole bucket drains
them touch-first.

A queued create carries no price of its own: it's re-derived from the
CURRENT desired set each pump, so a queue that lagged behind a fair move can
never send a stale quote.

A side whose ladder moved while one of its creates or amends was in flight
gets no new create or move until Kalshi answers (see `missing`); cancels
are never held back.

An edge quote is cancelled when (a) it is more than `keep_inside` inside the
edge, (b) a pull, (c) its size is needed under the position cap, (d) it has
sat above others' touch + max_improve_ticks for `ahead_grace_s` with no
ladder level to move down to, (e) the side has more orders than
`Desired.max_levels` (off-ladder ones, most aggressive first, ahead of any
create), or (f) the low-priority trim. `keep_inside`
only applies to quotes already resting: nothing is ever placed or moved
inside the edge.

The LIP quote is cancelled at once when its side has no LIP target (no fair,
stale signal, too little queue ahead, LIP off), and moved -- one amend -- when
the target price or size changes and it has rested at least `lip_hold_s`.

Slot state per order, the one source of truth for "what is live":

    creating   POST sent, no ack yet          counts as resting for risk
    resting    acked, on the book
    amending   price/size amend sent          a cancel waits for its answer
    cancelling DELETE sent                    still counts until confirmed
    (gone)     removed once cancel/fill/reject is confirmed

Real and shadow use the same manager and the same bucket rules; only the
executor differs (see LiveEngine), so a shadow session predicts the real
one's write spend and queue lag.
"""
from __future__ import annotations

import collections
import dataclasses
import heapq
import time

from kalshi_infra.kalshi.venue import TICK

# Write-token costs (GET /account/endpoint_costs, 2026-10-10): create and
# amend 10, v2 cancel 2.
COST_CREATE = 10.0
COST_AMEND = 10.0
COST_CANCEL = 2.0

CREATING, RESTING, AMENDING, CANCELLING = "creating", "resting", "amending", "cancelling"
EDGE, LIP = "edge", "lip"

# Creates at the same (ticker, side, role) price that Kalshi rejected: back
# off this long before trying that price again, so a persistent reject can't
# burn the bucket once per decision.
REJECT_BACKOFF_S = 2.0

# LIP re-queue: an amend adding this many lots (Kalshi's minimum size step)
# moves the order to the back of its queue. A side is re-queued at most once
# per LIP_REQUEUE_MIN_S: five sides at that rate is 10 tokens/s, an eighth of
# the 80/s crypto bucket.
LIP_REQUEUE_BUMP = 0.01
LIP_REQUEUE_MIN_S = 5.0
# Bumps accumulate (each re-queue adds another 0.01); a LIP quote this far over
# its target is left alone rather than amended back down. 100 re-queues.
LIP_REQUEUE_SLACK = 1.0

# Window for the live cancel-rate stats in summary().
CANCEL_STATS_WINDOW_S = 900

# Reasons that are part of a full pull rather than normal quoting.
_PULL_REASONS = ("pull", "halt", "disarm", "stop", "shutdown", "not_quotable")


@dataclasses.dataclass
class Quote:
    """One of our resting (or in-flight) quote orders."""
    order_id: int
    ticker: str
    side: str              # "yes" | "no" -- own-side terms throughout
    price: float
    size: float
    state: str = CREATING
    exchange_id: str | None = None
    filled: float = 0.0
    sent_at: float = 0.0   # clock() at the create, or at the last landed move
    role: str = EDGE
    prev_price: float | None = None   # set while an amend is in flight
    prev_size: float | None = None
    ahead_since: float | None = None  # clock() since it's been above Desired.max_ahead_price

    @property
    def remaining(self) -> float:
        return max(self.size - self.filled, 0.0)


@dataclasses.dataclass
class Desired:
    """What the quoter wants on one (ticker, side): prices nearest the touch
    first, own-side terms, each `lots` contracts. `edge_limit` is the best
    (highest) own-side price that is still outside the quote reservation; any
    live quote above it is inside the edge."""
    prices: list[float]
    lots: float
    edge_limit: float | None   # None = nothing is acceptable (no reservation / blocked)
    room: float                # contracts this side may have resting in total
    # Highest own-side price we're willing to keep resting relative to OTHERS'
    # touch (their best + max_improve_ticks). None = no cap (nobody else
    # bidding). A quote above it for ahead_grace_s is moved down to the
    # ladder, or cancelled.
    max_ahead_price: float | None = None
    # Most edge orders this side may have live (quote_levels_behind + 1).
    # None = no cap. Levels left ahead of a ladder that stepped back count
    # too: creating the new levels under them doubled our size into a sweep
    # (BTC 5 levels / 4.0 lots vs a 2-level ladder, 2026-10-12 17:25:51).
    max_levels: int | None = None


@dataclasses.dataclass
class LipTarget:
    """The LIP quote wanted on one side: one order, own-side price."""
    price: float
    size: float


class TokenBucket:
    """Refills at `rate` tokens/s, holds at most one second."""

    def __init__(self, rate: float, clock=time.monotonic):
        self._clock = clock
        self.rate = rate
        self._tokens = rate
        self._t = clock()

    def available(self) -> float:
        now = self._clock()
        self._tokens = min(self.rate, self._tokens + (now - self._t) * self.rate)
        self._t = now
        return self._tokens

    def spend(self, n: float):
        self.available()
        self._tokens -= n


class CancelStats:
    """Cancels sent per wall second, split into sparse (normal quoting) and
    pull, over the last CANCEL_STATS_WINDOW_S."""

    def __init__(self, clock):
        self._clock = clock
        self.sparse: collections.Counter = collections.Counter()
        self.pull: collections.Counter = collections.Counter()
        self._t0 = clock()

    def note(self, reason: str):
        sec = int(self._clock())
        (self.pull if reason.startswith(_PULL_REASONS) else self.sparse)[sec] += 1

    def _prune(self, now: int):
        cut = now - CANCEL_STATS_WINDOW_S
        for c in (self.sparse, self.pull):
            for s in [s for s in c if s < cut]:
                del c[s]

    def summary(self) -> dict:
        now = int(self._clock())
        self._prune(now)
        start = max(int(self._t0), now - CANCEL_STATS_WINDOW_S)
        n = max(now - start, 1)
        per_s = sorted(self.sparse.get(s, 0) for s in range(start, now))
        q = lambda p: per_s[min(len(per_s) - 1, int(p * len(per_s)))] if per_s else 0
        return {"window_s": n,
                "sparse_cancels": sum(self.sparse.values()),
                "sparse_cancels_per_s": {"mean": round(sum(per_s) / n, 2), "p50": q(.5), "p90": q(.9),
                                         "p99": q(.99), "p999": q(.999), "max": per_s[-1] if per_s else 0},
                "pull_cancels": sum(self.pull.values()),
                "pull_bursts": len(self.pull),
                "largest_pull_second": max(self.pull.values(), default=0)}


class OrderManager:
    """Owns every quote order for one event. Not thread-safe: called only
    from the event loop (executor callbacks are marshalled back onto it by
    the engine)."""

    def __init__(self, executor, *, cancels_token: float = 20.0, keep_inside: float = 0.0,
                 clock=time.monotonic, sim_bucket_tokens_per_s: float | None = None):
        """`executor` sends writes: .next_order_id() -> int, .create(Quote) ->
        bool, .amend(Quote, price, size) -> bool, .cancel(Quote, reason),
        .bucket_tokens() -> float | None (None = no real bucket). Each returns
        False when it didn't send. Callbacks (on_created, on_amended,
        on_cancel_done, ...) may fire before the call returns.

        `cancels_token`: tokens creates/moves must leave in the bucket, for
        cancels; `cancels_token_fn` overrides it for live edits.
        `keep_inside`: $ a resting edge quote may sit inside the edge before
        it's cancelled; `keep_inside_fn` overrides it.
        `sim_bucket_tokens_per_s`: model the account write bucket at this rate
        when the executor has none (shadow, backtest)."""
        self.ex = executor
        self.clock = clock
        self.sim_bucket = (TokenBucket(sim_bucket_tokens_per_s, clock)
                           if sim_bucket_tokens_per_s is not None else None)
        # Set by WriteGroup.attach: the group then owns the reserve, the
        # cancel order and the pump, across every member's quotes.
        self.group = None
        self.cancels_token = cancels_token
        self.cancels_token_fn = None
        self.keep_inside = keep_inside
        self.keep_inside_fn = None
        # An edge quote above Desired.max_ahead_price is fixed only after this
        # long; a LIP quote is moved at most once per lip_hold_s.
        self.ahead_grace_s = 1.0
        self.ahead_grace_fn = None
        self.lip_hold_s = 1.0
        self.lip_hold_fn = None
        self.quotes: dict[int, Quote] = {}
        self.desired: dict[tuple[str, str], Desired] = {}
        self.lip: dict[tuple[str, str], LipTarget | None] = {}
        self._reject_until: dict[tuple[str, str, str, float], float] = {}
        self._requeued_at: dict[tuple[str, str], float] = {}
        # Cancels waiting to be sent: order_id -> reason. Drained nearest the
        # touch first; nothing else is sent while it's non-empty.
        self._cancel_q: dict[int, str] = {}
        self.counts: dict[str, int] = {}
        self.cancel_stats = CancelStats(clock)

    def _fn(self, fn, default) -> float:
        v = fn() if fn is not None else default
        return max(float(v or 0.0), 0.0)

    def _keep(self) -> float:
        return self._fn(self.keep_inside_fn, self.keep_inside)

    def own_reserve(self) -> float:
        return self._fn(self.cancels_token_fn, self.cancels_token)

    def reserve(self) -> float:
        """Tokens creates/moves must leave in the bucket for cancels."""
        return self.group.reserve() if self.group is not None else self.own_reserve()

    def _bump(self, k: str):
        self.counts[k] = self.counts.get(k, 0) + 1

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def live(self, ticker: str | None = None, side: str | None = None, role: str | None = None) -> list[Quote]:
        """Every quote not yet confirmed gone. All of it can still fill, so
        all of it counts for risk."""
        return [q for q in self.quotes.values()
                if (ticker is None or q.ticker == ticker) and (side is None or q.side == side)
                and (role is None or q.role == role)]

    def resting_size(self, ticker: str, side: str, role: str | None = EDGE) -> float:
        return sum(q.remaining for q in self.live(ticker, side, role))

    def ladder(self, ticker: str, side: str, role: str | None = EDGE) -> list[tuple[float, float]]:
        """[(price, remaining)] of our live quotes on one side (edge only by
        default) -- everything that can fill, for risk. An amend in flight is
        at its new price."""
        return [(q.price, q.remaining) for q in self.live(ticker, side, role)]

    def on_book(self, ticker: str, side: str) -> list[tuple[float, float]]:
        """[(price, remaining)] of ALL our quotes (edge and LIP) Kalshi's book
        actually shows: acked and not yet confirmed gone, an amend in flight at
        its OLD price and size. Netting anything else out of the displayed book
        would hide another participant's size."""
        out = []
        for q in self.live(ticker, side, None):
            if q.exchange_id is None:
                continue
            amending = q.state == AMENDING
            p = q.prev_price if amending and q.prev_price is not None else q.price
            z = (q.prev_size - q.filled) if amending and q.prev_size is not None else q.remaining
            out.append((p, max(z, 0.0)))
        return out

    def lip_quote(self, ticker: str, side: str) -> Quote | None:
        live = [q for q in self.live(ticker, side, LIP) if q.state != CANCELLING]
        return live[0] if live else None

    # ------------------------------------------------------------------
    # Bucket
    # ------------------------------------------------------------------

    def _bucket(self) -> float | None:
        b = self.ex.bucket_tokens()
        if b is None and self.sim_bucket is not None:
            return self.sim_bucket.available()
        return b

    def _spend_sim(self, n: float):
        if self.sim_bucket is not None and self.ex.bucket_tokens() is None:
            self.sim_bucket.spend(n)

    def _can_spend(self, cost: float) -> bool:
        bucket = self._bucket()
        return bucket is None or bucket - cost >= self.reserve() - 1e-9

    # ------------------------------------------------------------------
    # Desired state in
    # ------------------------------------------------------------------

    def set_desired(self, ticker: str, side: str, d: Desired | None):
        """Publish one side's edge ladder, queue the cancels it implies and
        send what the bucket allows. Moves/creates are left for `pump`."""
        if d is None:
            d = Desired([], 0.0, None, 0.0)
        self.desired[(ticker, side)] = d
        for q in self._unsafe(ticker, side, d):
            self._cancel(q, "inside_edge" if d.edge_limit is not None else "not_quotable")
        for q in self._over_room(ticker, side, d):
            self._cancel(q, "room")
        for q in self._over_levels(ticker, side, d):
            self._cancel(q, "excess_level")
        self._mark_ahead(ticker, side, d)
        self.drain_cancels()

    def requeue_lip(self, q: Quote, urgent: bool) -> bool:
        """Send `q` (a resting LIP quote) to the back of its queue with one
        amend that adds LIP_REQUEUE_BUMP lots: Kalshi puts a size increase at
        the back. One write, and no moment with no order (or two) resting.

        `urgent` (almost nothing ahead) may spend anything above the cancel
        reserve, like a LIP create; otherwise it needs two creates' worth
        spare on top, so it only uses tokens the edge ladder isn't waiting
        for. At most once per side per LIP_REQUEUE_MIN_S. Returns whether
        the amend was sent."""
        if q.role != LIP or q.state != RESTING or q.exchange_id is None or q.order_id in self._cancel_q:
            return False
        key = (q.ticker, q.side)
        now = self.clock()
        if now - self._requeued_at.get(key, -1e18) < LIP_REQUEUE_MIN_S:
            return False
        if not self._can_spend(COST_AMEND if urgent else COST_AMEND + 2 * COST_CREATE):
            self._bump("lip_requeue_deferred:reserve")
            return False
        if not self._move(q, q.price, q.filled + q.remaining + LIP_REQUEUE_BUMP):
            return False
        self._requeued_at[key] = now
        self._bump("lip_requeue")
        return True

    def set_lip(self, ticker: str, side: str, t: LipTarget | None):
        """Publish one side's LIP target (None = no LIP quote there). A live
        LIP quote with no target is cancelled at once; a changed target is
        applied by `pump` once the quote has rested lip_hold_s."""
        self.lip[(ticker, side)] = t
        if t is None:
            for q in self.live(ticker, side, LIP):
                if q.state != CANCELLING:
                    self._cancel(q, "lip_off")
            self.drain_cancels()

    def _unsafe(self, ticker, side, d: Desired) -> list[Quote]:
        """Live edge quotes more than keep_inside inside the edge, or every
        edge quote if the side may not be quoted at all."""
        live = [q for q in self.live(ticker, side, EDGE) if q.state != CANCELLING]
        if d.edge_limit is None:
            return live
        limit = d.edge_limit + self._keep()
        return [q for q in live if q.price > limit + 1e-9]

    def _over_room(self, ticker, side, d: Desired) -> list[Quote]:
        """Edge quotes to drop so resting size fits under `room`, deepest
        first. Every level counts toward the position cap."""
        if d.edge_limit is None:
            return []
        keep = [q for q in self.live(ticker, side, EDGE) if q.state != CANCELLING and q.order_id not in self._cancel_q]
        total = sum(q.remaining for q in keep)
        out = []
        for q in sorted(keep, key=lambda q: q.price):          # deepest (lowest own-side price) first
            if total <= d.room + 1e-9:
                break
            out.append(q)
            total -= q.remaining
        return out

    def _active_edge(self, ticker, side) -> list[Quote]:
        return [q for q in self.live(ticker, side, EDGE)
                if q.state != CANCELLING and q.order_id not in self._cancel_q]

    def _over_levels(self, ticker, side, d: Desired) -> list[Quote]:
        """Edge orders beyond max_levels, taken from those not at a wanted
        price, most aggressive first: a sweep hits the highest own-side price
        first, and a wanted level is never the one dropped."""
        if d.max_levels is None:
            return []
        active = self._active_edge(ticker, side)
        excess = len(active) - max(int(d.max_levels), 0)
        if excess <= 0:
            return []
        wanted = {round(p, 2) for p in d.prices}
        unwanted = sorted((q for q in active if round(q.price, 2) not in wanted), key=lambda q: -q.price)
        return unwanted[:excess]

    def _mark_ahead(self, ticker, side, d: Desired):
        """Start (or clear) each edge quote's too-far-ahead clock against the
        side's max_ahead_price. Only the clock: the fix waits for
        ahead_grace_s in pump, so a touch that dips for a moment and comes
        back costs nothing."""
        now = self.clock()
        cap = d.max_ahead_price
        for q in self.live(ticker, side, EDGE):
            if q.state == CANCELLING:
                continue
            if cap is not None and q.price > cap + 1e-9:
                if q.ahead_since is None:
                    q.ahead_since = now
            else:
                q.ahead_since = None

    # ------------------------------------------------------------------
    # Cancels
    # ------------------------------------------------------------------

    def pull_all(self, reason: str = "pull"):
        """Cancel every quote, edge and LIP (stale signal, halt, disarm,
        stop, shutdown). Desired sets are cleared, so nothing is re-created
        until the quoter publishes again; the whole bucket goes to the
        cancels, touch first."""
        self.desired.clear()
        self.lip.clear()
        for q in list(self.quotes.values()):
            if q.state != CANCELLING:
                self._cancel(q, reason)
        self.drain_cancels()

    def _cancel(self, q: Quote, reason: str):
        """Queue a cancel (or mark an unacked create to be cancelled on ack)."""
        if q.state == CANCELLING or q.order_id in self._cancel_q:
            return
        if q.state == CREATING and q.exchange_id is None:
            q.state = CANCELLING
            self._bump(f"cancel_pending_ack:{reason}")
            return
        self._cancel_q[q.order_id] = reason

    def _distance(self, q: Quote) -> int:
        """Ticks from that side's best live quote: 0 = our touch level."""
        top = max((x.price for x in self.live(q.ticker, q.side, None)), default=q.price)
        return int(round((top - q.price) / TICK))

    def cancel_order(self) -> list[tuple[int, int]]:
        """Queued cancels as (rank, order_id), lowest rank first: distance
        from the side's touch (touch levels of every side first), except
        "room" and "trim" cancels, which shed the deepest first."""
        out = []
        for oid, reason in list(self._cancel_q.items()):
            q = self.quotes.get(oid)
            d = 0 if q is None else self._distance(q)
            out.append((-d if reason in ("room", "trim") else d, oid))
        return out

    def send_cancel(self, oid: int) -> str:
        """Send one queued cancel if the bucket covers it: "sent", "skip"
        (no longer queued or already cancelling), "wait" (an amend is in
        flight; sent once it answers) or "bucket" (not enough tokens)."""
        q = self.quotes.get(oid)
        if oid not in self._cancel_q or q is None or q.state == CANCELLING:
            self._cancel_q.pop(oid, None)
            return "skip"
        if q.state == AMENDING:
            return "wait"
        bucket = self._bucket()
        if bucket is not None and bucket < COST_CANCEL - 1e-9:
            self._bump("cancel_deferred:bucket")
            return "bucket"
        reason = self._cancel_q.pop(oid)
        q.state = CANCELLING
        self._bump(f"cancel:{reason}")
        self.cancel_stats.note(reason)
        self._spend_sim(COST_CANCEL)
        self.ex.cancel(q, reason)
        return "sent"

    def drain_cancels(self) -> int:
        """Send queued cancels while the write bucket covers them, in
        cancel_order. In a write group, every member's cancels are drained
        together in one order. Returns how many were sent."""
        if self.group is not None:
            return self.group.drain_cancels()
        sent = 0
        for _, oid in sorted(self.cancel_order()):
            r = self.send_cancel(oid)
            if r == "bucket":
                break
            sent += r == "sent"
        return sent

    # ------------------------------------------------------------------
    # Edge moves and creates
    # ------------------------------------------------------------------

    def _stale_in_flight(self, ticker: str, side: str, wanted: set[float]) -> bool:
        """An edge create or amend on this side is still unanswered for a
        price the current ladder no longer wants: the ladder moved while it
        was in flight."""
        return any(q.state in (CREATING, AMENDING) and round(q.price, 2) not in wanted
                   for q in self.live(ticker, side, EDGE))

    def missing(self) -> list[tuple[int, str, str, float, float]]:
        """Every wanted edge level that has nothing at it, as heap entries
        (distance-from-touch, ticker, side, price, lots). Distance is ticks
        below the side's first wanted price, so the touch level of every
        market goes before any market's second level.

        While a create or amend on a side is in flight for a price the
        ladder has since moved off, the side gets nothing new until Kalshi
        answers: the ladder is still moving, and whatever we send now is
        sent for a price that may be gone too. Building a fresh ladder
        (every in-flight order still wanted) is not held back. KXINXDUD's top
        quote flipped 1 tick and back ~300x/min, each flip queued a create on
        top of the unacked one, and 27% of its creates were cancelled the
        moment they were acked (2026-10-15)."""
        out = []
        now = self.clock()
        for (ticker, side), d in self.desired.items():
            if not d.prices or d.edge_limit is None:
                continue
            if self._stale_in_flight(ticker, side, {round(p, 2) for p in d.prices}):
                self._bump("create_deferred:in_flight")
                continue
            # A cancelling order still occupies its price until confirmed gone:
            # re-creating there would stack a second order on one that may be
            # live (and a cancel that fails puts it straight back).
            have = {round(q.price, 2) for q in self.live(ticker, side, EDGE)}
            wanted = {round(p, 2) for p in d.prices}
            # A level is actionable if it can be CREATED within room, or a
            # spare quote below it can be MOVED there (a move adds no size).
            # Levels that are neither aren't queued: at the position cap they
            # would sit in the queue forever (22k pointless pump passes per
            # 90s with BTC at -10, 2026-10-11).
            room = d.room - self.resting_size(ticker, side)
            # Orders a create may add before the side exceeds max_levels; a
            # move reuses one, so it's always allowed.
            slots = (None if d.max_levels is None
                     else max(int(d.max_levels), 0) - len(self.live(ticker, side, EDGE)))
            spares = sorted(q.price for q in self.live(ticker, side, EDGE)
                            if q.state == RESTING and q.exchange_id is not None
                            and q.order_id not in self._cancel_q and round(q.price, 2) not in wanted)
            # At the level cap a spare above the ladder may move down too (see _spare).
            above_ok = slots is not None and slots <= 0
            top = d.prices[0]
            for p in d.prices:
                p = round(p, 2)
                if p in have or p > d.edge_limit + 1e-9:
                    continue
                if self._reject_until.get((ticker, side, EDGE, p), 0.0) > now:
                    continue
                if room >= d.lots - 1e-9 and (slots is None or slots > 0):
                    room -= d.lots
                    if slots is not None:
                        slots -= 1
                elif spares and spares[0] < p - 1e-9:
                    spares.pop(0)
                elif above_ok and spares:
                    spares.pop(0)
                else:
                    continue
                out.append((int(round((top - p) / TICK)), ticker, side, p, d.lots))
        return out

    def _at_level_cap(self, ticker: str, side: str, d: Desired) -> bool:
        return d.max_levels is not None and len(self.live(ticker, side, EDGE)) >= d.max_levels

    def _spare(self, ticker: str, side: str, d: Desired, price: float) -> Quote | None:
        """Resting edge quote at a price no longer wanted, to move to the
        missing level `price`: the deepest one below it. Upward only, unless
        the side is at its level cap -- then a quote left above the ladder is
        moved down onto it (lowest first), since nothing else can fill the
        ladder and a stale level above it is what a sweep hits first."""
        wanted = {round(p, 2) for p in d.prices}
        spare = [q for q in self.live(ticker, side, EDGE)
                 if q.state == RESTING and q.exchange_id is not None and q.order_id not in self._cancel_q
                 and round(q.price, 2) not in wanted]
        below = [q for q in spare if q.price < price - 1e-9]
        if below:
            return min(below, key=lambda q: q.price)
        if self._at_level_cap(ticker, side, d):
            above = [q for q in spare if q.price > price + 1e-9]
            return min(above, key=lambda q: q.price) if above else None
        return None

    def _fix_ahead(self) -> int:
        """Edge quotes above others' touch + max_improve_ticks for longer than
        the grace: move each down to the highest missing ladder level below it
        (one amend), or cancel it if the ladder is already full. Highest
        first. Returns writes sent."""
        now = self.clock()
        grace = self._fn(self.ahead_grace_fn, self.ahead_grace_s)
        sent = 0
        for (ticker, side), d in list(self.desired.items()):
            if d.max_ahead_price is None or d.edge_limit is None:
                continue
            late = sorted((q for q in self.live(ticker, side, EDGE)
                           if q.state == RESTING and q.exchange_id is not None and q.order_id not in self._cancel_q
                           and q.ahead_since is not None and now - q.ahead_since >= grace - 1e-9),
                          key=lambda q: -q.price)
            if not late:
                continue
            have = {round(q.price, 2) for q in self.live(ticker, side, EDGE)}
            holes = sorted((round(p, 2) for p in d.prices
                            if round(p, 2) not in have and p <= d.edge_limit + 1e-9), reverse=True)
            for q in late:
                target = next((p for p in holes if p < q.price - 1e-9), None)
                if target is not None:
                    if not self._can_spend(COST_AMEND):
                        continue
                    holes.remove(target)
                    if self._move(q, target):
                        self._bump("move_down:too_far_ahead")
                        sent += 1
                    continue
                self._cancel(q, "too_far_ahead")
                sent += 1
        if sent:
            self.drain_cancels()
        return sent

    # ------------------------------------------------------------------
    # LIP
    # ------------------------------------------------------------------

    def _lip_work(self) -> tuple[int, bool]:
        """Bring every side's LIP quote to its target: create if none, move
        (one amend) if the price or size changed and it has rested lip_hold_s.
        Missing LIP quotes go before LIP moves. Returns (writes sent, whether
        the step finished -- False when the bucket ran short)."""
        now = self.clock()
        hold = self._fn(self.lip_hold_fn, self.lip_hold_s)
        creates, moves = [], []
        for (ticker, side), t in list(self.lip.items()):
            if t is None:
                continue
            q = self.lip_quote(ticker, side)
            if q is None:
                if self._reject_until.get((ticker, side, LIP, round(t.price, 2)), 0.0) <= now:
                    creates.append((ticker, side, t))
                continue
            if q.state != RESTING or q.exchange_id is None or q.order_id in self._cancel_q:
                continue
            # A re-queued quote rests LIP_REQUEUE_BUMP over its target; amending
            # that back down would spend a write for nothing.
            extra = q.remaining - t.size
            same = abs(q.price - t.price) < 1e-9 and -1e-9 <= extra <= LIP_REQUEUE_SLACK + 1e-9
            if not same and now - q.sent_at >= hold - 1e-9:
                moves.append((q, t))
        sent = 0
        for ticker, side, t in creates:
            if not self._can_spend(COST_CREATE):
                self._bump("lip_deferred:reserve")
                return sent, False
            if self._create(ticker, side, t.price, t.size, LIP):
                sent += 1
        for q, t in moves:
            if not self._can_spend(COST_AMEND):
                self._bump("lip_deferred:reserve")
                return sent, False
            if self._move(q, t.price, q.filled + t.size):
                self._bump("lip_move")
                sent += 1
        return sent, True

    # ------------------------------------------------------------------
    # Trim
    # ------------------------------------------------------------------

    def _trim(self) -> int:
        """Lowest priority: cancel edge quotes that are no longer in the
        ladder and sit BELOW its deepest level -- extra levels left behind as
        the touch moved up, which only add size a big move would sweep. Run
        only when nothing else is waiting, deepest first, and only while the
        bucket stays above the cancel reserve."""
        sent = 0
        for (ticker, side), d in list(self.desired.items()):
            if not d.prices or d.edge_limit is None:
                continue
            floor = min(d.prices)
            wanted = {round(p, 2) for p in d.prices}
            extra = sorted((q for q in self.live(ticker, side, EDGE)
                            if q.state == RESTING and q.order_id not in self._cancel_q
                            and round(q.price, 2) not in wanted and q.price < floor - 1e-9),
                           key=lambda q: q.price)
            for q in extra:
                if not self._can_spend(COST_CANCEL):
                    return sent
                self._cancel(q, "trim")
                sent += 1
        if sent:
            self.drain_cancels()
        return sent

    # ------------------------------------------------------------------
    # Pump
    # ------------------------------------------------------------------

    def pump(self) -> int:
        """One pass down the priority list: cancels, edge fixes, each side's
        BEST edge level, LIP, the edge levels behind the best, trims. Each step
        runs only if everything above it is done. Returns how many writes were
        sent (cancels excluded). In a write group the group runs this pass
        across every member at once."""
        if self.group is not None:
            return self.group.pump()
        self.drain_cancels()
        if self._cancel_q:
            self._bump("create_deferred:cancels_pending")
            return 0
        sent = self._fix_ahead()
        if self._cancel_q:
            return sent
        n, done = self._fill_edge(lambda dist: dist == 0)
        sent += n
        if not done:
            return sent
        n, done = self._lip_work()
        sent += n
        if not done:
            return sent
        n, done = self._fill_edge(lambda dist: dist > 0)
        sent += n
        if done and not self.missing():
            self._trim()
        return sent

    def _fill_edge(self, which) -> tuple[int, bool]:
        """Move/create the missing edge levels whose distance-from-best
        passes `which`, nearest the touch first. Returns (writes sent, whether
        the step finished -- False when the bucket ran short)."""
        heap = [e for e in self.missing() if which(e[0])]
        heapq.heapify(heap)
        sent = 0
        while heap:
            if not self._can_spend(COST_CREATE):
                self._bump("create_deferred:reserve")
                return sent, False
            _, ticker, side, price, lots = heapq.heappop(heap)
            sent += self.fill_level(ticker, side, price, lots)
        return sent, True

    def fill_level(self, ticker: str, side: str, price: float, lots: float) -> bool:
        """Fill one missing edge level (a missing() entry): move a spare
        quote there, else create one if room and the level cap allow.
        Returns whether a write was sent. The caller checks the bucket."""
        d = self.desired[(ticker, side)]
        spare = self._spare(ticker, side, d, price)
        if spare is not None:
            return self._move(spare, price)
        if d.room - self.resting_size(ticker, side) < lots - 1e-9:
            self._bump("create_skipped:room")
            return False
        if d.max_levels is not None and len(self.live(ticker, side, EDGE)) >= d.max_levels:
            self._bump("create_skipped:levels")
            return False
        return self._create(ticker, side, price, lots)

    def _move(self, q: Quote, price: float, size: float | None = None) -> bool:
        """Amend `q` to `price` (and total size `size`, if given)."""
        q.prev_price, q.prev_size, q.state = q.price, q.size, AMENDING
        q.price = price
        if size is not None:
            q.size = size
        if not self.ex.amend(q, price, q.remaining):
            # Executor couldn't (shadow model, guard): leave it where it was.
            if q.order_id in self.quotes and q.state == AMENDING:
                q.price, q.size = q.prev_price, q.prev_size
                q.prev_price = q.prev_size = None
                q.state = RESTING
            return False
        self._spend_sim(COST_AMEND)
        self._bump("move")
        return True

    def _create(self, ticker, side, price, lots, role: str = EDGE) -> bool:
        # Registered BEFORE it's sent: the executor may ack synchronously
        # (shadow, tests) and the ack must find it.
        q = Quote(order_id=self.ex.next_order_id(), ticker=ticker, side=side, price=price,
                  size=lots, sent_at=self.clock(), role=role)
        self.quotes[q.order_id] = q
        if not self.ex.create(q):
            self.quotes.pop(q.order_id, None)
            return False
        self._spend_sim(COST_CREATE)
        self._bump("create" if role == EDGE else "lip_create")
        return True

    # ------------------------------------------------------------------
    # Executor callbacks (event loop)
    # ------------------------------------------------------------------

    def _check_landed(self, q: Quote, why: str):
        """A quote just became resting at `q.price`: cancel it if its target
        went away while it was in flight -- the edge moved through it, a pull
        cleared its side, or (LIP) the side lost its LIP target."""
        if q.role == LIP:
            if self.lip.get((q.ticker, q.side)) is None:
                self._cancel(q, "lip_off")
            return
        d = self.desired.get((q.ticker, q.side))
        if d is None or d.edge_limit is None or q.price > d.edge_limit + self._keep() + 1e-9:
            self._cancel(q, why)

    def on_created(self, order_id: int, exchange_id: str | None, filled: float = 0.0):
        q = self.quotes.get(order_id)
        if q is None:
            return
        q.exchange_id = exchange_id
        q.filled += filled
        q.sent_at = self.clock()
        if q.remaining <= 1e-9:
            self.quotes.pop(order_id, None)   # crossed in full on arrival
            return
        cancelled = q.state == CANCELLING
        q.state = RESTING
        if cancelled:
            self._cancel(q, "after_ack")      # now there's an id to cancel by
        else:
            self._check_landed(q, "inside_edge_on_ack")
        self.drain_cancels()

    def on_amended(self, order_id: int, *, ok: bool, gone: bool = False):
        """`ok`: Kalshi moved it. `gone`: 404, already off the book (filled or
        cancelled). Otherwise the amend failed and the order is still resting
        at its old price and size."""
        q = self.quotes.get(order_id)
        if q is None:
            return
        if gone:
            self.quotes.pop(order_id, None)
            self._cancel_q.pop(order_id, None)
            return
        if ok:
            q.sent_at = self.clock()
        else:
            if q.prev_price is not None:
                q.price = q.prev_price
            if q.prev_size is not None:
                q.size = q.prev_size
            self._bump("move_failed")
        q.prev_price = q.prev_size = None
        q.state = RESTING
        if order_id not in self._cancel_q:
            self._check_landed(q, "inside_edge_on_move")
        self.drain_cancels()

    def on_create_failed(self, order_id: int, *, rejected: bool):
        """The POST errored or was refused (429). `rejected` = Kalshi said no
        to this order specifically; back off that price."""
        q = self.quotes.pop(order_id, None)
        self._cancel_q.pop(order_id, None)
        if q is not None and rejected:
            self._reject_until[(q.ticker, q.side, q.role, round(q.price, 2))] = self.clock() + REJECT_BACKOFF_S

    def on_cancel_done(self, order_id: int):
        """Cancel confirmed, or 404 (already gone -- filled or expired)."""
        self.quotes.pop(order_id, None)
        self._cancel_q.pop(order_id, None)

    def on_cancel_failed(self, order_id: int, reason: str = "retry"):
        """Still on the book: re-queue it; the next pump retries."""
        q = self.quotes.get(order_id)
        if q is not None:
            q.state = RESTING if q.exchange_id is not None else CREATING
            if q.exchange_id is not None:
                self._cancel_q[order_id] = reason

    def on_fill(self, order_id: int, size: float):
        q = self.quotes.get(order_id)
        if q is None:
            return
        q.filled += size
        if q.remaining <= 1e-9:
            self.quotes.pop(order_id, None)
            self._cancel_q.pop(order_id, None)

    def forget_all(self):
        """Session reset: the engine has already swept the exchange."""
        self.quotes.clear()
        self.desired.clear()
        self.lip.clear()
        self._cancel_q.clear()

    def summary(self) -> dict:
        states: dict[str, int] = {}
        for q in self.quotes.values():
            k = q.state if q.role == EDGE else f"lip_{q.state}"
            states[k] = states.get(k, 0) + 1
        return {"live": states, "queued_creates": len(self.missing()),
                "queued_cancels": len(self._cancel_q),
                "cancels_token": self.reserve(),
                "bucket_tokens": None if self._bucket() is None else round(self._bucket(), 1),
                "write_group": None if self.group is None else self.group.summary(),
                "cancels": self.cancel_stats.summary(),
                "counts": dict(self.counts)}


class WriteGroup:
    """Several OrderManagers sharing ONE write bucket: one cancel reserve
    and one priority queue across all of them, instead of each member
    holding a fixed slice of the budget.

    Each pump pass interleaves the members' work in the same priority order
    one manager uses: every member's queued cancels (touch first, across all
    members), then too-far-ahead fixes, then every member's touch levels,
    then LIP, then the levels behind the touch, then trims. A deferred step
    in any member stops the pass for everyone, so a quiet series' backlog
    can't be starved by a busy one and vice versa.

    `bucket` is the shared token count (None = unmetered); the reserve is
    the largest member's own cancels_token, so tightening one series'
    setting never loosens another's. Not thread-safe: event loop only."""

    def __init__(self, sim_tokens_per_s: float | None = None, clock=time.monotonic):
        """`sim_tokens_per_s`: the shadow model of the shared bucket, used by
        members whose executor has no real bucket (shadow mode)."""
        self.members: dict[str, OrderManager] = {}
        self.sim_bucket = TokenBucket(sim_tokens_per_s, clock) if sim_tokens_per_s else None

    def attach(self, name: str, m: OrderManager):
        m.group = self
        if self.sim_bucket is not None:
            m.sim_bucket = self.sim_bucket
        self.members[name] = m

    def detach(self, name: str):
        m = self.members.pop(name, None)
        if m is not None:
            m.group = None

    def reserve(self) -> float:
        return max((m.own_reserve() for m in self.members.values()), default=0.0)

    def _partitions(self) -> list[list[tuple[str, "OrderManager"]]]:
        """Members grouped by the bucket they actually draw on: real-money
        members (executor has a real bucket) vs shadow members (the modelled
        bucket). Each partition runs its own pass. Mixed in one pass, shadow
        KXINX's starved modelled bucket stopped every pass before armed
        KXINXU's real creates were reached: KXINXU sat with 0 quotes and a
        full 80-token real bucket (2026-10-15)."""
        real, sim = [], []
        for name, m in self.members.items():
            (sim if m.ex.bucket_tokens() is None else real).append((name, m))
        return [p for p in (real, sim) if p]

    def drain_cancels(self) -> int:
        return sum(self._drain(p) for p in self._partitions())

    @staticmethod
    def _drain(members) -> int:
        queued = [(rank, i, oid) for i, (_, m) in enumerate(members) for rank, oid in m.cancel_order()]
        sent = 0
        for _, i, oid in sorted(queued):
            r = members[i][1].send_cancel(oid)
            if r == "bucket":
                break
            sent += r == "sent"
        return sent

    @staticmethod
    def _fill_edge(members, which) -> tuple[int, bool]:
        heap = [(e[0], i, *e[1:]) for i, (_, m) in enumerate(members) for e in m.missing() if which(e[0])]
        heapq.heapify(heap)
        sent = 0
        while heap:
            _, i, ticker, side, price, lots = heapq.heappop(heap)
            m = members[i][1]
            if not m._can_spend(COST_CREATE):
                m._bump("create_deferred:reserve")
                return sent, False
            sent += m.fill_level(ticker, side, price, lots)
        return sent, True

    def pump(self) -> int:
        return sum(self._pump(p) for p in self._partitions())

    def _pump(self, members) -> int:
        ms = [m for _, m in members]
        self._drain(members)
        if any(m._cancel_q for m in ms):
            for m in ms:
                if m._cancel_q:
                    m._bump("create_deferred:cancels_pending")
            return 0
        sent = sum(m._fix_ahead() for m in ms)
        if any(m._cancel_q for m in ms):
            return sent
        n, done = self._fill_edge(members, lambda dist: dist == 0)
        sent += n
        if not done:
            return sent
        for m in ms:
            n, done = m._lip_work()
            sent += n
            if not done:
                return sent
        n, done = self._fill_edge(members, lambda dist: dist > 0)
        sent += n
        if done and not any(m.missing() for m in ms):
            for m in ms:
                m._trim()
        return sent

    def summary(self) -> dict:
        return {"members": list(self.members), "reserve": self.reserve(),
                "queued_cancels": sum(len(m._cancel_q) for m in self.members.values())}