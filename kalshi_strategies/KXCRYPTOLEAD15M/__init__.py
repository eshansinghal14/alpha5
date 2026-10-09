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


async def _build(ev, cfg):
    from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBus
    from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel
    from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import CoinbaseAdapter

    bus = CompositeBus(coins=COINS, persist=True)
    coinbase = CoinbaseAdapter(coins=list(COINS))
    bus.deep_book_fn = coinbase.deep_book
    model = CoinRaceModel(ev, cfg, bus=bus, coinbase=coinbase)
    try:
        sq = model.backfill()
        print(f"[{TICKER}] start-TWAP backfill: {sq}")
    except Exception as e:
        print(f"[{TICKER}] start-TWAP backfill failed ({e!r}); pricing withheld until S is re-reached")
    return model


SERIES = {TICKER: Series(ticker=TICKER, config_cls=CryptoConfig, config_path=CONFIG_PATH, config_key=None,
                         pick_event=_pick_event, build_model=_build)}
