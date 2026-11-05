"""
LIP farmer: what to quote and the shared reads (the service that runs it is
runner.py). Quotes only low-flow LIP markets (at most max_trades_day over
the trailing flow_days, not in skip_categories), in one of
three shapes, chosen per market each cycle from today's book:

  wide     the yes/no spread is at least `wide_spread`: one bid of the market's
           Target at each side's touch, owning both halves of a thin book.
  decided  the expensive side's best bid is at least decided_min: one bid of
           Target there only (fills gain the last cents; the risk is a
           reversal). A bid at the market's top price (0.99, or 0.999 on 0.1c
           ticks) earns no LIP; the scorer rules those out.
  narrow   neither: both touches, only where exit liquidity backs it (each bid
           at most exit_depth_frac of others' bids just under its touch).

Each quoted side takes one of two layouts, whichever the LIP scorer prefers:
  touch     the bid at the touch, kept at the back of its queue (moves first):
            on a crowded touch the size ahead of us absorbs a sweep first.
  sentinel  SENTINEL_LOTS at the touch (back of the queue: it fills only once the
            touch is cleared) and the bid one tick under it, both in a Kalshi
            order group of limit GROUP_LIMIT. The sentinel's fill trips the
            group and Kalshi cancels the bid behind it. Taken when it keeps
            sentinel_keep of the touch layout's LIP (thin touches, 11-04: ~97%;
            crowded touches leave nothing to score one tick under).

Every side must reach Kalshi's Target size, ours included where we quote and
from others alone where we don't, or the snapshot pays nobody. Markets are
ranked by LIP per day net of expected loss. Kalshi holds no cash for resting
orders (11-04: $9.6k resting on a $16.3k balance, balance unchanged), so
`max_capital` caps what fills have left us holding: a bid that could fill us
past it isn't placed, and one is pulled as soon as a fill makes that true.

On a fill we first look for a way to lock the position by taking: the same
market's other side, or the other legs of any ARBS basket holding our leg
(ARBS's scanned and hand specs), whenever the bundle costs at most its payout
after taker fees. Whatever is left gets an exit bid one tick above the other
side's touch (Kalshi nets a yes+no pair to $1), at any price -- getting out
comes before the pair's profit. Fills of `adverse_lots` or more on one bid
are too much to wait out: we sell into the book at once (sweeping others'
bids on the side we hold), pull that market's bids and cool it down. Exits
sit ahead of our own LIP bids, follow
the touch, and are never pulled or moved to the back; a side we hold more
than DUST_LOTS of isn't bid for LIP until that's exited.

    python -m kalshi_strategies.LIPFARM.runner                # shadow until armed from the dashboard
    python -m kalshi_strategies.LIPFARM.runner --live         # armed from the start: REAL MONEY
    python -m kalshi_strategies.LIPFARM.runner --resume-arm   # re-arm if the last request was an arm (EC2 unit)

Arm/disarm is the dashboard's button (trading_mode.json, as for every
series); STOP.json disarms and exits. Params in config.json (dashboard
sidebar, re-read every cycle). Journal in data/live/LIPFARM/<mode>/state.json
survives restarts; data/live/LIPFARM/status.json holds the latest table.
"Expected LIP" is our modelled share (lip.py) integrated over time, not what
Kalshi pays: payouts under $1 per market-period are dropped, and others' size
between our reads is unseen.

How it runs (one asyncio loop, ARBS's Gateway and MultiFeed, fills exited
from the websocket book in memory) is described in runner.py.

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

import collections
import concurrent.futures as cf
import dataclasses
import json
import math
import re
import os
import threading
import time
import traceback

import pandas as pd
import urllib3

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import load, param, section
from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_infra.trading import lip

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CID_PREFIX = "lipf-"
# All our REST reads share the account's read limit with ARBS (300/s). 24
# unpaced book threads plus a parallel records refresh drew 429s on ARBS's
# balance reads and position audits (11-04 ~21:30), and the records refresh
# still drew 429s at 20/s; 10/s takes ~90 s a cycle.
READS_PER_S = 10.0
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
# Our writes share ARBS's shard bucket (300 tokens/s on shard 0); ARBS caps itself
# at 250/s and our own bucket (write_tokens_per_s) is what bounds us. A new quote
# goes out only with this fraction of the shard's bucket left after it, a
# back-of-queue move with MOVE_RESERVE: just enough that a write never meets an
# empty bucket. At 0.5 / 0.8, with ARBS holding the shard near 155 (11-05), 259
# bids were refused in an hour and moves never ran. Cancels always go.
PLACE_RESERVE = 0.1
MOVE_RESERVE = 0.15
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
# After a sentinel trip or adverse fill: 11-04, from 10 s after a sweep on, a further
# 2c fall was no likelier than a 2c rise (22% vs 18% to 60 s), so a short pull is enough.
TRIP_COOLDOWN_S = 60.0
# Trailing flow (max_trades_day) is re-read this often per
# market; at most FLOW_QUEUE reads wait at once, best markets first.
FLOW_TTL_S = 3600.0
FLOW_QUEUE = 40
MOVE_MIN_S = 5.0
MOVE_BEHIND_FRAC = 0.05
# At most this many moves a pass, most size behind first: 10 x 12 tokens / 5 s =
# 24 of our 40 tokens/s, leaving the rest for new bids and re-pricing (10 a pass
# drained a 30/s bucket with a 120 burst, 11-04 21:40; it's 40/s and 300 now).
MAX_MOVES = 10
# Our bucket's burst: 30 orders at once, so a cycle's bids (with group creates and
# sentinels) go out together. At 12, 65 bids a day were refused with the shard
# at 211-249 of 300 tokens (11-05); only ARBS and LIPFARM write on shard 0 now.
WRITE_BURST = 300.0
ORDER_TOKENS, CANCEL_TOKENS = 10.0, 2.0
# An exit is re-priced only when outbid, at most this often: re-pricing on
# every socket update into a penny-jumping bot spent ~250 orders exiting 13
# contracts and starved every LIP bid of budget (AL14/PR05, 11-04 20:50-21:06).
EXIT_REPRICE_S = 5.0
# A position this small is sold into the book at once instead of resting an exit.
DUST_LOTS = 25.0
# As ARBS: every LIP bid of an event rests in one Kalshi order group of limit
# GROUP_LIMIT. Kalshi trips it once MORE than that matches within
# GROUP_WINDOW_S (0 is refused) and cancels the rest inside its matching engine,
# so the pull needs no write of ours. A 1.1-lot sentinel trips it when it fills in full.
SENTINEL_LOTS = 1.1
GROUP_LIMIT = 1
GROUP_WINDOW_S = 15.0
GROUP_IDLE_S = 600.0                  # a group with nothing resting this long is deleted
GROUP_DELETES = 5                     # most deletes per cycle
LAYOUT_HYSTERESIS = 0.1
# Taker fee rate when a market's schedule can't be read (Kalshi's standard 0.07).
DEFAULT_TAKER_RATE = 0.07


@section
class Cfg:
    # ---- pricing: which markets, how much ----
    max_capital: float = param("Most dollars our fills may leave us holding (unexited positions at cost). A bid "
                               "that could take us past it isn't placed; after a fill, bids that could are "
                               "pulled. Resting bids hold no cash at Kalshi.", min=0.0, step=500.0, group="pricing")
    num_markets: int = param("Most markets quoted.", min=0, step=1, group="pricing")
    sentinel_keep: float = param("Use the sentinel layout on a side when it keeps at least this share of the "
                                 "touch layout's LIP. 11-04, 366 sides: thin touches (<200 ahead) keep ~97%, "
                                 "crowded ones (>1000) nothing.", min=0.0, max=1.0, step=0.05, group="pricing")
    min_lots_frac: float = param("Don't quote a side whose bid would be under this fraction of the market's "
                                 "Target size: LIP stays small until a bid reaches Target/5, where it sets the "
                                 "reference price (11-04: 1 to 49 lot bids earned ~$0).", min=0.0, max=1.0,
                                 step=0.05, group="pricing")
    min_ahead: float = param("Quote a side only where others already rest at least this many contracts at our "
                             "price (they are filled before us). 0 = may be alone.", min=0.0, step=100.0,
                             group="pricing")
    max_position: float = param("Bid size, and the most contracts we hold on a side: a side holding this many "
                                "isn't quoted. Size past Target still counts at the level where the book crosses "
                                "it, so it raises our share (narrow sides are capped by exit depth).", min=0.0,
                                step=100.0, group="pricing")
    wide_spread: float = param("Wide: quote both sides at the touch, Target size, when ask - bid is at least "
                               "this.", min=0.01, max=0.98, step=0.01, group="pricing", sub="wide markets")
    deep_keep: float = param("Wide: bid each side at the deepest price that keeps this share of its best LIP "
                             "(LIP scores every bid at or above the reference price in full, and on a thin book "
                             "our own size sets it; 11-05 T130: no at 6-7c scored ~84% of the side vs ~99% "
                             "at 57c). No sentinel there. 0 = always at the touch.", min=0.0, max=1.0, step=0.05,
                             group="pricing", sub="wide markets")
    min_leg_price: float = param("Wide and narrow: don't quote a side bid under this. A full fill on a 2-6c leg "
                                 "was an informed dump every time, sold back at 1c (ORAN, NMAN, IDS 11-04; "
                                 "DEEPSHARE, OPENSHARE 11-05).",
                                 min=0.0, max=0.5, step=0.01, group="pricing", sub="wide markets")
    decided_min: float = param("Decided: quote the expensive side, Target size, when its best bid is at least "
                               "this.", min=0.5, max=0.999, step=0.01, group="pricing", sub="decided markets")
    flow_days: float = param("Every quoted market must be low-flow over this many trailing days (public "
                             "trades, re-read hourly).", min=0.5, max=14.0, step=0.5, group="pricing",
                             sub="low flow (all markets)")
    max_trades_day: float = param("Never quote a market trading more than this per day (a market younger "
                                  "than flow_days over its own age); under it, LIP must pay for the expected "
                                  "loss (min_lip_to_loss). Out of sample (10 days predicting the next 4, "
                                  "10-22..11-05, 2,408 markets): above 50/day a 1000-lot touch bid lost "
                                  "~$68/day. Over 1000 trades in flow_days reads as busy.", min=0.0, step=1.0,
                                  group="pricing", sub="low flow (all markets)")
    loss_per_trade: float = param("Expected adverse-sweep loss, $/day per trade/day, for 1000 lots on each side "
                                  "(scaled by our bids' size). Same test: <=2 trades/day ~$0.5-1/day, 2-3 $3, "
                                  "3-10 $11-20, 10-50 $23-30.", min=0.0, step=0.5, group="pricing",
                                  sub="low flow (all markets)")
    loss_cap: float = param("Expected loss per market-day stops growing here (the 10-50 trades/day band "
                            "lost $23-30).", min=0.0, step=5.0, group="pricing", sub="low flow (all markets)")
    min_lip_to_loss: float = param("Quote a market only if its LIP/day is at least this many times its expected "
                                   "loss. Above 1 because the replay model understated our real fills' losses "
                                   "~1.5x (11-04/05).", min=0.0, step=0.25, group="pricing",
                                   sub="low flow (all markets)")
    skip_categories: str = param("Comma-separated series categories never quoted (11-01..05: Sports, "
                                 "Mentions ~50-60% safe).", group="pricing", sub="low flow (all markets)")
    exit_depth_frac: float = param("Neither wide nor decided: quote both touches only with exit liquidity, "
                                   "each bid at most this fraction of others' bids within exit_depth_slip "
                                   "under the touch (what a fill could be sold into), and at least "
                                   "min_lots_frac of Target.", min=0.05, max=10.0, step=0.1, group="pricing",
                                   sub="other markets: exit liquidity")
    exit_depth_slip: float = param("How far below the touch counts as exit depth (dollars).", min=0.0, max=0.5,
                                   step=0.01, group="pricing", sub="other markets: exit liquidity")
    # ---- exits ----
    adverse_lots: float = param("Fills of at least this many lots on one of our bids: sell up to "
                                "dump_slip_ticks under our fill at once, instead of waiting on an exit bid. "
                                "A risk cap, not a signal: 11-04 sweeps of 50 to 3000+ lots, and 0.5x to 10x+ "
                                "a market's median sweep, were all followed by a 2+ tick further fall "
                                "~21-24% of the time within 1 min.", min=1, max=10000, step=50, group="exits")
    dump_slip_ticks: int = param("After an informed fill, sell at once into bids down to this many ticks under "
                                 "our fill; rest the rest. 11-04: median 46 lots rest within 2 ticks under the "
                                 "touch.", min=0, max=50, step=1, group="exits")
    chase_ticks: int = param("Then sell more (again dump_slip_ticks deep) whenever the best bid is this many ticks "
                             "under our fill. 11-04: after a sweep the price fell 2+ ticks within 1 min ~22% "
                             "of the time on 10-90c markets, whatever its size.", min=1, max=50, step=1,
                             group="exits")
    max_chase_ticks: int = param("Never sell (dump or chase) more than this many ticks under our fill; below "
                                 "that the rest waits on a passive exit. 11-04 sweeps of 300+ lots: a further "
                                 "fall of up to ~5c in 10 s kept falling (holding lost 2-6c), one of 6c+ came "
                                 "back (holding gained 6-26c).", min=0, max=99, step=1, group="exits")
    event_pull_s: float = param("After an adverse fill or a sentinel trip, every market in its event is off this "
                                "long. 14-day tape (10-22..11-05): another market in the event was swept "
                                "adversely within 5 min of an adverse sweep 15% of the time vs 3% at random, "
                                "30 min 32% vs 17%, 1 h 43% vs 33% (FOC-40 then FOC-45, 23 min apart, 11-05).",
                                min=0.0, max=7200.0, step=300.0, group="exits")
    # ---- operations ----
    write_tokens_per_s: float = param("Our write budget (tokens/s; an order 10, a cancel 2) out of the 300 the "
                                      "shard shares with ARBS, which caps itself at 150-250. 11-04: our "
                                      "requotes need ~1/s; ARBS's last tokens are worth <$0.30/day each "
                                      "(its ranking at 125+ tokens/s covers every basket worth >$8/h, vs "
                                      "~$150/day realized). Spare goes to back-of-queue moves; exits and "
                                      "cancels always go.", min=1.0, max=50.0, step=1.0, group="operations")
    scan_markets: int = param("Books read each cycle: the markets our last read of them says pay us most per "
                              "dollar.", min=1, step=50, group="operations")
    scan_rotate: int = param("Plus this many more each cycle, rotating through every other LIP market (any "
                             "category), so each is re-read every programs/scan_rotate cycles.", min=0, step=100,
                             group="operations")
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
    layout: dict = dataclasses.field(default_factory=dict)   # side -> "touch" | "sentinel"
    big: dict = dataclasses.field(default_factory=dict)      # side -> price of the main bid
    loss_rate: float = 0.0             # expected adverse-sweep loss, $/s (expected_loss_day / 86400)


def expected_loss_day(trades_day: float, pl: Plan, cfg: Cfg) -> float:
    """Expected adverse-sweep loss, $/day, of quoting `pl` on a market trading
    `trades_day`: loss_per_trade per trade/day up to loss_cap, for 1000 lots
    on each side, scaled by the lots we actually bid (fit, max_trades_day)."""
    per_side = min(cfg.loss_per_trade * trades_day, cfg.loss_cap) / 2.0
    return per_side * pl.lots * len(pl.legs) / 1000.0


def key(ticker: str, side: str, hedge: bool = False) -> str:
    return f"{ticker}|{side}|h" if hedge else f"{ticker}|{side}"


def unkey(k: str) -> tuple[str, str]:
    parts = k.split("|")
    return parts[0], parts[1]


def is_hedge(k: str) -> bool:
    return k.endswith("|h")


def is_sentinel(k: str) -> bool:
    return k.endswith("|s")


def desired_orders(pl: Plan) -> dict[str, tuple[float, float, bool]]:
    """Order key -> (price, size, not a plain touch bid: a sentinel layout or a deep bid, never moved back)."""
    out = {}
    for s, p in pl.legs:
        if pl.layout.get(s) == "sentinel":
            out[f"{pl.ticker}|{s}|s"] = (p, SENTINEL_LOTS, True)
            out[key(pl.ticker, s)] = (pl.big[s], pl.lots, True)
        elif pl.layout.get(s) == "deep":
            out[key(pl.ticker, s)] = (p, pl.lots, True)          # not a touch bid: never moved back
        else:
            out[key(pl.ticker, s)] = (p, pl.lots, False)
    return out


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


def levels_multi(book: dict[float, float], ours) -> list[lip.LipLevel]:
    acc = {p: [s, 0.0] for p, s in book.items()}
    for price, size in ours:
        if size > 0:
            lv = acc.setdefault(round(price, 4), [0.0, 0.0])
            lv[0] += size
            lv[1] += size
    return sorted((lip.LipLevel(price=p, size=v[0], mine=v[1]) for p, v in acc.items()),
                  key=lambda lv: -lv.price)


def book_score(others: dict[str, dict[float, float]], legs, params: lip.LipParams) -> lip.LipBookScore:
    """Score with our `legs` ((side, price, size), ...; several per side allowed)
    added to others' books."""
    y = levels_multi(others["yes"], [(p, q) for s, p, q in legs if s == "yes"])
    n = levels_multi(others["no"], [(p, q) for s, p, q in legs if s == "no"])
    return lip.score_book(y, n, params)


