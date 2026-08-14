"""
LA temp-market crossing arb, ported from kalshi_quoting_arbs/fill_backtest.ipynb
onto the kalshi_backtest engine -- the first real strategy run through the
engine, both a working example of the calling convention and a sanity check
that the engine's fill/PnL numbers are directionally consistent with the
notebook's existing ad hoc analysis.

Once the running max temperature for the day passes a market's own
guarantee threshold, that market's outcome is locked on one side (see the
notebook's markdown cell for the exact three cases: '>strike' tail
guaranteed YES, '<strike' tail guaranteed NO, 'between' bucket guaranteed
NO). Resting a bid at FILL_BID on the guaranteed side is close to
risk-free -- this strategy detects that crossing (the same
first_persistent_crossing_time guards as the notebook) and rests exactly
one order per (day, market) at the moment the crossing becomes known.

The crossing signal itself comes from station-temperature data, not Kalshi
market data -- it's precomputed and fed into Backtest.run() as
`external_events` rather than derived from anything in the quote/trade
stream. See find_crossing_signals().
"""
import datetime
import os
import sys

_INFRA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "kalshi_infra")
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)

import pandas as pd
import predexon_functions as pf
import station_temp_data as std

FILL_BID = 0.99
ROUNDING_BUFFER_F = 1.0
MIN_CONSECUTIVE_OBS = 2


def find_crossing_signals(event_ticker: str, order_size: float = 100.0) -> list[tuple]:
    """
    Precompute (true_time, payload) tuples for every market in
    `event_ticker` that has a real, station-confirmed crossing -- payload is
    a dict {ticker, side, price, size} describing the resting order to
    place. Feed straight into Backtest.run(external_events=...).

    Returns [] (no crossings this day, or no station data at all -- a real
    IEM per-day gap, see fetch_station_temps's docstring) rather than
    raising.
    """
    series = event_ticker.split("-")[0]
    date = datetime.datetime.strptime(event_ticker.split("-", 1)[1], "%y%b%d").date()
    src = std.get_kalshi_settlement_source(series)
    try:
        trace = std.fetch_station_temps(date, station=src["station"])
    except Exception:
        return []
    if trace.empty:
        return []

    markets = pf.get_event_markets(event_ticker)
    signals = []
    for m in markets:
        try:
            floor, cap = pf._parse_strike_from_ticker(m["ticker"])
        except ValueError:
            continue
        strike_type = m.get("strike_type")
        if strike_type == "greater":
            side, strike = "yes", floor
        elif strike_type == "less":
            side, strike = "no", floor
        elif strike_type == "between" and cap is not None:
            side, strike = "no", cap
        else:
            continue
        t_cross = std.first_persistent_crossing_time(
            trace, strike, kind="max", min_consecutive=MIN_CONSECUTIVE_OBS, buffer=ROUNDING_BUFFER_F)
        if t_cross is None:
            continue
        t_cross_utc = pd.Timestamp(t_cross).tz_convert("UTC")
        signals.append((t_cross_utc, {
            "ticker": m["ticker"], "side": side, "price": FILL_BID, "size": order_size,
        }))
    return signals


def strategy_fn(ctx):
    """The Strategy callable itself: only acts on 'external' (crossing)
    wakeups, placing exactly one resting buy per signal. Market-data
    wakeups (quotes/trades becoming visible) are ignored -- this strategy's
    trigger is the station temperature, not book activity, by construction
    (see module docstring). ctx.last_event is the crossing signal dict for
    an external wakeup, or a Quote/Trade object for a market-data wakeup --
    the isinstance check is what tells them apart."""
    if not isinstance(ctx.last_event, dict):
        return
    sig = ctx.last_event
    ctx.buy(ticker=sig["ticker"], side=sig["side"], size=sig["size"],
            price=sig["price"], order_type="limit")
