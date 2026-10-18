"""Single-coin 15-minute up/down (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M,
KXHYPE15M), priced off the Coin Race spot stack. Pricing only.

    python -m kalshi_strategies.run --series CRYPTO15M

Two processes (write groups), each with one shared SpotFeed: BTC alone, the
other four together. One process for all five was CPU-bound on the trading
loop (loop lag p95 0.86s, 10-47% of decisions stale_signal, 2026-10-18):
KXBTC15M alone sends ~143 book updates/s against ~115/s for the other four.
"""
from __future__ import annotations

import os

from kalshi_infra.runtime.series import Series
from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH, UpDownConfig

COIN_OF = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXHYPE15M": "HYPE"}
GROUP_OF = {"KXBTC15M": "crypto15m-btc", **{t: "crypto15m-alts" for t in COIN_OF if t != "KXBTC15M"}}
# The only process that writes the shared spot files (composite bars, CF index,
# prints, burst thresholds): two writers race on the same hourly parquets.
# CRYPTO15M_PERSIST=0 turns it off where KXCRYPTOLEAD15M already writes them (EC2).
PERSIST_GROUP = "crypto15m-alts"
PERSIST = os.environ.get("CRYPTO15M_PERSIST", "1") != "0"

_SPOT = None
_WARM_LOCK = None


def _pick_event(events, cfg):
    return events[0] if events else None


async def _spot(cfg, group: str):
    global _SPOT, _WARM_LOCK
    import asyncio
    import time
    from kalshi_strategies.CRYPTO15M.shared_feed import SharedSpotFeed
    from kalshi_strategies.KXCRYPTOLEAD15M.model import warm_forecaster
    from kalshi_strategies.KXCRYPTOLEAD15M.prints import load_thresholds

    if _SPOT is None:
        persist = PERSIST and group == PERSIST_GROUP
        _SPOT = SharedSpotFeed(cfg, persist=persist)
        if not persist:
            # Read-only: still start from the saved burst thresholds.
            for src, th in load_thresholds().items():
                if src in _SPOT.calib:
                    _SPOT.calib[src].saved = th
        _WARM_LOCK = asyncio.Lock()
        now = time.time()
        try:
            await asyncio.get_running_loop().run_in_executor(None, _SPOT.cf.seed, now - 2 * 3600, now)
        except Exception as e:
            print(f"[CRYPTO15M] CF index seed failed ({e!r}); waiting on the live feed", flush=True)
    _SPOT.ensure_running()
    async with _WARM_LOCK:
        if not _SPOT.forecaster_warmed:
            try:
                warmed = await asyncio.get_running_loop().run_in_executor(None, warm_forecaster, cfg, time.time())
                _SPOT.adopt_forecaster(warmed)
            except Exception as e:
                print(f"[CRYPTO15M] forecaster warmup failed ({e!r}); it warms from live data instead", flush=True)
                _SPOT.forecaster_warmed = True
    return _SPOT


def _make(ticker: str) -> Series:
    coin, group = COIN_OF[ticker], GROUP_OF[ticker]

    async def build(ev, cfg):
        from kalshi_strategies.CRYPTO15M.model import UpDownModel
        return UpDownModel(ev, cfg, coin, await _spot(cfg, group))

    return Series(ticker=ticker, config_cls=UpDownConfig, config_path=CONFIG_PATH, config_key=ticker,
                  pick_event=_pick_event, build_model=build, write_group=group)


SERIES = {t: _make(t) for t in COIN_OF}
