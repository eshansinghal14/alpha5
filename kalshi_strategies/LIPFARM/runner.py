"""
LIP farmer on one asyncio loop, built the way ARBS's runner is (and on its
Gateway and MultiFeed). What to quote is unchanged (farm.plan_market and the
farm's pricing, exit and group rules); what changed is how it runs:

  * Our fills arrive on the websocket's `fill` channel and are handled on the
    loop at once: booked, counted against the event's order group, the market
    marked urgent. The eval loop takes urgent markets before any whose book
    merely ticked, in 1 ms slices.
  * An exit is decided from the websocket book in memory and sent as a
    spawned task through the Gateway's write pool; cancels go in the same
    gather, never ahead of it. A market with a write in flight is busy and is
    re-evaluated when its last task ends.
  * Everything slow runs off the loop: REST reads on the read pool (paced
    with the farm's ReadPacer), the ~900-market scan's planning on a worker,
    file writes on the pool. Nothing in the hot path reads REST.
  * Every exit journals its timing: Kalshi's fill time, when we saw it, when
    we decided, sent and were answered.

Shadow mode places nothing. With the websocket's public trades it fills our
shadow bids when a print clears their price (we sit at the back of the
touch, so a print below our price means the level was swept) and fills
exits against the book in memory, so the whole fill -> exit path runs.

    python -m kalshi_strategies.LIPFARM.runner --resume-arm        # as farm.py
    python -m kalshi_strategies.LIPFARM.runner --shadow-only --root /opt/alpha5/data/live/LIPFARM_ASYNC \
        --reads-per-s 3                                              # side-by-side test
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures as cf
import dataclasses
import gc
import json
import math
import os
import time
import traceback
import uuid
from types import SimpleNamespace

import pandas as pd

from kalshi_infra.kalshi.fees import fee as kfee
from kalshi_infra.kalshi.orders import http_status, is_order_group_reject
from kalshi_infra.runtime.control import StopWatcher, TradingModeWatcher, resumable_arm
from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM import farm
from kalshi_strategies.LIPFARM.levels import Levels
from kalshi_strategies.LIPFARM.farm import (CANCEL_TOKENS, CID_PREFIX, CLAIM_SETTLE_S, DUST_LOTS, EXIT_REPRICE_S,
                                            GROUP_DELETES, GROUP_IDLE_S, GROUP_LIMIT, GUARD_COOLDOWN_S, GUARD_S,
                                            MAX_MOVES, MOVE_BEHIND_FRAC, MOVE_MIN_S, MOVE_RESERVE, NAME,
                                            ORDER_TOKENS, PLACE_RESERVE, ROOT, STATUS_S,
                                            TRIP_COOLDOWN_S, WRITE_BURST, Cfg, TokenBucket, desired_orders,
                                            event_of, exit_lots, is_hedge, is_sentinel, key, parse_book,
                                            plan_market, remove_ours, series_of, unkey)

# As ARBS's eval loop: a pass gives the loop back after this long.
EVAL_SLICE_S = 0.001
# Socket down: fills are polled this often instead of every GUARD_S.
FILL_POLL_DOWN_S = 1.0
KEEPALIVE_S = 20.0
# A fill whose order id we don't know yet (its create still in flight) waits
# this long for the ack before it's dropped; the REST poll backstops it.
ORPHAN_S = 10.0
# A journaled order this young may not be in Kalshi's resting list yet.
SYNC_GRACE_S = 10.0
MARKS_S = 60.0
QUOTED_KEEP_DAYS = 30
# As ARBS's completion.max_exit_misses: an informed sell that comes back short
# (the socket book still showed bids the sweep had taken) is retried on the
# market's next book update, at most this many times in a row.
MAX_DUMP_MISSES = 5
# Markets whose contracts cover one we quote (a lower ladder strike, NO on another
# outcome) kept on the socket so a dump can buy them with no read. 221 quoted sides
# had 376 such markets on 11-07 (<= 3 per side); capped so the feed stays light.
SIBLING_FEED_MAX = 200
# Research log for a depth-drop pull (ARBS's pull_drop): per market we rest on,
# others' size at our prices and at each touch over the last DEPTH_KEEP_S; the
# FILL_CONTEXT_S before each LIP fill is journaled ("fill_context").
DEPTH_KEEP_S = 30.0
# A market we quote keeps its slot unless another is worth this much more per day: ranking
# ~1,400 started markets with no margin swapped the ones near the cut-off every cycle
# (131 bid and dropped in 2 h for 193 quoted, 11-07).
INCUMBENT_EDGE = 1.2
# After a refused create, the same order key waits this long. Without it an exit (which skips
# the budget) re-sent the moment it failed: 47k 409s in 10 min on a closed market, 11-06.
PLACE_RETRY_S = 5.0
PLACE_RETRY_CONFLICT_S = 60.0
FILL_CONTEXT_S = 10.0
# Accuracy test for levels.Levels: every QUEUE_CHECK_S, Kalshi's size ahead of
# each of our LIP bids vs the size ahead our reconstruction of the level gives.
QUEUE_CHECK_S = 60.0
# A side whose resting bids earn under REPRICE_FRAC of what its plan would there
# is re-placed at once, not at the next cycle (11-05 FOC-40: others stacked Target
# at the sentinel's level and the bid a tick under scored nothing), at most once
# per REPRICE_MIN_S per side so bots stepping over us don't start a write war.
REPRICE_FRAC = 0.5
# The exit prices off the first level where others hold this much (or this share of
# our exit): FOC-70 11-05, a 5-lot bid kept our 72-lot exit 13 ticks over the real bid.
EXIT_TOUCH_LOTS = 25.0
EXIT_TOUCH_FRAC = 0.1
REPRICE_MIN_S = 30.0
# Manual holds (an operator or agent writes holds.json: {ticker: {"until": epoch, "why": str}}):
# our LIP bids there are pulled and kept off until then; exits are untouched. Re-read this often.
HOLDS_S = 5.0


def lip_feed_class():
    """ARBS's MultiFeed, also handing each raw level snapshot/delta to `on_level`."""
    from kalshi_strategies.ARBS.feed import MultiFeed

    class LipFeed(MultiFeed):
        on_level = None

        def _handle_message(self, d):
            if self.on_level is not None and d.get("type") in ("orderbook_snapshot", "orderbook_delta"):
                msg = d.get("msg") or {}
                if msg.get("market_ticker") in self._books:
                    self.on_level(d["type"], msg)
            super()._handle_message(d)
    return LipFeed
FLUSH_S = 1.0


class _Dirty(set):
    """Tickers to evaluate. Adding one wakes the eval loop (as ARBS's)."""

    def __init__(self, wake: asyncio.Event):
        super().__init__()
        self.wake = wake

    def add(self, x):
        super().add(x)
        self.wake.set()

    def __ior__(self, other):
        super().__ior__(other)
        if other:
            self.wake.set()
        return self


def tick_in(ranges, price: float) -> float:
    for start, end, step in ranges:
        if start - 1e-9 <= price < end - 1e-9:
            return step
    return 0.01


def pool_rates(programs: dict) -> dict[str, float]:
    """Each program's whole pool, $/s (parsing all ~9,500 took ~0.8 s: a worker's job)."""
    out = {}
    for t, prog in programs.items():
        P = lip.params_from_program(prog)
        out[t] = P.period_reward_usd / P.snapshots_per_period
    return out


def plan_batch(jobs: list[tuple]) -> dict[str, tuple]:
    """plan_market over prepared inputs; pure, run on a worker thread.
    Returns ticker -> (plan or None, its LipParams)."""
    out = {}
    for t, others, prog, cfg, held, ranges, current in jobs:
        P = dataclasses.replace(lip.params_from_program(prog), price_ranges=ranges)
        out[t] = (plan_market(t, others, P, cfg, held, tick=lambda px, r=ranges: tick_in(r, px), current=current,
                             exit=exit_lots(others, cfg, cfg.max_position)), P)
    return out


