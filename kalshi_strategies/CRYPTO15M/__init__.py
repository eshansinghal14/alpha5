"""Single-coin 15-minute up/down (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M,
KXHYPE15M), priced off the Coin Race spot stack. Pricing only.

    python -m kalshi_strategies.run --series KXCRYPTOLEAD15M --only KXETH15M   # race + ETH, one process

ETH/SOL/XRP/HYPE share KXCRYPTOLEAD15M's write group (crypto.WRITE_GROUP), so
run together they are one process on one SpotFeed (KXCRYPTOLEAD15M/shared.py)
and one write bucket and quote queue at equal priority -- neither is capped
while the other is quiet. All are on shard 2. KXBTC15M keeps its own process:
~143 book updates/s against ~115/s for the other four together (2026-10-18),
and one process for all five was CPU-bound (loop lag p95 0.86s).
"""
from __future__ import annotations

import os

from kalshi_infra.runtime.series import Series
from kalshi_strategies.CRYPTO15M.config import CONFIG_PATH, UpDownConfig
from kalshi_strategies.KXCRYPTOLEAD15M.shared import get_spot

# Sidebar heading (kalshi_viz.app._groups), listed after the Coin Race.
DASHBOARD_GROUP = "Crypto"
DASHBOARD_RANK = 1
COIN_OF = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXHYPE15M": "HYPE"}
CRYPTO_GROUP = "crypto-shard2"
GROUP_OF = {"KXBTC15M": "crypto15m-btc", **{t: CRYPTO_GROUP for t in COIN_OF if t != "KXBTC15M"}}
# CRYPTO15M_PERSIST=0: a process that doesn't own the shared spot files
# (composite bars, CF index, prints, burst thresholds) reads them only. Two
# writers race on the same hourly parquets. Run with the race, it's the race's.
PERSIST = os.environ.get("CRYPTO15M_PERSIST", "1") != "0"


def _pick_event(events, cfg):
    return events[0] if events else None


async def _spot(cfg, group: str):
    from kalshi_strategies.KXCRYPTOLEAD15M.prints import load_thresholds
    persist = PERSIST and group == CRYPTO_GROUP
    spot = await get_spot(cfg, persist=persist, tag="CRYPTO15M")
    if not spot._persist and not getattr(spot, "_thr_loaded", False):
        # Read-only feed: still start from the saved burst thresholds.
        for src, th in load_thresholds().items():
            if src in spot.calib:
                spot.calib[src].saved = th
        spot._thr_loaded = True
    return spot


def _make(ticker: str) -> Series:
    coin, group = COIN_OF[ticker], GROUP_OF[ticker]

    async def build(ev, cfg):
        from kalshi_strategies.CRYPTO15M.model import UpDownModel
        return UpDownModel(ev, cfg, coin, await _spot(cfg, group))

    return Series(ticker=ticker, config_cls=UpDownConfig, config_path=CONFIG_PATH, config_key=ticker,
                  pick_event=_pick_event, build_model=build, write_group=group)


SERIES = {t: _make(t) for t in COIN_OF}
