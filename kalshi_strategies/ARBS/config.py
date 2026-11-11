"""ARBS config: every value lives in config.json, hot-reloaded within
runtime.control_poll_s. Only the strategy knobs are on the dashboard form;
plumbing (poll rates, write budget) is editable=False and
changed in config.json by hand."""
from __future__ import annotations

import os

from kalshi_infra.config import param, section

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CONFIG3_PATH = os.path.join(os.path.dirname(__file__), "config_shard3.json")
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
    min_exit_edge: float = param("Least profit ($ per bundle, after fees) to take or quote a basket's flip "
                                 "(every leg's other side) up to the bundles held in it. Kalshi nets the pair to "
                                 "$1 a leg at once, so this closes a position early and frees its capital; no "
                                 "max_dollars or max_bundles_per_basket check.", min=0.0, max=0.5, step=0.0025)
    exit_ref_days: float = param("A flip settling within this many days needs the full min_edge (quotes: plus "
                                 "quote_edge_add) to close held bundles; further out the requirement falls as "
                                 "exit_ref_days / days to settlement, the capital freed for longer, down to "
                                 "min_exit_edge. Live events always need the full edge.", min=1.0, max=2000.0,
                                 step=1.0)
    days_till_expiry: float = param("Trade only baskets whose markets are all expected to settle within this "
                                    "many days (the hand spec's settlement month, else Kalshi's expected "
                                    "expiration). The scan skips events past it. Open positions past it are still "
                                    "completed and settled.", min=0.0, max=2000.0, step=1.0)
    quote_size: int = param("Bundles per resting quote.", min=1, max=10000, step=1)
    quote_live_events: bool = param("Quote events that have started (a game after kickoff). A live score change "
                                    "sweeps every leg at once, so a fill there is usually already a loss; "
                                    "exits on live events are taken at once.")
    live_edge_add: float = param("Extra edge ($ per bundle) quotes and takes need on a live event, or on a market "
                                 "flagged live by a bad exit.", min=0.0, max=0.5, step=0.005)
    # 11-01..04 fills: soccer 75-120 min after kickoff was 1,110 arbs and -$47,
    # the rest of the game positive; 11-04's two goal sweeps (-$34) fell in it.
    # A stopgap until the live estimator learns game phase.
    late_quote_cutoff_min: dict[str, float] = param("Group (S:<sport tags> for Sports, else Kalshi's category) -> "
                                                    "minutes after a live event's start past which its baskets "
                                                    "stop quoting. Exits and takes are unaffected.",
                                                    editable=False)
    take_live_events: bool = param("Take arbs (and flips) on events that have started. A visible arb on a live "
                                   "game is mostly one leg not yet repriced after a score, which faster traders "
                                   "take first: 6 of 7 live takes filled unevenly (10-29). Exits of broken "
                                   "baskets are taken regardless.")
    live_quote_size: int = param("Bundles per resting quote on a live event.", min=1, max=10000, step=1)
    model_series: list[str] = param("Series or groups (S:Esports) whose live quotes take their edge and size "
                                    "from the exit-cost model (exit_model.json, refitted nightly by the shadow "
                                    "check) instead of min_edge + live_edge_add and live_quote_size. Widening "
                                    "and shrinking act at once; narrowing and growing wait (MODEL_*_HOLD_S). "
                                    "Empty = off.")
    live_take_size: int = param("Most bundles per take (or flip) on a live event, when take_live_events is on.",
                                min=1, max=10000, step=1)
    live_order_groups: bool = param("Put a live event's quotes in one Kalshi order group (limit 1): once 2 "
                                    "contracts match in 15 s, Kalshi cancels the rest inside the matching "
                                    "engine, before the rest of a sweep lands, and exits go out without "
                                    "waiting on our own cancels.")
    deep_quote_size: int = param("Most bundles in the second quote a basket rests one tick over the next bid "
                                 "behind each arb quote, where a sweep through the arb quote lands. Also held "
                                 "to the arb quote's own cap (live_quote_size / quote_size) and the room left "
                                 "after it, so at or over quote_size it is the regular size. 0 = off.",
                                 min=0, max=10000, step=1)
    # Scored on fill_context 11-02..04: sports, others' depth -50% in 2 s with a
    # 2 s hold skipped $60 of losing fills for $30 of winners, 1.7% of quote
    # time; live elections about even (most losses there gave no warning).
    pull_drop: float = param("Pull a live basket's quotes on a market side when other makers' size at its best "
                             "bid falls by this fraction of its peak at that price within pull_window_s "
                             "(0.5 = halved). 0 = off.", min=0.0, max=1.0, step=0.05)
    pull_window_s: float = param("Seconds the pull's peak depth is taken over.", min=0.05, max=30.0, step=0.05)
    pull_hold_s: float = param("Seconds a pulled side stays unquoted after the last pull signal.", min=0.0,
                               max=60.0, step=0.5)
    deep_min_gap_ticks: int = param("Least gap, in cents (not a tapered market's 0.1c ticks), between the arb "
                                    "quote and the deep quote's price; a smaller "
                                    "gap gains too little over the arb quote to be worth the order.",
                                    min=1, max=99, step=1)
    depth_age_s: float = param("New and grown quotes are held to the other legs' touch depth that has rested "
                               "this long (an existing quote keeps its size); 0 = off. Live fills 11-02..10 "
                               "over 50 lots made +2.1c/lot when that depth (5 s) covered them, -3.0c/lot "
                               "(-$322, 87 arbs) when it didn't.", min=0.0, max=10.0, step=0.5)
    max_bundles_per_basket: int = param("Most bundles held in one basket (complete or not).", min=0, max=100000,
                                        step=1)
    max_dollars: float = param("Most dollars in the strategy: cost paid on held baskets, plus per quoted "
                               "basket its largest resting quote x (payout + 1c per chase tick), the most it "
                               "can cost filled and completed however many legs fill.", min=0.0, step=50.0)
    max_dollars_auto: bool = param("Live, size against the traded shards' cash instead of max_dollars: room is "
                                   "Kalshi's available balance (net of resting orders, with YES/NO pairs and "
                                   "netted positions already credited) plus a basket's own resting quotes, "
                                   "less cash_reserve. Falls back to max_dollars while the balance is unread "
                                   "or stale.")
    cash_reserve: float = param("Dollars of shard cash max_dollars_auto leaves free, for completing a fill "
                                "while other quotes rest.", min=0.0, step=50.0)
    take_cooldown_s: float = param("Least time between takes on one basket. A take buys only the touch of "
                                   "every leg; this is how long to wait for a thin touch to refill at the same "
                                   "price before taking again.", min=0.0, step=0.5)
    requote_min_ticks: int = param("Move a resting quote only when the wanted price differs by this many "
                                   "ticks (a price past the edge limit always moves).", min=1, max=50, step=1)
    # 11-01/02: 41% of 1c up moves came straight back within 30 s, and a
    # quote moving up is mostly answering someone joining its price, where
    # it's already first in the queue.
    requote_hold_up: float = param("A resting quote isn't moved up when the price it wants is at most this many "
                                   "dollars higher (0.01 = 1c). Moves down always go out.", min=0.0, max=0.05,
                                   step=0.005, editable=False)
    requote_cooldown_s: float = param("Least time between voluntary moves (reprice or resize) of one resting "
                                      "quote. A new quote after a fill isn't a move.",
                                      min=0.0, step=0.5)