class Runner:
    """The LIP farm: state, ranking and I/O on one asyncio loop (planning in farm.py)."""

    def __init__(self, root: str = ROOT, shadow_only: bool = False, sim: bool = True,
                 beside: str | None = None):
        self.root = root
        # Side-by-side test: the live farm's state.json, whose resting bids are
        # taken out of every book (it would otherwise look like others' size).
        self.beside = beside
        self.ghosts: dict[str, list] = {}
        self.shadow_only = shadow_only
        self.sim = sim
        self.programs: dict[str, dict] = {}
        self.meta: dict[str, dict] = {}
        self.books: dict[str, dict[str, dict[float, float]]] = {}
        self.plans: dict[str, farm.Plan] = {}
        self.held_events: set[str] = set()
        self.waiting: list[str] = []
        self.last_programs = 0.0
        self.bucket = TokenBucket(5.0, WRITE_BURST)
        self.baskets: dict[tuple[str, str], list] = {}
        self.taker_rates: dict[str, float] = {}
        self.est: dict[str, float] = {}       # ticker -> our LIP $/s per dollar at its last read
        self.rot = 0
        self.dumping: set[str] = set()        # markets an informed fill hit: sell out now
        self.place_wait: dict[str, float] = {}  # order key -> earliest retry after a refused create
        self.pull: set[str] = set()           # markets whose LIP bids go once their sells are out
        self.arm_requested = False
        self.cfg_adverse = 300.0              # cfg.adverse_lots, refreshed each second
        self.cfg_adverse_frac = 1.0           # cfg.adverse_frac, likewise
        self.n_refused, self.last_refused_log = 0, 0.0
        self.marks_at = 0.0
        self.categories: dict[str, str] = {}  # series -> category
        self.series_titles: dict[str, str] = {}
        self.flow_pool = cf.ThreadPoolExecutor(1, thread_name_prefix="lipfarm-flow")
        self.flow_futs: dict[str, cf.Future] = {}
        self._open_mode(False)
        self.pool = cf.ThreadPoolExecutor(16, thread_name_prefix="lipfarm")
        self.write_pool = cf.ThreadPoolExecutor(16, thread_name_prefix="lipfarm-write")
        from kalshi_strategies.ARBS.execution import Gateway
        self.gw = Gateway(self.pool, self.write_pool)
        self.feed = None
        self.wake: asyncio.Event | None = None
        self.urgent_set: _Dirty | None = None
        self.dirty: _Dirty | None = None
        self.busy: set[str] = set()
        self.busy_n: dict[str, int] = {}
        self.urgent_after: set[str] = set()
        self.positions_now: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.orphans: list[tuple[float, dict]] = []
        self.seen_at: dict[str, tuple[float, object]] = {}    # ticker -> (our clock, Kalshi's fill time)
        self.rows: list[dict] = []                             # journal rows awaiting the flush
        self.group_wait: dict[str, asyncio.Future] = {}
        self.marks: dict[str, dict] = {}
        self.wanted: dict[str, tuple] = {}                     # leg key -> desired (price, lots, behind)
        self.cfg = None
        self.n_evals = 0
        self.lag_max = 0.0                                     # worst loop stall since the last cycle print
        self.pool_rate: dict[str, float] = {}
        self.P: dict[str, lip.LipParams] = {}                  # params by ticker, from the last cycle
        self.io_pool = cf.ThreadPoolExecutor(1, thread_name_prefix="lipfarm-io")   # file writes, in order
        self.flow_held: dict[str, dict] = {}                   # ticker -> its plan the flow filter holds back
        self.depth_hist: dict[str, collections.deque] = {}     # ticker -> (time, depth snapshot), DEPTH_KEEP_S
        self.levels = Levels()
        self.recovery: list[tuple] = []                        # (seen, ticker, side, price) of LIP fills to re-check
        self.repriced_at: dict[tuple, float] = {}              # (ticker, side) -> last guard re-place
        self.queue_err: collections.deque = collections.deque(maxlen=500)   # |inferred - actual| ahead, last checks
        self.book_seq: dict[str, int] = {}                     # ticker -> socket book updates seen
        self.dump_seq: dict[str, int] = {}                     # ticker -> book_seq its last informed sell saw

    # ---- state, positions, ranking ----
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            # Journals from before two-sided quoting keyed orders by ticker alone.
            st["orders"] = {(k if "|" in k else key(k, o["side"])): o for k, o in st["orders"].items()}
            return st
        return {"t0": time.time(), "orders": {}, "fills": [], "trade_ids": [], "accrued": {}}

    def order_tickers(self) -> set[str]:
        return {unkey(k)[0] for k in self.state["orders"]}

    def legs_of(self, t: str, hedges: bool = True) -> list[tuple[str, dict]]:
        """(side, order) for our orders on t; sentinels are always included, exits if `hedges`."""
        return [(unkey(k)[1], o) for k, o in self.state["orders"].items()
                if unkey(k)[0] == t and (hedges or not is_hedge(k))]

    def keys_of(self, t: str, hedges: bool = True) -> list[str]:
        return [k for k in self.state["orders"] if unkey(k)[0] == t and (hedges or not is_hedge(k))]

    def apply_refresh(self, result):
        programs, meta, baskets, cats = result
        self.programs = programs
        self.meta.update(meta)
        if baskets is not None:
            self.baskets = baskets
        if cats:
            self.categories = {k: v["category"] for k, v in cats.items()}
            self.series_titles = {k: v["title"] for k, v in cats.items()}
        self.last_programs = time.time()

    def our_rate(self, t: str, others, P) -> float:
        legs = [(s, o["price"], o["remaining"]) for s, o in self.legs_of(t)]
        return farm.market_rate(others, legs, P) if legs else 0.0

    def tick(self, t: str, price: float) -> float:
        for r in self.meta.get(t, {}).get("price_ranges") or []:
            if float(r["start"]) - 1e-9 <= price < float(r["end"]) - 1e-9:
                return float(r["step"])
        return 0.01

    def free_positions(self) -> dict[str, float]:
        """Hedged-fill positions not already locked in a taken basket."""
        pos = self.signed_positions(hedged_only=True)
        for t, v in self.state.get("locked", {}).items():
            if self.meta.get(t, {}).get("result"):
                continue                             # settled: signed_positions has dropped it too
            pos[t] = pos.get(t, 0.0) - v
        # Kalshi fills in hundredths of a contract; less than that is rounding.
        return {t: v for t, v in pos.items() if abs(v) >= 0.01}

    def avg_cost(self, t: str, side: str) -> float:
        ok = set(self.state.get("hedged_oids", []))
        fs = [f for f in self.state["fills"] if f["ticker"] == t and f["side"] == side and f.get("order_id") in ok]
        q = sum(f["size"] for f in fs)
        return sum(f["size"] * f["price"] for f in fs) / q if q else 1.0

    def ask(self, t: str, side: str) -> tuple[float, float] | None:
        """(offer price, its size) to buy `side` of t, from others' books."""
        o = self.books.get(t)
        opp = "no" if side == "yes" else "yes"
        if not o or not o[opp]:
            return None
        p = max(o[opp])
        return round(1.0 - p, 4), o[opp][p]

    def exposure(self) -> float:
        """Dollars our fills have left us holding, unexited, at cost."""
        return sum(abs(v) * self.avg_cost(t, "yes" if v > 0 else "no") for t, v in self.free_positions().items())

    def plan(self, t: str, others: dict, P: lip.LipParams, cfg: Cfg, pos: dict[str, float],
             flow: bool = True) -> farm.Plan | None:
        """plan_market, or None on an event ARBS holds or (`flow`) one whose flow read failed."""
        if event_of(t) in self.held_events:
            return None
        v = pos.get(t, 0.0)
        current = {}
        for k in self.keys_of(t, hedges=False):
            sd = unkey(k)[1]
            current[sd] = "sentinel" if (is_sentinel(k) or current.get(sd) == "sentinel") else "touch"
        # Unread flow doesn't pull a resting bid (scan_finish only places on a read that passed).
        if flow and self.flow_ok(t, cfg) is False:
            return None
        return plan_market(t, others, P, cfg, {"yes": max(v, 0.0), "no": max(-v, 0.0)},
                           tick=lambda px: self.tick(t, px), current=current,
                           exit=exit_lots(others, cfg, cfg.max_position))

    def signed_positions(self, hedged_only: bool = False) -> dict[str, float]:
        """Signed YES contracts (negative = long NO) from our fills, unsettled
        markets only; `hedged_only`: fills of orders whose fills we hedge."""
        out: dict[str, float] = {}
        ok = set(self.state.get("hedged_oids", [])) if hedged_only else None
        for f in self.state["fills"]:
            if self.meta.get(f["ticker"], {}).get("result") or (ok is not None and f.get("order_id") not in ok):
                continue
            out[f["ticker"]] = out.get(f["ticker"], 0.0) + (f["size"] if f["side"] == "yes" else -f["size"])
        return {t: v for t, v in out.items() if abs(v) > 1e-9}

    def write_claims(self, want: dict[str, farm.Plan]) -> bool:
        """Claim what we want, rest on, or hold; keep each ticker's first-claim
        time, which the placement gate waits on. True if anything new was claimed."""
        pos = self.signed_positions()
        now = time.time()
        old = self.state.setdefault("claims", {})
        claims = {t: old.get(t, now) for t in set(want) | self.order_tickers() | set(pos)}
        self.state["claims"] = claims
        os.makedirs(os.path.dirname(farm.CLAIMS_PATH), exist_ok=True)
        tmp = farm.CLAIMS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"written_at": now, "tickers": sorted(claims), "positions": pos}, f)
        os.replace(tmp, farm.CLAIMS_PATH)
        return any(t not in old for t in claims)

    def period_accrued(self, t: str) -> float:
        return self.state.setdefault("period_accrued", {}).get(self.programs[t]["id"], 0.0)

    def choose(self, cfg: Cfg) -> dict[str, farm.Plan]:
        """The markets to quote, within max_capital and num_markets, by what a slot is
        worth per day: LIP net of expected loss, plus, for a market under $1 this period
        that can still reach it, its accrual over the days still needed (quoting it that
        long unlocks it; dropping it forfeits it). Markets quoted now get INCUMBENT_EDGE
        so a near-tie doesn't swap them. A market not started this period must be able
        to earn START_PAYOUT_MARGIN x the $1 minimum before its period ends."""
        now = time.time()

        def left(t):
            return max(pd.Timestamp(self.programs[t]["end_date"]).timestamp() - now, 0.0)

        def net(p):
            return p.rate - p.loss_rate
        cool = self.state.setdefault("cooldown", {})
        def need(p):
            return farm.LIP_MIN_PAYOUT_USD * (1.0 if self.period_accrued(p.ticker) > 0 else farm.START_PAYOUT_MARGIN)
        cands = [p for p in self.plans.values() if p.ticker in self.programs and cool.get(p.ticker, 0.0) <= now
                 and (self.period_accrued(p.ticker) >= farm.LIP_MIN_PAYOUT_USD
                      or self.period_accrued(p.ticker) + p.rate * left(p.ticker) >= need(p))]

        quoted = self.order_tickers()

        def worth(p):
            a = self.period_accrued(p.ticker)
            v = net(p)
            if 0 < a < farm.LIP_MIN_PAYOUT_USD:
                v += a / max((farm.LIP_MIN_PAYOUT_USD - a) / max(p.rate, 1e-12), 1.0)
            return v * (INCUMBENT_EDGE if p.ticker in quoted else 1.0)
        out, exp = {}, self.exposure()
        for p in sorted(cands, key=lambda p: -worth(p)):
            if len(out) >= cfg.num_markets:
                break
            if exp + p.lots * max(px for _, px in p.legs) <= cfg.max_capital + 1e-9:
                out[p.ticker] = p
        return out

    def book_fills(self, fills: list[dict]) -> set[str]:
        """Record fills of our orders (deduped on trade_id, from the socket or
        REST); returns the tickers that had new ones."""
        # An order still being created has no id yet (None): it can't have fills, and None
        # in the set made sorting it raise and abort the cycle (11-06 00:5x).
        ours = {o["oid"]: k for k, o in self.state["orders"].items() if o.get("oid")}
        known = (set(self.state.get("all_oids", [])) | set(ours)) - {None}
        self.state["all_oids"] = sorted(known)
        seen = set(self.state["trade_ids"])
        booked = set(self.state.get("ioc_booked", []))
        new: set[str] = set()
        for f in fills:
            if f.get("order_id") not in known or f.get("trade_id") in seen or f.get("order_id") in booked:
                continue
            seen.add(f["trade_id"])
            f = dict(f, time=str(f["time"]) if f.get("time") is not None else None)
            self.state["fills"].append(f)
            new.add(f["ticker"])
            k = ours.get(f["order_id"])
            if k:
                o = self.state["orders"][k]
                o["remaining"] = max(o["remaining"] - f["size"], 0.0)
                if o.get("group"):
                    self.group_fill(o["group"], f["size"])
        self.state["trade_ids"] = sorted(seen)
        if new:
            print(f"fill: {sorted(new)}")
            self.flag_adverse()
        return new

    def flag_adverse(self):
        """Mark markets where fills on one of our LIP bids reached adverse_lots, or adverse_frac of the bid."""
        lim = self.cfg_adverse
        sizes = self.state.get("lip_oid_size", {})
        done = set(self.state.setdefault("adverse_oids", []))
        got: dict[str, float] = {}
        tick: dict[str, str] = {}
        for f in self.state["fills"]:
            o = f.get("order_id")
            if o in sizes and o not in done:
                got[o] = got.get(o, 0.0) + f["size"]
                tick[o] = f["ticker"]
        sent = self.state.get("sentinel_oids", {})
        for o, q in got.items():
            if o in sent or q >= min(lim, self.cfg_adverse_frac * sizes[o]) - 1e-9:
                self.state["adverse_oids"].append(o)
                self.dumping.add(tick[o])
                self.pull_event(tick[o])
                if o in sent:
                    # Its fill tripped the event's group (group_fill): Kalshi pulled the bids.
                    self.cool(tick[o], TRIP_COOLDOWN_S)
                    print(f"sentinel: {sent[o]} cleared; its market is pulled for {TRIP_COOLDOWN_S:.0f} s")
                else:
                    print(f"adverse: {tick[o]} filled {q:.0f} of {sizes[o]:.0f}")

    def cool(self, t: str, span: float):
        """Keep t off for `span` from now, never shortening a longer cooldown (an event pull)."""
        c = self.state.setdefault("cooldown", {})
        c[t] = max(c.get(t, 0.0), time.time() + span)

    def pull_event(self, t: str):
        """Every market in t's event off for cfg.event_pull_s; our bids there go once
        the filled market's exit is out (evaluate handles `pull`)."""
        span = self.cfg.event_pull_s if self.cfg is not None else 0.0
        if span <= 0:
            return
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        until, cool = time.time() + span, self.state.setdefault("cooldown", {})
        hit = sorted({x for x in self.programs if (self.meta.get(x, {}).get("event_ticker") or event_of(x)) == ev}
                     | {t})
        for x in hit:
            cool[x] = max(cool.get(x, 0.0), until)
            if x != t and self.keys_of(x, hedges=False):
                self.pull.add(x)
                if self.dirty is not None:
                    self.dirty.add(x)
        self.journal({"kind": "event_pull", "event": ev, "from": t, "markets": hit, "until": until})
        print(f"event pull: {ev} ({len(hit)} markets) off for {span / 60:.0f} min after the fill on {t}")

    def accrue(self):
        now = time.time()
        dt = now - self.last_accrue
        self.last_accrue = now
        per = self.state.setdefault("period_accrued", {})
        for t in self.order_tickers():
            gain = self.rates.get(t, 0.0) * dt
            self.state["accrued"][t] = self.state["accrued"].get(t, 0.0) + gain
            if t in self.programs:
                pid = self.programs[t]["id"]
                per[pid] = per.get(pid, 0.0) + gain
        # Market-days each series had LIP bids up, per UTC day, for realized_loss.
        qd = self.state.setdefault("quoted_days", {})
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        for t in {unkey(k)[0] for k in self.state["orders"] if not is_hedge(k)}:
            d = qd.setdefault(series_of(t), {})
            d[day] = d.get(day, 0.0) + dt / 86400
        cut = time.strftime("%Y-%m-%d", time.gmtime(now - QUOTED_KEEP_DAYS * 86400))
        for d in qd.values():
            for x in [x for x in d if x < cut]:
                del d[x]

    def realized_loss(self, cfg: Cfg) -> dict[str, float]:
        """Series -> its fill loss, $/day per market quoted at max_position, over the last
        realized_loss_days: positions first filled in the window, marked as in positions()."""
        if cfg.realized_loss_days <= 0:
            return {}
        now = time.time()
        since = now - cfg.realized_loss_days * 86400
        pnl, first = collections.Counter(), {}
        for p in self.positions():
            t = p["ticker"]
            if t not in self.meta or not p.get("first"):
                continue                      # no mark yet: its bid would read as 0
            pnl[t] += p["pnl"]
            first[t] = min(first.get(t, p["first"]), p["first"])
        loss = collections.Counter()
        for t, v in pnl.items():
            if pd.Timestamp(first[t]).timestamp() >= since:
                loss[series_of(t)] -= v
        cut = time.strftime("%Y-%m-%d", time.gmtime(since))
        out = {}
        for s, v in loss.items():
            days = sum(x for d, x in self.state.get("quoted_days", {}).get(s, {}).items() if d >= cut)
            out[s] = farm.realized_loss_day(v, days, cfg.realized_loss_prior)
        return out

    def flow_ok(self, t: str, cfg: Cfg) -> bool | None:
        """Low-flow check: True/False, None while its trades haven't been read.
        A stale or missing reading is queued for a re-read; a stale one still answers."""
        skip = {c.strip().lower() for c in cfg.skip_categories.split(",") if c.strip()}
        if self.category(t).lower() in skip:
            return False
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        if ev.split("-")[0].upper() in {x.strip().upper() for x in cfg.skip_series.split(",") if x.strip()}:
            return False
        dtc = self.days_to_close(t)
        if dtc is not None and dtc * 24 < cfg.min_close_h:
            return False
        flows = self.state.setdefault("flow", {})
        fut = self.flow_futs.get(t)
        if fut is not None and fut.done():
            del self.flow_futs[t]
            if fut.result() is not None:
                flows[t] = fut.result()
        f = flows.get(t)
        stale = f is None or time.time() - f["at"] > farm.FLOW_TTL_S or abs(f["days"] - cfg.flow_days) > 1e-9
        if stale and t not in self.flow_futs and len(self.flow_futs) < farm.FLOW_QUEUE:
            ot = self.meta.get(t, {}).get("open_time")
            self.flow_futs[t] = self.flow_pool.submit(farm.fetch_flow, t, cfg.flow_days,
                                                      pd.Timestamp(ot).timestamp() if ot else None)
        if cfg.flow_long_days > 0:
            longs = self.state.setdefault("flow_long", {})
            lk = t + "|long"
            fut = self.flow_futs.get(lk)
            if fut is not None and fut.done():
                del self.flow_futs[lk]
                if fut.result() is not None:
                    longs[t] = fut.result()
            g = longs.get(t)
            stale = (g is None or time.time() - g["at"] > farm.FLOW_LONG_TTL_S
                     or abs(g["days"] - cfg.flow_long_days) > 1e-9)
            if stale and lk not in self.flow_futs and len(self.flow_futs) < farm.FLOW_QUEUE:
                ot = self.meta.get(t, {}).get("open_time")
                self.flow_futs[lk] = self.flow_pool.submit(farm.fetch_flow_long, t, cfg.flow_long_days,
                                                           pd.Timestamp(ot).timestamp() if ot else None)
            if g is None and (f is None or f["trades_day"] <= cfg.max_trades_day + 1e-9):
                return None
        if f is None:
            return None
        return f["trades_day"] <= cfg.max_trades_day + 1e-9

    def trades_day(self, t: str) -> float:
        """The expected-loss rate: the higher of the flow_days and flow_long_days readings."""
        f = self.state.get("flow", {}).get(t) or {}
        g = self.state.get("flow_long", {}).get(t) or {}
        return max(f.get("trades_day") or 0.0, g.get("trades_day") or 0.0)

    def url(self, t: str) -> str:
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        return farm.kalshi_url(t, ev, self.series_titles.get(ev.split("-")[0], ""))

    def days_to_close(self, t: str) -> float | None:
        c = self.meta.get(t, {}).get("close_time")
        return None if not c else (pd.Timestamp(c).timestamp() - time.time()) / 86400

    def category(self, t: str) -> str:
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        return self.categories.get(ev.split("-")[0], "")

    def _group_fill_live(self, gid: str, size: float):
        """Count a grouped fill as Kalshi does; past the limit the group has tripped."""
        g = next((g for g in self.state.get("groups", {}).values() if g.get("id") == gid), None)
        if g is None or g.get("tripped"):
            return
        now = time.time()
        f = [x for x in g.setdefault("fills", []) if x[0] >= now - farm.GROUP_WINDOW_S] + [[now, size]]
        g["fills"] = f
        total = sum(x[1] for x in f)
        if total > GROUP_LIMIT + 1e-9:
            self.group_trip(gid, f"{total:g} contracts matched in {farm.GROUP_WINDOW_S:g} s")

    def _others_base(self, t: str, ob) -> dict[str, dict[float, float]]:
        """Others' books from a REST orderbook or (yes, no) bids: both include
        our live bids (a shadow one isn't there)."""
        yes, no = parse_book(ob) if isinstance(ob, dict) else ob
        others = {"yes": yes, "no": no}
        if self.live:
            for side, o in self.legs_of(t):
                others[side] = remove_ours(others[side], o["price"], o["remaining"])
        return others

    # ---- paths and persistence ----
    def _open_mode(self, live: bool):
        self.live = live
        self.dir = os.path.join(self.root, "live" if live else "shadow")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = self._load_state()
        self.rates = {}
        self.last_accrue = time.time()

    async def save(self):
        data = json.dumps(self.state)
        await asyncio.get_running_loop().run_in_executor(self.io_pool, _atomic_write, self.state_path, data)

    async def run_io(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *a)

    def journal(self, row: dict):
        self.rows.append({"t": time.time(), "live": self.live, **row})

    async def flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_S)
            if self.rows:
                rows, self.rows = self.rows, []
                path = os.path.join(self.root, "journal", f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}.jsonl")
                await asyncio.get_running_loop().run_in_executor(self.io_pool, _append_rows, path, rows)

    # ---- tasks (as ARBS's spawn: a ticker is busy until its last task ends) ----
    def spawn(self, coro, t: str | None = None):
        if t is not None:
            self.busy.add(t)
            self.busy_n[t] = self.busy_n.get(t, 0) + 1

        async def wrap():
            try:
                await coro
            except Exception:
                print(f"task error ({t}):")
                traceback.print_exc()
            finally:
                if t is not None:
                    self.busy_n[t] -= 1
                    if self.busy_n[t] <= 0:
                        del self.busy_n[t]
                        self.busy.discard(t)
                    if t in self.urgent_after:
                        self.urgent_after.discard(t)
                        self.urgent_set.add(t)
                    else:
                        self.dirty.add(t)
        task = asyncio.get_running_loop().create_task(wrap())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ---- exchange plumbing (no blocking calls on the loop) ----
    def shard(self, t: str) -> int | None:
        m = self.meta.get(t)
        return None if m is None else int(m.get("exchange_index") or 0)

    def client(self, shard: int):
        return self.gw.clients.get(shard)

    def taker_rate(self, t: str) -> float:
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        return self.taker_rates.get(ev, farm.DEFAULT_TAKER_RATE)

    async def prefetch_fees(self, tickers):
        from kalshi_infra.kalshi import fees
        for t in tickers:
            ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
            if ev in self.taker_rates:
                continue
            try:
                self.taker_rates[ev] = (await self.run_io(fees.for_event, ev.split("-")[0], ev)).taker_rate
            except Exception:
                self.taker_rates[ev] = farm.DEFAULT_TAKER_RATE

    async def setup_clients(self):
        shards = {t: s for t in self.meta if (s := self.shard(t)) is not None}
        if self.shadow_only:
            self.gw.shard_of = shards           # no clients: nothing is ever sent
            return
        await self.run_io(self.gw.setup, shards, None)

    def others_of(self, t: str, ob) -> dict[str, dict[float, float]]:
        others = self._others_base(t, ob)
        for side, price, rem in self.ghosts.get(t, ()):
            others[side] = remove_ours(others[side], price, rem)
        return others

    async def ghost_loop(self):
        while self.beside:
            try:
                st = json.loads(await self.run_io(_read, self.beside))
                g: dict[str, list] = {}
                for k, o in st.get("orders", {}).items():
                    g.setdefault(unkey(k)[0], []).append((o["side"], o["price"], o["remaining"]))
                self.ghosts = g
            except Exception as e:
                print(f"beside: {e}")
            await asyncio.sleep(5.0)

    def ws_others(self, t: str) -> dict | None:
        b = self.feed.ladder(t) if self.feed is not None and self.feed.up else None
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        bids = ({round(p, 4): q for p, q in dict(b.yes_bids).items() if q > 0},
                {round(p, 4): q for p, q in dict(b.no_bids).items() if q > 0})
        return self.others_of(t, bids)

    def ensure_feed(self, tickers):
        tickers = sorted(set(tickers))
        if not tickers:
            return
        if self.feed is None:
            self.feed = lip_feed_class()(tickers, self._on_quote, self._on_trade, label=NAME, on_fill=self._on_fill)
            self.feed.on_level = self._on_level
            self.spawn(self.feed.run())
            return
        new = [t for t in tickers if t not in self.feed.tickers]
        if new:
            self.feed.add(new)

    # ---- websocket callbacks (on the loop) ----
    def _on_quote(self, view):
        t = view.ladder.ticker
        self.book_seq[t] = self.book_seq.get(t, 0) + 1
        legs = self.legs_of(t, hedges=False)
        if legs:
            self.note_depth(t, view.ladder, legs)
        if legs or t in self.order_tickers() or t in self.positions_now:
            self.dirty.add(t)

    def note_depth(self, t: str, ladder, legs):
        """Others' size at each of our LIP prices (the book shows ours too) and at both touches."""
        now = time.time()
        books = {"yes": {round(p, 4): q for p, q in dict(ladder.yes_bids).items() if q > 0},
                 "no": {round(p, 4): q for p, q in dict(ladder.no_bids).items() if q > 0}}
        snap = {f"{s}_top": [max(b), b[max(b)]] if b else None for s, b in books.items()}
        snap["at_ours"] = {f"{side}@{o['price']:g}": round(books[side].get(round(o["price"], 4), 0.0) - o["remaining"], 2)
                           for side, o in legs}
        # Guessed orders (levels.Levels) at our prices and the three levels under each.
        snap["orders"] = {f"{side}@{p:g}": self.levels.view(t, side, p)["sizes"]
                          for side, o in legs for p in sorted(books[side], reverse=True)
                          if o["price"] - 0.03 - 1e-9 <= p <= o["price"] + 1e-9}
        dq = self.depth_hist.setdefault(t, collections.deque())
        dq.append((now, snap))
        while dq and dq[0][0] < now - DEPTH_KEEP_S:
            dq.popleft()

    def _on_fill(self, f: dict):
        if f.get("liquidity") == "taker" or f.get("order_id") in self.gw.ioc_ids:
            return                             # our IOCs are booked from their response
        oids = {o["oid"] for o in self.state["orders"].values()}
        known = f.get("order_id") in oids or f.get("order_id") in set(self.state.get("all_oids", []))
        if not known:
            if any(o.get("oid") is None for o in self.state["orders"].values()):
                self.orphans.append((time.time(), f))   # its create may not be answered yet
            return
        self.take_fills([f])

    def take_fills(self, fills: list[dict]):
        """Book fills, then: the markets they hit are urgent, and the capital guard runs."""
        now = time.time()
        new = self.book_fills(fills)
        sizes = self.state.get("lip_oid_size", {})
        for f in fills:
            if f.get("ticker") in new:
                self.seen_at[f["ticker"]] = (now, f.get("time"))
                if f.get("order_id") in sizes:
                    # Adverse after the fact if the price hasn't come back by recover_s (recovery_loop).
                    self.recovery.append((now, f["ticker"], f["side"], f["price"]))
                    hist = [(round(now - at, 3), snap) for at, snap in self.depth_hist.get(f["ticker"], ())
                            if at >= now - FILL_CONTEXT_S]
                    self.journal({"kind": "fill_context", "ticker": f["ticker"], "side": f["side"],
                                  "price": f["price"], "size": f["size"], "fill_time": str(f.get("time")),
                                  "seen": now, "depth_before": hist})
        self.positions_now = set(self.signed_positions())
        if new:
            self.urgent_set |= new
            self.capital_check()
        return new

    def replay_orphans(self):
        if not self.orphans:
            return
        oids = {o["oid"] for o in self.state["orders"].values()}
        now = time.time()
        keep, ready = [], []
        for at, f in self.orphans:
            if f.get("order_id") in oids:
                ready.append(f)
            elif now - at < ORPHAN_S:
                keep.append((at, f))
        self.orphans = keep
        if ready:
            self.take_fills(ready)

    def _on_level(self, mtype: str, msg: dict):
        try:
            now, t = time.time(), msg["market_ticker"]
            if mtype == "orderbook_snapshot":
                for side, k in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
                    self.levels.snapshot(t, side, [(float(p), float(q)) for p, q in msg.get(k) or []], now)
            else:
                self.levels.delta(t, msg["side"], float(msg["price_dollars"]), float(msg["delta_fp"]), now)
        except Exception as e:
            print(f"levels: {e!r}")

    def _on_trade(self, t: str, tr):
        """Every print marks its level for levels.Levels; in shadow, a print below
        our bid's price swept its level, and us with it."""
        if tr.taker_side not in ("yes", "no"):
            return
        maker = "no" if tr.taker_side == "yes" else "yes"
        self.levels.trade(t, maker, tr.price, time.time())
        if self.live or not self.sim:
            return
        fills = []
        for k, o in list(self.state["orders"].items()):
            if unkey(k)[0] != t or o["side"] != maker or o["remaining"] <= 1e-9:
                continue
            if tr.price < o["price"] - 1e-9:
                fills.append({"ticker": t, "side": maker, "price": o["price"], "size": o["remaining"], "fee": 0.0,
                              "order_id": o["oid"], "trade_id": "sim-" + uuid.uuid4().hex[:12],
                              "time": pd.Timestamp.now(tz="UTC"), "liquidity": "maker"})
        if fills:
            self.take_fills(fills)

    # ---- orders ----
    def cancel(self, k: str, reason: str = "lipfarm:cancel"):
        """Forget k now and send its cancel (spawned). A create still in flight
        is cancelled by its own task once it's answered."""
        o = self.state["orders"].pop(k, None)
        if o and self.live and o.get("oid"):
            t = unkey(k)[0]
            self.spawn(self._cancel(t, o["oid"], reason))

    async def _cancel(self, t: str, oid: str, reason: str):
        self.bucket.take(CANCEL_TOKENS, force=True)
        _, exc, _ = await self.gw.cancel(t, oid, reason)
        if exc is not None and http_status(exc) != 404:
            print(f"cancel {t} {oid}: {exc}")

    async def group_id(self, t: str) -> str | None:
        """t's event's order group, created or reset as needed (one call per
        event at a time); None if that fails."""
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        if not self.live:
            return "shadow-" + ev
        while ev in self.group_wait:
            await asyncio.shield(self.group_wait[ev])
        g = self.state.setdefault("groups", {}).get(ev)
        if g is not None and not g.get("tripped"):
            g["used"] = time.time()
            return g["id"]
        if not self.bucket.take(ORDER_TOKENS):
            return None
        fut = asyncio.get_running_loop().create_future()
        self.group_wait[ev] = fut
        try:
            if g is None:
                gid, exc = await self.gw.group_call(t, "create_order_group", GROUP_LIMIT)
                if exc is not None:
                    print(f"group {ev}: {exc}")
                    return None
                g = {"id": gid, "tripped": False, "fills": [], "ticker": t}
                self.state["groups"][ev] = g
            else:
                _, exc = await self.gw.group_call(t, "reset_order_group", g["id"])
                if exc is not None:
                    print(f"group {ev} reset: {exc}")
                    return None
                g["tripped"], g["fills"] = False, []
            g["used"] = time.time()
            return g["id"]
        finally:
            self.group_wait.pop(ev, None)
            fut.set_result(None)

    def budget_ok(self, t: str, hedge: bool, reserve: float = PLACE_RESERVE) -> bool:
        """Spend the tokens for one create, or refuse (a LIP bid only; an exit always goes)."""
        if not self.live:
            return True
        c = self.gw.client(t)
        if c is None:
            return False
        if hedge:
            self.bucket.take(ORDER_TOKENS, force=True)
            return c.allow_now("order", force=True, reason="lipfarm:hedge")
        if not c.can_afford("order", reserve=reserve * c.tokens_per_s) or not self.bucket.take(ORDER_TOKENS):
            self.n_refused += 1
            if time.time() - self.last_refused_log > 60:
                self.last_refused_log = time.time()
                print(f"budget: {self.n_refused} bids refused so far (our bucket {self.bucket.tokens:.0f}, "
                      f"shard {c.tokens_now():.0f})")
            return False
        if not c.allow_now("order", reason="lipfarm:quote"):
            self.bucket.refund(ORDER_TOKENS)
            return False
        return True

    async def place(self, t: str, side: str, price: float, lots: float, reserve: float = PLACE_RESERVE,
                    reason: str = "lipfarm:quote", hedge: bool = False, sentinel: bool = False,
                    force: bool = False) -> bool:
        """`force`: spend the write tokens regardless, as an exit does (a sentinel whose bid rests)."""
        if not hedge and self.state.get("cooldown", {}).get(t, 0.0) > time.time():
            return False
        k = f"{t}|{side}|s" if sentinel else key(t, side, hedge)
        if k in self.state["orders"] or self.place_wait.get(k, 0.0) > time.time():
            return False
        gid = None
        if not hedge:
            gid = await self.group_id(t)
            if gid is None:
                return False
        if not self.budget_ok(t, hedge or force, reserve):
            return False
        rec = {"oid": None, "side": side, "price": price, "size": lots, "remaining": lots, "placed": time.time()}
        if not hedge:
            rec["group"] = gid
            # Others' size at or above our price when we joined (status: has it grown since?).
            rec["crowd0"] = round(sum(q for p, q in self.books.get(t, {}).get(side, {}).items() if p >= price - 1e-9), 2)
        self.state["orders"][k] = rec
        if not self.live:
            rec["oid"] = "shadow-" + uuid.uuid4().hex[:12]
            self._note_placed(rec["oid"], t, side, lots, hedge, sentinel)
            return True
        c = self.gw.client(t)
        o = self.gw._order(t, side, price, lots, "quote", cid=CID_PREFIX + uuid.uuid4().hex[:24])
        rec["cid"] = o.client_order_id
        t0 = time.time()
        try:
            ack = await self.gw.write(c.place_resting, o, order_group_id=gid)
            exc = None
        except Exception as e:
            ack, exc = None, e
        self.journal({"kind": "order", "op": "rest", "ticker": t, "side": side, "price": price, "size": lots,
                      "hedge": hedge, "sentinel": sentinel, "sent": t0, "answered": time.time(),
                      "error": None if exc is None else repr(exc)[:200]})
        if exc is not None:
            if self.state["orders"].get(k) is rec:
                del self.state["orders"][k]
            self.place_wait[k] = time.time() + (PLACE_RETRY_CONFLICT_S if http_status(exc) == 409 else PLACE_RETRY_S)
            if http_status(exc) == 429:
                c.note_rate_limited(reason)
            print(f"place {t} {side}@{price}: {exc}")
            if gid and is_order_group_reject(exc):
                self.group_trip(gid, "create refused")
            return False
        rec["oid"] = ack.exchange_order_id
        self._note_placed(rec["oid"], t, side, lots, hedge, sentinel)
        self.levels.ours(t, side, price, lots, rec["oid"], time.time())
        if self.state["orders"].get(k) is not rec:
            # Cancelled (or a trip pulled it) while the create was in flight.
            self.spawn(self._cancel(t, rec["oid"], "lipfarm:late"))
            return False
        self.replay_orphans()
        return True

    def _note_placed(self, oid, t, side, lots, hedge, sentinel):
        self.state.setdefault("hedged_oids", []).append(oid)
        known = self.state.setdefault("all_oids", [])
        known.append(oid)
        if not hedge:
            self.state.setdefault("lip_oid_size", {})[oid] = lots
        if sentinel:
            self.state.setdefault("sentinel_oids", {})[oid] = key(t, side)

    async def take(self, t: str, side: str, price: float, q: float, why: str) -> float:
        """IOC buy of `side` at `price` (always sent); books and returns what filled."""
        t0 = time.time()
        if self.live:
            self.bucket.take(ORDER_TOKENS, force=True)
            fill, exc, _ = await self.gw.ioc(t, side, price, q, force=True, reason="lipfarm:take")
        else:
            fill, exc = self.sim_ioc(t, side, price, q), None
        seen, kts = self.seen_at.get(t, (None, None))
        self.journal({"kind": "order", "op": "ioc", "why": why, "ticker": t, "side": side, "price": price,
                      "size": q, "sent": t0, "answered": time.time(), "fill_seen": seen,
                      "fill_time": None if kts is None else str(kts),
                      "filled": 0.0 if fill is None else fill.size,
                      "error": None if exc is None else repr(exc)[:200]})
        got = 0.0 if exc is not None or fill is None else fill.size
        if why == "informed":
            misses = self.state.setdefault("dump_misses", {})
            if got < q - 1e-9 and misses.get(t, 0) + 1 < MAX_DUMP_MISSES:
                misses[t] = misses.get(t, 0) + 1
                self.dumping.add(t)                  # again on the next book update
            else:
                misses.pop(t, None)
        if exc is not None:
            print(f"take {t} {side}@{price} x{q:.0f}: {exc}")
            return 0.0
        if fill is None or fill.size <= 0:
            return 0.0
        oid = fill.exchange_order_id or ("ioc-" + uuid.uuid4().hex[:12])
        self.state.setdefault("hedged_oids", []).append(oid)
        self.state.setdefault("ioc_booked", []).append(oid)
        self.state["fills"].append({"ticker": t, "side": side, "price": fill.price, "size": fill.size,
                                    "fee": fill.fee, "order_id": oid, "trade_id": "ioc:" + oid,
                                    "time": pd.Timestamp.now(tz="UTC").isoformat(), "liquidity": "taker"})
        self.positions_now = set(self.signed_positions())
        print(f"take: {t} {side}@{fill.price:.4f} x{fill.size:.0f} ({why}, "
              f"{1000 * (t0 - seen) if seen else float('nan'):.0f} ms after the fill reached us)")
        return fill.size

    def sim_ioc(self, t: str, side: str, price: float, q: float):
        """Shadow IOC: buying `side` up to `price` is selling the other side into
        others' bids from 1 - price up, at the book in memory."""
        held = "no" if side == "yes" else "yes"
        book = self.books.get(t, {}).get(held, {})
        got, cost = 0.0, 0.0
        for p in sorted(book, reverse=True):
            if p < 1.0 - price - 1e-9 or got >= q - 1e-9:
                break
            n = min(book[p], q - got)
            got += n
            cost += n * (1.0 - p)
        if got <= 0:
            return None
        px = cost / got
        return SimpleNamespace(price=round(px, 4), size=got, fee=kfee(px, got, self.taker_rate(t)),
                               exchange_order_id="simioc-" + uuid.uuid4().hex[:12])

    # ---- order groups ----
    def group_trip(self, gid: str, why: str):
        """Kalshi has cancelled every bid in the group: off those markets for
        TRIP_COOLDOWN_S. Our cancels only confirm it, after the market's exit
        has been sent (evaluate)."""
        g = next((g for g in self.state.get("groups", {}).values() if g.get("id") == gid), None)
        if self.live and (g is None or g.get("tripped")):
            return
        if g is not None:
            g["tripped"] = True
        gone = [k for k, o in self.state["orders"].items() if o.get("group") == gid]
        tickers = sorted({unkey(k)[0] for k in gone})
        for t in tickers:
            self.pull.add(t)
            self.cool(t, TRIP_COOLDOWN_S)
        if self.dirty is not None:
            self.dirty |= set(tickers)       # urgent (the filled market) goes first
        self.journal({"kind": "group_trip", "group": gid, "why": why, "markets": tickers})
        print(f"group: tripped ({why}); our bids pulled on {', '.join(tickers) or 'nothing'} "
              f"for {TRIP_COOLDOWN_S:.0f} s")

    def group_fill(self, gid: str, size: float):
        if not self.live:
            # Shadow groups exist only in name: count them here.
            g = self.state.setdefault("shadow_groups", {}).setdefault(gid, {"fills": []})
            now = time.time()
            g["fills"] = [x for x in g["fills"] if x[0] >= now - farm.GROUP_WINDOW_S] + [[now, size]]
            if sum(x[1] for x in g["fills"]) > GROUP_LIMIT + 1e-9:
                g["fills"] = []
                self.group_trip(gid, "shadow: past the limit")
            return
        self._group_fill_live(gid, size)

    async def tidy_groups(self):
        if not self.live:
            return
        resting = {o.get("group") for o in self.state["orders"].values()}
        n = 0
        for ev, g in list(self.state.get("groups", {}).items()):
            if g.get("id") in resting or time.time() - g.get("used", 0.0) < GROUP_IDLE_S:
                continue
            t = g.get("ticker") or ev.split("|")[0]
            c = self.gw.client(t)
            if c is None or n >= GROUP_DELETES or not self.bucket.take(CANCEL_TOKENS) \
                    or not c.allow_now("cancel", reason="lipfarm:group"):
                return
            n += 1
            _, exc = await self.gw.group_call(t, "delete_order_group", g["id"])
            if exc is not None and http_status(exc) != 404:
                print(f"group {ev} delete: {exc}")
                continue
            self.state["groups"].pop(ev, None)

    # ---- the hot path ----
    async def eval_loop(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            self.replay_orphans()
            start = time.perf_counter()
            while self.urgent_set or self.dirty:
                if self.urgent_set:
                    t = self.urgent_set.pop()
                    self.dirty.discard(t)
                    urgent = True
                else:
                    t, urgent = self.dirty.pop(), False
                if t not in self.busy:
                    try:
                        self.evaluate(t, urgent)
                    except Exception:
                        print(f"eval error {t}:")
                        traceback.print_exc()
                elif urgent:
                    self.urgent_after.add(t)        # its task re-marks it urgent when done
                if time.perf_counter() - start > EVAL_SLICE_S:
                    break
            if self.urgent_set or self.dirty:
                self.wake.set()
            await asyncio.sleep(0)

    def evaluate(self, t: str, urgent: bool):
        """Exits first, then (only now) the cancels a trip owes, then the guard."""
        self.n_evals += 1
        cfg = self.cfg
        others = self.ws_others(t)
        if others is not None:
            self.books[t] = others
        self.exits(t, cfg, takes=urgent)
        if t in self.pull:
            self.pull.discard(t)
            for k in self.keys_of(t, hedges=False):
                self.cancel(k, reason="lipfarm:trip")
        if others is not None and t not in self.busy:
            self.guard_market(t, others, cfg)

    def exits(self, t: str, cfg: Cfg, takes: bool):
        """farm.hedge for one market: informed fill -> pair lock off the book or
        sell at once; dust -> sell; still falling -> chase; the rest rests an
        exit bid. Every write is spawned; t is busy until they're answered."""
        v = self.free_positions().get(t, 0.0)
        dtc = self.days_to_close(t)
        if dtc is not None and dtc <= 0:
            v = 0.0                                  # closed: nothing can trade until it settles
        if abs(v) < 0.01 or t not in self.books or t not in self.meta:
            self.dumping.discard(t)
            self.exit_bid(t, 0.0)
            return
        held = "yes" if v > 0 else "no"
        fill_px = self.avg_cost(t, held)
        tk = self.tick(t, fill_px)
        lowest = fill_px - cfg.max_chase_ticks * tk
        if t in self.dumping:
            if t in self.state.get("dump_misses", {}) and self.dump_seq.get(t) == self.book_seq.get(t, 0):
                return                               # a retry waits for the book to change
            self.dumping.discard(t)
            if takes and self.pair_lock(t, held, abs(v)):
                return
            self.dump_seq[t] = self.book_seq.get(t, 0)
            self.dump(t, floor=fill_px - min(cfg.dump_slip_ticks, cfg.max_chase_ticks) * tk, why="informed")
            return
        if takes and self.baskets.get((t, held)):
            # Not urgent (an informed fill sold above): the market waits on its REST reads.
            self.spawn(self.basket_lock(t, held), t)
            return
        if time.time() - self.state.get("chased", {}).get(t, 0.0) >= EXIT_REPRICE_S:
            if abs(v) <= DUST_LOTS:
                self.dump(t, floor=None, adverse=False, why="dust")
                return
            best = max(self.books[t][held]) if self.books[t][held] else 0.0
            if lowest - 1e-9 <= best <= fill_px - cfg.chase_ticks * tk + 1e-9:
                self.dump(t, floor=max(best - cfg.dump_slip_ticks * tk, lowest), adverse=False, why="chase")
                return
        self.exit_bid(t, v)

    def pair_lock(self, t: str, held: str, qty: float) -> bool:
        """Take the market's other side when the pair costs at most $1 after fees (off the book in hand)."""
        other = "no" if held == "yes" else "yes"
        a = self.ask(t, other)
        if not a:
            return False
        px, sz = a
        if self.avg_cost(t, held) + px + kfee(px, 1.0, self.taker_rate(t)) > 1.0 + 1e-9:
            return False
        q = math.floor(min(qty, sz))
        if q < 1:
            return False

        async def go():
            got = await self.take(t, other, px, q, "pair lock")
            if got >= 1.0:
                print(f"lock: {t} {held} x{got:.0f} (pair)")
        self.spawn(go(), t)
        return True

    async def basket_lock(self, t: str, held: str):
        """farm.try_lock's basket half: other legs' books may need REST reads, so
        it runs after the market's own exit, as its own task."""
        bs = self.baskets.get((t, held), [])
        if not bs:
            return
        need = sorted({tk for _, legs in bs for tk, _ in legs if tk != t and tk not in self.books})
        for tk, ob in zip(need, await asyncio.gather(*(self.run_io(farm.fetch_book, x) for x in need))):
            if ob is not None:
                self.books[tk] = self.others_of(tk, parse_book(ob))
        qty = abs(self.free_positions().get(t, 0.0))
        cost = self.avg_cost(t, held)
        best = None
        for pay, legs in bs:
            buys, c = [], cost
            for tk, sd in legs:
                if (tk, sd) == (t, held):
                    continue
                a = self.ask(tk, sd)
                if a is None:
                    break
                c += a[0] + kfee(a[0], 1.0, self.taker_rate(tk))
                buys.append((tk, sd, a[0], a[1]))
            else:
                if c <= pay + 1e-9 and (best is None or c / pay < best[0] / best[1]):
                    best = (c, pay, buys)
        if best is None:
            return
        q = math.floor(min([qty] + [sz for *_, sz in best[2]]))
        if q < 1:
            return
        got = await asyncio.gather(*(self.take(tk, sd, px, q, "basket lock") for tk, sd, px, _ in best[2]))
        locked = min([q] + list(got))
        if locked >= 1.0:
            lk = self.state.setdefault("locked", {})
            for tk, sd in [(t, held)] + [(tk, sd) for tk, sd, *_ in best[2]]:
                lk[tk] = lk.get(tk, 0.0) + (locked if sd == "yes" else -locked)
            print(f"lock: {t} {held} x{locked:.0f} at ${best[0]:.3f} for ${best[1]:.0f} (basket)")

    def sibling_covers(self, t: str, held: str) -> list[tuple[float, float, str, str]]:
        """(offer, its size, market, side) for contracts in other markets that pay $1
        whenever `held` of t pays nothing (ARBS's two-leg $1 baskets), from books the
        socket already has: nothing is read, so a dump never waits on one."""
        out = []
        for pay, legs in self.baskets.get((t, held), ()):
            rest = [leg for leg in legs if leg[0] != t]
            if len(legs) != 2 or len(rest) != 1 or abs(pay - 1.0) > 1e-9:
                continue
            tk, sd = rest[0]
            others = self.ws_others(tk)
            if others is None:
                continue
            self.books[tk] = others
            opp = "no" if sd == "yes" else "yes"
            out += [(round(1.0 - p, 4), q, tk, sd) for p, q in others[opp].items() if q > 0]
        return out

    def sibling_tickers(self, tickers: set[str]) -> set[str]:
        """Markets sibling_covers could buy for these, best-earning markets first, up to SIBLING_FEED_MAX."""
        out: set[str] = set()
        for t in sorted(tickers, key=lambda x: -self.est.get(x, 0.0)):
            for side in ("yes", "no"):
                for pay, legs in self.baskets.get((t, side), ()):
                    rest = [leg[0] for leg in legs if leg[0] != t]
                    if len(legs) == 2 and len(rest) == 1 and abs(pay - 1.0) < 1e-9 and rest[0] not in tickers:
                        out.add(rest[0])
                        if len(out) >= SIBLING_FEED_MAX:
                            return out
        return out

    def dump(self, t: str, floor: float | None = None, adverse: bool = True, why: str = ""):
        """Sell what we hold in t into others' bids from `floor` up, as one IOC,
        with the market's exit bids (and, adverse, its LIP bids) cancelled in
        the same gather -- never ahead of it. A cover in another market
        (sibling_covers) cheaper after fees takes its share of the lots; the
        market's own IOC is still sent first."""
        v = self.free_positions().get(t, 0.0)
        held, other = ("yes", "no") if v > 0 else ("no", "yes")
        cancels = [k for k in self.keys_of(t) if is_hedge(k)]
        if adverse and abs(v) > DUST_LOTS:
            cancels += self.keys_of(t, hedges=False)
            self.cool(t, TRIP_COOLDOWN_S)
        need = abs(v)
        cap = None if floor is None else round(1.0 - floor, 4)       # most we pay to cover a lot
        levels = [(round(1.0 - p, 4), q, t, other) for p, q in self.books[t][held].items()]
        if need >= 1.0 and self.baskets.get((t, held)):
            levels += self.sibling_covers(t, held)
        levels.sort(key=lambda x: (x[0] + self.taker_rate(x[2]) * x[0] * (1.0 - x[0]), x[2] != t))
        buys: dict[tuple[str, str], list] = {}
        cum = 0.0
        for px, q, tk, sd in levels:
            if cum >= need - 1e-9:
                break
            if cap is not None and px > cap + 1e-9:
                continue
            k = min(q, need - cum)
            if tk != t:
                k = math.floor(k)                    # whole lots in markets we don't quote
                if k < 1:
                    continue
            b = buys.setdefault((tk, sd), [px, 0.0])
            b[0], b[1] = max(b[0], px), b[1] + k
            cum += k
        own = buys.pop((t, other), None)
        q = math.floor(own[1] * 100) / 100 if own else 0.0
        go = q >= 0.01 or bool(buys)
        if go:
            self.state.setdefault("chased", {})[t] = time.time()
        if q >= 0.01:
            self.spawn(self.take(t, other, own[0], q, why), t)
        for (tk, sd), (px, k) in buys.items():
            self.spawn(self.cover_take(t, held, tk, sd, px, k, why), t)
        for k in cancels:
            self.cancel(k, reason="lipfarm:adverse" if not is_hedge(k) else "lipfarm:dump")
        if not go:
            print(f"exit: {t} no bids to sell {held} into above {floor}; resting an exit")
            self.exit_bid(t, v)
            return
        print(f"exit: {t} selling {held} x{q} up to {own[0] if own else None}"
              + "".join(f", cover {tk} {sd} x{k:g} up to {px}" for (tk, sd), (px, k) in buys.items()) + f" ({why})")

    async def cover_take(self, t: str, held: str, tk: str, sd: str, px: float, q: float, why: str):
        """Buy `sd` of tk to cover `held` of t; what fills is locked as a pair."""
        got = await self.take(tk, sd, px, q, f"cover {why}")
        if got >= 0.01:
            lk = self.state.setdefault("locked", {})
            lk[t] = lk.get(t, 0.0) + (got if held == "yes" else -got)
            lk[tk] = lk.get(tk, 0.0) + (got if sd == "yes" else -got)
            self.journal({"kind": "sibling_cover", "ticker": t, "held": held, "cover": tk, "side": sd,
                          "price": px, "lots": got, "why": why})
            print(f"lock: {t} {held} x{got:.0f} covered by {tk} {sd} up to {px} ({why})")

    def exit_bid(self, t: str, v: float):
        """The resting exit: a bid of what we hold one tick above the other
        side's touch, under its offer. The touch is the first level from the top
        where others' size reaches EXIT_TOUCH_LOTS (or EXIT_TOUCH_FRAC of ours),
        so a few lots can't push it; re-priced up or down to it, at most every
        EXIT_REPRICE_S."""
        k_have = [k for k in self.keys_of(t) if is_hedge(k)]
        want = None
        others = self.books.get(t)
        if abs(v) >= 0.01 and others:
            held, side = ("yes", "no") if v > 0 else ("no", "yes")
            need, cum, touch = max(EXIT_TOUCH_LOTS, EXIT_TOUCH_FRAC * abs(v)), 0.0, 0.0
            for p in sorted(others[side], reverse=True):
                cum += others[side][p]
                touch = p
                if cum >= need:
                    break
            ask = 1.0 - max(others[held]) if others[held] else 1.0
            px = min(round(touch + self.tick(t, touch), 4), round(ask - self.tick(t, ask - 1e-9), 4))
            if px >= 0.001:
                want = (key(t, side, True), px, abs(v))
        now = time.time()
        for k in k_have:
            o = self.state["orders"][k]
            if o.get("oid") is None:
                continue                                    # still being placed
            if (want is None or k != want[0] or o["remaining"] > want[2] + 1e-9 or o["remaining"] < 0.5 * want[2]
                    or ((want[1] > o["price"] + 1e-9 or want[1] < o["price"] - 2 * self.tick(t, o["price"]) + 1e-9)
                        and now - o["placed"] >= EXIT_REPRICE_S)):
                # Up whenever outbid; down only from 2+ ticks over (it flapped 33-35-36c on FOOTWEAR 11-05).
                self.cancel(k, reason="lipfarm:hedge")
        if want and want[0] not in self.state["orders"]:
            k, px, q = want
            side = unkey(k)[1]

            async def go():
                if await self.place(t, side, px, q, reserve=0.0, reason="lipfarm:hedge", hedge=True):
                    print(f"exit: {t} {side}@{px} x{q:.0f}")
            self.spawn(go(), t)

    def guard_market(self, t: str, others: dict, cfg: Cfg):
        """A quoted side its market's plan no longer has is pulled (and the
        market cooled down); our rate there is re-scored."""
        if t not in self.programs:
            return
        keys = self.keys_of(t, hedges=False)
        if not keys:
            return
        P = self.params(t)
        pl = self.plan(t, others, P, cfg, self.signed_positions())
        sides = {s for s, _ in pl.legs} if pl else set()
        for k in keys:
            o = self.state["orders"].get(k)
            if o is None or o.get("oid") is None or unkey(k)[1] in sides:
                continue
            print(f"guard: pull {k} @{o['price']} (not in its plan now)")
            self.cancel(k, reason="lipfarm:guard")
            self.cool(t, GUARD_COOLDOWN_S)
        if pl:
            self.reprice(t, pl, others, P)
        self.rates[t] = self.our_rate(t, others, P)

    def reprice(self, t: str, pl: farm.Plan, others: dict, P):
        """Re-place a side now when its resting bids earn under REPRICE_FRAC of its plan's."""
        want, now = desired_orders(pl), time.time()
        for s in {s for s, _ in pl.legs}:
            mine = [(k, o) for k, o in self.state["orders"].items()
                    if unkey(k)[0] == t and unkey(k)[1] == s and not is_hedge(k) and o.get("oid")]
            ws = {k: w for k, w in want.items() if unkey(k)[1] == s}
            if not mine or now - self.repriced_at.get((t, s), 0.0) < REPRICE_MIN_S:
                continue
            if all(k in self.state["orders"] and abs(self.state["orders"][k]["price"] - w[0]) < 1e-9
                   and abs(self.state["orders"][k]["size"] - w[1]) < 1e-9 for k, w in ws.items()):
                continue
            cur = farm.market_rate(others, [(s, o["price"], o["remaining"]) for _, o in mine], P)
            new = farm.market_rate(others, [(s, px, q) for px, q, _ in ws.values()], P)
            if new <= 0 or cur >= REPRICE_FRAC * new:
                continue
            self.repriced_at[(t, s)] = now
            print(f"reprice: {t} {s} earns {cur * 86400:.2f}/day vs {new * 86400:.2f} as planned; re-placing")
            for k, _ in mine:
                self.cancel(k, reason="lipfarm:reprice")
            for k in [k for k in self.wanted if unkey(k)[0] == t and unkey(k)[1] == s]:
                del self.wanted[k]
            self.wanted.update(ws)
            self.spawn(self.place_side(t, s, ws, 0.0), t)

    def capital_check(self):
        exp = self.exposure()
        for k, o in list(self.state["orders"].items()):
            if not is_hedge(k) and exp + o["price"] * o["remaining"] > self.cfg.max_capital + 1e-9:
                print(f"capital: pull {k} (exposure ${exp:.0f})")
                self.cancel(k, reason="lipfarm:capital")

    def params(self, t: str) -> lip.LipParams:
        P = self.P.get(t)
        if P is None:
            m = self.meta.get(t, {})
            P = self.P[t] = dataclasses.replace(lip.params_from_program(self.programs[t]), price_ranges=tuple(
                (float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or []))
        return P

    def candidates(self, cfg: Cfg) -> list[str]:
        """farm.candidates on pool rates cached at refresh (it re-parsed every program: 1.5 s on the loop)."""
        everything = sorted(self.programs)
        est, pr = self.est, self.pool_rate
        ranked = sorted(everything, key=lambda t: (-est.get(t, -1.0), -pr.get(t, 0.0)))
        top = ranked[:cfg.scan_markets]
        topset = set(top)
        rest = [t for t in everything if t not in topset]
        rot = []
        if rest and cfg.scan_rotate:
            i = self.rot % len(rest)
            rot = (rest[i:] + rest[:i])[:cfg.scan_rotate]
            self.rot = i + len(rot)
        held = sorted(self.order_tickers() | set(self.signed_positions()))
        # Markets started this period but under $1, most accrued first, so a restart or a
        # cooldown doesn't leave them unplanned (and their accrual forfeited).
        started = sorted((t for t in everything if 0 < self.period_accrued(t) < farm.LIP_MIN_PAYOUT_USD),
                         key=lambda t: -self.period_accrued(t))[:cfg.num_markets]
        return list(dict.fromkeys(top + rot + [t for t in held if t in self.programs] + started))

    # ---- the cycle ----
    async def cycle(self, cfg: Cfg):
        await self.poll_fills()
        await self.sync_live_orders()
        self.accrue()
        tickers = self.candidates(cfg)
        obs = await asyncio.gather(*(self.run_io(farm.fetch_book, t) for t in tickers))
        self.held_events = await self.run_io(farm.arbs_held_events)
        pos = self.signed_positions()
        jobs, books = [], {}
        for t, ob in zip(tickers, obs):
            if ob is None or t not in self.programs:
                continue
            others = self.others_of(t, ob)
            books[t] = others
            self.est[t] = 0.0
            m = self.meta.get(t, {})
            if (m and m.get("status") != "active") or event_of(t) in self.held_events:
                continue
            v = pos.get(t, 0.0)
            current = {}
            for k in self.keys_of(t, hedges=False):
                sd = unkey(k)[1]
                current[sd] = "sentinel" if (is_sentinel(k) or current.get(sd) == "sentinel") else "touch"
            ranges = tuple((float(r["start"]), float(r["end"]), float(r["step"]))
                           for r in m.get("price_ranges") or [])
            jobs.append((t, others, self.programs[t], cfg, {"yes": max(v, 0.0), "no": max(-v, 0.0)}, ranges,
                         current))
        planned = await self.run_io(plan_batch, jobs)
        plans, rates = {}, {}
        real = self.realized_loss(cfg)
        for t, (pl, P) in planned.items():
            self.P[t] = P
            ok = self.flow_ok(t, cfg) if pl else False
            if pl and ok:
                # LIP must pay for the market's expected adverse-sweep loss, and at least what
                # fills in its series have actually cost us.
                pl.loss_rate = max(farm.expected_loss_day(self.trades_day(t), pl, cfg, self.days_to_close(t),
                                                          books.get(t)),
                                   real.get(series_of(t), 0.0) * pl.lots / cfg.max_position) / 86400
                ok = pl.rate >= cfg.min_lip_to_loss * pl.loss_rate
                # Taking most of the pool means nobody else wants to quote here.
                if ok and farm.pool_share(pl) > cfg.max_pool_share + 1e-9:
                    ok = False
            self.est[t] = pl.rate - pl.loss_rate if pl and ok is not False else 0.0
            if pl and ok:
                plans[t] = pl
            # What the flow filter costs: plans it holds back (False) or hasn't read yet (None).
            if pl and not ok:
                self.flow_held[t] = {"usd_day": pl.rate * 86400, "per_k": pl.rate * 86400 / max(pl.capital / 1000, 0.05),
                                     "exp_loss_day": pl.loss_rate * 86400, "trades_day": self.trades_day(t),
                                     "pool_share": round(farm.pool_share(pl), 3),
                                     "mode": pl.mode, "at": time.time()}
            else:
                self.flow_held.pop(t, None)
        for t in books:
            if t in self.order_tickers():
                rates[t] = self.our_rate(t, books[t], self.params(t))
        # Books from the socket are fresher than this cycle's REST reads.
        for t, others in books.items():
            if self.ws_others(t) is None:
                self.books[t] = others
        self.plans, self.rates = plans, {**self.rates, **rates}
        await self.reconcile(cfg)
        await self.tidy_groups()
        self.report(cfg)
        await self.save()

    async def reconcile(self, cfg: Cfg):
        want = self.choose(cfg)
        if self.live:
            self.write_claims(want)          # small file; on the loop, so state isn't shared with a thread
        now = time.time()

        def settle(t: str) -> float:
            if not self.live:
                return 0.0
            c = self.state["claims"].get(t)
            return CLAIM_SETTLE_S if c is None else max(0.0, CLAIM_SETTLE_S - (now - c))
        self.waiting = [t for t in want if settle(t) > 0]
        legs = {k: v for pl in want.values() for k, v in desired_orders(pl).items()}
        self.wanted = legs
        for k, o in list(self.state["orders"].items()):
            if is_hedge(k) or o.get("oid") is None or unkey(k)[0] in self.busy:
                continue
            w = legs.get(k)
            if (w is None or abs(w[0] - o["price"]) > 1e-9 or abs(o["size"] - w[1]) > 1e-9
                    or o["remaining"] < 0.5 * w[1] or settle(unkey(k)[0]) > 0):
                self.cancel(k)
        await self.prefetch_fees(want)
        held = set(self.signed_positions())
        self.ensure_feed(set(want) | self.order_tickers() | held | self.sibling_tickers(set(want) | held))
        by_side: dict[tuple[str, str], dict] = {}
        for k, w in legs.items():
            t, side = unkey(k)
            by_side.setdefault((t, side), {})[k] = w
        busy = set(self.busy)                     # before this pass: a market's two sides both go
        for (t, side), ws in by_side.items():
            if t in busy or all(k in self.state["orders"] for k in ws):
                continue
            wait = settle(t)
            self.spawn(self.place_side(t, side, ws, wait), None if wait > 0 else t)
        for t, pl in want.items():
            if t in self.order_tickers():
                self.rates.setdefault(t, pl.rate)

    async def place_side(self, t: str, side: str, ws: dict, wait: float):
        """One side's bids: a sentinel layout's main bid, then its sentinel right
        after (main pulled if the sentinel can't go); a touch bid alone."""
        if wait > 0:
            await asyncio.sleep(wait)
        ws = {k: w for k, w in ws.items() if self.wanted.get(k) == w}
        main, sk = key(t, side), f"{t}|{side}|s"
        if main in ws and main not in self.state["orders"]:
            px, q, behind = ws[main]
            if behind and sk in ws:
                c = self.gw.client(t)
                if self.live and (not self.bucket.has(2 * ORDER_TOKENS) or c is None):
                    return
                if not await self.place(t, side, px, q):
                    return
                sp, sq, _ = ws[sk]
                # Its bid already rests: the sentinel always goes. Refused for budget (the shard's,
                # then our own bucket's), it had the bid pulled every cycle (11-05 17:55, 21:25).
                if sk not in self.state["orders"] and not await self.place(t, side, sp, sq, sentinel=True,
                                                                            force=True):
                    print(f"sentinel {t} {side}: not placed; pulling its bid")
                    self.cancel(main, reason="lipfarm:sentinel")
                return
            await self.place(t, side, px, q)
        elif sk in ws and sk not in self.state["orders"] and main in self.state["orders"]:
            sp, sq, _ = ws[sk]
            await self.place(t, side, sp, sq, sentinel=True)

    async def poll_fills(self):
        """REST backstop for the socket's fills."""
        if not self.live or not self.gw.clients:
            return
        c = next(iter(self.gw.clients.values()))
        since = self.state.get("fills_since", self.state["t0"]) - 120
        self.state["fills_since"] = time.time()
        try:
            fills = await self.run_io(lambda: c.fills("", min_ts=int(since)))
        except Exception as e:
            print(f"fills poll: {e}")
            return
        self.take_fills(fills)

    async def sync_live_orders(self):
        """farm.sync_live_orders, its reads on the pool. Orders still being
        created, or younger than SYNC_GRACE_S, are left alone."""
        if not self.live or not self.gw.clients:
            return
        c = self.gw.clients.get(0) or next(iter(self.gw.clients.values()))
        try:
            listed = {oid: (t, cid) for oid, t, cid in await self.run_io(c._resting, "")}
        except Exception as e:
            print(f"sync: {e}")
            return
        now = time.time()
        check = [(k, o) for k, o in self.state["orders"].items()
                 if o.get("oid") and o["oid"] not in listed and now - o["placed"] > SYNC_GRACE_S]
        for (k, o), st in zip(check, await asyncio.gather(*(self.run_io(c.order, o["oid"]) for _, o in check),
                                                          return_exceptions=True)):
            if isinstance(st, Exception):
                print(f"sync: lookup {k}: {st}")
                continue
            if (st or {}).get("status") != "resting" and self.state["orders"].get(k) is o:
                del self.state["orders"][k]
        mine = {o.get("oid") for o in self.state["orders"].values()}
        # A create still unanswered has no oid yet but can already be listed: sync runs right after
        # reconcile sends its creates, and cancelled fresh bids 0.3 s old as orphans (11-07).
        creating = {o.get("cid") for o in self.state["orders"].values() if not o.get("oid")}
        for oid, (t, cid) in listed.items():
            if (cid or "").startswith(CID_PREFIX) and oid not in mine and cid not in creating:
                print(f"sync: cancel orphan {t} {oid}")
                self.spawn(self._cancel(t, oid, "lipfarm:orphan"))

    # ---- loops ----
    async def guard_loop(self):
        """Every GUARD_S (FILL_POLL_DOWN_S with the socket down): REST fills,
        every market we quote or hold re-checked, back-of-queue moves."""
        last = 0.0
        while True:
            down = self.feed is None or not self.feed.up
            await asyncio.sleep(FILL_POLL_DOWN_S if down else 0.5)
            if self.cfg is None or time.time() - last < (FILL_POLL_DOWN_S if down else GUARD_S):
                continue
            last = time.time()
            try:
                await self.poll_fills()
                mine = self.order_tickers() | set(self.signed_positions())
                if down and mine:
                    obs = await asyncio.gather(*(self.run_io(farm.fetch_book, t) for t in sorted(mine)))
                    for t, ob in zip(sorted(mine), obs):
                        if ob is not None:
                            self.books[t] = self.others_of(t, parse_book(ob))
                self.dirty |= mine
                if not down:
                    await self.move_back()
            except Exception:
                traceback.print_exc()

    async def move_back(self):
        """farm.move_back: a touch bid or sentinel with size joined behind it
        goes back to the end of its queue (cancel + create)."""
        if not self.live:
            return
        now = time.time()
        due = {}
        for t in self.order_tickers():
            pl, others = self.plans.get(t), self.books.get(t)
            if pl is None or others is None or t in self.busy:
                continue
            want = desired_orders(pl)
            for k in self.keys_of(t, hedges=False):
                o = self.state["orders"][k]
                if (o.get("oid") and k in want and abs(want[k][0] - o["price"]) < 1e-9
                        and (is_sentinel(k) or not want[k][2]) and now - o["placed"] >= MOVE_MIN_S):
                    due[k] = others
        if not due:
            return
        c = self.gw.clients.get(0) or next(iter(self.gw.clients.values()), None)
        if c is None:
            return
        try:
            ahead = await self.run_io(farm.queue_ahead, c, sorted({unkey(k)[0] for k in due}))
        except Exception as e:
            print(f"queue positions: {e}")
            return
        moves, ranked = 0, []
        for k, others in due.items():
            o = self.state["orders"].get(k)
            a = None if o is None else ahead.get(o["oid"])
            if a is None:
                continue
            t, side = unkey(k)
            behind = others[side].get(round(o["price"], 4), 0.0) - a
            if behind >= (1.0 if is_sentinel(k) else MOVE_BEHIND_FRAC * o["size"]):
                ranked.append((behind, k))
        for behind, k in sorted(ranked, reverse=True):
            o = self.state["orders"].get(k)
            if o is None:
                continue
            t, side = unkey(k)
            cl = self.gw.client(t)
            if (moves >= MAX_MOVES or cl is None or not self.bucket.has(ORDER_TOKENS + CANCEL_TOKENS)
                    or not cl.can_afford("order", reserve=MOVE_RESERVE * cl.tokens_per_s + cl.COST["cancel"])):
                return
            moves += 1
            self.cancel(k, reason="lipfarm:move")
            self.spawn(self.place(t, side, o["price"], o["size"], reserve=0.0, reason="lipfarm:move",
                                  sentinel=is_sentinel(k)), t)

    async def cycle_loop(self):
        last = 0.0
        while True:
            await asyncio.sleep(0.5)
            if self.cfg is None or not self.programs or time.time() - last < self.cfg.refresh_s:
                continue
            last = time.time()
            try:
                await self.cycle(self.cfg)
            except Exception:
                traceback.print_exc()

    async def refresh_loop(self):
        while True:
            cfg = self.cfg
            if cfg is not None and time.time() - self.last_programs > cfg.programs_refresh_s:
                try:
                    self.apply_refresh(await self.run_io(farm.refresh_job, set(self.signed_positions())))
                    self.pool_rate = await self.run_io(pool_rates, self.programs)
                    self.P = {}
                    freeze_heap()
                    await self.setup_clients()
                except Exception:
                    traceback.print_exc()
                    self.last_programs = time.time() - cfg.programs_refresh_s + 60.0
            await asyncio.sleep(1.0)

    async def status_loop(self):
        while True:
            await asyncio.sleep(STATUS_S)
            if self.cfg is None or not self.programs:
                continue
            try:
                held = sorted({f["ticker"] for f in self.state["fills"]})
                if held and time.time() - self.marks_at > MARKS_S:
                    self.marks_at = time.time()
                    for t, m in (await self.run_io(farm.fetch_meta, held)).items():
                        self.meta[t] = m
                self.accrue()
                self.report(self.cfg, log=False)
            except Exception:
                traceback.print_exc()

    async def lag_loop(self):
        """How late the loop wakes: anything blocking it (a slow save, planning
        on the loop) delays every fill behind it."""
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            self.lag_max = max(self.lag_max, time.perf_counter() - t0 - 0.05)

    async def recovery_loop(self):
        """A LIP fill whose market's bid on our side is still under our fill price
        recover_s later is adverse: its event is pulled (pull_event)."""
        while True:
            await asyncio.sleep(1.0)
            cfg = self.cfg
            if cfg is None or cfg.recover_s <= 0 or not self.recovery:
                continue
            now, keep = time.time(), []
            for item in self.recovery:
                seen, t, side, price = item
                if now - seen < cfg.recover_s:
                    keep.append(item)
                    continue
                others = self.ws_others(t) or self.books.get(t)
                if not others:
                    continue
                best = max(others[side]) if others[side] else 0.0
                if best < price - 1e-9:
                    self.journal({"kind": "unrecovered", "ticker": t, "side": side, "price": price, "best": best,
                                  "after_s": round(now - seen)})
                    print(f"unrecovered: {t} {side} filled at {price}, best bid {best} after {now - seen:.0f} s")
                    self.pull_event(t)
            self.recovery = keep

    async def levels_loop(self):
        """Classify level decreases on time; every QUEUE_CHECK_S, test the guessed
        orders against Kalshi's queue positions for our LIP bids (live)."""
        last = 0.0
        while True:
            await asyncio.sleep(0.25)
            self.levels.flush(time.time())
            if not self.live or time.time() - last < QUEUE_CHECK_S or not self.gw.clients:
                continue
            last = time.time()
            mine = [(k, o) for k, o in self.state["orders"].items() if not is_hedge(k) and o.get("oid")]
            if not mine:
                continue
            c = self.gw.clients.get(0) or next(iter(self.gw.clients.values()))
            try:
                actual = await self.run_io(farm.queue_ahead, c, sorted({unkey(k)[0] for k, _ in mine}))
            except Exception as e:
                print(f"queue check: {e}")
                continue
            for k, o in mine:
                t, side = unkey(k)
                a = actual.get(o["oid"])
                g = self.levels.ahead_of(t, side, o["price"], o["oid"])
                v = self.levels.view(t, side, o["price"])
                if a is not None and g is not None:
                    self.queue_err.append(abs(g - a))
                self.journal({"kind": "queue_check", "ticker": t, "side": side, "price": o["price"], "actual": a,
                              "guessed": g, "chunks": v["sizes"], "snap": v["snap"]})

    def crowd(self, others: dict, side: str, price: float) -> float:
        return sum(q for p, q in others.get(side, {}).items() if p >= price - 1e-9)

    async def holds_loop(self):
        """Apply holds.json: pull our LIP bids on each held market and keep it on
        cooldown until its hold ends (the hold-and-watch experiment, 11-05)."""
        path = os.path.join(self.root, "holds.json")
        while True:
            await asyncio.sleep(HOLDS_S)
            if not self.live:
                continue
            try:
                holds = json.loads(await self.run_io(_read, path)) if os.path.exists(path) else {}
                now = time.time()
                for t, h in holds.items():
                    until = float(h.get("until", 0))
                    if until <= now:
                        continue
                    cool = self.state.setdefault("cooldown", {})
                    if cool.get(t, 0.0) < until:
                        cool[t] = until
                    keys = self.keys_of(t, hedges=False)
                    if keys:
                        for k in keys:
                            self.cancel(k, reason="lipfarm:hold")
                        self.journal({"kind": "hold", "ticker": t, "until": until, "why": h.get("why")})
                        print(f"hold: pulled {t} until {pd.Timestamp(until, unit='s', tz='UTC'):%H:%M:%S}Z ({h.get('why')})")
            except Exception as e:
                print(f"holds: {e!r}")

    async def keepalive_loop(self):
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            for c in list(self.gw.clients.values()):
                try:
                    await self.run_io(c.keepalive)
                except Exception as e:
                    print(f"keepalive: {e}")

    async def control_loop(self, arm: str | None, stop_ev: asyncio.Event):
        modes, stop = TradingModeWatcher(NAME), StopWatcher()
        if arm and not self.shadow_only:
            await self.set_live(True, arm)
        while True:
            try:
                self.cfg = Cfg.load()
                self.bucket.rate = self.cfg.write_tokens_per_s
                self.cfg_adverse = self.cfg.adverse_lots
                self.cfg_adverse_frac = self.cfg.adverse_frac
                reason = stop.poll()
                if reason:
                    print(f"STOP: {reason}")
                    stop_ev.set()
                    return
                want = modes.poll()
                if want is not None and not self.shadow_only:
                    await self.set_live(want, f"dashboard {modes.origin()}")
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(1.0)

    async def set_live(self, want: bool, why: str):
        self.arm_requested = want
        if want == self.live:
            return
        print(f"{'ARM' if want else 'DISARM'}: {why}")
        self.accrue()
        await self.cancel_all()
        if self.live:
            self.write_claims({})
        await self.save()
        self._open_mode(want)
        self.positions_now = set(self.signed_positions())
        if want:
            await self.setup_clients()
            await self.cancel_strays()

    async def cancel_all(self):
        """Pull every LIP bid. Exit bids stay on Kalshi (and in the journal): a restart or
        a pause shouldn't cost them their queue position; the next run adopts them."""
        orders = [(k, o) for k, o in self.state["orders"].items() if not is_hedge(k)]
        for k, _ in orders:
            del self.state["orders"][k]
        if self.live:
            await asyncio.gather(*(self._cancel(unkey(k)[0], o["oid"], "lipfarm:stop")
                                   for k, o in orders if o.get("oid")))

    async def cancel_strays(self):
        c = self.gw.clients.get(0) or next(iter(self.gw.clients.values()), None)
        if c is None:
            return
        exits = {o["oid"]: k for k, o in self.state["orders"].items() if is_hedge(k) and o.get("oid")}
        listed = set()
        for oid, t, cid in await self.run_io(c._resting, ""):
            listed.add(oid)
            if cid and cid.startswith(CID_PREFIX) and oid not in exits:
                await self._cancel(t, oid, "lipfarm:stray")
        # Exits from the last run that are still resting are adopted as they are.
        self.state["orders"] = {k: o for k, o in self.state["orders"].items()
                                if is_hedge(k) and o.get("oid") in listed}

    # ---- report (file writes off the loop) ----
    def positions(self) -> list[dict]:
        pos: dict[tuple, dict] = {}
        for f in self.state["fills"]:
            p = pos.setdefault((f["ticker"], f["side"]), {"ticker": f["ticker"], "side": f["side"],
                                                          "lots": 0.0, "cost": 0.0, "fees": 0.0})
            p["lots"] += f["size"]
            p["cost"] += f["size"] * f["price"]
            p["fees"] += f["fee"] or 0.0
            at = _utc_iso(f.get("time"))
            if at:
                p["first"] = min(p.get("first") or at, at)
                p["last"] = max(p.get("last") or at, at)
        for (t, side), p in pos.items():
            m = self.meta.get(t, {})
            p["url"] = self.url(t)
            res = m.get("result")
            pair = min(p["lots"], pos.get((t, "no" if side == "yes" else "yes"), {}).get("lots", 0.0))
            p["mark"] = (1.0 if res == side else 0.0) if res in ("yes", "no") else float(
                m.get(f"{side}_bid_dollars") or 0.0)
            p["paired"] = pair
            p["value"] = pair * 0.5 + (p["lots"] - pair) * p["mark"]
            p["pnl"] = p["value"] - p["cost"] - p["fees"]
        return list(pos.values())

    def report(self, cfg: Cfg, log: bool = True):
        rows = []
        for t in sorted(self.order_tickers(), key=lambda t: -self.rates.get(t, 0.0)):
            legs = sorted(self.legs_of(t), key=lambda so: so[0])
            hedged = {unkey(k)[1] for k in self.state["orders"] if is_hedge(k) and unkey(k)[0] == t}
            pl = self.plans.get(t)
            rows.append({"ticker": t, "url": self.url(t), "mode": pl.mode if pl else ("hedge" if hedged else ""),
                         "bids": " + ".join(f"{s}@{o['price']:.3f}x{o['remaining']:g}" for s, o in legs),
                         "layout": ",".join(f"{s}:{l}" for s, l in sorted((pl.layout if pl else {}).items())),
                         "resting": sum(o["remaining"] for _, o in legs),
                         "capital_usd": sum(o["price"] * o["remaining"] for _, o in legs),
                         "our_usd_day": self.rates.get(t, 0.0) * 86400,
                         "pool_usd_day": (pl.pool_rate if pl else 0.0) * 86400,
                         "exp_lip_usd": self.state["accrued"].get(t, 0.0),
                         "period_lip_usd": self.period_accrued(t) if t in self.programs else 0.0,
                         "crowd": {f"{o['side']}@{o['price']:g}": [round(self.crowd(self.books.get(t, {}), o["side"],
                                                                                    o["price"]), 1), o.get("crowd0")]
                                   for k, o in self.state["orders"].items()
                                   if unkey(k)[0] == t and not is_hedge(k) and not is_sentinel(k)}})
        pos = self.positions()
        tot_lip = sum(self.state["accrued"].values())
        locked = sum(v for v in self.state.get("period_accrued", {}).values() if v >= farm.LIP_MIN_PAYOUT_USD)
        fill_pnl = sum(p["pnl"] for p in pos)
        at_risk = sum(r["capital_usd"] for r in rows)
        hrs = (time.time() - self.state["t0"]) / 3600
        summary = {"mode": "LIVE" if self.live else "shadow", "live_trading": self.live,
                   "arm_requested": self.arm_requested, "hours": hrs, "quoting": len(rows),
                   "our_usd_day_now": sum(r["our_usd_day"] for r in rows), "exp_lip_usd": tot_lip,
                   "lip_locked_usd": locked, "lip_pending_usd": tot_lip - locked,
                   "fills": len(self.state["fills"]), "fill_lots": sum(p["lots"] for p in pos),
                   "fill_pnl_usd": fill_pnl, "net_usd": tot_lip + fill_pnl, "resting_cost_usd": at_risk,
                   "max_capital_usd": cfg.max_capital, "exposure_usd": self.exposure(),
                   "locked_bundles": len(self.state.get("locked", {})),
                   "write_tps_10m": self.bucket.rate_10m(), "write_tps_cap": cfg.write_tokens_per_s,
                   "ws_up": bool(self.feed and self.feed.up), "ws_updates": self.n_evals,
                   "waiting_on_arbs": len(self.waiting), "arbs_events_skipped": len(self.held_events),
                   "runner": "async", "loop_lag_ms_max": 1000 * self.lag_max, "updated_at": time.time()}
        held = {t: h for t, h in self.flow_held.items() if time.time() - h["at"] < 3600}
        summary["flow_held"] = len(held)
        errs = list(self.queue_err)
        summary["queue_checks"] = len(errs)
        summary["queue_exact_share"] = sum(e < 1.0 for e in errs) / len(errs) if errs else None
        summary["levels_counts"] = dict(self.levels.n)
        summary["flow_held_usd_day"] = sum(h["usd_day"] for h in held.values())
        top = sorted(held.items(), key=lambda kv: -kv[1]["per_k"])[:40]
        doc = json.dumps({"summary": summary, "quotes": rows, "positions": pos, "waiting": self.waiting,
                          "flow_held": [{"ticker": t, **h} for t, h in top]}, indent=1, default=str)
        asyncio.get_running_loop().run_in_executor(self.io_pool, _atomic_write,
                                                   os.path.join(self.root, "status.json"), doc)
        if not log:
            return
        point = {k: round(summary[k], 4) for k in ("exp_lip_usd", "lip_locked_usd", "fill_pnl_usd", "net_usd",
                                                    "fill_lots", "exposure_usd", "our_usd_day_now")}
        point.update(t=round(summary["updated_at"], 1), fills=summary["fills"], quoting=summary["quoting"])
        asyncio.get_running_loop().run_in_executor(self.io_pool, _append_line,
                                                   os.path.join(self.root, farm.HISTORY_FILE), json.dumps(point))
        print(f"\n=== LIPFARM {summary['mode']} (async) {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S}Z "
              f"| {hrs:.2f}h | quoting {len(rows)} | now ${summary['our_usd_day_now']:.0f}/day "
              f"| exp LIP ${tot_lip:.2f} | fills {summary['fills']} pnl ${fill_pnl:+.2f} "
              f"| net ${summary['net_usd']:+.2f} | resting ${at_risk:.0f} | exposure "
              f"${summary['exposure_usd']:.0f} of ${cfg.max_capital:.0f} | loop lag max "
              f"{1000 * self.lag_max:.0f} ms | evals {self.n_evals}", flush=True)
        self.lag_max = 0.0
        for r in rows:
            print(f"{r['ticker']:40} {r['mode']:7} {r['bids']:26} {r['capital_usd']:6.0f} "
                  f"{r['our_usd_day']:7.1f} {r['pool_usd_day']:7.1f} {r['exp_lip_usd']:7.2f}")

    # ---- main ----
    async def main(self, arm: str | None):
        loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.urgent_set, self.dirty = _Dirty(self.wake), _Dirty(self.wake)
        self.positions_now = set(self.signed_positions())
        stop_ev = asyncio.Event()
        # systemd stops us with SIGINT (KillSignal): end through the cleanup below, which pulls every bid.
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_ev.set)
            except (NotImplementedError, RuntimeError):
                pass                                    # Windows: KeyboardInterrupt still ends asyncio.run
        self.cfg = Cfg.load()
        self.apply_refresh(await self.run_io(farm.refresh_job, set(self.signed_positions())))
        self.pool_rate = await self.run_io(pool_rates, self.programs)
        freeze_heap()
        await self.setup_clients()
        held = self.order_tickers() | set(self.signed_positions())
        self.ensure_feed(held or set(list(self.programs)[:1]))
        coros = [self.control_loop(arm, stop_ev), self.eval_loop(), self.cycle_loop(), self.guard_loop(),
                 self.refresh_loop(), self.status_loop(), self.keepalive_loop(), self.flush_loop(),
                 self.lag_loop(), self.ghost_loop(), self.levels_loop(), self.holds_loop(), self.recovery_loop()]
        tasks = [loop.create_task(c) for c in coros]
        try:
            await stop_ev.wait()
        finally:
            for t in tasks:
                t.cancel()
            print("stopping: cancelling quotes")
            try:
                self.accrue()
                await self.cancel_all()
                if self.live:
                    self.write_claims({})
                await self.save()
            except Exception:
                print("WARN: shutdown cleanup failed -- CHECK KALSHI BY HAND for resting orders:")
                traceback.print_exc()
            await asyncio.gather(*tasks, return_exceptions=True)


def freeze_heap():
    """One full collection, then move what survived (programs, market records:
    ~10k dicts) out of the collector's reach: a full pass over them stalled the
    loop 0.2-0.3 s at random (local profile 11-05). Once per program refresh."""
    gc.unfreeze()
    gc.collect()
    gc.freeze()


def _append_line(path: str, line: str):
    with open(path, "a") as f:
        f.write(line + "\n")


def _atomic_write(path: str, data: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"           # status writes may overlap on the pool
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def _utc_iso(x) -> str | None:
    """A fill's time (REST, socket or our IOC record: mixed formats) as a UTC ISO string."""
    if x is None:
        return None
    try:
        ts = pd.Timestamp(str(x).replace(" ", "T", 1))
    except ValueError:
        return None
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).isoformat()


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _append_rows(path: str, rows: list[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="arm at start: real orders")
    ap.add_argument("--resume-arm", action="store_true",
                    help="re-arm if the dashboard's last request was an arm and no STOP since")
    ap.add_argument("--shadow-only", action="store_true",
                    help="never arm (ignores the dashboard): a side-by-side test beside the live farm")
    ap.add_argument("--root", default=ROOT, help="state, status and journal directory")
    ap.add_argument("--beside", default=None,
                    help="side-by-side test: the live farm's state.json; its resting bids are removed from books")
    ap.add_argument("--reads-per-s", type=float, default=None,
                    help="REST read pace (default farm.READS_PER_S; the account's read limit is shared with ARBS)")
    a = ap.parse_args()
    if a.reads_per_s:
        farm.PACER = farm.ReadPacer(a.reads_per_s)
    arm = None
    if not a.shadow_only:
        arm = "--live" if a.live else (resumable_arm(NAME) if a.resume_arm else None)
    r = Runner(root=a.root, shadow_only=a.shadow_only, beside=a.beside if a.shadow_only else None)
    try:
        asyncio.run(r.main(arm))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
