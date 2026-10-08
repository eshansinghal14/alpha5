"""
Event state: one object per in-flight KXCRYPTOLEAD15M event.

Owns:
  * `EventWindow` (times)
  * market tickers (one per coin)
  * `SettlementTracker` (feeds composite bars in, surfaces TWAPs)
  * `LipParams` discovered from Kalshi's /incentive_programs endpoint
  * Current fair-value snapshot (produced by the pricer, re-cached per
    composite tick)

Discovery of events: the Kalshi series ticker is KXCRYPTOLEAD15M.
Opening events roll every 15 minutes. `resolve_live_event_ticker()`
picks the soonest-closing OPEN event (never a future scheduled one --
until it opens we can't trade it) or, in `--event-ticker` mode,
whatever the operator asked for.
"""
from __future__ import annotations

import dataclasses
import math
import os
import sys
import time

import pandas as pd

from .config import CoinRaceConfig, CRYPTO_FEE_MULTIPLIER, CRYPTO_MAKER_FEE_MULTIPLIER
from .series import (SERIES_TICKER, COINS, TICK, market_ticker,
                     coin_from_market_ticker)
from .settlement_tracker import EventWindow, SettlementTracker
from .lip_scorer import LipParams


# Register the project-wide sys.path shims this package needs when
# running inside the alpha5 repo without an installed package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_INFRA_DIR = os.path.join(_HERE, "..", "kalshi_infra")
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)


def _fetch_json(path, params=None, auth=False, max_retries=8):
    """Thin wrapper so this module doesn't have to import api_functions
    at module load time. Lets tests run without the key file present."""
    import api_functions
    return api_functions.kalshi_get(path, params=params, auth=auth, max_retries=max_retries)


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

def discover_open_events() -> list[dict]:
    """Open KXCRYPTOLEAD15M events, soonest-closing first. Returns full
    event records (dicts with `event_ticker`, `close_time`, markets).
    """
    data = _fetch_json("/trade-api/v2/markets",
                       params={"series_ticker": SERIES_TICKER, "status": "open", "limit": 200})
    markets = data.get("markets", [])
    events: dict[str, dict] = {}
    for m in markets:
        et = m["event_ticker"]
        if et not in events:
            events[et] = {"event_ticker": et, "close_time": m.get("close_time"),
                          "open_time": m.get("open_time"), "markets": []}
        events[et]["markets"].append(m)
    out = list(events.values())
    out.sort(key=lambda e: e.get("close_time") or "")
    return out


def resolve_live_event_ticker(event_ticker_override: str | None = None) -> str:
    """`--event-ticker` wins. Else: the soonest-closing OPEN event.

    Unlike KXINX's "pick the 16:00 daily event" rule, this series rolls
    every 15 minutes, so soonest-closing is always what we want -- there
    is no equivalent of "an hourly that's about to expire" to avoid.
    """
    if event_ticker_override:
        return event_ticker_override
    # Kalshi keeps a market "open" for a few seconds past its close_time;
    # without this filter the just-ended event is picked again at rollover.
    now = pd.Timestamp.now(tz="UTC")
    events = [e for e in discover_open_events()
              if not e.get("close_time") or pd.Timestamp(e["close_time"]) > now]
    if not events:
        raise ValueError(f"no open {SERIES_TICKER} events right now")
    return events[0]["event_ticker"]


# ---------------------------------------------------------------------------
# Market discovery for one event
# ---------------------------------------------------------------------------

def event_markets(event_ticker: str) -> dict[str, dict]:
    """Returns {coin: market_record} for the 5 markets of `event_ticker`,
    pulled from Kalshi's REST. Fails loud if any coin is missing -- a
    partial event is almost certainly a Kalshi API transient and
    retrying is better than quoting against 4 of 5 markets."""
    data = _fetch_json("/trade-api/v2/markets",
                       params={"event_ticker": event_ticker, "limit": 50})
    out: dict[str, dict] = {}
    for m in data.get("markets", []):
        coin = coin_from_market_ticker(m["ticker"])
        if coin is not None:
            out[coin] = m
    missing = [c for c in COINS if c not in out]
    if missing:
        raise ValueError(f"{event_ticker}: markets missing for {missing}")
    return out


def parse_event_window(markets: dict[str, dict], event_ticker: str) -> EventWindow:
    """Pull S and T from the event's market records.

    S = open_time; T = close_time. Markets are expected to share both
    across all 5 coins (same event). If they differ (shouldn't happen on
    this series) we use the latest `open_time` and the earliest
    `close_time`, which is the safest -- it is strictly inside the
    actual trading window for every leg.
    """
    opens = [pd.Timestamp(m["open_time"]).timestamp() for m in markets.values() if m.get("open_time")]
    closes = [pd.Timestamp(m["close_time"]).timestamp() for m in markets.values() if m.get("close_time")]
    if not opens or not closes:
        raise ValueError(f"{event_ticker}: missing open/close times on markets")
    return EventWindow(event_ticker=event_ticker, S=max(opens), T=min(closes))


