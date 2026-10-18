"""
Series-level constants for KXCRYPTOLEAD15M.

The 5 markets of each event appear as `{EVENT}-BTC`, `{EVENT}-ETH`,
`{EVENT}-SOL`, `{EVENT}-XRP`, `{EVENT}-HYPE`. Order of COINS below is the
canonical index order the pricer uses in all 5-vectors and 5x5 matrices.
Changing it would require resimulating any cached covariance forecast.
"""

SERIES_TICKER = "KXCRYPTOLEAD15M"

# Canonical coin order. Everything downstream (pricer vector x, mu, V; the
# cov_forecaster interface; LIP accounting) uses these indices. Added or
# removed coins change the dimension of the pricer in a backward-incompatible
# way, so this list is a schema, not a config.
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")

# Coinbase advanced-trade product symbols for each. Must match 1-to-1 with
# COINS; the spot_feed adapter indexes by COINS and looks these up.
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "HYPE": "HYPE-USD",
}

# Settlement index per coin and its active constituent venues. BTC/ETH/SOL/XRP
# are CME CF Real Time Indices (BRTI etc., "CME CF Constituent Exchanges" v13.7,
# 24-Aug-26); HYPE is the CF Spot Rate HYPEUSD_RTI ("CF Constituent Exchanges"
# v10.3, 05-Oct-26). itBit is suspended from all of them since 27-Jul-26.
# Re-check both documents when CF publishes a new version.
SETTLEMENT_INDEX = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI",
                    "XRP": "XRPUSD_RTI", "HYPE": "HYPEUSD_RTI"}
CF_CONSTITUENTS = {
    "BTC":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish", "lmax"),
    "ETH":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish", "lmax"),
    "SOL":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "lmax"),
    "XRP":  ("coinbase", "kraken", "bitstamp", "crypto_com", "lmax"),
    "HYPE": ("coinbase", "kraken", "bitstamp"),
}
# Venues medianed into each coin's pricing mid.
# 15-min return RMS error vs Kalshi's 15M settlement values, 16 settlements per
# coin (recorder 2026-10-11 00:47-05:21 UTC), Coinbase alone vs median of the
# constituents we see: ETH 0.68 -> 0.32bp, XRP 0.59 -> 0.46, BTC 0.49 -> 0.41,
# SOL 0.51 -> 0.47 (a tie at n=16, kept on Coinbase), HYPE 0.88 -> 1.15 (worse:
# thin Kraken/Bitstamp books). Re-score once the recorder has a few days.
SPOT_VENUES = {
    "BTC":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish"),
    "ETH":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish"),
    "SOL":  ("coinbase",),
    "XRP":  ("coinbase", "kraken", "bitstamp", "crypto_com"),
    "HYPE": ("coinbase",),
}
# Per-index parameters from the methodology tables: deviation-from-mid D,
# potentially-erroneous threshold, and retrieval lag threshold (s). CME CF RTI
# methodology v16.8 table 6.2 for the four majors (D 0.5% BTC / 1% others,
# PED 5% except XRP 10%, lag 10 s since 18-May-26); CF Spot Rate guide table 6.2
# for HYPE (D 1%, PED 10%, lag 30 s).
INDEX_PARAMS = {
    "BTC":  {"deviation": 0.005, "ped": 0.05, "lag_s": 10.0},
    "ETH":  {"deviation": 0.01,  "ped": 0.05, "lag_s": 10.0},
    "SOL":  {"deviation": 0.01,  "ped": 0.05, "lag_s": 10.0},
    "XRP":  {"deviation": 0.01,  "ped": 0.10, "lag_s": 10.0},
    "HYPE": {"deviation": 0.01,  "ped": 0.10, "lag_s": 30.0},
}

# Kalshi contract mechanics (section 1 of the spec).
EVENT_WINDOW_S = 900           # [S, T] window
START_END_AVG_S = 60           # averaging length for both start & end TWAPs
RETURN_ROUNDING_PCT = 0.001    # rounded to 3 decimal places in percent

# Kalshi tick size (same across binary markets).
TICK = 0.01
MIN_QUOTE_PRICE = TICK
MAX_QUOTE_PRICE = 1.0 - TICK


def market_ticker(event_ticker: str, coin: str) -> str:
    """{EVENT_TICKER}-{COIN} -- Kalshi's naming scheme for the 5 legs."""
    if coin not in COINS:
        raise ValueError(f"unknown coin {coin!r}; expected one of {COINS}")
    return f"{event_ticker}-{coin}"


def coin_from_market_ticker(ticker: str) -> str | None:
    """Inverse of market_ticker. Returns None if the suffix isn't one of
    the five canonical coins, so callers can filter unrelated tickers out
    cheaply."""
    for coin in COINS:
        if ticker.endswith(f"-{coin}"):
            return coin
    return None


__all__ = [
    "SERIES_TICKER", "COINS", "COINBASE_PRODUCTS", "CF_CONSTITUENTS", "SETTLEMENT_INDEX", "INDEX_PARAMS",
    "SPOT_VENUES",
    "EVENT_WINDOW_S", "START_END_AVG_S", "RETURN_ROUNDING_PCT",
    "TICK", "MIN_QUOTE_PRICE", "MAX_QUOTE_PRICE",
    "market_ticker", "coin_from_market_ticker",
]
