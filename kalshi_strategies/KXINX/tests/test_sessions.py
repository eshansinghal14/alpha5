"""Overnight (Cboe GTH) scheduling for the S&P series."""
import datetime
import types

import pandas as pd

from kalshi_strategies.KXINX import _trade_from, ib_watchdog, sessions

ET = sessions.ET


def at(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=ET)


def ev(close_et, open_et=None):
    c = pd.Timestamp(close_et, tz="America/New_York").tz_convert("UTC")
    o = None if open_et is None else pd.Timestamp(open_et, tz="America/New_York").tz_convert("UTC")
    return types.SimpleNamespace(close_time=c, open_time=o)


def cfg(overnight, open_et="09:00"):
    return types.SimpleNamespace(pricing=types.SimpleNamespace(trade_overnight=overnight, event_open_et=open_et))


def test_gth_open_is_prior_weekday_evening():
    assert sessions.gth_open(datetime.date(2026, 10, 21)) == at("2026-10-20 20:15")   # Wed <- Tue
    assert sessions.gth_open(datetime.date(2026, 10, 19)) == at("2026-10-18 20:15")   # Mon <- Sun


def test_trade_from_overnight_starts_at_gth_open_not_before_kalshi_opens():
    e = ev("2026-10-21 16:00", open_et="2026-10-20 16:00")             # KXINXU lists at the prior close
    assert _trade_from(e, cfg(True)) == pd.Timestamp("2026-10-20 20:15", tz="America/New_York")
    late = ev("2026-10-21 16:00", open_et="2026-10-20 20:30")
    assert _trade_from(late, cfg(True)) == late.open_time


def test_trade_from_daytime_unchanged():
    e = ev("2026-10-21 16:00", open_et="2026-10-20 16:00")
    assert _trade_from(e, cfg(False)) == pd.Timestamp("2026-10-21 09:00", tz="America/New_York")


def test_options_gap():
    g = sessions.options_gap
    assert g(at("2026-10-20 20:16")) == "options_session_warmup"
    assert g(at("2026-10-20 20:18")) is None                            # overnight, quoting
    assert g(at("2026-10-21 03:00")) is None
    assert g(at("2026-10-21 09:27")) == "options_session_gap"
    assert g(at("2026-10-21 09:30")) is None                            # RTH: daytime guards as before
    assert g(at("2026-10-21 15:59")) is None
    assert g(at("2026-10-21 18:00")) == "options_session_gap"


def test_watchdog_covers_overnight_not_the_break_or_weekend():
    a = ib_watchdog.active_now
    assert a(at("2026-10-20 21:00"))        # Tue evening GTH
    assert a(at("2026-10-21 03:00"))        # Wed overnight
    assert a(at("2026-10-21 12:00"))        # Wed RTH
    assert not a(at("2026-10-21 18:00"))    # evening break
    assert a(at("2026-10-18 21:00"))        # Sun evening leads into Monday
    assert not a(at("2026-10-23 21:00"))    # Fri evening: no session until Sunday
    assert not a(at("2026-10-24 12:00"))    # Saturday
