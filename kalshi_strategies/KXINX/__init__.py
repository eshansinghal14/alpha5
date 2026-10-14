"""S&P 500 daily close, priced off SPXW 0DTE options: KXINX (range buckets),
KXINXU (above/below ladder) and KXINXDUD (one "above prior close" market).
Pricing only -- the runtime does the rest.

`python -m kalshi_strategies.run --series KXINX` runs every series here plus
the shared pricing hub (SERVICES); `--only KXINXU` runs one series."""
from __future__ import annotations

import datetime

import pandas as pd

from kalshi_infra.runtime.series import Series, Service
from kalshi_strategies.KXINX.config import CONFIG_PATH, HUB_KEY, SpxConfig

_ET = "America/New_York"
# All three series run in one process on one write bucket, cancel reserve
# and quote priority queue (they are all on shard 0).
WRITE_GROUP = "spx-daily"


def _pick_event(events, cfg):
    """The soonest event ending with pricing.event_suffix (the 16:00 ET
    daily close). Not soonest-close overall: KXINXU lists intraday hourlies
    that SPXW 0DTE can't price."""
    daily = [e for e in events if e.event_ticker.endswith(cfg.pricing.event_suffix)]
    return daily[0] if daily else None


def _trade_from(ev, cfg) -> pd.Timestamp | None:
    """pricing.event_open_et on the close day, not Kalshi's open_time: the
    next KXINXU event opens at the prior session's close, hours before the
    0DTE chain it is priced off exists."""
    if ev.close_time is None:
        return ev.open_time
    hh, mm = (int(x) for x in cfg.pricing.event_open_et.split(":"))
    day = ev.close_time.tz_convert(_ET).date()
    start = pd.Timestamp(datetime.datetime.combine(day, datetime.time(hh, mm)), tz=_ET).tz_convert("UTC")
    return start if ev.open_time is None else max(start, ev.open_time)


def _make(ticker: str) -> Series:
    async def build(ev, cfg):
        from kalshi_strategies.KXINX.model import SpxEventModel
        return SpxEventModel(ev, cfg, ticker)
    return Series(ticker=ticker, config_cls=SpxConfig, config_path=CONFIG_PATH, config_key=ticker,
                  pick_event=_pick_event, build_model=build, trade_from=_trade_from,
                  write_group=WRITE_GROUP)


def _hub_port() -> int:
    from kalshi_infra.config import load
    from kalshi_strategies.KXINX.config import HubConfig
    return load(HubConfig, CONFIG_PATH, HUB_KEY).port


SERIES = {t: _make(t) for t in ("KXINX", "KXINXU", "KXINXDUD")}
SERVICES = [Service(name="spx-hub", module="kalshi_strategies.KXINX.hub", port=_hub_port)]
