"""Shared infrastructure: Kalshi API/feeds/orders (kalshi/), the generic
trading layer (trading/), the live session runtime (runtime/), config, and
the older data/pricing helpers (flat modules). Import from the repo root."""
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_ROOT = os.path.join(REPO_ROOT, "data")
