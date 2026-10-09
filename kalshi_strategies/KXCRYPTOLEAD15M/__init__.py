"""Coin Race (KXCRYPTOLEAD15M): which of BTC/ETH/SOL/XRP/HYPE has the best
15-minute return. Pricing only -- the runtime does the rest."""
from __future__ import annotations

from kalshi_infra.runtime.series import Series
from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig

TICKER = "KXCRYPTOLEAD15M"


def _pick_event(events, cfg):
    """Soonest-closing event: the series rolls every 15 minutes, each
    opening at the previous one's close."""
    return events[0] if events else None


_SPOT = None   # one per process: the session-lifetime Coinbase feed (see live_feed)


async def _build(ev, cfg):
    global _SPOT
    import asyncio
    import time
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel, warm_forecaster

    loop = asyncio.get_running_loop()
    if _SPOT is None:
        _SPOT = SpotFeed(cfg)
    _SPOT.ensure_running()
    if not _SPOT.forecaster_warmed:
        # Off the loop: replaying 4h of bars takes seconds, and the spot feed
        # is already live on this loop.
        try:
            _SPOT.adopt_forecaster(await loop.run_in_executor(None, warm_forecaster, cfg, time.time()))
        except Exception as e:
            print(f"[{TICKER}] forecaster warmup failed ({e!r}); it warms from live data instead")
            _SPOT.forecaster_warmed = True
    model = CoinRaceModel(ev, cfg, spot=_SPOT)
    try:
        mem = _SPOT.recent_bars(model.start_window_since())
        sq = await loop.run_in_executor(None, model.backfill_start, mem)
        print(f"[{TICKER}] start-TWAP backfill: {sq}")
    except Exception as e:
        print(f"[{TICKER}] start-TWAP backfill failed ({e!r}); pricing withheld until S is re-reached")
    return model


SERIES = {TICKER: Series(ticker=TICKER, config_cls=CryptoConfig, config_path=CONFIG_PATH, config_key=None,
                         pick_event=_pick_event, build_model=_build)}
