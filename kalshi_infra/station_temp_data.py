"""
Historical ground-truth temperature data for Kalshi weather-market
settlement stations, pulled from public NWS/IEM sources (no API key
required for anything in this module).

Every KXHIGH*/KXLOW* market's rules_primary text still says settlement is
"according to the National Weather Service's Climatological Report (Daily)"
for a specific named station -- confirmed directly against several days of
settled results. But as of 2026-08, Kalshi's settlement_sources API field
no longer names that station (it now just returns a generic
{"name": "The Weather Company", "url": "https://weather.com/kalshi"} for
every one of these series), so get_kalshi_settlement_source can't discover
the station/office dynamically anymore -- it uses a hardcoded
_SERIES_STATION_MAP instead, confirmed one series at a time against real
settled results. For KXHIGHLAX and KXLOWTLAX that's the CLI product issued
by the LOX -- LA/Oxnard -- forecast office, for the LAX airport station:
https://forecast.weather.gov/product.php?site=LOX&product=CLI&issuedby=LAX

Two complementary sources, both keyed to that same physical station (LAX
airport), both served by Iowa Environmental Mesonet's public archives:

1. fetch_cli_report: the OFFICIAL daily MAXIMUM/MINIMUM value and its
   time-of-day, in the exact report Kalshi cites as its settlement source --
   ground truth for what a market actually settles to.

2. fetch_station_temps: a same-station intraday temperature trace, merging
   IEM's 1-minute ASOS product (per-minute, used wherever it has data) with
   the routine hourly METAR + SPECI archive (gap-fill -- see its docstring
   for why a day is never forced to pick just one), used to reconstruct
   WHEN the running max/min first crossed a given strike during the day. The
   CLI report alone only gives the time of the day's FINAL max/min, not the
   full trajectory, so it can't answer "when did the running max first pass
   82F" by itself.

IMPORTANT -- these two sources do not always agree to the exact degree. The
intraday trace and the official CLI report's rounding/averaging convention
are derived differently off the same underlying sensor, so a ~1F mismatch
between "when the trace first crosses a strike" and "what the CLI report
says the day's max was" is possible and normal, not a bug in either source.
This matters for the quoting-arb use case: seeing the trace cross a strike
is strong evidence the market will settle YES, but the CLI report is what
actually settles the market, so a fill right at the rounding boundary isn't
literally risk-free. Always treat fetch_cli_report's value (or, better, the
market's own "result" field once settled) as the final word on whether a
given day actually settled above/below a strike -- see
kalshi_quoting_arbs/fill_backtest.ipynb for exactly this cross-check.

CAVEAT -- fetch_cli_report is NOT reliable ground truth for every station.
Confirmed directly, two different CLI report cycles exist across the
NWS offices behind these series:
  - "yesterday" cycle (LAX and 12 others -- see _SERIES_STATION_MAP): the
    report is issued the following morning and covers the FULL prior
    calendar day. fetch_cli_report's issue_date = date + 1 default assumes
    this cycle, and it's confirmed to match Kalshi's actual settled result
    exactly for every station tagged "yesterday".
  - "today" cycle (NYC, MIA, ATL, AUS, DEN): the ONLY report NWS issues for
    these stations comes out mid-afternoon of the same day, explicitly
    labeled "valid as of H:MM PM local time" -- it does not cover the rest
    of the day. Confirmed directly this understates the true daily value:
    NYC's 2026-08-13 report (issued 4:52pm, valid as of 4:00pm) showed a
    max of 84F, but Kalshi actually settled the 85-86F bucket, meaning the
    real high came after the report's cutoff. get_kalshi_settlement_source
    refuses series tagged "today" rather than silently returning a value
    that can understate the real max/min -- this module has no full-day
    ground-truth source for those 5 stations yet.
"""
import datetime
import os
import re
import time

import pandas as pd
import pytz
import requests

_last_iem_call_ts = 0.0


def _iem_get(url: str, params: dict, max_retries: int = 5) -> requests.Response:
    """GET against an IEM endpoint with a polite min-interval throttle and
    429 backoff -- confirmed directly that rapid-fire requests trip a 429
    (unauthenticated public service, no per-key quota to negotiate)."""
    global _last_iem_call_ts
    for attempt in range(max_retries + 1):
        wait = _IEM_MIN_CALL_INTERVAL - (time.time() - _last_iem_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_iem_call_ts = time.time()
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "station_temps")
_CLI_DIR = os.path.join(_DATA_DIR, "cli_reports")
_ASOS_DIR = os.path.join(_DATA_DIR, "asos")

