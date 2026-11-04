"""
LIP farmer. Rests one post-only bid of `lots` on the CHEAP side's touch
(best bid <= `max_price`) of the `num_markets` LIP markets whose reward to
us, at today's book, is highest in $/s. Never quotes the expensive side.

    python -m kalshi_strategies.LIPFARM.farm                # shadow until armed from the dashboard
    python -m kalshi_strategies.LIPFARM.farm --live         # armed from the start: REAL MONEY
    python -m kalshi_strategies.LIPFARM.farm --resume-arm   # re-arm if the last request was an arm (EC2 unit)

Arm/disarm is the dashboard's button (trading_mode.json, as for every
series); STOP.json disarms and exits. Params in config.json (dashboard
sidebar, re-read every cycle). Journal in data/live/LIPFARM/<mode>/state.json
survives restarts; data/live/LIPFARM/status.json holds the latest table. "Expected LIP" is our modelled share
(lip.py) integrated over time, not what Kalshi pays: payouts under $1 per
market-period are dropped, and others' size between our scans is unseen.

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
# Between cycles, every GUARD_S each quote's book is re-read: a quote the
# filters would no longer place is pulled, and one with MOVE_BEHIND_FRAC *
# lots joined behind it is re-placed at the back (cancel + create, 12 tokens;
# LIP has no time priority, so the back only sheds fills). Per market at most
# once per MOVE_MIN_S, at most MAX_MOVES per pass: <= 5 * 12 / 5 s = 12
# tokens/s, 4% of the bucket, and only while MOVE_RESERVE of it is spare.
GUARD_S = 5.0
# A market the guard pulled isn't re-quoted for this long: its offer was
# flickering in and out of min_spread, and the 30 s cycle re-placed it into
# the next flicker (STL, JOHN 11-04 19:19).
GUARD_COOLDOWN_S = 300.0
MOVE_MIN_S = 10.0
MOVE_BEHIND_FRAC = 0.2
MAX_MOVES = 5


@section
class Cfg:
    num_markets: int = param("Markets quoted: the ones paying us most LIP per second at today's book.",
                             min=0, step=1)
    lots: float = param("Contracts per quote, one quote per market at the cheap side's best bid.",
                        min=1.0, step=100.0)
    max_price: float = param("Cheap side only: quote a side only if its best bid is at most this.",
                             min=0.001, max=0.5, step=0.01)
    min_spread: float = param("Quote a side only if its offer is at least this far above our bid: a 1c offer "
                              "over a 1c bid gets sold into (11-04, 108 picks: fills cost 1.2-1.8x the LIP "
                              "under 4c of spread, 0.6x from 4c).", min=0.0, max=0.5, step=0.01)
    min_ahead: float = param("Quote only where others already rest at least this many contracts at our "
                             "price (they are filled before us). 0 = may be alone.", min=0.0, step=100.0)
    max_position: float = param("Stop quoting a market once we hold this many contracts in it (refills on a "
                                "dumped market only add losers).", min=0.0, step=100.0)
    switch_margin: float = param("Stickiness: once a quoted market has passed Kalshi's $1 minimum payout "
                                 "this period, a new one replaces it only if it pays this much more "
                                 "(0.5 = 50% more $/s). Below $1 it is never replaced.", min=0.0, step=0.1)
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
class Pick:
    ticker: str
    side: str
    price: float
    rate: float        # our $/s with `lots` resting at price
    pool_rate: float   # whole program's $/s


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


def rate_at(others: dict[str, dict[float, float]], side: str, price: float, size: float,
            params: lip.LipParams) -> float:
    """Our $/s with `size` at `price` on `side`, given others' books."""
    y = levels(others["yes"], price, size) if side == "yes" else levels(others["yes"])
    n = levels(others["no"], price, size) if side == "no" else levels(others["no"])
    return lip.reward_rate(y, n, params)


