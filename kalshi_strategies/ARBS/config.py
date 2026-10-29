"""ARBS config: every value lives in config.json, hot-reloaded within
runtime.control_poll_s. Only the strategy knobs are on the dashboard form;
plumbing (poll rates, write budget, requote pacing) is editable=False and
changed in config.json by hand."""
from __future__ import annotations

import os

from kalshi_infra.config import param, section

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SPEC_PATH = os.path.join(os.path.dirname(__file__), "Kalshi arb baskets spec.json")


@section
class TradingParams:
    enable_takes: bool = param("Take a whole basket (IOC on every leg) when its return clears min_edge after fees.")
    enable_quoting: bool = param("Rest a bid on every leg of the hottest baskets that can clear the quote "
                                 "edge; a fill is completed at once by taking the other legs.")
    # A fraction of the bundle's dollar cost, not cents per bundle: a payout-2
    # basket costs ~2x a payout-1 one, so a flat cent edge earned it half the
    # return on capital (10-24 fills: 0.9% on SENATEME:TAL vs 1.1% on DEMDEM).
    min_edge: float = param("Required profit as a fraction of what a bundle costs (0.015 = 1.5%), after fees, "
                            "for a take, and for a quote assuming it fills and the other legs are taken at "
                            "today's asks.", min=0.0, max=0.5, step=0.0025)
    quote_edge_add: float = param("Extra return (fraction of bundle cost) a quote must clear on top of min_edge: "
                                  "pays for being filled just as the other legs move away.", min=0.0, max=0.5,
                                  step=0.0025)
    exit_edge: float = param("Required profit ($ per bundle, after fees) to take or quote a basket's flip "
                             "(every leg's other side) up to the bundles held in it. Kalshi nets the pair to $1 "
                             "a leg at once, so this closes a position early and frees its capital; no "
                             "min_edge, max_dollars or max_bundles_per_basket check.", min=0.0, max=0.5,
                             step=0.0025)
    days_till_expiry: float = param("Trade only baskets whose markets are all expected to settle within this "
                                    "many days (the hand spec's settlement month, else Kalshi's expected "
                                    "expiration). The scan skips events past it. Open positions past it are still "
                                    "completed and settled.", min=0.0, max=2000.0, step=1.0)
    quote_size: int = param("Bundles per resting quote.", min=1, max=10000, step=1)
    max_bundles_per_basket: int = param("Most bundles held in one basket (complete or not).", min=0, max=100000,
                                        step=1)
    max_dollars: float = param("Most dollars in the strategy: cost paid on held baskets, plus per quoted "
                               "basket its largest resting quote x (payout + chase_per_bundle), the most it "
                               "can cost filled and completed however many legs fill.", min=0.0, step=50.0)
    take_cooldown_s: float = param("Least time between takes on one basket. A take buys only the touch of "
                                   "every leg; this is how long to wait for a thin touch to refill at the same "
                                   "price before taking again.", min=0.0, step=0.5)
    requote_min_ticks: int = param("Move a resting quote only when the wanted price differs by this many "
                                   "ticks (a price past the edge limit always moves).", min=1, max=50, step=1,
                                   editable=False)
    requote_cooldown_s: float = param("Least time between voluntary moves of one quote.", min=0.0, step=0.5,
                                      editable=False)


@section
class CompletionParams:
    chase_per_bundle: float = param("A broken basket rests post-only bids on its cheaper exit (buy the missing "
                                    "legs, or net the extra ones out by buying their other side) and takes it "
                                    "the moment it costs this much more per bundle ($) than the best seen since "
                                    "the break. An exit that loses nothing, and any exit on a live event, is "
                                    "taken at once; 0 = always take at once.", min=0.0, max=0.5, step=0.005)
    retry_s: float = param("Least seconds between exit IOCs on one broken basket.", min=0.05, step=0.05,
                           editable=False)
    max_broken_baskets: int = param("No new takes or quotes while this many baskets are broken.", min=1,
                                    max=1000, step=1, editable=False)


@section
class WatchParams:
    max_quoted_baskets: int = param("Quote only this many baskets: the top of the ranking (payout minus "
                                    "the cost of taking every leg). Bounds write-token spend.", min=0, max=1000,
                                    step=1)
    rank_interval_s: float = param("Seconds between re-rankings.", min=0.5, step=0.5, editable=False)
    eval_interval_s: float = param("Seconds between passes over baskets whose books changed.", min=0.01,
                                   max=5.0, step=0.01, editable=False)
    exclude: list[str] = param("basket_id, relationship_id or cluster names never traded. Open positions "
                               "in them are still completed and settled.", editable=False)


@section
class ScanParams:
    enable: bool = param("Scan Kalshi every interval_s for MECNET / DIRECNET events and trade their baskets "
                         "alongside the hand spec.")
    max_markets: int = param("Most scanned markets traded beyond the hand spec's: the scan lists every event, "
                             "and the most traded in the last 24h are taken until this many. Each is one more "
                             "websocket subscription.", min=0, max=20000, step=100)
    shards: list[int] = param("Exchange shards (event exchange_index) the scan takes events from. Kalshi "
                              "holds collateral per shard: an order on one we haven't funded is rejected "
                              "insufficient_shard_balance, and arming refuses a zero-balance shard.",
                              editable=False)
    interval_s: float = param("Seconds between scans.", min=60.0, step=60.0, editable=False)
    mecnet_max_legs: int = param("Largest MECNET subset listed (NO on the k likeliest markets, k = 2..this).",
                                 min=2, max=50, step=1, editable=False)
    ladder_span: int = param("DIRECNET strikes each strike is paired with, going up the ladder.", min=1, max=100,
                             step=1, editable=False)
    complete_set_exclude: list[str] = param("Series whose MECNET buckets are not traded as complete sets "
                                            "(buy every YES / every NO), though they look like they cover "
                                            "every outcome.", editable=False)


@section
class RuntimeParams:
    control_poll_s: float = param("Config and arm-request poll.", min=0.1, step=0.1, editable=False)
    max_write_tokens_per_s: float | None = param("Cap on this strategy's write spend per shard, on top of the "
                                                 "shard's shared bucket. Empty = none.", min=1.0, step=10.0,
                                                 editable=False)
    # Must stay below max_write_tokens_per_s - 10: the capped bucket holds one
    # second of its rate, so a larger reserve blocks every quote (60 vs a cap of
    # 50 placed no live quote 10-24 21:30 UTC).
    write_reserve_tokens: float = param("Quotes are only placed or moved with this many tokens left over, kept "
                                        "for completions and cancels.", min=0.0, step=10.0, editable=False)
    audit_s: float = param("Seconds between position audits against Kalshi (and resting-order checks).",
                           min=5.0, step=5.0, editable=False)
    fill_poll_s: float = param("REST backstop for quote fills the websocket missed.", min=1.0, step=1.0,
                               editable=False)
    meta_refresh_s: float = param("Seconds between market status / settlement reads.", min=30.0, step=30.0,
                                  editable=False)
    max_order_errors: int = param("Consecutive order errors that stop new takes and quotes until a clean "
                                  "response.", min=1, max=100, step=1, editable=False)
    liquidity_log_s: float = param("Seconds per row of the per-market liquidity log "
                                   "(data/live/ARBS/liquidity/).", min=30.0, step=30.0, editable=False)
    feed_down_pull_s: float = param("Pull every quote once the Kalshi websocket has been down this long.",
                                    min=0.0, step=1.0, editable=False)


@section
class ArbsConfig:
    trading: TradingParams
    completion: CompletionParams
    watch: WatchParams
    scan: ScanParams
    runtime: RuntimeParams