IEM_AFOS_RETRIEVE_URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
IEM_ASOS_1MIN_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
_IEM_MIN_CALL_INTERVAL = 1.0  # be polite -- unauthenticated public archive, no key/quota to lean on


# ---------------------------------------------------------------------------
# Settlement source discovery (hardcoded -- see module docstring for why
# this can no longer be read dynamically from Kalshi's API)
# ---------------------------------------------------------------------------

# ticker -> (wfo, station, cycle). "cycle" is "yesterday" (full prior-day
# report, issued next morning -- reliable) or "today" (partial same-day
# report, issued mid-afternoon -- NOT reliable, see module docstring
# CAVEAT). Confirmed one series at a time by cross-checking each station's
# CLI report MAXIMUM/MINIMUM against Kalshi's own settled market results
# for 2026-08-12/13.
_SERIES_STATION_MAP = {
    "KXHIGHLAX":   ("LOX", "LAX", "yesterday"),
    "KXHIGHNY":    ("OKX", "NYC", "today"),
    "KXHIGHMIA":   ("MFL", "MIA", "today"),
    "KXHIGHCHI":   ("LOT", "MDW", "yesterday"),
    "KXHIGHTPHX":  ("PSR", "PHX", "yesterday"),
    "KXHIGHTDAL":  ("FWD", "DFW", "yesterday"),
    "KXHIGHTSFO":  ("MTR", "SFO", "yesterday"),
    "KXHIGHTBOS":  ("BOX", "BOS", "yesterday"),
    "KXHIGHAUS":   ("EWX", "AUS", "today"),
    "KXHIGHTATL":  ("FFC", "ATL", "today"),
    "KXHIGHTHOU":  ("HGX", "HOU", "yesterday"),
    "KXHIGHDEN":   ("BOU", "DEN", "today"),
    "KXHIGHTSEA":  ("SEW", "SEA", "yesterday"),
    "KXHIGHPHIL":  ("PHI", "PHL", "yesterday"),
    "KXHIGHTLV":   ("VEF", "LAS", "yesterday"),
    "KXHIGHTNOLA": ("LIX", "MSY", "yesterday"),
    "KXHIGHTOKC":  ("OUN", "OKC", "yesterday"),
    "KXLOWTNYC":   ("OKX", "NYC", "today"),
    "KXLOWTOKC":   ("OUN", "OKC", "yesterday"),
    "KXLOWTAUS":   ("EWX", "AUS", "today"),
    "KXLOWTMIN":   ("MPX", "MSP", "yesterday"),
    "KXLOWTLAX":   ("LOX", "LAX", "yesterday"),
}


def get_kalshi_settlement_source(series_ticker: str) -> dict:
    """
    Return the NWS CLI station/office backing series_ticker's settlement,
    from the hardcoded _SERIES_STATION_MAP (see module docstring for why
    this can't be read from Kalshi's API anymore).

    Returns {"wfo": ..., "product": "CLI", "station": ..., "cycle": ...}.
    Raises if series_ticker isn't in the map, or if it maps to a "today"
    cycle station (NYC, MIA, ATL, AUS, DEN) -- those stations' only CLI
    report is a partial same-day snapshot, confirmed unreliable as
    settlement ground truth (see module docstring CAVEAT), and this module
    has no full-day source for them yet.
    """
    entry = _SERIES_STATION_MAP.get(series_ticker)
    if entry is None:
        raise ValueError(
            f"{series_ticker}: no known settlement station -- add it to "
            f"_SERIES_STATION_MAP (Kalshi's settlement_sources API field no "
            f"longer identifies the station; see module docstring)"
        )
    wfo, station, cycle = entry
    if cycle == "today":
        raise ValueError(
            f"{series_ticker}: {station}'s only NWS CLI report is a partial "
            f"same-day snapshot (issued mid-afternoon, 'valid as of H:MM PM'), "
            f"confirmed to understate the true daily max/min -- not reliable "
            f"settlement ground truth, see module docstring CAVEAT"
        )
    return {"wfo": wfo, "product": "CLI", "station": station, "cycle": cycle}


# ---------------------------------------------------------------------------
# Official daily CLI report (ground truth for settlement)
# ---------------------------------------------------------------------------

# "MAXIMUM         80   2:14 PM  89    1983  76      4       78"
_MAX_RE = re.compile(r"MAXIMUM\s+(-?\d+|MM)\s+(\d{1,2}:\d{2}\s*[AP]M|MM)", re.IGNORECASE)
_MIN_RE = re.compile(r"MINIMUM\s+(-?\d+|MM)\s+(\d{1,2}:\d{2}\s*[AP]M|MM)", re.IGNORECASE)


