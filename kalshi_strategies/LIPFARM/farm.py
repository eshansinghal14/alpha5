"""
LIP farmer. Quotes LIP markets in one of two shapes, chosen per market each
cycle from today's book:

  wide     the yes/no spread is at least `wide_spread`: one bid of `lots` at
           each side's touch, owning both halves of a thin book.
  decided  the expensive side's best bid is at least decided_min: one bid of
           `lots` there only (fills gain the last cents; the risk is a
           reversal). A bid at the market's top price (0.99, or 0.999 on 0.1c
           ticks) earns no LIP; the scorer rules those out.

Every side must reach Kalshi's Target size, ours included where we quote and
from others alone where we don't, or the snapshot pays nobody. Markets are
ranked by LIP per dollar of resting bids. Kalshi holds no cash for resting
orders (11-04: $9.6k resting on a $16.3k balance, balance unchanged), so
`max_capital` caps what fills have left us holding: a bid that could fill us
past it isn't placed, and one is pulled as soon as a fill makes that true.
Each bid is at most `exit_depth_frac` of the others' bids resting on its own
side within `exit_depth_slip` of the touch: what an informed fill could be
sold back into.

On a fill we first look for a way to lock the position by taking: the same
market's other side, or the other legs of any ARBS basket holding our leg
(ARBS's scanned and hand specs), whenever the bundle costs at most its payout
after taker fees. Whatever is left gets an exit bid one tick above the other
side's touch (Kalshi nets a yes+no pair to $1), at any price -- getting out
comes before the pair's profit. A fill taking `adverse_fill_frac` or more of a
bid is taken as informed: we sell into the book at once (sweeping others'
bids on the side we hold), pull that market's bids and cool it down. Exits
sit ahead of our own LIP bids, follow
the touch, are never pulled or moved to the back, and the market's LIP bids
keep running beside them up to max_position.

    python -m kalshi_strategies.LIPFARM.farm                # shadow until armed from the dashboard
    python -m kalshi_strategies.LIPFARM.farm --live         # armed from the start: REAL MONEY
    python -m kalshi_strategies.LIPFARM.farm --resume-arm   # re-arm if the last request was an arm (EC2 unit)

Arm/disarm is the dashboard's button (trading_mode.json, as for every
series); STOP.json disarms and exits. Params in config.json (dashboard
sidebar, re-read every cycle). Journal in data/live/LIPFARM/<mode>/state.json
survives restarts; data/live/LIPFARM/status.json holds the latest table.
"Expected LIP" is our modelled share (lip.py) integrated over time, not what
Kalshi pays: payouts under $1 per market-period are dropped, and others' size
between our reads is unseen.

Live, the books of every market we quote or hold and our fills come over
the websocket (ARBS's MultiFeed): a book update re-checks that market's plan
and a fill is exited as it arrives. REST reads (30 s scan, 5 s guard) remain
the fallback while the socket is down.

Writes: our own bucket of write_tokens_per_s (burst WRITE_BURST) on top of
the shard bucket ARBS shares. Exits and cancels always go, charged to our
bucket, so they only ever delay our own LIP writes.

ARBS: live, every ticker we want, rest on or hold goes in claimed.json (with
our positions). ARBS reads it every second: it neither takes nor quotes a
claimed ticker and pulls its quotes there, its sweep skips our orders, and its
position audit takes our positions off Kalshi's. We quote CLAIM_SETTLE_S after
claiming. Events where ARBS holds a basket are never claimed: it may have to
take there to complete one.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures as cf
import dataclasses
import json
import math
import os
import threading
import time
import traceback
import uuid

import pandas as pd
import urllib3

from kalshi_backtest.core import Order
from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import load, param, section
from kalshi_infra.runtime.control import StopWatcher, TradingModeWatcher, resumable_arm
from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_infra.kalshi.fees import fee as kfee
from kalshi_infra.trading import lip

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CID_PREFIX = "lipf-"
# A cycle reads ~900 books (scan_markets + scan_rotate) off the main thread,
# so exits never wait on it; READS_PER_S paces them.
BOOK_WORKERS = 8
# All our REST reads share the account's read limit with ARBS (300/s). 24
# unpaced book threads plus a parallel records refresh drew 429s on ARBS's
# balance reads and position audits (11-04 ~21:30); 20/s takes ~45 s a cycle.
READS_PER_S = 20.0
NAME = "LIPFARM"
ROOT = os.path.join(LIVE_ROOT, NAME)
ARBS_ROOT = os.path.join(LIVE_ROOT, "ARBS")
# Read by ARBS's scanner (ARBS.scanner.lipfarm_claimed): it selects no event here.
CLAIMS_PATH = os.path.join(ROOT, "live", "claimed.json")
# ARBS polls claims every runtime.control_poll_s (1 s) and then cancels its
# quotes on the claimed tickers; this covers both with room.
CLAIM_SETTLE_S = 3.0
# Kalshi pays nothing for a market-period whose LIP is under $1 (help article).
LIP_MIN_PAYOUT_USD = 1.0
# Our writes share ARBS's shard bucket (300 tokens/s on shard 0, 2026-11-04;
# ARBS drained it to refusals that day). A new quote goes out only with this
# fraction of the bucket left after it, a back-of-queue move only with
# MOVE_RESERVE: we spend what ARBS isn't using. Cancels always go.
PLACE_RESERVE = 0.5
MOVE_RESERVE = 0.8
# Between cycles, every GUARD_S each quoted market's book is re-read: a quote
# its market's plan no longer has is pulled, and one with MOVE_BEHIND_FRAC *
# lots joined behind it is re-placed at the back (cancel + create, 12 tokens;
# LIP has no time priority, so the back only sheds fills). Per quote at most
# once per MOVE_MIN_S, at most MAX_MOVES per pass, within our own bucket and
# only while MOVE_RESERVE of the shard's is spare.
GUARD_S = 5.0
STATUS_S = 5.0
# A market the guard pulled isn't re-quoted for this long: books flickering
# across a threshold had the 30 s cycle re-place into the next flicker
# (STL, JOHN 11-04 19:19).
GUARD_COOLDOWN_S = 300.0
MOVE_MIN_S = 5.0
MOVE_BEHIND_FRAC = 0.2
MAX_MOVES = 10
# Fills are polled this often between guard passes; a new one is exited at once.
# With the websocket up, fills and book updates wake the loop as they arrive
# and this is only the idle tick.
FILL_POLL_S = 1.0
# Our bucket's burst: 12 orders at once.
WRITE_BURST = 120.0
ORDER_TOKENS, CANCEL_TOKENS = 10.0, 2.0
# An exit is re-priced only when outbid, at most this often: re-pricing on
# every socket update into a penny-jumping bot spent ~250 orders exiting 13
# contracts and starved every LIP bid of budget (AL14/PR05, 11-04 20:50-21:06).
EXIT_REPRICE_S = 5.0
# A position this small is sold into the book at once instead of resting an exit.
DUST_LOTS = 25.0
# Taker fee rate when a market's schedule can't be read (Kalshi's standard 0.07).
DEFAULT_TAKER_RATE = 0.07


@section
class Cfg:
    max_capital: float = param("Most dollars our fills may leave us holding (unexited positions at cost). A bid "
                               "that could take us past it isn't placed; after a fill, bids that could are "
                               "pulled. Resting bids hold no cash at Kalshi.", min=0.0, step=500.0)
    num_markets: int = param("Most markets quoted.", min=0, step=1)
    lots: float = param("Contracts per bid (one bid per quoted side), before exit_vol_frac.", min=1.0,
                        step=100.0)
    adverse_fill_frac: float = param("A fill of at least this fraction of one of our bids is informed: sell "
                                     "into the book at once, at any price, instead of waiting on an exit bid "
                                     "(ORAN 11-04: 1000 of 1000 at 6c, then 1c within 10 min).", min=0.05,
                                     max=1.0, step=0.05)
    exit_depth_frac: float = param("A bid is at most this fraction of others' bids on its side within "
                                   "exit_depth_slip of the touch: what a fill could be sold back into.",
                                   min=0.05, max=10.0, step=0.1)
    exit_depth_slip: float = param("How far below the touch counts as exit depth (dollars).", min=0.0, max=0.5,
                                   step=0.01)
    wide_spread: float = param("Wide: quote both sides at the touch when ask - bid is at least this.",
                               min=0.01, max=0.98, step=0.01)
    min_leg_price: float = param("Wide: don't quote a side bid under this. A full fill on a 5-6c leg was an "
                                 "informed dump every time, sold back at 1c (ORAN, NMAN, IDS 11-04).",
                                 min=0.0, max=0.5, step=0.01)
    decided_min: float = param("Decided: quote the expensive side when its best bid is at least this.",
                               min=0.5, max=0.999, step=0.01)
    min_ahead: float = param("Quote a side only where others already rest at least this many contracts at our "
                             "price (they are filled before us). 0 = may be alone.", min=0.0, step=100.0)
    max_position: float = param("Stop quoting a side once we hold this many contracts of it.", min=0.0,
                                step=100.0)
    write_tokens_per_s: float = param("Our write budget (tokens/s; an order 10, a cancel 2) out of the 300 the "
                                      "shard shares with ARBS, which caps itself at 150-250. 11-04: our "
                                      "requotes need ~1/s; ARBS's last tokens are worth <$0.30/day each "
                                      "(its ranking at 125+ tokens/s covers every basket worth >$8/h, vs "
                                      "~$150/day realized). Spare goes to back-of-queue moves; exits and "
                                      "cancels always go.", min=1.0, max=50.0, step=1.0)
    scan_markets: int = param("Books read each cycle: the markets our last read of them says pay us most per "
                              "dollar.", min=1, step=50)
    scan_rotate: int = param("Plus this many more each cycle, rotating through every other LIP market (any "
                             "category), so each is re-read every programs/scan_rotate cycles.", min=0, step=100)
    refresh_s: float = param("Seconds between cycles.", min=5.0, editable=False)
    programs_refresh_s: float = param("Seconds between re-reads of the LIP program list.", min=60.0,
                                      editable=False)

    @classmethod
    def load(cls) -> "Cfg":
        return load(LipFarmConfig, CONFIG_PATH).farm


@section
class LipFarmConfig:
    farm: Cfg


@dataclasses.dataclass
class Plan:
    ticker: str
    mode: str                          # "wide" | "decided"
    legs: tuple                        # ((side, price), ...)
    rate: float                        # our $/s with `lots` on every leg
    capital: float                     # dollars those bids cost if all fill
    pool_rate: float                   # whole program's $/s
    lots: float = 0.0                  # contracts per bid


def key(ticker: str, side: str, hedge: bool = False) -> str:
    return f"{ticker}|{side}|h" if hedge else f"{ticker}|{side}"


def unkey(k: str) -> tuple[str, str]:
    parts = k.split("|")
    return parts[0], parts[1]


def is_hedge(k: str) -> bool:
    return k.endswith("|h")


def parse_book(ob: dict) -> tuple[dict[float, float], dict[float, float]]:
    """(yes bids, no bids) as price -> size. 4 dp: some markets quote 0.1c ticks."""
    def side(rows):
        return {round(float(p), 4): float(s) for p, s in rows or [] if float(s) > 0}
    return side(ob.get("yes_dollars")), side(ob.get("no_dollars"))


def levels(book: dict[float, float], price: float | None = None, size: float = 0.0) -> list[lip.LipLevel]:
    acc = {p: [s, 0.0] for p, s in book.items()}
    if price is not None and size > 0:
        lv = acc.setdefault(round(price, 4), [0.0, 0.0])
        lv[0] += size
        lv[1] += size
    return sorted((lip.LipLevel(price=p, size=v[0], mine=v[1]) for p, v in acc.items()),
                  key=lambda lv: -lv.price)


def book_score(others: dict[str, dict[float, float]], legs, params: lip.LipParams) -> lip.LipBookScore:
    """Score with our `legs` ((side, price, size), ...) added to others' books."""
    mine = {s: (p, q) for s, p, q in legs}
    y = levels(others["yes"], *mine["yes"]) if "yes" in mine else levels(others["yes"])
    n = levels(others["no"], *mine["no"]) if "no" in mine else levels(others["no"])
    return lip.score_book(y, n, params)


