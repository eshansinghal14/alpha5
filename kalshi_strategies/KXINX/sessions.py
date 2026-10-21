"""
When SPXW options quote, for the S&P series: Cboe's overnight (GTH) session
and regular hours, in ET.

  GTH  20:15 the prior evening -> 09:25    (curb 16:15-17:00 not traded)
  RTH  09:30 -> 16:00                      (16:15 for non-expiring series)

IB lists exactly these for SPXW (contract details 2026-10-20, US/Central:
"20261020:1915-20261021:0825;20261021:0830-..."). Between the two sessions
there's no option quote to price off, so the fair is stale even though the
hub keeps re-sending the last fit.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GTH_OPEN = datetime.time(20, 15)
GTH_CLOSE = datetime.time(9, 25)
RTH_OPEN = datetime.time(9, 30)
# The overnight session's first quotes trickle in; not quoting until the
# book has filled in. Not applied at 09:30, which daytime trading already
# handles (fair_suspect, the spread hold).
WARMUP = datetime.timedelta(minutes=2)


def prev_weekday(d: datetime.date) -> datetime.date:
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def gth_open(expiry: datetime.date) -> datetime.datetime:
    """The overnight session that leads into `expiry`'s regular session opens
    at 20:15 ET on the prior weekday (Sunday 20:15 for a Monday expiry)."""
    if expiry.weekday() == 0:
        d = expiry - datetime.timedelta(days=1)
    else:
        d = prev_weekday(expiry)
    return datetime.datetime.combine(d, GTH_OPEN, tzinfo=ET)


def options_gap(now: datetime.datetime) -> str | None:
    """Why SPXW options aren't quoting at `now` (ET-aware or UTC), or None
    when they are: the 09:25-09:30 handover, or the evening break before
    GTH (the session's own close is handled by the event's close_time)."""
    t = now.astimezone(ET)
    tod = t.time()
    if GTH_CLOSE <= tod < RTH_OPEN:
        return "options_session_gap"
    open_dt = datetime.datetime.combine(t.date(), GTH_OPEN, tzinfo=ET)
    if open_dt <= t < open_dt + WARMUP:
        return "options_session_warmup"
    if datetime.time(16, 15) <= tod < GTH_OPEN:
        return "options_session_gap"
    return None