# ---------------------------------------------------------------------------
# LIP params lookup
# ---------------------------------------------------------------------------

def fetch_lip_params(markets: dict[str, dict], config: CoinRaceConfig
                     ) -> dict[str, LipParams]:
    """{coin: LipParams} for one event. Uses
    `/incentive_programs?status=active`.

    If the API misses one or more of the event's markets (incentive
    program not yet listed, program finished, intermittent 500) we fall
    back to `config.lip_target_size_default` / `lip_discount_factor
    _default` / `period_reward=0` -- the zero-reward case makes the LIP
    credit term in the quoter identically zero for that market, so we
    quote on edge alone, which is the correct behaviour when the
    program isn't running.
    """
    out: dict[str, LipParams] = {}
    tickers = {m["ticker"] for m in markets.values()}
    try:
        data = _fetch_json("/trade-api/v2/incentive_programs",
                           params={"status": "active"}, auth=True)
    except Exception as e:
        print(f"[kalshi_crypto] WARN: /incentive_programs lookup failed ({e!r}); "
              f"using defaults for all markets")
        data = {}
    programs_by_ticker: dict[str, dict] = {}
    for p in data.get("incentive_programs", []):
        mt = p.get("market_ticker")
        if mt in tickers:
            programs_by_ticker[mt] = p
    for coin, m in markets.items():
        p = programs_by_ticker.get(m["ticker"])
        if p is None:
            out[coin] = LipParams(
                target_size=config.lip_target_size_default,
                discount_factor=config.lip_discount_factor_default,
                period_reward_usd=0.0,
                snapshots_per_period=config.lip_snapshots_per_period,
            )
            continue
        target_fp = _as_float(p.get("target_size_fp"), config.lip_target_size_default)
        df_bps = _as_float(p.get("discount_factor_bps"), config.lip_discount_factor_default * 10_000)
        df = df_bps / 10_000.0
        raw_reward = _as_float(p.get("period_reward"), 0.0)
        # The API's `period_reward` unit is NOT confirmed. Spec defaults
        # to a centi-cent conversion (divide by 10000 -> dollars) based
        # on the observed 200000 / discount pattern yielding ~$20 per
        # 15-min event. All we can do is apply the config divisor and
        # let the reconciliation loop in strategy.py log the gap vs
        # realised payouts.
        reward_usd = raw_reward / config.lip_period_reward_divisor
        out[coin] = LipParams(
            target_size=target_fp,
            discount_factor=df,
            period_reward_usd=reward_usd,
            snapshots_per_period=config.lip_snapshots_per_period,
        )
    return out


def _as_float(v, default):
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# EventState -- live container
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class EventState:
    """Everything the strategy needs to decide for one event.

    `fair_result` is written by the strategy on every composite update
    (and whenever Kalshi book events require a re-score); the quoter
    reads it. `last_bar_ts` holds the ts of the most recent composite
    bar, so a stale-feed check can be made without re-walking the
    settlement tracker's deque."""
    event_ticker: str
    window: EventWindow
    markets: dict[str, dict]              # coin -> Kalshi market record
    tickers: dict[str, str]               # coin -> market_ticker
    tracker: SettlementTracker
    lip_params: dict[str, LipParams]      # coin -> LipParams
    fair_result: object | None = None     # pricer.FairResult (not imported to avoid a cycle)
    last_bar_ts: float = 0.0
    fee_multiplier: float = CRYPTO_FEE_MULTIPLIER
    maker_fee_multiplier: float = CRYPTO_MAKER_FEE_MULTIPLIER


def build_event_state(event_ticker: str, config: CoinRaceConfig) -> EventState:
    markets = event_markets(event_ticker)
    window = parse_event_window(markets, event_ticker)
    tracker = SettlementTracker(window)
    lip_params = fetch_lip_params(markets, config)
    tickers = {c: market_ticker(event_ticker, c) for c in COINS}
    return EventState(
        event_ticker=event_ticker, window=window, markets=markets,
        tickers=tickers, tracker=tracker, lip_params=lip_params,
    )


__all__ = [
    "EventState", "build_event_state",
    "discover_open_events", "resolve_live_event_ticker",
    "event_markets", "parse_event_window", "fetch_lip_params",
]