def market_rate(others, legs, params) -> float:
    """Our $/s with `legs` ((side, price, size), ...) resting."""
    return book_score(others, legs, params).expected_reward_this_snapshot_usd


def plan_market(ticker: str, others: dict[str, dict[float, float]], params: lip.LipParams, cfg: Cfg,
                held: dict[str, float] | None = None, lots=None) -> Plan | None:
    """How to quote this market now, or None. Joins others' best bids (never
    improves them). `held`: contracts we already hold per side; `lots`: bid
    size, one number or per side (exit_lots); a side sized under 1 isn't
    quoted. Every quoted side gets the smallest of the quoted sides' sizes."""
    held = held or {}
    if lots is None:
        lots = cfg.lots
    side_lots = lots if isinstance(lots, dict) else {"yes": lots, "no": lots}
    if not others["yes"] or not others["no"]:
        return None
    by, bn = max(others["yes"]), max(others["no"])
    hi_side, hi = ("yes", by) if by >= bn else ("no", bn)
    if hi >= cfg.decided_min - 1e-9:
        mode, legs = "decided", [(hi_side, hi)]
    elif 1.0 - by - bn >= cfg.wide_spread - 1e-9:
        mode, legs = "wide", [(s, p) for s, p in (("yes", by), ("no", bn)) if p >= cfg.min_leg_price - 1e-9]
    else:
        return None
    legs = [(s, p) for s, p in legs
            if others[s].get(p, 0.0) >= cfg.min_ahead - 1e-9 and held.get(s, 0.0) < cfg.max_position - 1e-9
            and side_lots.get(s, 0.0) >= 1.0]
    if not legs:
        return None
    lots = min(side_lots[s] for s, _ in legs)
    sized = [(s, p, lots) for s, p in legs]
    # A side we quote must reach Target with our lots, one we don't from others
    # alone: otherwise the snapshot is excluded and pays nobody.
    sc = book_score(others, sized, params)
    if not sc.qualifies or sc.expected_reward_this_snapshot_usd <= 0:
        return None
    return Plan(ticker, mode, tuple(legs), sc.expected_reward_this_snapshot_usd,
                lots * sum(p for _, p in legs), params.period_reward_usd / params.snapshots_per_period, lots)


def exit_lots(others: dict[str, dict[float, float]], cfg: Cfg) -> dict[str, float]:
    """Bid size per side: cfg.lots, capped by that side's exit depth, others'
    bids strictly BELOW the touch we'd join, within exit_depth_slip. Bids at
    our own price don't count: a seller who fills us full size has cleared
    that level too (NMAN 11-04: 1000 of 1000 at 5c, the next bid 1c)."""
    out = {}
    for s, book in others.items():
        if not book:
            out[s] = 0.0
            continue
        top = max(book)
        d = sum(q for p, q in book.items() if top - cfg.exit_depth_slip - 1e-9 <= p < top - 1e-9)
        out[s] = min(cfg.lots, math.floor(cfg.exit_depth_frac * d))
    return out


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def arbs_held_events() -> set[str]:
    """Events where ARBS holds a real basket: it may take there to complete or
    exit one, so we never claim them."""
    tickers: set[str] = set()
    for b in ((_json(os.path.join(ARBS_ROOT, "ledger.json")) or {}).get("baskets") or {}).values():
        tickers |= {t for t, leg in (b.get("legs") or {}).items() if float(leg.get("qty") or 0) > 0}
    return {event_of(t) for t in tickers}


