"""CF Spot Rate methodology parameters (CF Spot Rate Methodology Guide,
Sept 2026). Published methodology facts, not tunables -- re-verify against
the guide if CF revises it."""

# Per-coin venue staleness threshold (ms). HYPE is 30s in the guide.
STALENESS_MS_DEFAULT = {"BTC": 10_000, "ETH": 10_000, "SOL": 10_000, "XRP": 10_000, "HYPE": 30_000}

# "Potentially erroneous" venue check: deviation from mid, as a fraction.
# HYPE 10% per the guide; the others conservatively 2% until re-verified.
MID_DEVIATION_FRACTION = {"BTC": 0.02, "ETH": 0.02, "SOL": 0.02, "XRP": 0.02, "HYPE": 0.10}

# Utilized-depth deviation from mid (step 4). HYPE 1% per the guide.
UTILIZED_DEPTH_DEVIATION = {"BTC": 0.005, "ETH": 0.005, "SOL": 0.005, "XRP": 0.005, "HYPE": 0.01}
