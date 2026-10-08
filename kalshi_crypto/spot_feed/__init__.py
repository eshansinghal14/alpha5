"""
Venue-pluggable spot feed interface + adapters.

An adapter maintains a per-product L2 book and emits a single-venue
`VenueBook` snapshot every time its book changes. The CF Spot Rate replica
(cf_rate.py) consumes an iterable of `VenueBook`s per coin once per second,
merges them into a consolidated book, and emits one composite price.

Phase 1 ships only the Coinbase adapter (every coin is on it). Phase 2
adds Kraken + Bitstamp; adapters share the shape in `base.VenueAdapter`
so the composite logic doesn't change.

Health kill switch: each VenueBook carries `receive_ts` + `is_resyncing`.
The CF rate pipeline marks a composite reading INVALID if every venue
contributing to one coin is stale > the per-coin threshold or resyncing.
`spot_feed.health.FeedHealthMonitor` wraps that check behind one call.
"""
from .base import VenueBook, VenueAdapter, OrderBookLevel
from .coinbase import CoinbaseAdapter
from .health import FeedHealthMonitor, FeedHealthState

__all__ = [
    "VenueBook", "VenueAdapter", "OrderBookLevel",
    "CoinbaseAdapter",
    "FeedHealthMonitor", "FeedHealthState",
]
