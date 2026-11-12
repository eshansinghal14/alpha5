"""Shared infrastructure: Kalshi API/feeds/orders (kalshi/), the generic
trading layer (trading/), the live session runtime (runtime/), config, and
the older data/pricing helpers (flat modules). Import from the repo root."""
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# ALPHA5_DATA_ROOT: a shadow run next to live writes its prints, thresholds and
# composite bars elsewhere instead of appending to live's files.
DATA_ROOT = os.environ.get("ALPHA5_DATA_ROOT") or os.path.join(REPO_ROOT, "data")
