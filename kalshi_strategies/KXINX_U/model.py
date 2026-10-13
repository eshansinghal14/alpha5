"""
Live S&P model for one KXINX / KXINXU event: IB SPXW options ->
Breeden-Litzenberger bucket probabilities -> per-market Fair.

Band = union of call-side and put-side bid/ask bounds (live_pricing), so
call/put disagreement widens the edge directly.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from collections import deque

from kalshi_infra import databento_functions as dbf
from kalshi_infra import ib_functions as ibf
from kalshi_infra import keys
from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.trading.fair import Fair
from kalshi_strategies.KXINX_U.ib_feed import HoldParams, LiveIBFeed
from kalshi_strategies.KXINX_U.live_pricing import compute_live_bucket_probs


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
        self.markets = dbf._kxinx_event_markets(ev.event_ticker)
        self.tickers = [m["ticker"] for m in self.markets]
        self._order = ibf.ordered_tickers(self.markets)
        self._centre = ibf.kalshi_centre(ev.event_ticker)
        p = cfg.pricing
        scope = self._scope_around(self._centre)
        self.ib = LiveIBFeed(ev.event_ticker, tickers=scope,
                             client_id=keys.IB_CLIENT_ID + p.ib_client_id_offset,
                             hold=HoldParams(p.hold_baseline_s, p.hold_multiplier, p.hold_settle_s,
                                             p.hold_min_samples, p.hold_sample_s),
                             reconnect_delay_s=p.ib_reconnect_delay_s)
        print(f"[{series_ticker}] IB scope: {len(scope)} buckets {scope[0].split('-')[-1]}.."
              f"{scope[-1].split('-')[-1]} around {self._centre.split('-')[-1]}")
        self.noise = NoiseTracker(p)
        self._last_key: dict[str, tuple] = {}
        self._fairs: dict[str, Fair] = {}
        self._last_ok_mono: float | None = None

    def _scope_around(self, centre: str) -> list[str]:
        """Contiguous window: tradeable count + margin each side."""
        q, p = self.cfg.quoting, self.cfg.pricing
        n = q.max_markets if q.max_markets is not None else len(self._order)
        i = self._order.index(centre)
        half = n // 2 + p.scope_margin_buckets
        lo = max(0, min(i - half, len(self._order) - (2 * half + 1)))
        return self._order[lo:lo + 2 * half + 1]

    # --- FairModel ---

    def reprice(self, now_s: float):
        if not self.ib.drain():
            return None
        b = self.ib.book
        a = b.snapshot_arrays()
        probs = compute_live_bucket_probs(self.markets, b.call_strikes, a[0], a[1], a[2],
                                          b.put_strikes, a[3], a[4], a[5])
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
        # Stamped only after a successful pass, so a repricing that keeps
        # throwing trips the freshness guard.
        self._last_ok_mono = time.monotonic()
        return out

    def set_wakeup(self, fn):
        self.ib.on_tick_hook = fn

    def on_market_event(self, ticker, now_s):
        return None

    def signal_age_s(self):
        return None if self._last_ok_mono is None else time.monotonic() - self._last_ok_mono

    def feed_age_s(self):
        return self.ib.seconds_since_record()

    def applied_age_s(self):
        return self.ib.seconds_since_applied()

    def side_block(self, ticker, side, fair, now_s):
        return None

    def summary_extras(self) -> dict:
        return {"feed_problem": self.ib.gateway_problem(), "ib_farms": dict(self.ib.farm_status),
                "ib_ticks_per_s": self.ib.records_per_s(), "ib_held_ticks": self.ib.book.n_held,
                "ib_scope": list(self.ib.scope_tickers or []),
                "noise_dropped": dict(self.noise.dropped), "noise_warming": len(self.noise.warming)}

    # --- lifecycle ---

    def feed_tasks(self):
        return [self.ib.run(), self._rescope_loop()]

    async def _rescope_loop(self):
        """Re-centre the IB subscription when the 50/50 bucket drifts
        rescope_drift_buckets from the centre, or reaches the scope edge."""
        while True:
            await asyncio.sleep(self.cfg.pricing.rescope_check_s)
            try:
                cur = self.ib.scope_tickers or []
                in_scope = [t for t in cur if t in self._fairs]
                if not in_scope:
                    continue
                atm = min(in_scope, key=lambda t: abs(self._fairs[t].prob - 0.5))
                drift = abs(self._order.index(atm) - self._order.index(self._centre))
                if drift < self.cfg.pricing.rescope_drift_buckets and atm not in (cur[0], cur[-1]):
                    continue
                new = self._scope_around(atm)
                if await self.ib.rescope(new):
                    print(f"[{self.series}] IB scope re-centred {self._centre.split('-')[-1]} -> "
                          f"{atm.split('-')[-1]}")
                self._centre = atm
            except Exception:
                print(f"[{self.series}] WARN: IB rescope failed:")
                traceback.print_exc()

    async def close(self):
        self.ib.stop()
