"""LIP farming on the cheap side's touch (farm.py). Runs its own loop
(`python -m kalshi_strategies.LIPFARM.farm`), not through run.py: no series,
only a dashboard page."""
SERIES: dict = {}
DASHBOARD_GROUP = "LIP"
EXTRA_PAGES = {"LIPFARM": "kalshi_viz.lipfarm_page:render"}
DASHBOARD_LABELS = {"LIPFARM": "LIP farm"}
