"""Reads what the live runtime writes (kalshi_infra.runtime.persist) and
the strategy registry. Pure readers -- the dashboard never launches a session."""
from __future__ import annotations

import dataclasses
import json
import os

import pandas as pd

from kalshi_infra.runtime import journal
from kalshi_infra.runtime.persist import day_dir, stream_filename


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


def _parquet(path: str) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except (OSError, ValueError):
        return pd.DataFrame()


def load_day(series: str, date: str | None = None, *, use_journal: bool = True) -> LiveDay | None:
    """None if no session has written today. Decisions/fills fall back to
    the day's journal when it has more rows (the snapshot is only the current
    session; a restart or an arm starts it over). PnL stays session-scoped:
    splicing two ledgers' curves means neither."""
    date = date or today()
    summary = read_summary(series, date)
    if summary is None:
        return None
    d = day_dir(series, date)
    q, f = _parquet(os.path.join(d, "quote_df.parquet")), _parquet(os.path.join(d, "fills_df.parquet"))
    if use_journal:
        jq, jf = journal.read_journal(series, date, "decisions"), journal.read_journal(series, date, "fills")
        if len(jq) > len(q):
            q = jq
        if len(jf) > len(f):
            f = jf
    if not f.empty and "yes_equiv_price" not in f.columns:
        f["yes_equiv_price"] = f["price"].where(f["side"] == "yes", 1.0 - f["price"])
    return LiveDay(series=series, date=date, summary=summary, quote_df=q, fills_df=f,
                   pnl_df=_parquet(os.path.join(d, "pnl_df.parquet")),
                   fair_df=_parquet(os.path.join(d, "fair_df.parquet")))


def stream_url(series: str, ticker: str) -> str:
    return f"/app/static/{series}/{stream_filename(ticker)}"


def summary_age_s(summary: dict) -> float | None:
    ts = summary.get("last_update")
    return None if ts is None else (pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)).total_seconds()
