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
ranked by LIP per dollar of resting capital, up to `max_capital`.

A fill gets an exit within about FILL_POLL_S: a bid of our net position's
size rests at the other side's touch (Kalshi nets a yes+no pair to $1), at
any price -- getting out comes before the pair's profit. It follows the touch,
is never pulled by the guard, and counts toward max_capital. The market's LIP
bids keep running beside it up to max_position.

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

ARBS: live, every ticker we want, rest on or hold goes in claimed.json (with
our positions). ARBS reads it every second: it neither takes nor quotes a
claimed ticker and pulls its quotes there, its sweep skips our orders, and its
position audit takes our positions off Kalshi's. We quote CLAIM_SETTLE_S after
claiming. Events where ARBS holds a basket are never claimed: it may have to
take there to complete one.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import os
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
from kalshi_infra.trading import lip

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CID_PREFIX = "lipf-"
BOOK_WORKERS = 8
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
# once per MOVE_MIN_S, at most MAX_MOVES per pass: <= 5 * 12 / 5 s = 12
# tokens/s, 4% of the bucket, and only while MOVE_RESERVE of it is spare.
GUARD_S = 5.0
# A market the guard pulled isn't re-quoted for this long: books flickering
# across a threshold had the 30 s cycle re-place into the next flicker
# (STL, JOHN 11-04 19:19).
GUARD_COOLDOWN_S = 300.0
MOVE_MIN_S = 10.0
MOVE_BEHIND_FRAC = 0.2
MAX_MOVES = 5
# Fills are polled this often between guard passes; a new one is exited at once.
FILL_POLL_S = 1.0