@section
class CompletionParams:
    chase_ticks: int = param("A broken basket rests post-only bids on its cheaper exit (buy the missing legs, "
                             "or net the extra ones out by buying their other side) and takes it once that exit "
                             "gets this many ticks per bundle worse within chase_window_s: a fast move. A slow "
                             "drift keeps resting. Live events always take at once; 0 = always take at once.",
                             min=0, max=50, step=1)
    chase_window_s: float = param("How recent the best exit price a fast move is measured from.", min=0.0,
                                  max=60.0, step=0.1)
    # 11-02..04: misses retried on the next book update (~28 ms) finished
    # better than waiting; episodes of 4-6 tries ended $13 better than plan.
    max_exit_misses: int = param("Exit takes (IOCs) in a row that may miss before a broken basket stops taking "
                                 "and rests post-only bids instead, live or not. Reset by an exit that fills "
                                 "in full.", min=1, max=100, step=1)
    max_broken_baskets: int = param("No new takes or quotes while this many baskets are broken.", min=1,
                                    max=1000, step=1, editable=False)


@section
class WatchParams:
    queue_window_s: float = param("Seconds of trades a market side's fill rate is measured over: contracts "
                                  "sold into its bids.", min=300.0, step=300.0, editable=False)
    rank_interval_s: float = param("Seconds between re-rankings, and least time between pricing one basket "
                                   "for the ranking.", min=0.5, step=0.5, editable=False)
    exclude: list[str] = param("basket_id, relationship_id, event or cluster names never traded. Open positions "
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
    live_windows: dict[str, list[str]] = param("Kalshi category -> [from, until] UTC: its events are live "
                                               "(live size, edge and exits) from `from`, for scans before "
                                               "`until` (election results night).", editable=False)
    hand_spec: bool = param("Trade the hand spec's baskets besides the scan's. Off for a second process "
                            "scanning other shards (ARBS3): the hand spec's markets belong to the first.",
                            editable=False)
    live_before_settle_h: float = param("Hours before expected settlement an event without a kickoff turns live "
                                        "(live size, edge, exits). Non-sports arbs started within "
                                        "6h of settlement netted -$3 over 10-28..11-09, then -$179 on 11-10; "
                                        "0 = off.", min=0.0, max=72.0, step=1.0)


@section
class RuntimeParams:
    control_poll_s: float = param("Config and arm-request poll.", min=0.1, step=0.1, editable=False)
    max_write_tokens_per_s: float | None = param("Cap on this strategy's write spend per shard, on top of the "
                                                 "shard's shared bucket. Empty = none.", min=1.0, step=10.0,
                                                 editable=False)
    max_write_tokens_per_s_solo: float | None = param("The cap instead while none of write_priority_series is "
                                                      "trading (live, inside its session). Empty = "
                                                      "max_write_tokens_per_s always.", min=1.0, step=10.0,
                                                      editable=False)
    write_priority_series: list[str] = param("Series whose trading session holds this strategy to "
                                             "max_write_tokens_per_s.", editable=False)
    # Must stay below max_write_tokens_per_s - 10: the capped bucket holds one
    # second of its rate, so a larger reserve blocks every quote (60 vs a cap of
    # 50 placed no live quote 10-24 21:30 UTC).
    write_reserve_tokens: float = param("Quotes are only placed or moved with this many tokens left over, kept "
                                        "for completions and cancels.", min=0.0, step=10.0, editable=False)
    audit_s: float = param("Seconds between position audits against Kalshi (and resting-order checks).",
                           min=5.0, step=5.0, editable=False)
    fill_poll_s: float = param("REST backstop for quote fills the websocket missed.", min=1.0, step=1.0,
                               editable=False)
    balance_poll_s: float = param("Seconds between shard balance reads, for trading.max_dollars_auto.",
                                  min=1.0, step=1.0, editable=False)
    meta_refresh_s: float = param("Seconds between market status / settlement reads.", min=30.0, step=30.0,
                                  editable=False)
    max_order_errors: int = param("Consecutive order errors that stop new takes and quotes until a clean "
                                  "response.", min=1, max=100, step=1, editable=False)
    liquidity_log_s: float = param("Seconds per row of the per-market liquidity log "
                                   "(data/live/ARBS/liquidity/).", min=30.0, step=30.0, editable=False)
    feed_down_pull_s: float = param("Pull every quote once the Kalshi websocket has been down this long.",
                                    min=0.0, step=1.0, editable=False)
    record_prints: bool = param("Log trade-time book snapshots on basket legs (data/live/ARBS/prints/), for "
                                "replaying edge and size settings offline.")
    live_params_shadow: bool = param("Score each recorded live print at every edge (replay.py) and keep the "
                                     "edge live_params.py would quote per sport, series and event, in the "
                                     "summary and journal only: no order uses it. Needs record_prints.")
    regime_shadow: bool = param("Journal (kind regime_shadow) when an event's markets move the way news "
                                "moves them (depth.MoveWatch: legs moving together, or a price jump) and "
                                "it would be switched to live settings. No order uses it. Needs record_prints.")


@section
class ArbsConfig:
    trading: TradingParams
    completion: CompletionParams
    watch: WatchParams
    scan: ScanParams
    runtime: RuntimeParams
