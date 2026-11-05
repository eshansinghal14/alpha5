"""Kalshi venue layer: auth, REST, websocket feed, order client."""
from kalshi_infra.kalshi import routing as _routing

_routing.install_from_env()   # ALPHA5_KALSHI_ROUTE=1: connect through the nearest LB nodes