@section
class Cfg:
    max_capital: float = param("Most dollars resting in our bids across all markets (each bid costs lots x "
                               "price). Markets are taken best LIP per dollar first.", min=0.0, step=500.0)
    num_markets: int = param("Most markets quoted.", min=0, step=1)
    lots: float = param("Contracts per bid (one bid per quoted side).", min=1.0, step=100.0)
    wide_spread: float = param("Wide: quote both sides at the touch when ask - bid is at least this.",
                               min=0.01, max=0.98, step=0.01)
    decided_min: float = param("Decided: quote the expensive side when its best bid is at least this.",
                               min=0.5, max=0.999, step=0.01)
    min_ahead: float = param("Quote a side only where others already rest at least this many contracts at our "
                             "price (they are filled before us). 0 = may be alone.", min=0.0, step=100.0)
    max_position: float = param("Stop quoting a side once we hold this many contracts of it.", min=0.0,
                                step=100.0)
    scan_markets: int = param("Books read each cycle: the largest LIP pools.", min=1, step=50)
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
    capital: float                     # dollars those bids tie up
    pool_rate: float                   # whole program's $/s


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
                held: dict[str, float] | None = None) -> Plan | None:
    """How to quote this market now, or None. Joins others' best bids (never
    improves them). `held`: contracts we already hold per side."""
    held = held or {}
    if not others["yes"] or not others["no"]:
        return None
    by, bn = max(others["yes"]), max(others["no"])
    hi_side, hi = ("yes", by) if by >= bn else ("no", bn)
    if hi >= cfg.decided_min - 1e-9:
        mode, legs = "decided", [(hi_side, hi)]
    elif 1.0 - by - bn >= cfg.wide_spread - 1e-9:
        mode, legs = "wide", [("yes", by), ("no", bn)]
    else:
        return None
    legs = [(s, p) for s, p in legs
            if others[s].get(p, 0.0) >= cfg.min_ahead - 1e-9 and held.get(s, 0.0) < cfg.max_position - 1e-9]
    if not legs:
        return None
    sized = [(s, p, cfg.lots) for s, p in legs]
    # A side we quote must reach Target with our lots, one we don't from others
    # alone: otherwise the snapshot is excluded and pays nobody.
    sc = book_score(others, sized, params)
    if not sc.qualifies or sc.expected_reward_this_snapshot_usd <= 0:
        return None
    return Plan(ticker, mode, tuple(legs), sc.expected_reward_this_snapshot_usd,
                cfg.lots * sum(p for _, p in legs), params.period_reward_usd / params.snapshots_per_period)


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
        return kalshi_get(f"/trade-api/v2/markets/{t}/orderbook").get("orderbook_fp", {})
    except Exception:
        return None


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
            data = kalshi_get("/trade-api/v2/markets", params={"tickers": ",".join(tickers[i:i + 100]),
                                                               "limit": 1000})
            for m in data.get("markets", []):
                self.meta[m["ticker"]] = m

    def shard(self, ticker: str) -> int:
        if ticker not in self.meta:
            self._fetch_meta([ticker])
        return int(self.meta[ticker].get("exchange_index") or 0)

    def refresh_programs(self, cfg: Cfg):
        out, cursor = {}, None
        now = pd.Timestamp.now(tz="UTC")
        while True:
            params = {"status": "active", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = kalshi_get("/trade-api/v2/incentive_programs", params=params, auth=True)
            batch = data.get("incentive_programs", [])
            for p in batch:
                if p.get("incentive_type") == "liquidity" and pd.Timestamp(p["end_date"]) > now:
                    out[p["market_ticker"]] = p
            cursor = data.get("next_cursor") or data.get("cursor")
            if not cursor or not batch:
                break
        self.programs = out
        self._fetch_meta(self.candidates(cfg))
        self.last_programs = time.time()

    def candidates(self, cfg: Cfg) -> list[str]:
        """Top `scan_markets` by pool $/s, plus every market we rest in."""
        def pool(p):
            P = lip.params_from_program(p)
            return P.period_reward_usd / P.snapshots_per_period
        top = sorted(self.programs, key=lambda t: -pool(self.programs[t]))[:cfg.scan_markets]
        return list(dict.fromkeys(top + sorted(self.order_tickers())))

    def others_of(self, t: str, ob: dict) -> dict[str, dict[float, float]]:
        """Others' books: the REST book includes our live bids (a shadow one isn't there)."""
        yes, no = parse_book(ob)
        others = {"yes": yes, "no": no}
        if self.live:
            for side, o in self.legs_of(t):
                others[side] = remove_ours(others[side], o["price"], o["remaining"])
        return others

    def our_rate(self, t: str, others, P) -> float:
        legs = [(s, o["price"], o["remaining"]) for s, o in self.legs_of(t)]
        return market_rate(others, legs, P) if legs else 0.0

    def scan(self, cfg: Cfg):
        tickers = self.candidates(cfg)
        with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
            got = list(zip(tickers, ex.map(fetch_book, tickers)))
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
        self.books, self.plans, self.rates = books, plans, rates

    def sync_live_orders(self):
        """Drop journaled orders Kalshi no longer has resting (filled, or the market closed)."""
        if not self.live or not self.state["orders"]:
            return
        c = self.client(0)
        resting = {oid for oid, _, _ in c._resting("")}
        for k, o in list(self.state["orders"].items()):
            if o["oid"] not in resting:
                del self.state["orders"][k]

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
            c.allow_now("cancel", force=True, reason=reason)
            try:
                c.cancel(o["oid"], t)
            except Exception as e:
                print(f"cancel {k}: {e}")

    def place(self, t: str, side: str, price: float, lots: float, reserve: float = PLACE_RESERVE,
              reason: str = "lipfarm:quote", hedge: bool = False) -> bool:
        if self.live:
            c = self.client(self.shard(t))
            if not (c.can_afford("order", reserve=reserve * c.tokens_per_s) and c.allow_now("order", reason=reason)):
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
        else:
            oid = "shadow-" + uuid.uuid4().hex[:12]
        self.state["orders"][key(t, side, hedge)] = {"oid": oid, "side": side, "price": price, "size": lots,
                                                     "remaining": lots, "placed": time.time()}
        return True

    def hedge(self, cfg: Cfg):
        """Rest a bid of our net position's size at the other side's touch
        (just under its offer, never crossing). Live only."""
        if not self.live:
            return
        pos, want = self.signed_positions(hedged_only=True), {}
        for t, v in pos.items():
            others = self.books.get(t)
            if not others or t not in self.meta:
                continue
            held, side = ("yes", "no") if v > 0 else ("no", "yes")
            touch = max(others[side]) if others[side] else 0.01
            ask = 1.0 - max(others[held]) if others[held] else 1.0
            px = min(touch, round(ask - 0.01, 4))
            if px >= 0.01:
                want[key(t, side, True)] = (px, abs(v))
        for k, o in list(self.state["orders"].items()):
            if not is_hedge(k):
                continue
            w = want.get(k)
            if (w is None or abs(w[0] - o["price"]) > 1e-9 or o["remaining"] > w[1] + 1e-9
                    or o["remaining"] < 0.5 * w[1]):
                self.cancel(k, reason="lipfarm:hedge")
        for k, (px, q) in want.items():
            if k not in self.state["orders"]:
                t, side = unkey(k)
                if self.place(t, side, px, q, reserve=0.0, reason="lipfarm:hedge", hedge=True):
                    print(f"hedge: {t} {side}@{px} x{q:.0f}")

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
        return plan_market(t, others, P, cfg, {"yes": max(v, 0.0), "no": max(-v, 0.0)})

    def guard(self, cfg: Cfg):
        """Live, between cycles: re-read each quoted market's book; pull a bid
        its plan no longer has, and move one with size behind it to the back.
        A price change is left to the next cycle."""
        if not self.live:
            return
        self.poll_fills()
        tickers = sorted(self.order_tickers() | set(self.signed_positions()))
        if not tickers:
            return
        with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
            got = dict(zip(tickers, ex.map(fetch_book, tickers)))
        pos, keep = self.signed_positions(), {}
        for t in tickers:
            ob = got.get(t)
            if ob is None or t not in self.programs:
                continue
            self.books[t] = self.others_of(t, ob)
        self.hedge(cfg)                     # exits first
        for t in tickers:
            others = self.books.get(t)
            if got.get(t) is None or t not in self.programs or t not in self.order_tickers():
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
            if moves >= MAX_MOVES or not c.can_afford("order", reserve=MOVE_RESERVE * c.tokens_per_s
                                                      + c.COST["cancel"]):
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
        out, spent = {}, self.hedge_capital()
        for p in sorted(cands, key=lambda p: (rank(p), -per_dollar(p))):
            if len(out) >= cfg.num_markets:
                break
            if spent + p.capital <= cfg.max_capital + 1e-9:
                out[p.ticker] = p
                spent += p.capital
        return out

    def reconcile(self, cfg: Cfg):
        want = self.choose(cfg)
        if self.live:
            if self.write_claims(want):
                time.sleep(CLAIM_SETTLE_S)
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
        legs = {key(t, s): p for t, pl in want.items() for s, p in pl.legs}
        for k, o in list(self.state["orders"].items()):
            if is_hedge(k):
                continue
            px = legs.get(k)
            stale = (px is None or abs(px - o["price"]) > 1e-9 or abs(o["size"] - cfg.lots) > 1e-9
                     or o["remaining"] < 0.5 * cfg.lots or not ready(unkey(k)[0]))
            if stale:
                self.cancel(k)
        for k, px in legs.items():
            t, side = unkey(k)
            if k not in self.state["orders"] and ready(t):
                self.place(t, side, px, cfg.lots)
        for t, pl in want.items():
            if t in self.order_tickers():
                self.rates.setdefault(t, pl.rate)
        self.hedge(cfg)

    def poll_fills(self) -> set[str]:
        """Book new fills of our orders; returns the tickers that had any."""
        if not self.live or not self.clients:
            return set()
        new: set[str] = set()
        ours = {o["oid"]: k for k, o in self.state["orders"].items()}
        known = set(self.state.get("all_oids", []))
        known |= set(ours)
        self.state["all_oids"] = sorted(known)
        seen = set(self.state["trade_ids"])
        c = next(iter(self.clients.values()))
        # Overlapping windows, deduped on trade_id: polled every FILL_POLL_S.
        since = self.state.get("fills_since", self.state["t0"]) - 120
        self.state["fills_since"] = time.time()
        for f in c.fills("", min_ts=int(since)):
            if f["order_id"] not in known or f["trade_id"] in seen:
                continue
            seen.add(f["trade_id"])
            self.state["fills"].append(f)
            new.add(f["ticker"])
            k = ours.get(f["order_id"])
            if k:
                self.state["orders"][k]["remaining"] = max(self.state["orders"][k]["remaining"] - f["size"], 0.0)
        self.state["trade_ids"] = sorted(seen)
        return new

    def exit_now(self, cfg: Cfg):
        """Between guard passes: on a new fill, read that market's book and
        rest its exit at once."""
        new = self.poll_fills() if self.live else set()
        if not new:
            return
        with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
            for t, ob in zip(sorted(new), ex.map(fetch_book, sorted(new))):
                if ob is not None:
                    self.books[t] = self.others_of(t, ob)
        self.hedge(cfg)

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
        if pos:
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

    def report(self, cfg: Cfg):
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
                   "max_capital_usd": cfg.max_capital,
                   "waiting_on_arbs": len(self.waiting), "arbs_events_skipped": len(self.held_events),
                   "updated_at": time.time()}
        tmp = os.path.join(ROOT, "status.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"summary": summary, "quotes": rows, "positions": pos, "waiting": self.waiting}, f, indent=1)
        os.replace(tmp, os.path.join(ROOT, "status.json"))

        print(f"\n=== LIPFARM {summary['mode']} {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S}Z "
              f"| {hrs:.2f}h | quoting {len(rows)} | now ${summary['our_usd_day_now']:.0f}/day "
              f"| exp LIP ${tot_lip:.2f} | fills {summary['fills']} pnl ${fill_pnl:+.2f} "
              f"| net ${summary['net_usd']:+.2f} | resting ${at_risk:.0f} of ${cfg.max_capital:.0f}")
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
        modes, stop = TradingModeWatcher(NAME), StopWatcher()
        if arm:
            self.set_live(True, arm)
        try:
            while True:
                cfg = Cfg.load()
                t0 = time.time()
                try:
                    reason = stop.poll()
                    if reason:
                        print(f"STOP: {reason}")
                        break
                    want = modes.poll()
                    if want is not None:
                        self.set_live(want, f"dashboard {modes.origin()}")
                    if time.time() - self.last_programs > cfg.programs_refresh_s:
                        self.refresh_programs(cfg)
                    self.poll_fills()
                    self.sync_live_orders()
                    self.accrue()
                    self.scan(cfg)
                    self.reconcile(cfg)
                    self.report(cfg)
                    self._save()
                except Exception:
                    traceback.print_exc()
                last_guard = time.time()
                while time.time() - t0 < cfg.refresh_s - 1e-3:
                    time.sleep(max(min(FILL_POLL_S, cfg.refresh_s - (time.time() - t0)), 0.05))
                    try:
                        if time.time() - last_guard >= GUARD_S and time.time() - t0 < cfg.refresh_s - 1.0:
                            last_guard = time.time()
                            self.guard(cfg)
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
