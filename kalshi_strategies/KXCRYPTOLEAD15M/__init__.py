"""Coin Race (KXCRYPTOLEAD15M): which of BTC/ETH/SOL/XRP/HYPE has the best
15-minute return. Pricing only -- the runtime does the rest.

Shares a process, a SpotFeed (shared.py) and a write group with whichever
single-coin CRYPTO15M series run beside it (run --series KXCRYPTOLEAD15M
--only KXETH15M): one bucket and quote queue at equal priority."""
from __future__ import annotations

from kalshi_infra.runtime.series import Series
from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig

TICKER = "KXCRYPTOLEAD15M"
# Same string as CRYPTO15M.CRYPTO_GROUP; not imported to keep the race free of it.
WRITE_GROUP = "crypto-shard2"


def _pick_event(events, cfg):
    """Soonest-closing event: the series rolls every 15 minutes, each
    opening at the previous one's close."""
    return events[0] if events else None


async def _build(ev, cfg):
    from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel
    from kalshi_strategies.KXCRYPTOLEAD15M.shared import get_spot
    spot = await get_spot(cfg, persist=True, tag=TICKER, owner=True)
    return CoinRaceModel(ev, cfg, cf=spot.cf, spot=spot)


SERIES = {TICKER: Series(ticker=TICKER, config_cls=CryptoConfig, config_path=CONFIG_PATH, config_key=None,
                         pick_event=_pick_event, build_model=_build, write_group=WRITE_GROUP)}
