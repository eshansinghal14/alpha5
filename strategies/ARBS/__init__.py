"""Cross-market arbitrage on the baskets in `Kalshi arb baskets spec.json`:
take a whole basket when it costs less than its guaranteed payout after
fees, or rest a bid on each leg and take the rest the moment one fills.

    python -m strategies.run --series ARBS

"ARBS" is not a Kalshi series: it names the data/live folder and dashboard
page. The strategy runs its own loop (runner.run_arbs) over ~70 events."""
from __future__ import annotations

from infra.runtime.series import Series
from strategies.ARBS.config import CONFIG3_PATH, CONFIG_PATH, CONFIGPM_PATH, ArbsConfig

DASHBOARD_GROUP = "Arbitrage"
DASHBOARD_PAGE = "viz.arbs_page:render"
DASHBOARD_LABELS = {"ARBS": "risk-free"}
# Shown inside the ARBS page (the same strategy, another process), not as a page of its own.
DASHBOARD_MERGED = {"ARBS3", "ARBSPM"}


async def _runner(series, **kw):
    from strategies.ARBS.runner import run_arbs
    await run_arbs(series, **kw)


def _unused(*_a, **_k):
    raise RuntimeError("ARBS runs its own loop (Series.runner)")


SERIES = {"ARBS": Series(ticker="ARBS", config_cls=ArbsConfig, config_path=CONFIG_PATH, config_key=None,
                         pick_event=_unused, build_model=_unused, runner=_runner),
          # The same strategy on Kalshi's shard 3 (tennis, basketball) in its own process:
          # with both shards one process ran at ~90% of a core (11-11). Its own folder,
          # ledger, write bucket and cash; the two trade disjoint markets.
          "ARBS3": Series(ticker="ARBS3", config_cls=ArbsConfig, config_path=CONFIG3_PATH, config_key=None,
                          pick_event=_unused, build_model=_unused, runner=_runner),
          # The same strategy on Polymarket US (soccer three-way, weather buckets) and across
          # exchanges (one outcome on both), in its own process: a Polymarket hang or rate
          # limit can't stall Kalshi trading. Quotes rest on Polymarket legs only.
          "ARBSPM": Series(ticker="ARBSPM", config_cls=ArbsConfig, config_path=CONFIGPM_PATH, config_key=None,
                           pick_event=_unused, build_model=_unused, runner=_runner)}
