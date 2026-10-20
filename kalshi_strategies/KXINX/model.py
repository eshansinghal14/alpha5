"""
Live S&P model for one KXINXU / KXINXDUD event. Pricing runs in the
shared hub (hub.py): one IB feed and one curve fit for every S&P series.
This side registers the event, keeps the subscribed scope centred, turns
the hub's (lo, mid, hi) into Fairs through the noise filter, and reports
positions for the combined risk limit.

Band = union of call-side and put-side bid/ask bounds (live_pricing), so
call/put disagreement widens the edge directly.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
import traceback
from collections import deque

from kalshi_infra import ib_functions as ibf
from kalshi_infra.config import load
from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import Fair
from kalshi_strategies.KXINX.config import CONFIG_PATH, HUB_KEY, HubConfig
from kalshi_strategies.KXINX.hub import HOST, LINE_LIMIT, MARKET_FIELDS

POSITIONS_SEND_S = 0.5
HUB_RETRY_S = 1.0


class NoiseTracker:
    """Drops markets whose fair spikes and immediately reverts. Deep-ITM
    buckets are the offenders: B-L divides large premium differences by a
    small strike gap, so one tick of quote noise moves the estimate far more
    than the instantaneous band can express. Causal and rolling, so live and
    backtest decide identically; a market with too little history is
    untradeable, not clean."""

    def __init__(self, p):
        self.p = p
        self._hist: dict[str, deque] = {}
        self._spikes: dict[str, deque] = {}
        self.dropped: dict[str, int] = {}
        self.warming: dict[str, int] = {}

    def accept(self, ticker: str, prob: float) -> bool:
        p = self.p
        h = self._hist.setdefault(ticker, deque(maxlen=p.noise_window))
        s = self._spikes.setdefault(ticker, deque(maxlen=p.noise_window))
        h.append(float(prob))
        if len(h) >= 3:
            d_prev, d_next = h[-2] - h[-3], h[-1] - h[-2]
            s.append(abs(d_prev) > p.noise_jump and (d_next < 0) == (d_prev > 0)
                     and abs(d_next) > p.noise_revert_frac * abs(d_prev))
        if len(s) < p.noise_min_samples:
            self.warming[ticker] = self.warming.get(ticker, 0) + 1
            return False
        if sum(s) / len(s) > p.noise_max_rate:
            self.dropped[ticker] = self.dropped.get(ticker, 0) + 1
            return False
        return True


class SpxEventModel:
    def __init__(self, ev: EventInfo, cfg, series_ticker: str):
        self.cfg = cfg
        self.series = series_ticker
        self.event_ticker = ev.event_ticker
        self.markets = ibf.event_markets(ev.event_ticker)
        self.tickers = [m["ticker"] for m in self.markets]
        self._order = ibf.ordered_tickers(self.markets)
        self._centre = ibf.kalshi_centre(ev.event_ticker)
        close = datetime.datetime.fromisoformat(self.markets[0]["close_time"].replace("Z", "+00:00"))
        self.expiry = close.astimezone(ibf.ET).date()
        self.scope = self._scope_around(self._centre)
        self.port = load(HubConfig, CONFIG_PATH, HUB_KEY).port
        print(f"[{series_ticker}] scope: {len(self.scope)} buckets {self.scope[0].split('-')[-1]}.."
              f"{self.scope[-1].split('-')[-1]} around {self._centre.split('-')[-1]} (priced by the hub)")
        self.noise = NoiseTracker(cfg.pricing)
        self._last_key: dict[str, tuple] = {}
        self._fairs: dict[str, Fair] = {}
        self._pending: dict[str, tuple[float, float, float]] = {}
        self._pending_t: float | None = None
        self._last_ok_mono: float | None = None
        self._last_fair_mono: float | None = None
        self._status: dict = {}
        self._blocks: dict[str, str] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._wake = None
        self._engine = None
        self._monotone_sent: bool | None = None

    def _scope_around(self, centre: str) -> list[str]:
        """Contiguous window: tradeable count + margin each side."""
        q, p = self.cfg.quoting, self.cfg.pricing
        n = q.max_markets if q.max_markets is not None else len(self._order)
        i = self._order.index(centre)
        half = n // 2 + p.scope_margin_buckets
        lo = max(0, min(i - half, len(self._order) - (2 * half + 1)))
        return self._order[lo:lo + 2 * half + 1]

    # --- hub connection ---

    def _send(self, msg: dict):
        if self._writer is None or not self._connected:
            return
        try:
            self._writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except Exception:
            self._connected = False

    def _hello(self):
        self._send({"op": "hello", "series": self.series, "event_ticker": self.event_ticker,
                    "expiry": self.expiry.isoformat(),
                    "markets": [{k: m.get(k) for k in MARKET_FIELDS} for m in self.markets],
                    "scope": self.scope,
                    "monotone": self.cfg.pricing.monotone_levels})
        self._monotone_sent = self.cfg.pricing.monotone_levels

    async def _hub_loop(self):
        """Stay connected to the hub; on every (re)connect re-register, so a
        restarted hub picks the series back up."""
        while True:
            try:
                reader, self._writer = await asyncio.open_connection(HOST, self.port, limit=LINE_LIMIT)
                self._connected = True
                self._hello()
                print(f"[{self.series}] connected to pricing hub on {HOST}:{self.port}", flush=True)
                while line := await reader.readline():
                    self._on_hub(json.loads(line))
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as e:
                if self._connected:
                    print(f"[{self.series}] pricing hub connection lost ({e!r}); retrying", flush=True)
            except Exception:
                print(f"[{self.series}] WARN: pricing hub error:", flush=True)
                traceback.print_exc()
            finally:
                if self._connected:
                    print(f"[{self.series}] pricing hub disconnected", flush=True)
                self._connected = False
                self._status = {}
                self._blocks = {}
                if self._writer is not None:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
            await asyncio.sleep(HUB_RETRY_S)

    def _on_hub(self, msg: dict):
        op = msg.get("op")
        if op == "fairs":
            self._pending.update({t: tuple(v) for t, v in msg["fairs"].items()})
            self._pending_t = time.monotonic()
            if self._wake is not None:
                self._wake()
        elif op == "status":
            self._status = msg
            self._blocks = msg.get("blocks") or {}

    async def _positions_loop(self):
        while True:
            await asyncio.sleep(POSITIONS_SEND_S)
            if self._monotone_sent is not None and self._monotone_sent != self.cfg.pricing.monotone_levels:
                self._send({"op": "monotone", "on": self.cfg.pricing.monotone_levels})
                self._monotone_sent = self.cfg.pricing.monotone_levels
            if self._engine is None:
                continue
            legs = [[t, p.yes_qty, p.no_qty, p.yes_cost + p.no_cost + p.fees_paid]
                    for t, p in self._engine.portfolio.positions.items()
                    if p.yes_qty or p.no_qty]
            self._send({"op": "positions", "legs": legs})

    # --- FairModel ---

    def reprice(self, now_s: float):
        if not self._pending:
            return None
        probs, self._pending = self._pending, {}
        self._last_fair_mono = self._pending_t
        r = self.cfg.pricing.round_decimals
        out = {}
        for ticker, (lo, mid, hi) in probs.items():
            key = (round(mid, r), round(lo, r), round(hi, r))
            if key == self._last_key.get(ticker):
                continue
            self._last_key[ticker] = key
            if not self.noise.accept(ticker, mid):
                continue
            f = Fair(prob=mid, lo=lo, hi=hi)
            self._fairs[ticker] = f
            out[ticker] = f
        self._last_ok_mono = time.monotonic()
        return out

    def set_wakeup(self, fn):
        self._wake = fn

    def attach_engine(self, engine):
        self._engine = engine

    def on_market_event(self, ticker, now_s):
        return None

    def signal_age_s(self):
        # The hub's last fit, not our last reprice: a dead hub must go stale.
        if not self._connected or self._last_fair_mono is None:
            return None
        return time.monotonic() - self._last_fair_mono

    def feed_age_s(self):
        return self._status.get("feed_age_s")

    def applied_age_s(self):
        return self._status.get("applied_age_s")

    def side_block(self, ticker, side, fair, now_s):
        return self._blocks.get(f"{ticker}:{side}")

    def summary_extras(self) -> dict:
        st = self._status
        problem = st.get("feed_problem") if self._connected else "not connected to the pricing hub"
        return {"feed_problem": problem, "ib_farms": st.get("ib_farms", {}),
                "ib_ticks_per_s": st.get("ib_ticks_per_s", 0.0), "ib_held_ticks": st.get("ib_held_ticks", 0),
                "ib_scope": list(self.scope), "hub_lines": st.get("n_lines"),
                "combined_risk": st.get("combined"), "combined_blocks": len(self._blocks),
                "noise_dropped": dict(self.noise.dropped), "noise_warming": len(self.noise.warming)}

    # --- lifecycle ---

    def feed_tasks(self):
        return [self._hub_loop(), self._rescope_loop(), self._positions_loop()]

    async def _rescope_loop(self):
        """Re-centre the scope when the 50/50 bucket drifts
        rescope_drift_buckets from the centre, or reaches the scope edge.
        The hub re-subscribes to the union of every series' scope."""
        while True:
            await asyncio.sleep(self.cfg.pricing.rescope_check_s)
            try:
                in_scope = [t for t in self.scope if t in self._fairs]
                if not in_scope:
                    continue
                atm = min(in_scope, key=lambda t: abs(self._fairs[t].prob - 0.5))
                drift = abs(self._order.index(atm) - self._order.index(self._centre))
                if drift < self.cfg.pricing.rescope_drift_buckets and atm not in (self.scope[0], self.scope[-1]):
                    continue
                new = self._scope_around(atm)
                if new != self.scope:
                    self.scope = new
                    self._send({"op": "scope", "scope": new})
                    print(f"[{self.series}] scope re-centred {self._centre.split('-')[-1]} -> "
                          f"{atm.split('-')[-1]}")
                self._centre = atm
            except Exception:
                print(f"[{self.series}] WARN: rescope failed:")
                traceback.print_exc()

    async def close(self):
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
