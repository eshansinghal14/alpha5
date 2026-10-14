"""
IB TWS/Gateway helpers for live SPXW option streaming.

Live-only. Historical option quotes still come from databento_functions
(IB's historical option endpoints are too slow and line-limited for a full
chain). See docs/ib-migration.md for the overall shape.
"""
import datetime
import logging
import os
import sys

from zoneinfo import ZoneInfo

from kalshi_infra import keys

# Kalshi-side helpers are venue-agnostic; reusing them keeps backtest and
# live paths selecting the same strikes from the same bucket boundaries.
from kalshi_infra.databento_functions import (_kxinx_event_markets, bracket_listed_strikes,
                                 select_event_buckets, IB_MARKET_DATA_LINES)

ET = ZoneInfo("America/New_York")

# 10090 "part of requested market data is not subscribed": fires once per
# SPXW contract because the account lacks the SPX index feed. The OPRA leg
# still streams (verified 2026-10-08, 22/22 contracts ticking), so it's noise.
IGNORED_IB_ERROR_CODES = (10090,)


class _DropIBCodes(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(msg.startswith(f"Error {c},") for c in IGNORED_IB_ERROR_CODES)


def quiet_ib_logging() -> None:
    log = logging.getLogger("ib_async.wrapper")
    if not any(isinstance(f, _DropIBCodes) for f in log.filters):
        log.addFilter(_DropIBCodes())


def _import_ib():
    """Deferred so backtest-only environments don't need the IB client
    installed. ib_async is the actively maintained fork of ib_insync; try
    it first, fall back to the original."""
    try:
        import ib_async as ib_mod       # type: ignore[import-not-found]
        return ib_mod
    except ImportError:
        pass
    try:
        import ib_insync as ib_mod      # type: ignore[import-not-found]
        return ib_mod
    except ImportError as exc:
        raise ImportError(
            "live SPXW streaming needs ib_async (preferred) or ib_insync installed:\n"
            "    pip install ib_async\n"
            "and a running TWS or IB Gateway on "
            f"{keys.IB_HOST}:{keys.IB_PORT}"
        ) from exc


def make_spxw_option(expiry: datetime.date, strike: float, right: str):
    """One SPXW option Contract, ready for IB.qualifyContractsAsync.

    tradingClass="SPXW" is what distinguishes the weeklies from the AM-
    settled monthly SPX chain -- same symbol, different contracts.
    """
    ib_mod = _import_ib()
    return ib_mod.Option(
        symbol="SPXW",
        lastTradeDateOrContractMonth=expiry.strftime("%Y%m%d"),
        strike=float(strike),
        right=right.upper(),
        exchange="CBOE",
        multiplier="100",
        currency="USD",
        tradingClass="SPXW",
    )


def _bucket_key(m: dict) -> float:
    """Sort position of a bucket on the price axis. A "below X" tail has only
    a cap; it sorts just before the bucket whose floor is X."""
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    if m.get("cap_strike") is not None:
        return float(m["cap_strike"]) - 1.0
    return float("nan")


def ordered_tickers(markets: list[dict]) -> list[str]:
    """Event's bucket tickers in price order (unpriceable buckets dropped)."""
    ms = [m for m in markets if _bucket_key(m) == _bucket_key(m)]
    return [m["ticker"] for m in sorted(ms, key=_bucket_key)]


def kalshi_centre(event_ticker: str) -> str:
    """Bucket nearest 50/50 on Kalshi's own touch -- the scope's centre
    when there is no option-implied price yet."""
    t = select_event_buckets(event_ticker, 1)
    if not t:
        raise ValueError(f"no quoted buckets on {event_ticker}")
    return t[0]


def market_boundaries(markets: list[dict]) -> set[float]:
    return {float(m[k]) for m in markets for k in ("floor_strike", "cap_strike") if m.get(k) is not None}


def strikes_for_boundaries(boundaries: set[float]) -> list[float]:
    """SPXW strikes to subscribe for these Kalshi boundaries: the nearest
    listed strike to each, plus its neighbours."""
    if not boundaries:
        return []
    # Snapped to the $5 grid: KXINXDUD's strike is the prior close (e.g.
    # 7808.69), and stepping from lo - pad gave 7803/7808/7813, none listed.
    # qualifyContractsAsync drops anything the chain doesn't list, so this
    # only has to be a superset.
    lo, hi = min(boundaries), max(boundaries)
    pad = 25.0
    start = int((lo - pad) // 5) * 5
    listed = [float(k) for k in range(start, int(hi + pad) + 1, 5)]
    return sorted(bracket_listed_strikes(boundaries, listed))


def resolve_event_strikes(event_ticker: str, tickers: list[str] | None = None) -> dict:
    """Which SPXW strikes and expiry to subscribe to for `event_ticker`.

    Pure Kalshi-side computation -- no IB call. `tickers` scopes the
    universe to those buckets' boundaries (same convention as
    load_kxinx_event_option_quotes). The full event's market list still
    flows through unchanged so every bucket is priced.
    """
    all_markets = _kxinx_event_markets(event_ticker)
    if not all_markets:
        raise ValueError(f"no Kalshi markets found for {event_ticker}")
    if tickers is not None:
        markets = [m for m in all_markets if m["ticker"] in set(tickers)]
        if not markets:
            raise ValueError(f"none of {tickers} found in {event_ticker}")
    else:
        markets = all_markets

    close_time = datetime.datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    expiry = close_time.astimezone(ET).date()
    boundaries = market_boundaries(markets)
    if not boundaries:
        raise ValueError(f"no floor/cap strikes on any {event_ticker} market")
    strikes = strikes_for_boundaries(boundaries)

    return {
        "markets": all_markets,     # every bucket -- passed to live_pricing
        "scoped_markets": markets,  # the tickers-scoped subset
        "expiry": expiry,
        "strikes": strikes,
        "boundaries": boundaries,
    }
