"""
Feed-health kill switch (spec section 2.1), three-tier.

The Coin Race winner is a JOINT comparison across all 5 coins, so a coin
with no usable data makes every fair unknowable. But a crypto mid that is
a few seconds old is still a fine input to a multi-minute-horizon
ranking. So each coin's feed is classified into one of three tiers:

  OK        -- a fresh (<= soft_stale_s), two-sided, non-resyncing book
               exists on at least one venue. Price it normally.
  DEGRADED  -- the newest book is between soft_stale_s and hard_stale_s
               old (or one-sided/resyncing but recently fresh). Keep the
               coin's LAST good price and keep pricing the event; the
               pricer's variance term widens on its own as data thins.
  DOWN      -- no book at all this event, or the newest is older than
               hard_stale_s. The joint is unknowable: the event's theo
               is invalid and all legs pull.

`event_usable()` is the gate the strategy wants: True iff NO coin is
DOWN. A DEGRADED coin does not block the event. After a coin leaves
DOWN, it must spend `recovery_s` continuously not-DOWN before
`event_usable` counts it again -- otherwise a flapping socket produces
a quote-cancel-quote storm.

The monitor is cheap and stateful. One instance per strategy. Call
`observe(books_per_coin, now)` once per tick, then `event_usable(now)`
to gate theo and `status(coin, now)` for per-leg decisions.
"""
from __future__ import annotations

import dataclasses
import time

from .base import VenueBook
from ..constants import STALENESS_MS_DEFAULT

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"


@dataclasses.dataclass
class FeedHealthState:
    """Per-coin health. `last_status` is the most recent tier.
    `not_down_since` is the monotonic time the coin last entered a
    not-DOWN tier (None while DOWN); the recovery hold-off reads it.
    `last_reason` is a human snapshot of the current tier's cause."""
    last_status: str = DOWN
    not_down_since: float | None = None
    last_reason: str = "startup"
    last_good_ts: float | None = None   # monotonic ts of newest OK/DEGRADED book
    # First monotonic time of the current UNBROKEN run of DOWN
    # classifications, used to debounce a transient blip (one empty-venue
    # tick, a brief resync) from tearing down a warmed-up coin. None while
    # the latest classification was not-DOWN. See `_observe_one`.
    down_candidate_since: float | None = None
    # Running counters -- operational observability. Reset only on a new
    # session.
    down_transitions: int = 0
    seconds_down: float = 0.0
    _last_check_ts: float = 0.0


