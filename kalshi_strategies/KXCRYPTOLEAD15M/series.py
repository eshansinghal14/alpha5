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

# CF Spot Rate constituent venues per coin (CME CF constituents, Sept 2026
# per the spec). Phase 1 only subscribes to Coinbase; this is the roadmap
# for Phase 2 and the authoritative "is Coinbase a CF constituent for X" check.
CF_CONSTITUENTS = {
    "BTC":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish", "lmax"),
    "ETH":  ("coinbase", "kraken", "bitstamp", "gemini", "crypto_com", "bullish", "lmax"),
    "SOL":  ("coinbase", "kraken", "gemini", "bitstamp", "crypto_com", "lmax"),
    "XRP":  ("coinbase", "kraken", "bitstamp", "crypto_com", "lmax"),
    "HYPE": ("coinbase", "kraken", "bitstamp"),
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
    "SERIES_TICKER", "COINS", "COINBASE_PRODUCTS", "CF_CONSTITUENTS",
    "EVENT_WINDOW_S", "START_END_AVG_S", "RETURN_ROUNDING_PCT",
    "TICK", "MIN_QUOTE_PRICE", "MAX_QUOTE_PRICE",
    "market_ticker", "coin_from_market_ticker",
]