def best_pick(ticker: str, others: dict[str, dict[float, float]], params: lip.LipParams,
              lots: float, max_price: float, min_spread: float = 0.0, min_ahead: float = 0.0) -> Pick | None:
    """Best cheap-side touch for `lots`, or None. Joins others' best bid (never
    improves it); on an empty side, bids 0.01. The side's offer must be at
    least `min_spread` above that bid, and others must rest `min_ahead` there."""
    best = None
    for side, opp in (("yes", "no"), ("no", "yes")):
        book = others[side]
        price = max(book) if book else 0.01
        ask = 1.0 - max(others[opp]) if others[opp] else 1.0
        if (price > max_price + 1e-9 or ask - price < max(min_spread, 1e-9) - 1e-9
                or book.get(price, 0.0) < min_ahead - 1e-9):
            continue
        r = rate_at(others, side, price, lots, params)
        if r > 0 and (best is None or r > best.rate):
            best = Pick(ticker, side, price, r, params.period_reward_usd / params.snapshots_per_period)
    return best


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
        self.picks: dict[str, Pick] = {}
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
        self.rates: dict[str, float] = {}       # ticker -> our current $/s for the resting order
        self.last_accrue = time.time()

    def set_live(self, want: bool, why: str):
        """Switch shadow <-> live. Disarming pulls every real quote; positions are kept."""
        self.arm_requested = want
        if want == self.live:
            return
        print(f"{'ARM' if want else 'DISARM'}: {why}")
        self.accrue()
        for t in list(self.state["orders"]):
            self.cancel(t)
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
                return json.load(f)
        return {"t0": time.time(), "orders": {}, "fills": [], "trade_ids": [], "accrued": {}}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

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
        """Top `scan_markets` by pool $/s, plus every market we rest or hold in."""
        def pool(p):
            P = lip.params_from_program(p)
            return P.period_reward_usd / P.snapshots_per_period
        top = sorted(self.programs, key=lambda t: -pool(self.programs[t]))[:cfg.scan_markets]
        return list(dict.fromkeys(top + list(self.state["orders"])))

    def scan(self, cfg: Cfg):
        tickers = self.candidates(cfg)

        def one(t):
            try:
                return t, kalshi_get(f"/trade-api/v2/markets/{t}/orderbook").get("orderbook_fp", {})
            except Exception:
                return t, None
        with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
            got = list(ex.map(one, tickers))
        picks, rates, books = {}, {}, {}
        self.held_events = arbs_held_events()
        pos = self.signed_positions()
        for t, ob in got:
            if ob is None or t not in self.programs:
                continue
            yes, no = parse_book(ob)
            others = {"yes": yes, "no": no}
            mine = self.state["orders"].get(t)
            # The REST book includes our live order; a shadow one isn't there.
            if mine and self.live:
                others[mine["side"]] = remove_ours(others[mine["side"]], mine["price"], mine["remaining"])
            books[t] = others
            m = self.meta.get(t, {})
            if m and m.get("status") != "active":
                continue
            P = self.params(t)
            if mine:
                rates[t] = rate_at(others, mine["side"], mine["price"], mine["remaining"], P)
            pk = self.pick(t, others, P, cfg, pos)
            if pk:
                picks[t] = pk
        self.books, self.picks, self.rates = books, picks, rates

    def sync_live_orders(self):
        """Drop journaled orders Kalshi no longer has resting (filled, or the market closed)."""
        if not self.live or not self.state["orders"]:
            return
        c = self.client(0)
        resting = {oid for oid, _, _ in c._resting("")}
        for t, o in list(self.state["orders"].items()):
            if o["oid"] not in resting:
                del self.state["orders"][t]

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

    def cancel(self, ticker: str, reason: str = "lipfarm:cancel"):
        o = self.state["orders"].pop(ticker, None)
        if o and self.live:
            c = self.client(self.shard(ticker))
            c.allow_now("cancel", force=True, reason=reason)
            try:
                c.cancel(o["oid"], ticker)
            except Exception as e:
                print(f"cancel {ticker}: {e}")

    def place(self, pk: Pick, lots: float, reserve: float = PLACE_RESERVE, reason: str = "lipfarm:quote") -> bool:
        if self.live:
            c = self.client(self.shard(pk.ticker))
            if not (c.can_afford("order", reserve=reserve * c.tokens_per_s) and c.allow_now("order", reason=reason)):
                return False
            order = Order(order_id=0, ticker=pk.ticker, side=pk.side, order_type="limit",
                          price=pk.price, size=lots, decision_time=pd.Timestamp.now(tz="UTC"),
                          intent="quote")
            order.client_order_id = CID_PREFIX + uuid.uuid4().hex[:24]
            try:
                ack = c.place_resting(order)
            except Exception as e:
                print(f"place {pk.ticker} {pk.side}@{pk.price}: {e}")
                return False
            oid = ack.exchange_order_id
        else:
            oid = "shadow-" + uuid.uuid4().hex[:12]
        self.state["orders"][pk.ticker] = {"oid": oid, "side": pk.side, "price": pk.price,
                                           "size": lots, "remaining": lots, "placed": time.time()}
        self.rates[pk.ticker] = pk.rate
        return True

    def params(self, t: str) -> lip.LipParams:
        m = self.meta.get(t, {})
        return dataclasses.replace(lip.params_from_program(self.programs[t]), price_ranges=tuple(
            (float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or []))

    def pick(self, t: str, others: dict, P: lip.LipParams, cfg: Cfg, pos: dict[str, float]) -> Pick | None:
        """best_pick under every filter, or None (ARBS-held event, position cap)."""
        if event_of(t) in self.held_events:
            return None
        pk = best_pick(t, others, P, cfg.lots, cfg.max_price, cfg.min_spread, cfg.min_ahead)
        if pk and (pos.get(t, 0.0) if pk.side == "yes" else -pos.get(t, 0.0)) >= cfg.max_position - 1e-9:
            return None
        return pk

    def guard(self, cfg: Cfg):
        """Live, between cycles: re-read each quote's book; pull a quote the
        filters would no longer place, and move one with size behind it to the
        back. A price change is left to the next cycle."""
        orders = dict(self.state["orders"])
        if not self.live or not orders:
            return
        with cf.ThreadPoolExecutor(BOOK_WORKERS) as ex:
            got = dict(zip(orders, ex.map(fetch_book, orders)))
        pos, keep = self.signed_positions(), {}
        for t, o in orders.items():
            ob = got.get(t)
            if ob is None or t not in self.programs:
                continue
            yes, no = parse_book(ob)
            others = {"yes": yes, "no": no}
            others[o["side"]] = remove_ours(others[o["side"]], o["price"], o["remaining"])
            P = self.params(t)
            pk = self.pick(t, others, P, cfg, pos)
            if pk is None or pk.side != o["side"]:
                print(f"guard: pull {t} {o['side']}@{o['price']} (fails the filters now)")
                self.cancel(t, reason="lipfarm:guard")
                self.state.setdefault("cooldown", {})[t] = time.time() + GUARD_COOLDOWN_S
                continue
            self.books[t] = others
            self.rates[t] = rate_at(others, o["side"], o["price"], o["remaining"], P)
            if abs(pk.price - o["price"]) < 1e-9:
                keep[t] = (pk, others)
        now = time.time()
        due = {t: v for t, v in keep.items() if now - orders[t]["placed"] >= MOVE_MIN_S}
        if not due:
            return
        c = self.client(0)
        try:
            ahead = queue_ahead(c, list(due))
        except Exception as e:
            print(f"queue positions: {e}")
            return
        moves = 0
        for t, (pk, others) in sorted(due.items(), key=lambda kv: orders[kv[0]]["placed"]):
            o = orders[t]
            a = ahead.get(o["oid"])
            if a is None or others[o["side"]].get(round(o["price"], 4), 0.0) - a < MOVE_BEHIND_FRAC * cfg.lots:
                continue
            c = self.client(self.shard(t))
            if moves >= MAX_MOVES or not c.can_afford("order", reserve=MOVE_RESERVE * c.tokens_per_s
                                                      + c.COST["cancel"]):
                return
            self.cancel(t, reason="lipfarm:move")
            if self.place(pk, cfg.lots, reserve=0.0, reason="lipfarm:move"):
                moves += 1

    def signed_positions(self) -> dict[str, float]:
        """Signed YES contracts (negative = long NO) from our fills, unsettled markets only."""
        out: dict[str, float] = {}
        for f in self.state["fills"]:
            if self.meta.get(f["ticker"], {}).get("result"):
                continue
            out[f["ticker"]] = out.get(f["ticker"], 0.0) + (f["size"] if f["side"] == "yes" else -f["size"])
        return {t: v for t, v in out.items() if abs(v) > 1e-9}

    def write_claims(self, want: dict[str, Pick]) -> bool:
        """Claim what we want, rest on, or hold; keep each ticker's first-claim
        time, which the placement gate waits on. True if anything new was claimed."""
        pos = self.signed_positions()
        now = time.time()
        old = self.state.setdefault("claims", {})
        claims = {t: old.get(t, now) for t in set(want) | set(self.state["orders"]) | set(pos)}
        self.state["claims"] = claims
        os.makedirs(os.path.dirname(CLAIMS_PATH), exist_ok=True)
        tmp = CLAIMS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"written_at": now, "tickers": sorted(claims), "positions": pos}, f)
        os.replace(tmp, CLAIMS_PATH)
        return any(t not in old for t in claims)

    def period_accrued(self, t: str) -> float:
        return self.state.setdefault("period_accrued", {}).get(self.programs[t]["id"], 0.0)

    def choose(self, cfg: Cfg) -> dict[str, Pick]:
        """The markets to quote. Only ones that can still reach the $1 payout
        before their period ends; a quoted one under $1 this period is kept
        (dropping it forfeits what it accrued); past $1 it keeps its place
        unless a newcomer pays `switch_margin` more."""
        now = time.time()

        def left(t):
            return max(pd.Timestamp(self.programs[t]["end_date"]).timestamp() - now, 0.0)
        cool = self.state.setdefault("cooldown", {})
        cands = [p for p in self.picks.values() if p.ticker in self.programs and cool.get(p.ticker, 0.0) <= now
                 and self.period_accrued(p.ticker) + p.rate * left(p.ticker) >= LIP_MIN_PAYOUT_USD]
        held = set(self.state["orders"])
        locked = sorted((p for p in cands if p.ticker in held and self.period_accrued(p.ticker) < LIP_MIN_PAYOUT_USD),
                        key=lambda p: -p.rate)
        rest = sorted((p for p in cands if p not in locked),
                      key=lambda p: -p.rate * (1.0 + cfg.switch_margin if p.ticker in held else 1.0))
        return {p.ticker: p for p in (locked + rest)[:cfg.num_markets]}

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
        for t, o in list(self.state["orders"].items()):
            pk = want.get(t)
            stale = (pk is None or pk.side != o["side"] or abs(pk.price - o["price"]) > 1e-9
                     or abs(o["size"] - cfg.lots) > 1e-9 or o["remaining"] < 0.5 * cfg.lots or not ready(t))
            if stale:
                self.cancel(t)
        for t, pk in want.items():
            if t not in self.state["orders"] and ready(t):
                self.place(pk, cfg.lots)

    def poll_fills(self):
        if not self.live or not self.clients:
            return
        ours = {o["oid"]: t for t, o in self.state["orders"].items()}
        known = set(self.state.get("all_oids", []))
        known |= set(ours)
        self.state["all_oids"] = sorted(known)
        seen = set(self.state["trade_ids"])
        c = next(iter(self.clients.values()))
        for f in c.fills("", min_ts=int(self.state["t0"]) - 60):
            if f["order_id"] not in known or f["trade_id"] in seen:
                continue
            seen.add(f["trade_id"])
            self.state["fills"].append(f)
            t = ours.get(f["order_id"])
            if t:
                self.state["orders"][t]["remaining"] = max(self.state["orders"][t]["remaining"] - f["size"], 0.0)
        self.state["trade_ids"] = sorted(seen)

    def accrue(self):
        now = time.time()
        dt = now - self.last_accrue
        self.last_accrue = now
        per = self.state.setdefault("period_accrued", {})
        for t in self.state["orders"]:
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
            if res in ("yes", "no"):
                mark = 1.0 if res == side else 0.0
            else:
                # Liquidation value: best bid on our side.
                mark = float(m.get(f"{side}_bid_dollars") or 0.0)
            p["mark"] = mark
            p["value"] = p["lots"] * mark
            p["pnl"] = p["value"] - p["cost"] - p["fees"]
        return list(pos.values())

    def report(self, cfg: Cfg):
        rows = []
        for t, o in sorted(self.state["orders"].items(), key=lambda kv: -self.rates.get(kv[0], 0.0)):
            pk = self.picks.get(t)
            rows.append({"ticker": t, "side": o["side"], "price": o["price"], "resting": o["remaining"],
                         "our_usd_day": self.rates.get(t, 0.0) * 86400,
                         "pool_usd_day": (pk.pool_rate if pk else 0.0) * 86400,
                         "exp_lip_usd": self.state["accrued"].get(t, 0.0),
                         "period_lip_usd": self.period_accrued(t) if t in self.programs else 0.0})
        pos = self.positions()
        tot_lip = sum(self.state["accrued"].values())
        fill_pnl = sum(p["pnl"] for p in pos)
        at_risk = sum(o["price"] * o["remaining"] for o in self.state["orders"].values())
        hrs = (time.time() - self.state["t0"]) / 3600
        summary = {"mode": "LIVE" if self.live else "shadow", "live_trading": self.live,
                   "arm_requested": self.arm_requested, "hours": hrs, "quoting": len(rows),
                   "our_usd_day_now": sum(r["our_usd_day"] for r in rows), "exp_lip_usd": tot_lip,
                   "fills": len(self.state["fills"]), "fill_lots": sum(p["lots"] for p in pos),
                   "fill_pnl_usd": fill_pnl, "net_usd": tot_lip + fill_pnl, "resting_cost_usd": at_risk,
                   "waiting_on_arbs": len(self.waiting), "arbs_events_skipped": len(self.held_events),
                   "updated_at": time.time()}
        tmp = os.path.join(ROOT, "status.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"summary": summary, "quotes": rows, "positions": pos, "waiting": self.waiting}, f, indent=1)
        os.replace(tmp, os.path.join(ROOT, "status.json"))

        print(f"\n=== LIPFARM {summary['mode']} {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S}Z "
              f"| {hrs:.2f}h | quoting {len(rows)} | now ${summary['our_usd_day_now']:.0f}/day "
              f"| exp LIP ${tot_lip:.2f} | fills {summary['fills']} pnl ${fill_pnl:+.2f} "
              f"| net ${summary['net_usd']:+.2f} | resting ${at_risk:.0f}")
        print(f"{'ticker':44} {'side':4} {'px':>6} {'rest':>6} {'$/day':>7} {'pool/d':>7} {'expLIP':>8}")
        for r in rows:
            print(f"{r['ticker']:44} {r['side']:4} {r['price']:6.3f} {r['resting']:6.0f} "
                  f"{r['our_usd_day']:7.1f} {r['pool_usd_day']:7.1f} {r['exp_lip_usd']:8.2f}")
        if pos:
            print(f"{'fills: ticker':44} {'side':4} {'lots':>6} {'cost':>7} {'mark':>6} {'pnl':>8}")
            for p in pos:
                print(f"{p['ticker']:44} {p['side']:4} {p['lots']:6.0f} {p['cost']:7.2f} "
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
                while time.time() - t0 < cfg.refresh_s - 1e-3:
                    time.sleep(max(min(GUARD_S, cfg.refresh_s - (time.time() - t0)), 0.1))
                    if time.time() - t0 < cfg.refresh_s - 1.0:
                        try:
                            self.guard(cfg)
                        except Exception:
                            traceback.print_exc()
        except KeyboardInterrupt:
            print("stopping: cancelling quotes")
        finally:
            self.accrue()
            for t in list(self.state["orders"]):
                self.cancel(t)
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
