"""S&P 500 option-implied strategy for KXINX (range buckets) and KXINXU
(above/below ladder). Pricing only -- the runtime does the rest."""
from __future__ import annotations

import datetime

import pandas as pd

from kalshi_infra.runtime.series import Series
from kalshi_strategies.KXINX_U.config import CONFIG_PATH, SpxConfig

_ET = "America/New_York"


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
        from kalshi_strategies.KXINX_U.model import SpxEventModel
        return SpxEventModel(ev, cfg, ticker)
    return Series(ticker=ticker, config_cls=SpxConfig, config_path=CONFIG_PATH, config_key=ticker,
                  pick_event=_pick_event, build_model=build, trade_from=_trade_from)


SERIES = {"KXINX": _make("KXINX"), "KXINXU": _make("KXINXU")}