class FeedHealthMonitor:
    def __init__(self, soft_stale_s: float = 3.0, hard_stale_s: float = 15.0,
                 recovery_s: float = 5.0,
                 hard_stale_ms_per_coin: dict[str, int] | None = None,
                 down_debounce_s: float = 2.0):
        """`soft_stale_s` / `hard_stale_s` are the tier boundaries in
        seconds (coarse, cross-coin). `hard_stale_ms_per_coin` lets a
        specific coin override the hard bound -- HYPE's CF methodology
        staleness is 30s, so its hard bound is naturally larger (see
        config.STALENESS_MS_DEFAULT). `recovery_s` is the not-DOWN
        hold-off before a coin counts toward `event_usable` again.

        `down_debounce_s`: a coin that is already not-DOWN must classify
        DOWN continuously for this long before it actually transitions to
        DOWN. A single blip -- an empty venue list on one dispatch (race
        between the Coinbase callback and the sampling loop), a one-tick
        resync -- must NOT tear down a warmed-up coin and reset its
        recovery window, or a coin whose feed is genuinely fine flaps DOWN
        and the whole event's theo (a joint 5-way comparison) goes
        unknowable. A true hard-stale/outage lasts far longer than the
        debounce, so this only suppresses transients. The FIRST time a
        coin is seen (cold start, last_status already DOWN) is NOT
        debounced -- there is nothing to protect yet."""
        self.soft_stale_s = soft_stale_s
        self.hard_stale_s = hard_stale_s
        self.recovery_s = recovery_s
        self.down_debounce_s = down_debounce_s
        # Per-coin hard bound: max(global hard, that coin's methodology
        # staleness). A coin that CF itself tolerates to 30s should not
        # be declared DOWN at 15s.
        self._hard_ms = dict(STALENESS_MS_DEFAULT if hard_stale_ms_per_coin is None
                             else hard_stale_ms_per_coin)
        self._states: dict[str, FeedHealthState] = {}

    def state(self, coin: str) -> FeedHealthState:
        if coin not in self._states:
            self._states[coin] = FeedHealthState()
        return self._states[coin]

    def _hard_bound_s(self, coin: str) -> float:
        return max(self.hard_stale_s, self._hard_ms.get(coin, 0) / 1000.0)

    def observe(self, venue_books_per_coin: dict[str, list[VenueBook]],
                now: float | None = None):
        now = time.monotonic() if now is None else now
        for coin, venues in venue_books_per_coin.items():
            self._observe_one(coin, venues, now)

    def _classify(self, coin: str, venues: list[VenueBook], now: float
                  ) -> tuple[str, str, bool]:
        """Return (tier, reason, transient). A venue counts as a usable
        book if it is non-resyncing, two-sided and uncrossed. The FRESHEST
        such venue's age decides the tier.

        `transient` is True only for a DOWN caused by the momentary ABSENCE
        of a usable book (no venue, all resyncing/one-sided) -- the kind of
        single-tick blip the debounce suppresses. A DOWN from a MEASURED
        hard-stale age is not transient: the book is really that old and
        the coin should transition immediately."""
        hard_s = self._hard_bound_s(coin)
        freshest_age = None
        for vb in venues:
            # A crossed book is NOT dropped here -- it still yields a usable
            # mid (0.5*(bid+ask)), which is all the composite needs, and
            # dropping it would mark the coin DOWN while compute_cf_spot is
            # happily producing a fallback mid from it (observed 2026-10-06:
            # a stale crossing level on BTC marked it DOWN for ~278s and
            # invalidated the whole event's theo). Resyncing / one-sided
            # books genuinely have no mid, so those are still skipped.
            if vb.is_resyncing or vb.is_one_sided():
                continue
            if vb.best_bid() is None or vb.best_ask() is None:
                continue
            age = now - vb.receive_ts
            if freshest_age is None or age < freshest_age:
                freshest_age = age
        if freshest_age is None:
            # No usable book at all. If we have NEVER seen one this event
            # it's a cold start; either way it's DOWN until data arrives.
            return DOWN, "no usable book", True
        if freshest_age <= self.soft_stale_s:
            return OK, f"fresh {freshest_age * 1000:.0f}ms", False
        if freshest_age <= hard_s:
            return DEGRADED, f"soft-stale {freshest_age:.1f}s (<= {hard_s:.0f}s hard)", False
        return DOWN, f"hard-stale {freshest_age:.1f}s (> {hard_s:.0f}s)", False

    def _observe_one(self, coin: str, venues: list[VenueBook], now: float):
        st = self.state(coin)
        raw_tier, reason, transient = self._classify(coin, venues, now)

        # Debounce a TRANSIENT DOWN (momentary absence of a usable book):
        # a coin that is CURRENTLY not-DOWN must stay transient-DOWN
        # continuously for `down_debounce_s` before it actually
        # transitions. This stops a single-tick blip (empty venue list on
        # one dispatch, a brief resync) from tearing down a warmed-up coin
        # and resetting its recovery window -- the flap that made
        # `event_usable` thrash and the event theo wink out even though
        # every coin's book was fresh (observed 2026-10-06/07). A DOWN from
        # a MEASURED hard-stale age is NOT debounced: the book really is
        # that old. Cold start (last_status already DOWN) is not debounced
        # either -- there is no healthy state to protect.
        if raw_tier == DOWN and transient:
            if st.down_candidate_since is None:
                st.down_candidate_since = now
            held_s = now - st.down_candidate_since
            if st.last_status != DOWN and held_s < self.down_debounce_s:
                tier = st.last_status
                reason = f"down-pending {held_s:.1f}s/{self.down_debounce_s:.0f}s ({reason})"
            else:
                tier = DOWN
        else:
            st.down_candidate_since = None
            tier = raw_tier

        # Time-in-DOWN accounting (on the EFFECTIVE tier).
        if st._last_check_ts > 0.0 and st.last_status == DOWN:
            st.seconds_down += now - st._last_check_ts
        st._last_check_ts = now

        if tier == DOWN:
            if st.last_status != DOWN:
                st.down_transitions += 1
            st.not_down_since = None
        else:
            if st.not_down_since is None:
                st.not_down_since = now
            st.last_good_ts = now

        st.last_status = tier
        st.last_reason = reason

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def status(self, coin: str, now: float | None = None) -> str:
        """Current tier (OK / DEGRADED / DOWN), applying the recovery
        hold-off: a coin that just left DOWN still reports DOWN until it
        has been not-DOWN for `recovery_s`."""
        st = self._states.get(coin)
        if st is None or st.not_down_since is None:
            return DOWN
        now = time.monotonic() if now is None else now
        if (now - st.not_down_since) < self.recovery_s:
            return DOWN   # inside the recovery window -- not trusted yet
        return st.last_status

    def is_healthy(self, coin: str, now: float | None = None) -> bool:
        """Back-compat: True iff the coin is OK (strictly fresh and past
        the recovery window). DEGRADED is NOT healthy by this predicate,
        but IS usable -- callers that only want "can I price the event"
        should use `event_usable` / `status`."""
        return self.status(coin, now) == OK

    def coin_usable(self, coin: str, now: float | None = None) -> bool:
        """True iff the coin is OK or DEGRADED (i.e. not DOWN). This is
        what lets the event keep pricing off a slightly-stale last
        price."""
        return self.status(coin, now) != DOWN

    def event_usable(self, coins, now: float | None = None) -> bool:
        """True iff NO coin in `coins` is DOWN. The strategy gates the
        whole event's theo on this: a single DOWN coin makes the joint
        winner distribution unknowable."""
        now = time.monotonic() if now is None else now
        return all(self.coin_usable(c, now) for c in coins)

    def down_coins(self, coins, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        return [c for c in coins if not self.coin_usable(c, now)]

    def snapshot(self, now: float | None = None) -> dict[str, dict]:
        """Serialisable view for the viz / summary.json."""
        now = time.monotonic() if now is None else now
        out = {}
        for coin, st in self._states.items():
            out[coin] = {
                "status": self.status(coin, now),
                "usable": self.coin_usable(coin, now),
                "not_down_since_s_ago": None if st.not_down_since is None else now - st.not_down_since,
                "last_reason": st.last_reason,
                "down_transitions": st.down_transitions,
                "seconds_down": st.seconds_down,
            }
        return out


__all__ = ["FeedHealthMonitor", "FeedHealthState", "OK", "DEGRADED", "DOWN"]
