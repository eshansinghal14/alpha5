"""Reads what the live runtime writes (kalshi_infra.runtime.persist) and
the strategy registry. Pure readers -- the dashboard never launches a session."""
from __future__ import annotations

import dataclasses
import json
import os

import pandas as pd

from kalshi_infra.runtime.control import trading_mode_path
from kalshi_infra.runtime.persist import LIVE_ROOT, day_dir, stream_filename


@dataclasses.dataclass
class LiveDay:
    series: str
    date: str
    summary: dict
    quote_df: pd.DataFrame
    fills_df: pd.DataFrame
    pnl_df: pd.DataFrame
    fair_df: pd.DataFrame


def today() -> str:
    return pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")


def read_summary(series: str, date: str | None = None) -> dict | None:
    path = os.path.join(day_dir(series, date or today()), "summary.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def latest_summary(series: str) -> tuple[str, dict] | None:
    """(date, summary) of the most recent day that has one."""
    root = os.path.join(LIVE_ROOT, series)
    try:
        dates = sorted((d for d in os.listdir(root) if d[:2] == "20"), reverse=True)
    except OSError:
        return None
    for d in dates:
        s = read_summary(series, d)
        if s is not None:
            return d, s
    return None


def read_trading_mode(series: str) -> tuple[bool, pd.Timestamp] | None:
    """(live_trading, file mtime) of the last arm/disarm request."""
    path = trading_mode_path(series)
    try:
        with open(path) as fh:
            want = bool(json.load(fh).get("live_trading", False))
        return want, pd.Timestamp(os.path.getmtime(path), unit="s", tz="UTC")
    except (OSError, json.JSONDecodeError):
        return None


_PARQUET_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}


def _parquet(path: str) -> pd.DataFrame:
    """Re-read only when the writer has replaced the file; every widget
    interaction reruns the whole script."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return pd.DataFrame()
    hit = _PARQUET_CACHE.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError):
        return hit[1] if hit is not None else pd.DataFrame()
    _PARQUET_CACHE[path] = (mtime, df)
    return df


def _current(df: pd.DataFrame, event_ticker: str | None, since: pd.Timestamp | None) -> pd.DataFrame:
    """Rows of the running event and session only. The snapshot files are
    rewritten per session, but a parquet from before a rollover or arm can
    still be on disk until the writer's next heavy write replaces it."""
    if df.empty:
        return df
    keep = pd.Series(True, index=df.index)
    if event_ticker and "ticker" in df.columns:
        keep &= df["ticker"].astype(str).str.startswith(event_ticker + "-")
    if since is not None and "time" in df.columns:
        keep &= pd.to_datetime(df["time"], utc=True) >= since
    return df if keep.all() else df[keep]


def load_day(series: str, date: str | None = None) -> LiveDay | None:
    """The running session only: its event's markets, from session_started.
    None if no session has written today. The day journal is the record of
    everything else and is deliberately not read here (it reached 188MB by
    midday on 2026-10-09)."""
    date = date or today()
    summary = read_summary(series, date)
    if summary is None:
        return None
    d = day_dir(series, date)
    ev = summary.get("event_ticker")
    since = pd.Timestamp(summary["session_started"]) if summary.get("session_started") else None
    q = _current(_parquet(os.path.join(d, "quote_df.parquet")), ev, since)
    f = _current(_parquet(os.path.join(d, "fills_df.parquet")), ev, since)
    if not f.empty and "yes_equiv_price" not in f.columns:
        f = f.assign(yes_equiv_price=f["price"].where(f["side"] == "yes", 1.0 - f["price"]))
    return LiveDay(series=series, date=date, summary=summary, quote_df=q, fills_df=f,
                   pnl_df=_current(_parquet(os.path.join(d, "pnl_df.parquet")), None, since),
                   fair_df=_current(_parquet(os.path.join(d, "fair_df.parquet")), ev, since))


def stream_url(series: str, ticker: str) -> str:
    return f"/app/static/{series}/{stream_filename(ticker)}"


def has_lip(summary: dict | None) -> bool:
    """Whether the running event has any LIP program (S&P events have none)."""
    lip = (summary or {}).get("lip") or {}
    return bool(lip.get("period_reward_usd"))


def summary_age_s(summary: dict) -> float | None:
    ts = summary.get("last_update")
    return None if ts is None else (pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)).total_seconds()
