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


_FRAME_CACHE: dict[str, tuple[tuple, pd.DataFrame]] = {}


def _frame(path: str, columns: list[str] | None, event_ticker: str | None,
           since: pd.Timestamp | None) -> pd.DataFrame:
    """The file's rows of the running event and session, read and filtered
    once per write of the file: the writer replaces it every 3s and every
    open tab reruns this every few seconds."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return pd.DataFrame()
    key = (mtime, event_ticker, since)
    hit = _FRAME_CACHE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        df = pd.read_parquet(path, columns=columns)
    except (OSError, ValueError):
        # Columns missing from an older writer's file, or a read racing the replace.
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError):
            return hit[1] if hit is not None else pd.DataFrame()
    df = _current(df, event_ticker, since)
    _FRAME_CACHE[path] = (key, df)
    return df


def _current(df: pd.DataFrame, event_ticker: str | None, since: pd.Timestamp | None) -> pd.DataFrame:
    """Rows of the running event and session only. The snapshot files are
    rewritten per session, but a parquet from before a rollover or arm can
    still be on disk until the writer's next heavy write replaces it."""
    if df.empty:
        return df
    keep = pd.Series(True, index=df.index)
    if event_ticker and "ticker" in df.columns:
        prefix = event_ticker + "-"
        ours = [t for t in df["ticker"].unique() if str(t).startswith(prefix)]
        keep &= df["ticker"].isin(ours)
    if since is not None and "time" in df.columns:
        t = df["time"]
        if not isinstance(t.dtype, pd.DatetimeTZDtype):
            t = pd.to_datetime(t, utc=True)
        keep &= t >= since
    return df if keep.all() else df[keep]


def load_day(series: str, date: str | None = None) -> LiveDay | None:
    """The running session only: its event's markets, from session_started.
    None if no session has written today. The day journal is the record of
    everything else and is deliberately not read here (it reached 188MB by
    midday on 2026-10-09). quote_df and fair_df carry only the columns the
    page reads."""
    date = date or today()
    summary = read_summary(series, date)
    if summary is None:
        return None
    d = day_dir(series, date)
    ev = summary.get("event_ticker")
    since = pd.Timestamp(summary["session_started"]) if summary.get("session_started") else None
    q = _frame(os.path.join(d, "quote_df.parquet"), ["time", "ticker", "atm_tradeable"], ev, since)
    f = _frame(os.path.join(d, "fills_df.parquet"), None, ev, since)
    if not f.empty and "yes_equiv_price" not in f.columns:
        f = f.assign(yes_equiv_price=f["price"].where(f["side"] == "yes", 1.0 - f["price"]))
    return LiveDay(series=series, date=date, summary=summary, quote_df=q, fills_df=f,
                   fair_df=_frame(os.path.join(d, "fair_df.parquet"), ["time", "ticker", "prob"], ev, since))


def stream_url(series: str, ticker: str) -> str:
    return f"/app/static/{series}/{stream_filename(ticker)}"


def has_lip(summary: dict | None) -> bool:
    """Whether the running event has any LIP program (S&P events have none)."""
    lip = (summary or {}).get("lip") or {}
    return bool(lip.get("period_reward_usd"))


def summary_age_s(summary: dict) -> float | None:
    ts = summary.get("last_update")
    return None if ts is None else (pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)).total_seconds()
