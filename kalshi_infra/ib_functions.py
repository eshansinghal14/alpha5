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

# Buckets the strategy may trade at once (run_live sets max_markets to this).
# Bounded by Kalshi's order rate, not IB lines: each quoted market costs
# ~1.5 orders/s even with the requote throttle, against a 5/s per-series
# budget when both series run.
DEFAULT_ATM_BUCKETS = {"KXINX": 5, "KXINXU": 5}

# Extra buckets subscribed beyond the tradeable set, on each side, so the
# ATM set can drift (and ride atm_hysteresis) without leaving the priced
# region. KXINX buckets are 25pts wide, KXINXU thresholds 5pts. Lines this
# costs, measured 2026-10-08: KXINX 7 buckets -> 48, KXINXU 13 -> 30.
SCOPE_MARGIN_BUCKETS = {"KXINX": 1, "KXINXU": 4}

# Re-scope once the option-implied ATM bucket is this many buckets from the
# centre we subscribed around (or has reached the scope's edge). KXINX at 1
# ping-ponged every ~60s on 2026-10-08 with ATM sitting on a bucket boundary;
# the scope's margin already covers a 1-bucket move.
RESCOPE_DRIFT_BUCKETS = {"KXINX": 2, "KXINXU": 3}

# Each concurrent process needs its own clientId (a duplicate disconnects
# the older one), so the two series can run side by side.
CLIENT_ID_OFFSET = {"KXINX": 0, "KXINXU": 1}

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


def scope_around(markets: list[dict], centre_ticker: str, series: str) -> list[str]:
    """Contiguous window of buckets centred on `centre_ticker`: the tradeable
    count plus SCOPE_MARGIN_BUCKETS each side. Contiguous so the bracketing
    strikes overlap (cheapest in IB lines) and drift is measurable in buckets."""
    order = ordered_tickers(markets)
    i = order.index(centre_ticker)
    half = DEFAULT_ATM_BUCKETS[series] // 2 + SCOPE_MARGIN_BUCKETS[series]
    lo = max(0, min(i - half, len(order) - (2 * half + 1)))
    return order[lo:lo + 2 * half + 1]


def kalshi_centre(event_ticker: str) -> str:
    """Bucket nearest 50/50 on Kalshi's own touch -- the scope's centre
    when there is no option-implied price yet."""
    t = select_event_buckets(event_ticker, 1)
    if not t:
        raise ValueError(f"no quoted buckets on {event_ticker}")
    return t[0]


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

    boundaries: set[float] = set()
    for m in markets:
        for k in ("floor_strike", "cap_strike"):
            v = m.get(k)
            if v is not None:
                boundaries.add(float(v))
    if not boundaries:
        raise ValueError(f"no floor/cap strikes on any {event_ticker} market")

    # Candidate strikes: full $5 integer ladder spanning the boundaries plus
    # a $25 pad. qualifyContractsAsync drops anything the chain doesn't
    # actually list, so this just has to be a superset.
    lo, hi = min(boundaries), max(boundaries)
    pad = 25.0
    listed = [float(k) for k in range(int(lo - pad), int(hi + pad) + 1, 5)]
    strikes = sorted(bracket_listed_strikes(boundaries, listed))

    return {
        "markets": all_markets,     # every bucket -- passed to live_signal
        "scoped_markets": markets,  # the tickers-scoped subset
        "expiry": expiry,
        "strikes": strikes,
        "boundaries": boundaries,
    }