def basket_index(tickers: set[str], skip_events: set[str]) -> dict[tuple[str, str], list]:
    """(ticker, side) -> [(payout, legs), ...] for ARBS baskets (scanned and hand
    specs) with a leg in `tickers` and none in `skip_events` (events ARBS holds:
    taking there would trade against its own completions)."""
    from kalshi_strategies.ARBS.config import SPEC_PATH
    from kalshi_strategies.ARBS.spec import parse_spec
    out: dict[tuple[str, str], list] = {}
    for path in (os.path.join(ARBS_ROOT, "scanned_spec.json"), SPEC_PATH):
        doc = _json(path)
        if not doc:
            continue
        rels = [r for r in doc.get("relationships", [])
                if any(l["ticker"] in tickers for b in r["baskets"] for l in b["legs"])]
        try:
            baskets = parse_spec({**doc, "relationships": rels})
        except Exception as e:
            print(f"basket index {path}: {e}")
            continue
        for b in baskets:
            legs = tuple((l.ticker, l.side) for l in b.legs)
            if any(event_of(t) in skip_events for t, _ in legs):
                continue
            for leg in legs:
                if leg[0] in tickers:
                    out.setdefault(leg, []).append((float(b.payout), legs))
    return out


class ReadPacer:
    """Thread-safe pacing of our REST reads to READS_PER_S."""

    def __init__(self, rate: float):
        self.gap, self.lock, self.next = 1.0 / rate, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(self.next, now)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


PACER = ReadPacer(READS_PER_S)


def read(path: str, params: dict | None = None, auth: bool = False) -> dict:
    PACER.wait()
    return kalshi_get(path, params=params, auth=auth)


def load_programs() -> dict[str, dict]:
    """Active liquidity programs not yet ended, by market ticker."""
    out, cursor = {}, None
    now = pd.Timestamp.now(tz="UTC")
    while True:
        params = {"status": "active", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = read("/trade-api/v2/incentive_programs", params=params, auth=True)
        batch = data.get("incentive_programs", [])
        for p in batch:
            if p.get("incentive_type") == "liquidity" and pd.Timestamp(p["end_date"]) > now:
                out[p["market_ticker"]] = p
        cursor = data.get("next_cursor") or data.get("cursor")
        if not cursor or not batch:
            return out


def fetch_meta(tickers) -> dict[str, dict]:
    """Market records for `tickers`, 100 per read, in parallel."""
    tickers = sorted(tickers)
    chunks = [tickers[i:i + 100] for i in range(0, len(tickers), 100)]

    def one(ch):
        return read("/trade-api/v2/markets", params={"tickers": ",".join(ch), "limit": 1000}).get("markets", [])
    out = {}
    with cf.ThreadPoolExecutor(2) as ex:
        for ms in ex.map(one, chunks):
            for m in ms:
                out[m["ticker"]] = m
    return out


def refresh_job(held: set[str]) -> tuple[dict, dict, dict]:
    """(programs, their markets' records, basket index): the slow refresh, for a worker thread."""
    programs = load_programs()
    meta = fetch_meta(set(programs) | held)
    try:
        baskets = basket_index(set(programs) | held, arbs_held_events())
    except Exception:
        traceback.print_exc()
        baskets = None
    return programs, meta, baskets


def queue_ahead(c, tickers: list[str]) -> dict[str, float]:
    """Our order id -> contracts ahead of it at its price, for `tickers`, in
    one read (the endpoint takes market_tickers)."""
    path = "/trade-api/v2/portfolio/orders/queue_positions"
    from kalshi_infra.kalshi.orders import BASE_URL
    resp = c._session.get(BASE_URL + path, headers=c._headers("GET", path),
                          params={"market_tickers": ",".join(tickers)}, timeout=c.timeout_s)
    resp.raise_for_status()
    return {q["order_id"]: float(q.get("queue_position_fp") or 0.0)
            for q in resp.json().get("queue_positions") or [] if q.get("order_id")}


def fetch_book(t: str) -> dict | None:
    try:
        return read(f"/trade-api/v2/markets/{t}/orderbook").get("orderbook_fp", {})
    except Exception:
        return None


class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate, self.capacity = rate, capacity
        self.tokens, self._t = capacity, time.monotonic()
        self.spent: collections.deque = collections.deque()      # (monotonic, cost), last 10 min

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._t) * self.rate)
        self._t = now

    def take(self, cost: float, force: bool = False) -> bool:
        """Spend `cost`, or refuse; force always spends (the bucket may go negative)."""
        self._refill()
        if not force and self.tokens < cost:
            return False
        # Forced spends (exits, cancels) may borrow at most one burst.
        self.tokens = max(self.tokens - cost, -self.capacity)
        now = time.monotonic()
        self.spent.append((now, cost))
        while self.spent and now - self.spent[0][0] > 600:
            self.spent.popleft()
        return True

    def refund(self, cost: float):
        self.tokens += cost
        if self.spent:
            self.spent.pop()

    def has(self, cost: float) -> bool:
        self._refill()
        return self.tokens >= cost

    def rate_10m(self) -> float:
        return sum(c for _, c in self.spent) / 600.0


class WsBooks:
    """ARBS's MultiFeed on its own thread and event loop. Callbacks only queue
    what changed; the farm's thread reads books and handles fills."""

    def __init__(self):
        self.feed = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.dirty: set[str] = set()
        self.fills: list[dict] = []
        self.n_updates = 0

    def _on_quote(self, view):
        with self.lock:
            self.dirty.add(view.ladder.ticker)
            self.n_updates += 1
        self.wake.set()

    def _on_fill(self, f: dict):
        with self.lock:
            self.fills.append(f)
        self.wake.set()

    def ensure(self, tickers):
        """Subscribe to `tickers` (starting the feed on the first ones)."""
        tickers = sorted(tickers)
        if not tickers:
            return
        if self.feed is None:
            from kalshi_strategies.ARBS.feed import MultiFeed
            self.feed = MultiFeed(tickers, self._on_quote, lambda t, tr: None, label="LIPFARM",
                                  on_fill=self._on_fill)
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_until_complete, args=(self.feed.run(),), daemon=True,
                             name="lipfarm-ws").start()
            return
        new = [t for t in tickers if t not in self.feed.tickers]
        if new:
            self.loop.call_soon_threadsafe(self.feed.add, new)

    @property
    def up(self) -> bool:
        return self.feed is not None and self.feed.up

    def bids(self, t: str) -> tuple[dict, dict] | None:
        """(yes bids, no bids) as the socket has them, or None."""
        b = self.feed.ladder(t) if self.up else None
        if b is None or (not b.yes_bids and not b.no_bids):
            return None
        return ({round(p, 4): q for p, q in dict(b.yes_bids).items() if q > 0},
                {round(p, 4): q for p, q in dict(b.no_bids).items() if q > 0})

    def take(self) -> tuple[set[str], list[dict]]:
        with self.lock:
            d, f = self.dirty, self.fills
            self.dirty, self.fills = set(), []
        return d, f


def remove_ours(book: dict[float, float], price: float, size: float) -> dict[float, float]:
    out = dict(book)
    p = round(price, 4)
    if p in out:
        out[p] = max(out[p] - size, 0.0)
    return {k: v for k, v in out.items() if v > 1e-9}


