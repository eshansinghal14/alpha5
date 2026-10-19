"""Startup: an event that opened seconds before the process can be missing from
Kalshi's listing; the session must re-read it instead of sleeping to the next one."""
import asyncio
import types

import pandas as pd
import pytest

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_infra.runtime import session


def _ev(ticker, open_s, close_s):
    now = pd.Timestamp.now(tz="UTC")
    return EventInfo(ticker, now + pd.Timedelta(seconds=open_s), now + pd.Timedelta(seconds=close_s), [])


def test_startup_relooks_when_the_current_event_is_missing(monkeypatch):
    current, nxt = _ev("EV-NOW", -8, 892), _ev("EV-NEXT", 892, 1792)
    listings = [[nxt], [current, nxt]]       # 1st read misses the just-opened event
    reads, ran = [], []

    def upcoming(series):
        reads.append(len(listings))
        return listings.pop(0) if len(listings) > 1 else listings[0]

    async def run_event(series, ev, *a, **kw):
        ran.append(ev.event_ticker)
        raise asyncio.CancelledError

    monkeypatch.setattr(session.km, "upcoming_events", upcoming)
    monkeypatch.setattr(session, "run_event", run_event)
    monkeypatch.setattr(session, "STARTUP_RELOOK_S", 0.01)
    monkeypatch.setattr(session, "ConfigStore", lambda *a: types.SimpleNamespace(
        config=types.SimpleNamespace(), poll=lambda: [], apply_pending=lambda: []))
    monkeypatch.setattr(session, "check_strategy_config", lambda cfg: None)
    monkeypatch.setattr(session, "TradingModeWatcher", lambda s: None)
    monkeypatch.setattr(session, "StopWatcher", lambda: types.SimpleNamespace(poll=lambda: None))
    series = types.SimpleNamespace(ticker="EV", config_cls=None, config_path="", config_key=None,
                                   pick_event=lambda events, cfg: events[0] if events else None,
                                   trade_from=lambda ev, cfg: ev.open_time)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session._session_loop(series, writer=None, live_trading=False, carry_arm=True, event_ticker=None))
    assert ran == ["EV-NOW"] and len(reads) == 2
