"""
Kalshi's own start value per coin, to replace our start TWAP.

Kalshi's single-coin 15M series (KXBTC15M etc.) settle on the same 60s CF RTI
average our race starts from, and publish it as `expiration_value` a few
seconds after each boundary. Ranking coins by those values matched the race
winner on 182/182 settled events (2026-10-10..11), so the start is known
exactly once it's published -- our own TWAP missed it by 0.4-0.9bp rms per
coin, enough to swing a close race by 5-10c (26OCT111730: BTC +0.63bp,
HYPE -0.64bp, fairs off ~11c / 7c for the whole event).

Our end price is still our mid, which sits at a steady level offset from the
index (HYPE ~-2.7bp, XRP ~-1.5bp on 2026-10-11). Using Kalshi's start raw
would put that offset into every return, so the anchor is
    K_S * exp(mean over recent events of ln(our_start_twap / K_S))
i.e. Kalshi's start moved onto our mid's level. On 59 events 2026-10-11 this
cut the BTC-HYPE return-gap error from 0.89 to 0.73bp rms (ETH 0.70->0.56,
XRP 0.91->0.80, BTC/HYPE flat, SOL 0.69->0.78).
"""
from __future__ import annotations

import math
import threading

import numpy as np
import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS, START_END_AVG_S

SINGLE_COIN_SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M",
                      "HYPE": "KXHYPE15M"}
BOUNDARY_S = 900
# A previous start TWAP needs this many of its 60 bars to count toward the offset.
MIN_BARS = 55
# How far back to look for the n usable boundaries: sessions have gaps (none
# ran 17:00-21:00 UTC 2026-10-11). The offset barely moves within a day
# (HYPE -2.8bp 16-20h vs -2.3bp 20-22h, 2026-10-11), so 12h is safe.
LOOKBACK_S = 12 * 3600


def kalshi_values(coin: str, lo_s: float, hi_s: float) -> dict[float, float]:
    """{boundary unix s: published value} for `coin`'s 15M markets closing in
    [lo_s, hi_s]. Every strike in one event carries the same value."""
    out: dict[float, float] = {}
    cursor = None
    while True:
        params = {"series_ticker": SINGLE_COIN_SERIES[coin], "min_close_ts": int(lo_s) - 1,
                  "max_close_ts": int(hi_s) + 1, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = kalshi_get("/trade-api/v2/markets", params)
        for m in r.get("markets", []):
            try:
                v = float(m["expiration_value"])
            except (KeyError, TypeError, ValueError):
                continue
            if v > 0:
                out[pd.Timestamp(m["close_time"]).timestamp()] = v
        cursor = r.get("cursor")
        if not cursor or not r.get("markets"):
            return out


# (coin, boundary) -> ln(our start TWAP / Kalshi value), or None if unusable.
# Process-wide so each event computes only the newest boundary: a cold
# 12h read is ~20s, which delayed the anchor by that much at every event.
_cache: dict[tuple[str, float], float | None] = {}
_lock = threading.Lock()


def _boundary_logs(coin: str, bounds: list[float], S: float, mids_of) -> list[float | None]:
    missing = [B for B in bounds if (coin, B) not in _cache]
    if missing:
        lo, hi = min(missing), max(missing)
        vals = kalshi_values(coin, lo, hi)
        mids = mids_of(coin, lo - START_END_AVG_S, hi)
        ts = np.fromiter(mids.keys(), float, len(mids))
        vs = np.fromiter(mids.values(), float, len(mids))
        for B in missing:
            k = vals.get(B)
            sel = vs[(ts >= B - START_END_AVG_S) & (ts < B)]
            v = math.log(float(sel.mean()) / k) if k and len(sel) >= MIN_BARS else None
            # A miss on the latest boundary may just be unpublished or unflushed
            # yet; only older misses (session gaps) are final.
            if v is not None or B <= S - 2 * BOUNDARY_S:
                _cache[(coin, B)] = v
    return [_cache.get((coin, B)) for B in bounds]


def level_offsets(S: float, n: int, mids_of) -> dict[str, float | None]:
    """Per coin, mean ln(our start TWAP / Kalshi value) over the `n` most
    recent usable boundaries before S, within LOOKBACK_S (strictly earlier:
    no lookahead). `mids_of(coin, lo, hi)` returns {ts_s: mid} over [lo, hi).
    None when fewer than n//2 usable boundaries. Blocking: call off the loop."""
    bounds = [S - BOUNDARY_S * k for k in range(1, int(LOOKBACK_S / BOUNDARY_S) + 1)]
    out: dict[str, float | None] = {}
    with _lock:
        for key in [k for k in _cache if k[1] < S - LOOKBACK_S - BOUNDARY_S]:
            del _cache[key]
        for c in COINS:
            logs = [v for v in _boundary_logs(c, bounds, S, mids_of) if v is not None][:n]
            out[c] = float(np.mean(logs)) if len(logs) >= max(1, n // 2) else None
    return out


__all__ = ["SINGLE_COIN_SERIES", "kalshi_values", "level_offsets", "LOOKBACK_S"]