def market_rate(others, legs, params) -> float:
    """Our $/s with `legs` ((side, price, size), ...) resting."""
    return book_score(others, legs, params).expected_reward_this_snapshot_usd


def plan_market(ticker: str, others: dict[str, dict[float, float]], params: lip.LipParams, cfg: Cfg,
                held: dict[str, float] | None = None, lots=None, tick=lambda p: 0.01,
                current: dict[str, str] | None = None, exit: dict[str, float] | None = None) -> Plan | None:
    """How to quote this market now, or None. Joins others' best bids (never
    improves them). `held`: contracts we already hold per side; `lots`: bid
    size, one number or per side, default max_position; a side sized under 1
    isn't quoted. Every quoted side
    gets the smallest of the quoted sides' sizes. A market neither wide nor
    decided ("narrow") is quoted only with `exit` (exit_lots), which then caps
    `lots`; without it, None. Flow is checked by the caller."""
    held = held or {}
    if lots is None:
        lots = cfg.max_position
    side_lots = lots if isinstance(lots, dict) else {"yes": lots, "no": lots}
    if not others["yes"] or not others["no"]:
        return None
    by, bn = max(others["yes"]), max(others["no"])
    hi_side, hi = ("yes", by) if by >= bn else ("no", bn)
    if hi >= cfg.decided_min - 1e-9:
        mode, legs = "decided", [(hi_side, hi)]
    elif 1.0 - by - bn >= cfg.wide_spread - 1e-9:
        mode, legs = "wide", [(s, p) for s, p in (("yes", by), ("no", bn)) if p >= cfg.min_leg_price - 1e-9]
    elif exit is not None:
        # Cheap narrow legs were swept like wide ones: DEEPSHARE 2c, OPENSHARE 4c, 1000 lots each (11-05 16:38).
        mode, legs = "narrow", [(s, p) for s, p in (("yes", by), ("no", bn)) if p >= cfg.min_leg_price - 1e-9]
        side_lots = {s: min(side_lots.get(s, 0.0), exit.get(s, 0.0)) for s in ("yes", "no")}
    else:
        return None
    # A side we already hold more than dust of is being exited: bidding there adds to a position a
    # sweep just handed us (OPENSHARE 11-05: +1000 at 4c on 797 held from 11c).
    legs = [(s, p) for s, p in legs
            if others[s].get(p, 0.0) >= cfg.min_ahead - 1e-9 and held.get(s, 0.0) <= DUST_LOTS
            and held.get(s, 0.0) < cfg.max_position - 1e-9
            and side_lots.get(s, 0.0) >= max(1.0, cfg.min_lots_frac * params.target_size)]
    if not legs:
        return None
    lots = min(side_lots[s] for s, _ in legs)
    sized = [(s, p, lots) for s, p in legs]
    # A side we quote must reach Target with our lots, one we don't from others
    # alone: otherwise the snapshot is excluded and pays nobody.
    sc = book_score(others, sized, params)
    if not sc.qualifies or sc.expected_reward_this_snapshot_usd <= 0:
        return None
    layout, big = {s: "touch" for s, _ in legs}, {s: p for s, p in legs}
    # Wide: as deep as keeps deep_keep of the side's best LIP. No sentinel there: a sweep that
    # reaches a deep bid comes as one order and fills both in one match, and the event's order
    # group already pulls the rest on the bid's own fill.
    deep = {}
    if mode == "wide" and cfg.deep_keep > 0:
        for s, p in legs:
            d = deepest_price(s, p, others, [(x, px, lots) for x, px in legs if x != s], lots, params, cfg, tick)
            if d is not None and d < p - 1e-9:
                deep[s] = d
        for s, d in deep.items():
            layout[s], big[s] = "deep", d
            sized = [(x, px, q) for x, px, q in sized if x != s] + [(s, d, lots)]
            legs = [(x, d if x == s else px) for x, px in legs]
    # Per side, the sentinel layout where it keeps sentinel_keep of the touch layout's LIP.
    base = sc.expected_reward_this_snapshot_usd
    for s, p in legs:
        if s in deep:
            continue
        deeper = round(p - tick(p), 4)
        if deeper < 0.01:
            continue
        trial = [(x, px, lots) for x, px in legs if x != s] + [(s, p, SENTINEL_LOTS), (s, deeper, lots)]
        r = book_score(others, trial, params)
        # Hysteresis: a side keeps its current layout unless the other wins by
        # LAYOUT_HYSTERESIS (scores near the line flipped it every pass, 11-04).
        keep = cfg.sentinel_keep - (LAYOUT_HYSTERESIS if (current or {}).get(s) == "sentinel" else -LAYOUT_HYSTERESIS
                                    if (current or {}).get(s) == "touch" else 0.0)
        if r.qualifies and r.expected_reward_this_snapshot_usd >= keep * base - 1e-15:
            layout[s], big[s] = "sentinel", deeper
            sized = [(x, px, q) for x, px, q in sized if x != s] + [(s, p, SENTINEL_LOTS), (s, deeper, lots)]
    final = book_score(others, sized, params)
    return Plan(ticker, mode, tuple(legs), final.expected_reward_this_snapshot_usd,
                lots * sum(big.values()), params.period_reward_usd / params.snapshots_per_period, lots,
                layout, big)


