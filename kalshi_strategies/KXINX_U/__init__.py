"""S&P 500 option-implied strategy for KXINX (range buckets) and KXINXU
(above/below ladder). Pricing only -- the runtime does the rest."""
from __future__ import annotations

import datetime

import pandas as pd

from kalshi_infra.runtime.series import Series
from kalshi_strategies.KXINX_U.config import CONFIG_PATH, SpxConfig

_ET = "America/New_York"


def _pick_event(events, cfg, now: pd.Timestamp):
    """The event ending with pricing.event_suffix (the 16:00 ET daily close),
    soonest first, once it is past pricing.event_open_et. Not soonest-close:
    KXINXU lists intraday hourlies that SPXW 0DTE can't price."""
    p = cfg.pricing
    daily = [e for e in events if e.event_ticker.endswith(p.event_suffix)]
    if not daily:
        return None
    ev = daily[0]
    if ev.close_time is not None:
        hh, mm = (int(x) for x in p.event_open_et.split(":"))
        day = ev.close_time.tz_convert(_ET).date()
        if now < pd.Timestamp(datetime.datetime.combine(day, datetime.time(hh, mm)), tz=_ET):
            return None
    return ev


def _make(ticker: str) -> Series:
    async def build(ev, cfg):
        from kalshi_strategies.KXINX_U.model import SpxEventModel
        return SpxEventModel(ev, cfg, ticker)
    return Series(ticker=ticker, config_cls=SpxConfig, config_path=CONFIG_PATH, config_key=ticker,
                  pick_event=_pick_event, build_model=build)


SERIES = {"KXINX": _make("KXINX"), "KXINXU": _make("KXINXU")}