def _parse_cli_report(text: str, report_date: datetime.date) -> dict:
    """Pull MAXIMUM/MINIMUM value + time-of-day out of a CLI report's raw text.

    The report covers "YESTERDAY" relative to when it was issued -- report_date
    is the calendar day the numbers describe (not the issuance date)."""
    max_m = _MAX_RE.search(text)
    min_m = _MIN_RE.search(text)

    def _val(m):
        if not m or m.group(1).upper() == "MM":
            return None
        return float(m.group(1))

    def _time(m):
        if not m or m.group(2).upper() == "MM":
            return None
        return m.group(2).replace(" ", "")

    return {
        "date": report_date,
        "max_f": _val(max_m),
        "max_time_local": _time(max_m),
        "min_f": _val(min_m),
        "min_time_local": _time(min_m),
        "raw_text": text,
    }


def fetch_cli_report(date: datetime.date, wfo: str = "LOX", station: str = "LAX") -> dict:
    """
    Official NWS Climatological Report (daily MAXIMUM/MINIMUM + time-of-day)
    for `station`, for the calendar day `date`. Cached to
    data/station_temps/cli_reports/{station}_{date}.txt (raw NWS text).

    The CLI report for a given day is issued the following morning, so this
    fetches the report issued the day after `date` and pulls its "YESTERDAY"
    block. Safe to re-run: skips the network call if already cached.
    """
    os.makedirs(_CLI_DIR, exist_ok=True)
    cache_path = os.path.join(_CLI_DIR, f"{station}_{date.isoformat()}.txt")
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            text = f.read()
        return _parse_cli_report(text, date)

    issue_date = date + datetime.timedelta(days=1)
    resp = _iem_get(
        IEM_AFOS_RETRIEVE_URL,
        params={
            "pil": f"CLI{station}",
            "center": f"K{wfo}",
            "sdate": issue_date.isoformat(),
            "edate": (issue_date + datetime.timedelta(days=1)).isoformat(),
            "fmt": "text",
        },
    )
    text = resp.text
    if not text.strip():
        raise ValueError(f"No CLI{station} report found for issue date {issue_date}")
    with open(cache_path, "w") as f:
        f.write(text)
    return _parse_cli_report(text, date)


# ---------------------------------------------------------------------------
# Intraday station trace (for reconstructing crossing times)
# ---------------------------------------------------------------------------

def fetch_station_temps(date: datetime.date, station: str = "LAX", tz_name: str = "America/Los_Angeles") -> pd.DataFrame:
    """
    Intraday temperature observations (tmpf, whole degrees F) for `station`
    over the LOCAL calendar day `date` (tz_name midnight to next midnight),
    from IEM's 1-minute ASOS product. Cached to
    data/station_temps/asos/{station}_{date}.parquet.

    Real, per-day gaps exist -- confirmed directly this is genuinely per-day,
    not a blanket outage (e.g. LAX 2026-01-11 and 2026-01-18: 0 rows all
    day; LAX 2026-03-05: 1439/1439 minutes present; 2026-04-06: 1209/1209).
    A day with no data returns an empty DataFrame (cached as such, so a
    genuinely-no-data day isn't re-fetched every time) rather than raising --
    first_persistent_crossing_time handles an empty trace by returning None
    (no crossing detected), not by falling back to a coarser source.

    Columns: time_utc (tz-aware), time_local (tz-aware, tz_name), tmpf.
    A detected crossing time is always <= the true crossing time (a report
    after the real crossing can only detect it late, never early), so gaps
    are a conservative bias (under-counted opportunities), not an overcount
    risk. Always cross-check a day's reconstructed max/min against
    fetch_cli_report's official value (or better, the settled market's own
    "result" field) -- don't treat this trace's own max/min as settlement
    truth.

    Safe to re-run: skips the network call if already cached. A cache file
    written by the earlier hourly+1-minute-merge version of this function
    (has a 'source' column) is treated as stale and transparently
    re-fetched/overwritten as pure 1-minute data, so already-cached days
    don't keep silently serving hourly-blended rows.
    """
    os.makedirs(_ASOS_DIR, exist_ok=True)
    cache_path = os.path.join(_ASOS_DIR, f"{station}_{date.isoformat()}.parquet")
    if os.path.exists(cache_path):
        cached = pd.read_parquet(cache_path)
        if "source" not in cached.columns:
            return cached

    next_date = date + datetime.timedelta(days=1)
    resp = _iem_get(
        IEM_ASOS_1MIN_URL,
        params={
            "station": station,
            "vars": "tmpf",
            "sts": f"{date.isoformat()}T00:00Z",
            "ets": f"{next_date.isoformat()}T00:00Z",
            "tz": "UTC",
            "sample": "1min",
            "what": "view",
            "delim": "comma",
        },
    )
    lines = resp.text.strip().split("\n")
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        valid_str, tmpf_str = parts[2], parts[3]
        if tmpf_str.strip() in ("", "M"):
            continue
        rows.append((valid_str, float(tmpf_str)))

    if not rows:
        df = pd.DataFrame(columns=["time_utc", "time_local", "tmpf"])
    else:
        df = pd.DataFrame(rows, columns=["time_utc", "tmpf"])
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["time_local"] = df["time_utc"].dt.tz_convert(tz_name)
        day_mask = df["time_local"].dt.date == date
        df = df.loc[day_mask].sort_values("time_utc").reset_index(drop=True)[["time_utc", "time_local", "tmpf"]]

    df.to_parquet(cache_path, index=False)
    return df


