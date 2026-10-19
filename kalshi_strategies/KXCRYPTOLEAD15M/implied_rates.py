"""
Per-coin vol for the Coin Race from the single-coin 15M markets (KXBTC15M,
...): the same event window, so each coin's Kalshi mid prices that coin's
15-minute up/down with exactly the start and spot the race uses, and backs out
its vol (CRYPTO15M/implied_vol.py: precision-weighted EMA of ln vol).

The race's five legs on Kalshi only identify vol *differences* between coins;
the single-coin mids identify each coin's level directly, with more volume
behind them (KXETH15M ~670k contracts/h vs the race's ~5 legs, 2026-10-18).

Mids come from one REST call per second for all five (~170ms, off the loop),
so they lag the websocket by up to ~1.2s; the EMA's 120s window makes that
immaterial. Correlation stays the HAR model's: the single-coin markets carry
nothing about it.
"""
from __future__ import annotations

import asyncio
import math
import time

from kalshi_infra.api_functions import kalshi_get
from kalshi_strategies.CRYPTO15M.implied_vol import ImpliedVolEMA, observe
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS

POLL_S = 1.0


def single_coin_tickers(race_event: str) -> dict[str, str]:
    """KXCRYPTOLEAD15M-26OCT182215 -> {BTC: KXBTC15M-26OCT182215-15, ...}."""
    sfx = race_event.split("-", 1)[1]
    return {c: f"KX{c}15M-{sfx}-{sfx[-2:]}" for c in COINS}


class ImpliedRates:
    """Session-lifetime (on the SpotFeed, like the forecaster): EMAs carry
    across events. `poll(event)` runs on the loop while an event is attached."""

    def __init__(self):
        self.ema = {c: ImpliedVolEMA() for c in COINS}
        self.mids: dict[str, tuple[float, float, float]] = {}   # coin -> (bid, ask, received mono)
        self.n_polls = 0
        self.n_poll_errors = 0
        self._last_obs_sec: dict[str, int] = {}
        # coin -> (vol, wall time) of the last estimate with enough readings.
        self._held: dict[str, tuple[float, float]] = {}
        # The event whose markets are being polled, and its five tickers.
        self.event: str | None = None
        self.tickers: dict[str, str] = {}

    async def poll(self, race_event: str):
        """Poll this event's five 15M markets until another event takes over.

        Each mid is stored with the ticker it came from and read back only for
        the current event's tickers: a poller is a model feed task, and one
        left from the previous event wrote its settled (~0/1) mids over the
        live ones (10-19 05:15 UTC: every coin read 0.002 and fell to HAR)."""
        tks = single_coin_tickers(race_event)
        coin_of = {t: c for c, t in tks.items()}
        self.event = race_event
        self.tickers = tks
        loop = asyncio.get_running_loop()
        params = {"tickers": ",".join(tks.values())}
        while self.event == race_event:
            t0 = time.monotonic()
            try:
                r = await loop.run_in_executor(None, lambda: kalshi_get("/trade-api/v2/markets", params, max_retries=1))
                if self.event != race_event:
                    return
                now = time.monotonic()
                for m in r.get("markets", []):
                    tk = m.get("ticker")
                    c = coin_of.get(tk)
                    try:
                        bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if c is not None and 0 < bid < ask < 1:
                        self.mids[c] = (bid, ask, now, tk)
                self.n_polls += 1
            except Exception:
                self.n_poll_errors += 1
            await asyncio.sleep(max(0.05, POLL_S - (time.monotonic() - t0)))

    def _mid(self, coin: str):
        """(bid, ask, received mono) for the current event's market, else None."""
        q = self.mids.get(coin)
        if q is None or (self.tickers and q[3] != self.tickers.get(coin)):
            return None
        return q[:3]

    def observe(self, now_s: float, coin: str, x: float, tau: float, spot_move: float, p):
        """One reading per coin per second, from the latest polled mid. `x` on
        CF's level (the race's own x), `p` the race's pricing config."""
        sec = int(now_s)
        if self._last_obs_sec.get(coin) == sec:
            return
        self._last_obs_sec[coin] = sec
        q = self._mid(coin)
        if q is None or time.monotonic() - q[2] > p.implied_max_mid_age_s:
            return
        bid, ask, _ = q
        km = 0.5 * (bid + ask)
        if ask - bid > p.implied_max_spread + 1e-9 or not (p.implied_mid_min <= km <= p.implied_mid_max):
            return
        e = self.ema[coin]
        spot_err = math.hypot(p.implied_spot_err_bp * 1e-4, spot_move)
        obs = observe(km, (ask - bid) / 2, x, 1.0, tau, p.implied_basis_sd_bp * 1e-4, p.t_nu,
                      mid_noise_floor=p.implied_mid_noise, spot_err=spot_err, max_obs_se=p.implied_max_obs_se,
                      max_curvature=p.implied_max_curvature,
                      s_bounds=(p.implied_vol_min_bp * 1e-4, p.implied_vol_max_bp * 1e-4),
                      ref_s=e.value(now_s, p.implied_ema_s, 1.0))
        e.add(now_s, obs, p.implied_ema_s)

    def rates(self, now_s: float, p) -> dict[str, float | None]:
        """Per-coin per-second variance, None where there is no estimate.

        Once the EMA falls short of implied_min_obs (its 15M mid stopped giving
        readings), the last value it had with enough readings is held for
        implied_hold_s. Live journals 10-18 23:25-10-19 03:30 UTC: a 3-minute-old
        estimate was 3-9% off the later one (median), against the HAR fallback's
        8-15%; at 10 minutes holding was worse than HAR for 4 of 5 coins."""
        out = {}
        for c in COINS:
            s = self.ema[c].value(now_s, p.implied_ema_s, p.implied_min_obs)
            if s is not None:
                self._held[c] = (s, now_s)
            else:
                h = self._held.get(c)
                if h is not None and 0 <= now_s - h[1] <= p.implied_hold_s:
                    s = h[0]
            out[c] = None if s is None else s * s
        return out

    def held_age(self, now_s: float, p) -> dict[str, float | None]:
        """Per coin: seconds the price rests on a held estimate (None = live or none)."""
        out = {}
        for c in COINS:
            live = self.ema[c].value(now_s, p.implied_ema_s, p.implied_min_obs) is not None
            h = self._held.get(c)
            out[c] = None if live or h is None or now_s - h[1] > p.implied_hold_s else round(now_s - h[1], 1)
        return out

    def snapshot(self, now_s: float, p) -> dict:
        r = self.rates(now_s, p)
        held = self.held_age(now_s, p)
        return {c: {"vol_bp": None if v is None else round(math.sqrt(v) * 1e4, 4),
                    "n_eff": round(self.ema[c].n_eff(), 1), "held_s": held[c],
                    "mid": None if (q := self._mid(c)) is None else round((q[0] + q[1]) / 2, 4),
                    "rejects": dict(self.ema[c].rejects)} for c, v in r.items()} | {
                        "_polls": self.n_polls, "_poll_errors": self.n_poll_errors}


__all__ = ["ImpliedRates", "single_coin_tickers"]
