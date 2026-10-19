"""
The one crypto SpotFeed per process, shared by every crypto strategy in it
(KXCRYPTOLEAD15M and the single-coin CRYPTO15M series): one Coinbase socket,
one venue process, one CF index feed, one vol forecaster, one set of burst
detectors. A second feed in the same process would double every socket and
parse on the trading loop.

Whichever series builds first creates it (with its own config for the feed's
static settings); later ones attach to it.
"""
from __future__ import annotations

import asyncio
import time

_SPOT = None
_LOCK: asyncio.Lock | None = None


def adopt_owner(spot, cfg):
    """Make `cfg` the feed's config: burst pulls (burst_pull_coins,
    burst_hold_s) and feed health are read from spot.cfg at run time. The race
    owns it, so its settings hold whichever series happened to build the feed."""
    spot.cfg = cfg
    p = cfg.pricing
    spot.health.soft_stale_s, spot.health.hard_stale_s = p.soft_stale_s, p.hard_stale_s
    spot.health.recovery_s = p.recovery_s


async def get_spot(cfg, *, persist: bool = True, tag: str = "crypto", owner: bool = False):
    """The process's SpotFeed, started, seeded and with its forecaster warmed.
    `owner`: this series' config becomes the feed's (the race passes True)."""
    global _SPOT, _LOCK
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    from kalshi_strategies.KXCRYPTOLEAD15M.model import warm_forecaster

    if _LOCK is None:
        _LOCK = asyncio.Lock()
    async with _LOCK:
        loop = asyncio.get_running_loop()
        if _SPOT is None:
            from kalshi_strategies.KXCRYPTOLEAD15M.corr_fit import CorrFit
            from kalshi_strategies.KXCRYPTOLEAD15M.implied_rates import ImpliedRates
            _SPOT = SpotFeed(cfg, persist=persist)
            _SPOT.implied_rates = ImpliedRates()
            _SPOT.corr_fit = CorrFit()
            # Disk + REST before the socket's first tick, so an event adopted
            # mid-window has its start average at once.
            now = time.time()
            try:
                await loop.run_in_executor(None, _SPOT.cf.seed, now - 2 * 3600, now)
            except Exception as e:
                print(f"[{tag}] CF index seed failed ({e!r}); waiting on the live feed", flush=True)
        if owner and _SPOT.cfg is not cfg:
            adopt_owner(_SPOT, cfg)
        _SPOT.ensure_running()
        if not _SPOT.forecaster_warmed:
            # Off the loop: replaying 4h of bars takes seconds, and the spot
            # feed is already live on this loop.
            try:
                _SPOT.adopt_forecaster(await loop.run_in_executor(None, warm_forecaster, cfg, time.time()))
            except Exception as e:
                print(f"[{tag}] forecaster warmup failed ({e!r}); it warms from live data instead", flush=True)
                _SPOT.forecaster_warmed = True
    return _SPOT


def reset():
    """Tests only."""
    global _SPOT, _LOCK
    _SPOT, _LOCK = None, None


__all__ = ["get_spot", "adopt_owner", "reset"]