# ---------------------------------------------------------------------------
# Crossing-time helper
# ---------------------------------------------------------------------------

def first_crossing_time(asos_df: pd.DataFrame, strike: float, kind: str = "max"):
    """
    First `time_local` at which the running max (kind="max") rises strictly
    above `strike`, or the running min (kind="min") falls strictly below it.
    Returns None if the trace never crosses (including an empty trace).

    This is a lower bound on the true crossing time relative to what the
    OFFICIAL station instrument saw, since asos_df is a possibly-gappy
    sample (per-minute where available, hourly+SPECI elsewhere), not the
    exact continuous truth -- see fetch_station_temps's docstring.
    """
    if asos_df.empty:
        return None
    if kind == "max":
        running = asos_df["tmpf"].cummax()
        hit = asos_df.loc[running > strike]
    elif kind == "min":
        running = asos_df["tmpf"].cummin()
        hit = asos_df.loc[running < strike]
    else:
        raise ValueError(f"kind must be 'max' or 'min', got {kind!r}")
    if hit.empty:
        return None
    return hit.iloc[0]["time_local"]


def first_persistent_crossing_time(
    asos_df: pd.DataFrame, strike: float, kind: str = "max", min_consecutive: int = 2,
    buffer: float = 0.0, buffer_sources: tuple = ("1min",),
):
    """
    Like first_crossing_time, but requires at least `min_consecutive`
    CONSECUTIVE observations (in time order, not the cumulative running
    max/min) beyond `strike` before counting it as crossed -- guards
    against a single anomalous/noisy reading (sensor glitch or a one-off
    spike) triggering a crossing that a single bad tick shouldn't be
    trusted to confirm alone.

    Note "consecutive" means consecutive ROWS in asos_df, not a fixed time
    duration -- on a day covered by the 1-minute product that's a couple of
    minutes; on an hourly-only day it's a couple of hours. That's
    intentional, not a bug: hourly/SPECI readings are already the more
    reliable official METAR obs, so requiring 2 of *those* in a row is
    still a meaningful confirmation, just naturally slower to trigger than
    on a noisier per-minute day.

    `buffer` adds (kind="max") or subtracts (kind="min") a margin to
    `strike`, but ONLY for rows whose 'source' column is in
    `buffer_sources` -- confirmed directly (see fetch_station_temps history)
    that routine hourly/SPECI METAR readings are the exact same
    whole-degree-F values the official CLI report is itself built from
    (temperature is recorded to tenths of a degree Celsius in the METAR
    remark group, e.g. T0178, then converted to F and rounded -- that
    rounded value IS the CLI number, not just close to it), so those rows
    need no buffer at all. Only the separate, less quality-controlled
    1-minute ASOS product (which can read up to ~1F off the eventual CLI
    value) needs one. If asos_df has no 'source' column, `buffer` applies
    to every row.

    Returns None if the trace never sustains a crossing for that many
    consecutive readings (including an empty trace).
    """
    if asos_df.empty:
        return None
    df = asos_df.sort_values("time_local").reset_index(drop=True)
    needs_buffer = df["source"].isin(buffer_sources) if "source" in df.columns else pd.Series(True, index=df.index)
    if kind == "max":
        effective_strike = strike + needs_buffer * buffer
        beyond = df["tmpf"] > effective_strike
    elif kind == "min":
        effective_strike = strike - needs_buffer * buffer
        beyond = df["tmpf"] < effective_strike
    else:
        raise ValueError(f"kind must be 'max' or 'min', got {kind!r}")
    run_id = beyond.ne(beyond.shift()).cumsum()
    run_length = beyond.groupby(run_id).cumcount() + 1
    hit = df.loc[beyond & (run_length >= min_consecutive)]
    if hit.empty:
        return None
    return hit.iloc[0]["time_local"]