def deepest_price(s: str, touch: float, others, rest: list, lots: float, params, cfg: Cfg, tick) -> float | None:
    """The lowest price on side `s` where `lots` keep deep_keep of the best LIP
    available from the touch down. The score only changes at others' levels, so
    the candidates are the touch, each lower level (joining it) and one tick
    above each (alone), and the lowest price."""
    lower = sorted((q for q in others[s] if q < touch - 1e-9), reverse=True)
    cands = {touch, 0.01}
    for lv in lower:
        cands.add(lv)
        up = round(lv + tick(lv), 4)
        if up < touch - 1e-9:
            cands.add(up)
    # This side's own LIP: the market's with our bid here, less without it. Comparing the whole
    # market's let the other side's LIP carry a bid down to 1c where it earned nothing (11-05).
    base = book_score(others, rest, params) if rest else None
    base = base.expected_reward_this_snapshot_usd if base is not None and base.qualifies else 0.0
    scored = []
    for c in sorted(cands, reverse=True):
        r = book_score(others, rest + [(s, c, lots)], params)
        if r.qualifies:
            scored.append((c, r.expected_reward_this_snapshot_usd - base))
    if not scored:
        return None
    best = max(v for _, v in scored)
    if best <= 0:
        return None
    return min(c for c, v in scored if v >= cfg.deep_keep * best - 1e-15)