class Farm:
    def __init__(self):
        self._open_mode(False)
        self.arm_requested = False
        self.programs: dict[str, dict] = {}
        self.meta: dict[str, dict] = {}
        self.clients: dict[int, object] = {}
        self.books: dict[str, dict[str, dict[float, float]]] = {}
        self.plans: dict[str, Plan] = {}
        self.held_events: set[str] = set()
        self.waiting: list[str] = []
        self.last_programs = 0.0
        self.bucket = TokenBucket(5.0, WRITE_BURST)
        self.ws = WsBooks()
        self.baskets: dict[tuple[str, str], list] = {}
        self.taker_rates: dict[str, float] = {}
        self.est: dict[str, float] = {}       # ticker -> our LIP $/s per dollar at its last read
        self.rot = 0
        self.pool = cf.ThreadPoolExecutor(BOOK_WORKERS, thread_name_prefix="lipfarm-read")
        self.pending: dict[str, tuple[float, float]] = {}      # leg key -> (price, lots) awaiting its claim
        self.dumping: set[str] = set()        # markets an informed fill hit: sell out now
        self.cfg_adverse = 0.8                # cfg.adverse_fill_frac, refreshed each loop
        self.n_refused, self.last_refused_log = 0, 0.0
        self.marks_at = 0.0

    # ---- mode ----
    def _open_mode(self, live: bool):
        self.live = live
        self.dir = os.path.join(ROOT, "live" if live else "shadow")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = self._load_state()
        self.rates: dict[str, float] = {}       # ticker -> our current $/s with our resting bids
        self.last_accrue = time.time()

    def set_live(self, want: bool, why: str):
        """Switch shadow <-> live. Disarming pulls every real quote; positions are kept."""
        self.arm_requested = want
        if want == self.live:
            return
        print(f"{'ARM' if want else 'DISARM'}: {why}")
        self.accrue()
        for k in list(self.state["orders"]):
            self.cancel(k)
        if self.live:
            self.write_claims({})       # held positions stay claimed from ARBS
        self._save()
        self._open_mode(want)
        if want:
            self.cancel_strays()

    # ---- persistence ----
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            # Journals from before two-sided quoting keyed orders by ticker alone.
            st["orders"] = {(k if "|" in k else key(k, o["side"])): o for k, o in st["orders"].items()}
            return st
        return {"t0": time.time(), "orders": {}, "fills": [], "trade_ids": [], "accrued": {}}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def order_tickers(self) -> set[str]:
        return {unkey(k)[0] for k in self.state["orders"]}

    def legs_of(self, t: str, hedges: bool = True) -> list[tuple[str, dict]]:
        return [(unkey(k)[1], o) for k, o in self.state["orders"].items()
                if unkey(k)[0] == t and (hedges or not is_hedge(k))]

    # ---- exchange ----
    def client(self, shard: int):
        if shard not in self.clients:
            from kalshi_infra.kalshi.orders import KalshiOrderClient
            c = KalshiOrderClient.from_account(shard)
            c.price_dp = 4
            self.clients[shard] = c
        return self.clients[shard]

    def _fetch_meta(self, tickers):
        tickers = [t for t in tickers]
        for i in range(0, len(tickers), 100):
            data = read("/trade-api/v2/markets", params={"tickers": ",".join(tickers[i:i + 100]), "limit": 1000})
            for m in data.get("markets", []):
                self.meta[m["ticker"]] = m

    def shard(self, ticker: str) -> int:
        if ticker not in self.meta:
            self._fetch_meta([ticker])
        return int(self.meta[ticker].get("exchange_index") or 0)

    def refresh_programs(self, cfg: Cfg):
        """Blocking refresh (dry runs); the live loop runs refresh_job on a worker."""
        self.apply_refresh(refresh_job(set(self.signed_positions())))

    def apply_refresh(self, result):
        programs, meta, baskets = result
        self.programs = programs
        self.meta.update(meta)
        if baskets is not None:
            self.baskets = baskets
        self.last_programs = time.time()

    def candidates(self, cfg: Cfg) -> list[str]:
        """The `scan_markets` best by our last estimate of LIP per dollar
        there (pool size until a market has been read: a big pool on a full
        book pays us nothing), a rotating `scan_rotate` of every other LIP
        market, and every market we rest in or hold."""
        def pool(t):
            P = lip.params_from_program(self.programs[t])
            return P.period_reward_usd / P.snapshots_per_period
        everything = sorted(self.programs)
        ranked = sorted(everything, key=lambda t: (-self.est.get(t, -1.0), -pool(t)))
        top = ranked[:cfg.scan_markets]
        rest = [t for t in everything if t not in set(top)]
        rot = []
        if rest and cfg.scan_rotate:
            i = self.rot % len(rest)
            rot = (rest[i:] + rest[:i])[:cfg.scan_rotate]
            self.rot = i + len(rot)
        held = sorted(self.order_tickers() | set(self.signed_positions()))
        return list(dict.fromkeys(top + rot + [t for t in held if t in self.programs]))

    def others_of(self, t: str, ob) -> dict[str, dict[float, float]]:
        """Others' books from a REST orderbook or (yes, no) bids: both include
        our live bids (a shadow one isn't there)."""
        yes, no = parse_book(ob) if isinstance(ob, dict) else ob
        others = {"yes": yes, "no": no}
        if self.live:
            for side, o in self.legs_of(t):
                others[side] = remove_ours(others[side], o["price"], o["remaining"])
        return others

    def our_rate(self, t: str, others, P) -> float:
        legs = [(s, o["price"], o["remaining"]) for s, o in self.legs_of(t)]
        return market_rate(others, legs, P) if legs else 0.0

    def scan(self, cfg: Cfg):
        """Blocking scan (dry runs); the live loop reads books on workers and calls scan_finish."""
        tickers = self.candidates(cfg)
        self.scan_finish(cfg, list(zip(tickers, self.pool.map(fetch_book, tickers))))

    def scan_finish(self, cfg: Cfg, got: list[tuple[str, dict | None]]):
        plans, rates, books = {}, {}, {}
        self.held_events = arbs_held_events()
        pos = self.signed_positions()
        for t, ob in got:
            if ob is None or t not in self.programs:
                continue
            others = self.others_of(t, ob)
            books[t] = others
            m = self.meta.get(t, {})
            if m and m.get("status") != "active":
                continue
            P = self.params(t)
            if t in self.order_tickers():
                rates[t] = self.our_rate(t, others, P)
            pl = self.plan(t, others, P, cfg, pos)
            if pl:
                plans[t] = pl
        # What each read market would pay us per dollar, for the next cycle's ranking.
        for t in books:
            pl = plans.get(t)
            self.est[t] = pl.rate / max(pl.capital, 1e-9) if pl else 0.0
        self.books, self.plans, self.rates = books, plans, rates

    def sync_live_orders(self):
        """Make the journal and Kalshi agree, one bid per journal entry. A
        journaled order missing from the resting list is dropped only once its
        own lookup says it's done: dropping a still-resting one had the next
        cycle place a second bid beside it (11-04: 12 orphan 1000-lot bids,
        two or three per side on PR05). Any lipf- order the journal doesn't
        know (a cancel that failed after we forgot it) is cancelled."""
        if not self.live:
            return
        c = self.client(0)
        listed = {oid: (t, cid) for oid, t, cid in c._resting("")}
        mine = {o["oid"] for o in self.state["orders"].values()}
        for k, o in list(self.state["orders"].items()):
            if o["oid"] in listed:
                continue
            try:
                status = (c.order(o["oid"]) or {}).get("status")
            except Exception as e:
                print(f"sync: lookup {k}: {e}")
                continue
            if status != "resting":
                del self.state["orders"][k]
        for oid, (t, cid) in listed.items():
            if (cid or "").startswith(CID_PREFIX) and oid not in mine:
                print(f"sync: cancel orphan {t} {oid}")
                self.bucket.take(CANCEL_TOKENS, force=True)
                try:
                    self.client(self.shard(t)).cancel(oid, t)
                except Exception as e:
                    print(f"sync: cancel orphan {t}: {e}")

    def cancel_strays(self):
        """Cancel our resting orders left by an earlier run (client id prefix)."""
        c = self.client(0)
        for oid, ticker, cid in c._resting(""):
            if cid and cid.startswith(CID_PREFIX):
                try:
                    self.client(self.shard(ticker)).cancel(oid, ticker)
                except Exception as e:
                    print(f"stray cancel {ticker}: {e}")
        self.state["orders"] = {}

    def cancel(self, k: str, reason: str = "lipfarm:cancel"):
        o = self.state["orders"].pop(k, None)
        t = unkey(k)[0]
        if o and self.live:
            c = self.client(self.shard(t))
            self.bucket.take(CANCEL_TOKENS, force=True)
            c.allow_now("cancel", force=True, reason=reason)
            try:
                c.cancel(o["oid"], t)
            except Exception as e:
                print(f"cancel {k}: {e}")

    def place(self, t: str, side: str, price: float, lots: float, reserve: float = PLACE_RESERVE,
              reason: str = "lipfarm:quote", hedge: bool = False) -> bool:
        if self.live:
            c = self.client(self.shard(t))
            if hedge:
                # An exit always goes, at our own LIP writes' expense, not ARBS's.
                self.bucket.take(ORDER_TOKENS, force=True)
                c.allow_now("order", force=True, reason=reason)
            else:
                if not c.can_afford("order", reserve=reserve * c.tokens_per_s) or not self.bucket.take(ORDER_TOKENS):
                    self.n_refused += 1
                    if time.time() - self.last_refused_log > 60:
                        self.last_refused_log = time.time()
                        print(f"budget: {self.n_refused} bids refused so far (our bucket "
                              f"{self.bucket.tokens:.0f}, shard {c.tokens_now():.0f})")
                    return False
                if not c.allow_now("order", reason=reason):
                    self.bucket.refund(ORDER_TOKENS)
                    return False
            order = Order(order_id=0, ticker=t, side=side, order_type="limit", price=price, size=lots,
                          decision_time=pd.Timestamp.now(tz="UTC"), intent="quote")
            order.client_order_id = CID_PREFIX + uuid.uuid4().hex[:24]
            try:
                ack = c.place_resting(order)
            except Exception as e:
                print(f"place {t} {side}@{price}: {e}")
                return False
            oid = ack.exchange_order_id
            # Fills of these orders get hedged; fills from before two-sided quoting don't.
            self.state.setdefault("hedged_oids", []).append(oid)
            if not hedge:
                self.state.setdefault("lip_oid_size", {})[oid] = lots
        else:
            oid = "shadow-" + uuid.uuid4().hex[:12]
        self.state["orders"][key(t, side, hedge)] = {"oid": oid, "side": side, "price": price, "size": lots,
                                                     "remaining": lots, "placed": time.time()}
        return True

    def tick(self, t: str, price: float) -> float:
        for r in self.meta.get(t, {}).get("price_ranges") or []:
            if float(r["start"]) - 1e-9 <= price < float(r["end"]) - 1e-9:
                return float(r["step"])
        return 0.01

    def taker_rate(self, t: str) -> float:
        ev = self.meta.get(t, {}).get("event_ticker") or event_of(t)
        if ev not in self.taker_rates:
            try:
                from kalshi_infra.kalshi import fees
                self.taker_rates[ev] = fees.for_event(ev.split("-")[0], ev).taker_rate
            except Exception:
                self.taker_rates[ev] = DEFAULT_TAKER_RATE
        return self.taker_rates[ev]

    def free_positions(self) -> dict[str, float]:
        """Hedged-fill positions not already locked in a taken basket."""
        pos = self.signed_positions(hedged_only=True)
        for t, v in self.state.get("locked", {}).items():
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

    def take(self, t: str, side: str, price: float, q: float) -> float:
        """IOC buy of `side` at `price`; returns contracts filled. An exit: it always goes."""
        c = self.client(self.shard(t))
        self.bucket.take(ORDER_TOKENS, force=True)
        c.allow_now("order", force=True, reason="lipfarm:take")
        order = Order(order_id=0, ticker=t, side=side, order_type="limit", price=price, size=q,
                      decision_time=pd.Timestamp.now(tz="UTC"), intent="take")
        order.client_order_id = CID_PREFIX + uuid.uuid4().hex[:24]
        try:
            fill = c.place_ioc(order)
        except Exception as e:
            print(f"take {t} {side}@{price} x{q:.0f}: {e}")
            return 0.0
        if fill is None:
            return 0.0
        oid = fill.exchange_order_id or ("ioc-" + order.client_order_id)
        # Booked from the IOC's own response at once, so a basket's locked legs
        # never look unexited while the fill is in flight; the same order's
        # fills arriving later (socket, REST) are then skipped.
        self.state.setdefault("hedged_oids", []).append(oid)
        self.state.setdefault("ioc_booked", []).append(oid)
        self.state["fills"].append({"ticker": t, "side": side, "price": fill.price, "size": fill.size,
                                    "fee": fill.fee, "order_id": oid, "trade_id": "ioc:" + oid,
                                    "time": pd.Timestamp.now(tz="UTC").isoformat(), "liquidity": "taker"})
        print(f"take: {t} {side}@{fill.price} x{fill.size:.0f}")
        return fill.size

    def try_lock(self, t: str, held: str, qty: float) -> float:
        """Lock `qty` of our `held` side of t by taking the cheapest bundle
        that costs at most its payout after taker fees: the same market's other
        side, or the other legs of an ARBS basket holding our leg. Returns the
        contracts locked."""
        other = "no" if held == "yes" else "yes"
        cost = self.avg_cost(t, held)
        best = None                                   # (cost per payout dollar, payout, [(t, side, px, size)])
        a = self.ask(t, other)
        if a:
            px, sz = a
            c = cost + px + kfee(px, 1.0, self.taker_rate(t))
            if c <= 1.0 + 1e-9:
                best = (c, 1.0, [(t, other, px, sz)])
        legs_needed = {tk for pay, legs in self.baskets.get((t, held), []) for tk, _ in legs if tk != t}
        missing = [tk for tk in legs_needed if tk not in self.books]
        if missing:
            self.books.update(self.fresh_books(missing))
        for pay, legs in self.baskets.get((t, held), []):
            buys, c = [], cost
            for tk, sd in legs:
                if (tk, sd) == (t, held):
                    continue
                a = self.ask(tk, sd)
                if a is None:
                    break
                px, sz = a
                c += px + kfee(px, 1.0, self.taker_rate(tk))
                buys.append((tk, sd, px, sz))
            else:
                if c <= pay + 1e-9 and (best is None or c / pay < best[0] / best[1]):
                    best = (c, pay, buys)
        if best is None:
            return 0.0
        q = min([qty] + [sz for *_, sz in best[2]])
        if q < 1.0:
            return 0.0
        got = [self.take(tk, sd, px, math.floor(q)) for tk, sd, px, _ in best[2]]
        locked = min([q] + got)
        if locked >= 1.0 and not (len(best[2]) == 1 and best[2][0][0] == t):
            # Across markets nothing nets at Kalshi: keep the bundle out of exits.
            lk = self.state.setdefault("locked", {})
            for tk, sd in [(t, held)] + [(tk, sd) for tk, sd, *_ in best[2]]:
                lk[tk] = lk.get(tk, 0.0) + (locked if sd == "yes" else -locked)
        print(f"lock: {t} {held} x{locked:.0f} at ${best[0]:.3f} for ${best[1]:.0f} "
              f"({'pair' if len(best[2]) == 1 and best[2][0][0] == t else 'basket'})")
        return locked

    def dump(self, t: str):
        """Sell what we hold in t into others' bids, as deep as it takes. After
        an informed fill (not just dust) also pull its LIP bids and cool it down."""
        self.dumping.discard(t)
        v = self.free_positions().get(t, 0.0)
        if abs(v) > DUST_LOTS:
            for side, o in self.legs_of(t, hedges=False):
                self.cancel(key(t, side), reason="lipfarm:adverse")
            self.state.setdefault("cooldown", {})[t] = time.time() + GUARD_COOLDOWN_S
        if abs(v) < 0.01:
            return
        held, other = ("yes", "no") if v > 0 else ("no", "yes")
        # Selling `held` = buying `other` against others' `held` bids, best first.
        need, cum, worst = abs(v), 0.0, None
        for p in sorted(self.books[t][held], reverse=True):
            cum += self.books[t][held][p]
            worst = p
            if cum >= need:
                break
        if worst is None:
            print(f"adverse: {t} no bids to sell {held} into; resting an exit")
            return
        # Hundredths, as Kalshi fills: flooring to whole contracts left 0.69 behind
        # that was then dumped and re-exited every pass (AL14 11-04 21:10).
        q = math.floor(min(need, cum) * 100) / 100
        if q < 0.01:
            return
        for k in [k for k in self.state["orders"] if is_hedge(k) and unkey(k)[0] == t]:
            self.cancel(k, reason="lipfarm:dump")
        self.take(t, other, round(1.0 - worst, 4), q)
        print(f"adverse: {t} sold {held} x{q} down to {worst}")

    def hedge(self, cfg: Cfg, takes: bool = False):
        """Exits for what our fills left us holding. `takes`: first try locking
        it by taking (try_lock). Then rest a bid of the rest one tick above
        the other side's touch, under its offer. Live only."""
        if not self.live:
            return
        for t, v in self.free_positions().items():
            if abs(v) <= DUST_LOTS and t in self.books and t in self.meta:
                self.dumping.add(t)
        for t in sorted(self.dumping):
            if t in self.books and t in self.meta:
                self.dump(t)
        pos = self.free_positions()
        if takes:
            for t, v in list(pos.items()):
                if t in self.books and t in self.meta:
                    self.try_lock(t, "yes" if v > 0 else "no", abs(v))
            pos = self.free_positions()
        want = {}
        for t, v in pos.items():
            others = self.books.get(t)
            if not others or t not in self.meta:
                continue
            held, side = ("yes", "no") if v > 0 else ("no", "yes")
            touch = max(others[side]) if others[side] else 0.0
            ask = 1.0 - max(others[held]) if others[held] else 1.0
            px = min(round(touch + self.tick(t, touch), 4), round(ask - self.tick(t, ask - 1e-9), 4))
            if px >= 0.001:
                want[key(t, side, True)] = (px, abs(v))
        now = time.time()
        for k, o in list(self.state["orders"].items()):
            if not is_hedge(k):
                continue
            w = want.get(k)
            if w is None or o["remaining"] > w[1] + 1e-9 or o["remaining"] < 0.5 * w[1]:
                self.cancel(k, reason="lipfarm:hedge")
            elif w[0] > o["price"] + 1e-9 and now - o["placed"] >= EXIT_REPRICE_S:
                self.cancel(k, reason="lipfarm:hedge")       # outbid: step up
            else:
                want.pop(k)                                   # still best, or re-priced too recently
        for k, (px, q) in want.items():
            if k not in self.state["orders"]:
                t, side = unkey(k)
                if self.place(t, side, px, q, reserve=0.0, reason="lipfarm:hedge", hedge=True):
                    print(f"exit: {t} {side}@{px} x{q:.0f}")

    def exposure(self) -> float:
        """Dollars our fills have left us holding, unexited, at cost."""
        return sum(abs(v) * self.avg_cost(t, "yes" if v > 0 else "no") for t, v in self.free_positions().items())

    def hedge_capital(self) -> float:
        return sum(o["price"] * o["remaining"] for k, o in self.state["orders"].items() if is_hedge(k))

    def params(self, t: str) -> lip.LipParams:
        m = self.meta.get(t, {})
        return dataclasses.replace(lip.params_from_program(self.programs[t]), price_ranges=tuple(
            (float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or []))

    def plan(self, t: str, others: dict, P: lip.LipParams, cfg: Cfg, pos: dict[str, float]) -> Plan | None:
        """plan_market, or None on an event ARBS holds."""
        if event_of(t) in self.held_events:
            return None
        v = pos.get(t, 0.0)
        return plan_market(t, others, P, cfg, {"yes": max(v, 0.0), "no": max(-v, 0.0)}, exit_lots(others, cfg))

    def fresh_books(self, tickers) -> dict[str, dict]:
        """Others' books now: from the socket where it has the market, else REST."""
        out, missing = {}, []
        for t in tickers:
            b = self.ws.bids(t)
            if b is None:
                missing.append(t)
            else:
                out[t] = self.others_of(t, b)
        if missing:
            with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
                for t, ob in zip(missing, ex.map(fetch_book, missing)):
                    if ob is not None:
                        out[t] = self.others_of(t, parse_book(ob))
        return out

    def check(self, cfg: Cfg, tickers, moves: bool, takes: bool = False):
        """Exits first (`takes`: try locking positions by taking); then pull a
        bid its market's plan no longer has or that could fill us past
        max_capital; then (`moves`) re-place bids with size behind them at the
        back."""
        books = self.fresh_books(tickers)
        self.books.update(books)
        self.hedge(cfg, takes=takes)
        pos, keep, exp = self.signed_positions(), {}, self.exposure()
        for k, o in list(self.state["orders"].items()):
            if not is_hedge(k) and exp + o["price"] * o["remaining"] > cfg.max_capital + 1e-9:
                print(f"capital: pull {k} (exposure ${exp:.0f})")
                self.cancel(k, reason="lipfarm:capital")
        for t, others in books.items():
            if t not in self.programs or t not in self.order_tickers():
                continue
            P = self.params(t)
            pl = self.plan(t, others, P, cfg, pos)
            want = dict(pl.legs) if pl else {}
            for side, o in self.legs_of(t, hedges=False):
                if side not in want:
                    print(f"guard: pull {t} {side}@{o['price']} (not in its plan now)")
                    self.cancel(key(t, side), reason="lipfarm:guard")
                    self.state.setdefault("cooldown", {})[t] = time.time() + GUARD_COOLDOWN_S
                elif abs(want[side] - o["price"]) < 1e-9:
                    keep[key(t, side)] = others
            self.rates[t] = self.our_rate(t, others, P)
        if moves:
            self.move_back(cfg, keep)

    def guard(self, cfg: Cfg):
        """Live, every GUARD_S: fills (REST backstop), then check every market we
        quote or hold, with back-of-queue moves."""
        if not self.live:
            return
        new = self.poll_fills()
        tickers = sorted(self.order_tickers() | set(self.signed_positions()))
        if tickers:
            self.check(cfg, tickers, moves=True, takes=bool(new))

    def react(self, cfg: Cfg):
        """Live, on a websocket wake: book our fills, then check the markets
        whose books changed or that just filled."""
        dirty, fills = self.ws.take()
        if not self.live:
            return
        new = self.book_fills(fills)
        mine = self.order_tickers() | set(self.signed_positions())
        tickers = (dirty & mine) | new
        if tickers:
            self.check(cfg, sorted(tickers), moves=False, takes=bool(new))

    def move_back(self, cfg: Cfg, keep: dict[str, dict]):
        now = time.time()
        orders = dict(self.state["orders"])
        due = {k: v for k, v in keep.items() if k in orders and now - orders[k]["placed"] >= MOVE_MIN_S}
        if not due:
            return
        c = self.client(0)
        try:
            ahead = queue_ahead(c, sorted({unkey(k)[0] for k in due}))
        except Exception as e:
            print(f"queue positions: {e}")
            return
        moves = 0
        for k, others in sorted(due.items(), key=lambda kv: orders[kv[0]]["placed"]):
            o, (t, side) = orders[k], unkey(k)
            a = ahead.get(o["oid"])
            if a is None or others[side].get(round(o["price"], 4), 0.0) - a < MOVE_BEHIND_FRAC * cfg.lots:
                continue
            c = self.client(self.shard(t))
            # Moves only spend the top half of our bucket: at 10 per pass they drained it and
            # 50 new LIP bids were refused (11-04 21:40).
            if (moves >= MAX_MOVES or not self.bucket.has(0.5 * self.bucket.capacity + ORDER_TOKENS + CANCEL_TOKENS)
                    or not c.can_afford("order", reserve=MOVE_RESERVE * c.tokens_per_s + c.COST["cancel"])):
                return
            self.cancel(k, reason="lipfarm:move")
            if self.place(t, side, o["price"], cfg.lots, reserve=0.0, reason="lipfarm:move"):
                moves += 1

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

    def write_claims(self, want: dict[str, Plan]) -> bool:
        """Claim what we want, rest on, or hold; keep each ticker's first-claim
        time, which the placement gate waits on. True if anything new was claimed."""
        pos = self.signed_positions()
        now = time.time()
        old = self.state.setdefault("claims", {})
        claims = {t: old.get(t, now) for t in set(want) | self.order_tickers() | set(pos)}
        self.state["claims"] = claims
        os.makedirs(os.path.dirname(CLAIMS_PATH), exist_ok=True)
        tmp = CLAIMS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"written_at": now, "tickers": sorted(claims), "positions": pos}, f)
        os.replace(tmp, CLAIMS_PATH)
        return any(t not in old for t in claims)

    def period_accrued(self, t: str) -> float:
        return self.state.setdefault("period_accrued", {}).get(self.programs[t]["id"], 0.0)

    def choose(self, cfg: Cfg) -> dict[str, Plan]:
        """The markets to quote, within max_capital and num_markets. Only ones
        that can still reach the $1 payout before their period ends. Quoted
        markets past $1 this period come first (every further dollar there is
        paid), then quoted ones under $1 (dropping one forfeits its accrual),
        then new ones; best LIP per dollar first within each."""
        now = time.time()

        def left(t):
            return max(pd.Timestamp(self.programs[t]["end_date"]).timestamp() - now, 0.0)

        def per_dollar(p):
            return p.rate / max(p.capital, 1e-9)
        cool = self.state.setdefault("cooldown", {})
        cands = [p for p in self.plans.values() if p.ticker in self.programs and cool.get(p.ticker, 0.0) <= now
                 and self.period_accrued(p.ticker) + p.rate * left(p.ticker) >= LIP_MIN_PAYOUT_USD]
        held = self.order_tickers()

        def rank(p):
            if p.ticker not in held:
                return 2
            return 0 if self.period_accrued(p.ticker) >= LIP_MIN_PAYOUT_USD else 1
        out, exp = {}, self.exposure()
        for p in sorted(cands, key=lambda p: (rank(p), -per_dollar(p))):
            if len(out) >= cfg.num_markets:
                break
            if exp + p.lots * max(px for _, px in p.legs) <= cfg.max_capital + 1e-9:
                out[p.ticker] = p
        return out

    def reconcile(self, cfg: Cfg):
        want = self.choose(cfg)
        if self.live:
            self.write_claims(want)
            now = time.time()

            def ready(t: str) -> bool:
                c = self.state["claims"].get(t)
                return c is not None and now - c >= CLAIM_SETTLE_S
        else:
            # Shadow writes no claims (ARBS keeps trading as is) and shows
            # what live would quote once ARBS has given way.
            def ready(t: str) -> bool:
                return True
        self.waiting = [t for t in want if not ready(t)]
        legs = {key(t, s): (p, pl.lots) for t, pl in want.items() for s, p in pl.legs}
        for k, o in list(self.state["orders"].items()):
            if is_hedge(k):
                continue
            w = legs.get(k)
            stale = (w is None or abs(w[0] - o["price"]) > 1e-9 or abs(o["size"] - w[1]) > 1e-9
                     or o["remaining"] < 0.5 * w[1] or not ready(unkey(k)[0]))
            if stale:
                self.cancel(k)
        self.pending = {}
        for k, (px, q) in legs.items():
            t, side = unkey(k)
            if k in self.state["orders"]:
                continue
            if ready(t):
                self.place(t, side, px, q)
            else:
                self.pending[k] = (px, q)
        for t, pl in want.items():
            if t in self.order_tickers():
                self.rates.setdefault(t, pl.rate)
        self.hedge(cfg)
        if self.live:
            self.ws.ensure(self.order_tickers() | set(self.signed_positions()))

    def book_fills(self, fills: list[dict]) -> set[str]:
        """Record fills of our orders (deduped on trade_id, from the socket or
        REST); returns the tickers that had new ones."""
        ours = {o["oid"]: k for k, o in self.state["orders"].items()}
        known = set(self.state.get("all_oids", [])) | set(ours)
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
                self.state["orders"][k]["remaining"] = max(self.state["orders"][k]["remaining"] - f["size"], 0.0)
        self.state["trade_ids"] = sorted(seen)
        if new:
            print(f"fill: {sorted(new)}")
            self.flag_adverse()
        return new

    def flag_adverse(self):
        """Mark markets where fills took adverse_fill_frac of one of our LIP bids."""
        frac = self.cfg_adverse
        sizes = self.state.get("lip_oid_size", {})
        done = set(self.state.setdefault("adverse_oids", []))
        got: dict[str, float] = {}
        tick: dict[str, str] = {}
        for f in self.state["fills"]:
            o = f.get("order_id")
            if o in sizes and o not in done:
                got[o] = got.get(o, 0.0) + f["size"]
                tick[o] = f["ticker"]
        for o, q in got.items():
            if q >= frac * sizes[o] - 1e-9:
                self.state["adverse_oids"].append(o)
                self.dumping.add(tick[o])
                print(f"adverse: {tick[o]} filled {q:.0f} of {sizes[o]:.0f}")

    def place_pending(self):
        """Place legs whose new claim has had CLAIM_SETTLE_S for ARBS to stand aside."""
        if not self.pending or not self.live:
            return
        now = time.time()
        for k, (px, q) in list(self.pending.items()):
            t, side = unkey(k)
            c = self.state.get("claims", {}).get(t)
            if c is not None and now - c >= CLAIM_SETTLE_S:
                del self.pending[k]
                if k not in self.state["orders"]:
                    self.place(t, side, px, q)

    def poll_fills(self) -> set[str]:
        """REST backstop for the socket's fills; returns tickers with new ones."""
        if not self.live or not self.clients:
            return set()
        c = next(iter(self.clients.values()))
        # Overlapping windows, deduped on trade_id.
        since = self.state.get("fills_since", self.state["t0"]) - 120
        self.state["fills_since"] = time.time()
        return self.book_fills(c.fills("", min_ts=int(since)))

    def exit_now(self, cfg: Cfg):
        """Socket down: poll fills over REST and exit new ones at once."""
        new = self.poll_fills() if self.live else set()
        if new:
            self.check(cfg, sorted(new), moves=False, takes=True)

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

    # ---- report ----
    def positions(self) -> list[dict]:
        pos: dict[tuple, dict] = {}
        for f in self.state["fills"]:
            p = pos.setdefault((f["ticker"], f["side"]), {"ticker": f["ticker"], "side": f["side"],
                                                          "lots": 0.0, "cost": 0.0, "fees": 0.0})
            p["lots"] += f["size"]
            p["cost"] += f["size"] * f["price"]
            p["fees"] += f["fee"] or 0.0
        # Marks need fresh bids, but the status is written every STATUS_S: refetch at most once a minute.
        if pos and time.time() - self.marks_at > 60:
            self.marks_at = time.time()
            self._fetch_meta(sorted({t for t, _ in pos}))
        for (t, side), p in pos.items():
            m = self.meta.get(t, {})
            res = m.get("result")
            # A yes+no pair is $1 (Kalshi nets it); only the unpaired rest is marked.
            pair = min(p["lots"], pos.get((t, "no" if side == "yes" else "yes"), {}).get("lots", 0.0))
            if res in ("yes", "no"):
                mark = 1.0 if res == side else 0.0
            else:
                # Liquidation value: best bid on our side.
                mark = float(m.get(f"{side}_bid_dollars") or 0.0)
            p["mark"] = mark
            p["paired"] = pair
            p["value"] = pair * 0.5 + (p["lots"] - pair) * mark
            p["pnl"] = p["value"] - p["cost"] - p["fees"]
        return list(pos.values())

    def report(self, cfg: Cfg, log: bool = True):
        """Write status.json for the dashboard; `log`: also print the table (once a cycle)."""
        rows = []
        for t in sorted(self.order_tickers(), key=lambda t: -self.rates.get(t, 0.0)):
            legs = sorted(self.legs_of(t), key=lambda so: so[0])
            hedged = {unkey(k)[1] for k in self.state["orders"] if is_hedge(k) and unkey(k)[0] == t}
            pl = self.plans.get(t)
            rows.append({"ticker": t, "mode": pl.mode if pl else ("hedge" if hedged else ""),
                         "bids": " + ".join(f"{s}@{o['price']:.3f}x{o['remaining']:.0f}" for s, o in legs),
                         "resting": sum(o["remaining"] for _, o in legs),
                         "capital_usd": sum(o["price"] * o["remaining"] for _, o in legs),
                         "our_usd_day": self.rates.get(t, 0.0) * 86400,
                         "pool_usd_day": (pl.pool_rate if pl else 0.0) * 86400,
                         "exp_lip_usd": self.state["accrued"].get(t, 0.0),
                         "period_lip_usd": self.period_accrued(t) if t in self.programs else 0.0})
        pos = self.positions()
        tot_lip = sum(self.state["accrued"].values())
        per = self.state.get("period_accrued", {}).values()
        locked = sum(v for v in per if v >= LIP_MIN_PAYOUT_USD)
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
                   "ws_up": self.ws.up, "ws_updates": self.ws.n_updates,
                   "waiting_on_arbs": len(self.waiting), "arbs_events_skipped": len(self.held_events),
                   "updated_at": time.time()}
        tmp = os.path.join(ROOT, "status.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"summary": summary, "quotes": rows, "positions": pos, "waiting": self.waiting}, f, indent=1)
        os.replace(tmp, os.path.join(ROOT, "status.json"))
        if not log:
            return

        print(f"\n=== LIPFARM {summary['mode']} {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S}Z "
              f"| {hrs:.2f}h | quoting {len(rows)} | now ${summary['our_usd_day_now']:.0f}/day "
              f"| exp LIP ${tot_lip:.2f} | fills {summary['fills']} pnl ${fill_pnl:+.2f} "
              f"| net ${summary['net_usd']:+.2f} | resting ${at_risk:.0f} | exposure "
              f"${summary['exposure_usd']:.0f} of ${cfg.max_capital:.0f}")
        print(f"{'ticker':40} {'mode':7} {'bids':26} {'cap$':>6} {'$/day':>7} {'pool/d':>7} {'expLIP':>7}")
        for r in rows:
            print(f"{r['ticker']:40} {r['mode']:7} {r['bids']:26} {r['capital_usd']:6.0f} "
                  f"{r['our_usd_day']:7.1f} {r['pool_usd_day']:7.1f} {r['exp_lip_usd']:7.2f}")
        if pos:
            print(f"{'fills: ticker':40} {'side':4} {'lots':>6} {'cost':>7} {'mark':>6} {'pnl':>8}")
            for p in pos:
                print(f"{p['ticker']:40} {p['side']:4} {p['lots']:6.0f} {p['cost']:7.2f} "
                      f"{p['mark']:6.3f} {p['pnl']:+8.2f}")

    # ---- loop ----
    def run(self, arm: str | None):
        """The main thread only decides and writes; book reads and the program
        refresh run on workers, so exits and pulls never wait on them."""
        modes, stop = TradingModeWatcher(NAME), StopWatcher()
        if arm:
            self.set_live(True, arm)
        refresh, scan, last_cycle, last_guard, last_status = None, None, 0.0, time.time(), 0.0
        try:
            while True:
                cfg = Cfg.load()
                self.bucket.rate = cfg.write_tokens_per_s
                self.cfg_adverse = cfg.adverse_fill_frac
                try:
                    reason = stop.poll()
                    if reason:
                        print(f"STOP: {reason}")
                        break
                    want = modes.poll()
                    if want is not None:
                        self.set_live(want, f"dashboard {modes.origin()}")
                    now = time.time()
                    if refresh is None and now - self.last_programs > cfg.programs_refresh_s:
                        refresh = self.pool.submit(refresh_job, set(self.signed_positions()))
                    if refresh is not None and refresh.done():
                        job, refresh = refresh, None
                        try:
                            self.apply_refresh(job.result())
                        except Exception:
                            traceback.print_exc()
                            self.last_programs = now - cfg.programs_refresh_s + 60.0    # retry in a minute
                    if self.programs and scan is None and now - last_cycle >= cfg.refresh_s:
                        self.poll_fills()
                        self.sync_live_orders()
                        self.accrue()
                        tickers = self.candidates(cfg)
                        scan, last_cycle = (tickers, [self.pool.submit(fetch_book, t) for t in tickers]), now
                    if scan is not None and all(fu.done() for fu in scan[1]):
                        (tickers, futs), scan = scan, None
                        self.scan_finish(cfg, [(t, fu.result()) for t, fu in zip(tickers, futs)])
                        self.reconcile(cfg)
                        self.report(cfg)
                        self._save()
                    self.place_pending()
                    # The dashboard polls every 5 s; a scan cycle takes ~1 min at READS_PER_S.
                    if self.programs and time.time() - last_status >= STATUS_S:
                        last_status = time.time()
                        self.accrue()
                        self.report(cfg, log=False)
                except Exception:
                    traceback.print_exc()
                self.ws.wake.wait(timeout=FILL_POLL_S)
                self.ws.wake.clear()
                try:
                    if time.time() - last_guard >= GUARD_S:
                        last_guard = time.time()
                        self.react(cfg)
                        self.guard(cfg)
                    elif self.ws.up:
                        self.react(cfg)
                    else:
                        self.exit_now(cfg)
                except Exception:
                    traceback.print_exc()
        except KeyboardInterrupt:
            print("stopping: cancelling quotes")
        finally:
            self.accrue()
            for k in list(self.state["orders"]):
                self.cancel(k)
            if self.live:
                self.write_claims({})   # held positions stay claimed from ARBS
            self._save()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="arm at start: real orders")
    ap.add_argument("--resume-arm", action="store_true",
                    help="re-arm if the dashboard's last request was an arm and no STOP since")
    a = ap.parse_args()
    arm = "--live" if a.live else (resumable_arm(NAME) if a.resume_arm else None)
    Farm().run(arm)


if __name__ == "__main__":
    main()
