"""ARBS config: every value lives in config.json (edited from the dashboard,
hot-reloaded within runtime.control_poll_s)."""
from __future__ import annotations

import os

from kalshi_infra.config import param, section

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SPEC_PATH = os.path.join(os.path.dirname(__file__), "Kalshi arb baskets spec.json")


@section
class TradingParams:
    enable_takes: bool = param("Take a whole basket (IOC on every leg) when it clears min_edge after fees.")
    enable_quoting: bool = param("Rest a bid on one leg of the hottest baskets; a fill is completed at once "
                                 "by taking the other legs.")
    min_edge: float = param("Required profit per bundle ($), after fees, for a take, and for a quote assuming "
                            "it fills and the other legs are taken at today's asks.", min=0.0, max=0.5, step=0.005)
    quote_edge_add: float = param("Extra edge per bundle ($) a quote must clear on top of min_edge: pays for "
                                  "being filled just as the other legs move away.", min=0.0, max=0.5, step=0.005)
    quote_size: int = param("Bundles per resting quote.", min=1, max=10000, step=1)
    max_bundles_per_basket: int = param("Most bundles held in one basket (complete or not).", min=0, max=100000,
                                        step=1)
    max_dollars: float = param("Most dollars in the strategy: cost paid on held baskets plus what resting "
                               "quotes would commit if filled and completed.", min=0.0, step=50.0)
    requote_min_ticks: int = param("Move a resting quote only when the wanted price differs by this many "
                                   "ticks (a price past the edge limit always moves).", min=1, max=50, step=1)
    requote_cooldown_s: float = param("Least time between voluntary moves of one quote.", min=0.0, step=0.5)
    take_cooldown_s: float = param("Least time between takes on one basket (our IOCs consume the book before "
                                   "the feed shows it).", min=0.0, step=0.5)
    leg_switch_margin: float = param("Move a quote to another leg only if that leg's edge-limited price sits "
                                     "this much ($) further above its bid.", min=0.0, step=0.005)


@section
class CompletionParams:
    max_loss_per_bundle: float = param("A broken basket is finished at any price that loses at most this much "
                                       "per bundle ($). Each missing leg's IOC limit is its cap_j.",
                                       min=0.0, max=1.0, step=0.01)
    retry_s: float = param("Seconds between completion attempts while a basket is broken.", min=0.1, step=0.5)
    max_broken_baskets: int = param("No new takes or quotes while this many baskets are broken.", min=1,
                                    max=1000, step=1)


@section
class WatchParams:
    max_quoted_baskets: int = param("Quote only this many baskets: the top of the ranking (payout minus "
                                    "the cost of taking every leg). Bounds write-token spend.", min=0, max=1000,
                                    step=1)
    rank_interval_s: float = param("Seconds between re-rankings.", min=0.5, step=0.5)
    eval_interval_s: float = param("Seconds between passes over baskets whose books changed.", min=0.01,
                                   max=5.0, step=0.01)
    exclude: list[str] = param("basket_id, relationship_id or cluster names never traded. Open positions "
                               "in them are still completed and settled.")


@section
class RuntimeParams:
    control_poll_s: float = param("Config and arm-request poll.", min=0.1, step=0.1)
    max_write_tokens_per_s: float | None = param("Cap on this strategy's write spend per shard, on top of the "
                                                 "shard's shared bucket. Empty = none.", min=1.0, step=10.0)
    write_reserve_tokens: float = param("Quotes are only placed or moved with this many tokens left over, kept "
                                        "for completions and cancels.", min=0.0, step=10.0)
    audit_s: float = param("Seconds between position audits against Kalshi (and resting-order checks).",
                           min=5.0, step=5.0)
    fill_poll_s: float = param("REST backstop for quote fills the websocket missed.", min=1.0, step=1.0)
    meta_refresh_s: float = param("Seconds between market status / settlement reads.", min=30.0, step=30.0)
    max_order_errors: int = param("Consecutive order errors that stop new takes and quotes until a clean "
                                  "response.", min=1, max=100, step=1)
    feed_down_pull_s: float = param("Pull every quote once the Kalshi websocket has been down this long.",
                                    min=0.0, step=1.0)


@section
class ArbsConfig:
    trading: TradingParams
    completion: CompletionParams
    watch: WatchParams
    runtime: RuntimeParams