def exit_lots(others: dict[str, dict[float, float]], cfg: Cfg, target: float) -> dict[str, float]:
    """Bid size per side: the program's `target`, capped by that side's exit depth, others'
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
        out[s] = min(target, math.floor(cfg.exit_depth_frac * d))
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
    with cf.ThreadPoolExecutor(1) as ex:
        for ms in ex.map(one, chunks):
            for m in ms:
                out[m["ticker"]] = m
    return out


def fetch_flow(t: str, days: float, opened: float | None = None) -> dict | None:
    """{trades_day, days, at} over the trailing `days`, one read;
    a market `opened` (epoch s) more recently is rated over its own age, at
    least a day. A full page (1000 trades) means busier than any sane limit:
    counted as inf."""
    span = days if opened is None else min(days, max(1.0, (time.time() - opened) / 86400))
    try:
        d = read("/trade-api/v2/markets/trades",
                 params={"ticker": t, "min_ts": int(time.time() - days * 86400), "limit": 1000})
    except Exception:
        return None
    tr = d.get("trades") or []
    if d.get("cursor"):
        return {"trades_day": math.inf, "days": days, "at": time.time()}
    return {"trades_day": len(tr) / span, "days": days, "at": time.time()}


def load_categories() -> dict[str, dict]:
    """Series ticker -> {category, title}."""
    d = read("/trade-api/v2/series", params={"include_product_metadata": "false"})
    return {x["ticker"]: {"category": x.get("category") or "", "title": x.get("title") or ""}
            for x in d.get("series") or []}


def kalshi_url(ticker: str, event: str, series_title: str = "") -> str:
    """The market's page on kalshi.com (series / title slug / event); the ticker
    rides in the fragment so the dashboard can show it as the link text."""
    ser = event.split("-")[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", series_title.lower()).strip("-") or ser
    return f"https://kalshi.com/markets/{ser}/{slug}/{event.lower()}#{ticker}"


def refresh_job(held: set[str]) -> tuple[dict, dict, dict, dict | None]:
    """(programs, their markets' records, basket index, series categories): the
    slow refresh, for a worker thread."""
    programs = load_programs()
    meta = fetch_meta(set(programs) | held)
    try:
        baskets = basket_index(set(programs) | held, arbs_held_events())
    except Exception:
        traceback.print_exc()
        baskets = None
    try:
        cats = load_categories()
    except Exception:
        traceback.print_exc()
        cats = None
    return programs, meta, baskets, cats


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


def remove_ours(book: dict[float, float], price: float, size: float) -> dict[float, float]:
    out = dict(book)
    p = round(price, 4)
    if p in out:
        out[p] = max(out[p] - size, 0.0)
    return {k: v for k, v in out.items() if v > 1e-9}
