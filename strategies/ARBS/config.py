"""ARBS config: every value lives in config.json, hot-reloaded within
runtime.control_poll_s. Only the strategy knobs are on the dashboard form;
plumbing (poll rates, write budget) is editable=False and
changed in config.json by hand."""
from __future__ import annotations

import os

from infra.config import param, section

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CONFIG3_PATH = os.path.join(os.path.dirname(__file__), "config_shard3.json")
CONFIG0B_PATH = os.path.join(os.path.dirname(__file__), "config_shard0b.json")
CONFIG0C_PATH = os.path.join(os.path.dirname(__file__), "config_shard0c.json")
CONFIG3B_PATH = os.path.join(os.path.dirname(__file__), "config_shard3b.json")
CONFIGPM_PATH = os.path.join(os.path.dirname(__file__), "config_pm.json")
OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), "settlement_overrides.json")


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
    exit_ref_days: float | None = param("A flip settling within this many days needs the full min_edge (quotes: "
                                        "plus quote_edge_add) to close held bundles; further out the requirement "
                                        "falls as exit_ref_days / days to settlement, the capital freed for longer, "
                                        "down to min_exit_edge. Live events always need the full edge. Empty: every "
                                        "flip needs the full edge (Kalshi nets an event's positions, so held "
                                        "bundles tie up no capital).", min=1.0, max=2000.0, step=1.0)
    days_till_expiry: float | None = param("Trade only baskets whose markets are all expected to settle within "
                                           "this many days (Kalshi's expected expiration). The scan skips events "
                                           "past it. Open positions past it are still completed and settled. Empty: "
                                           "no limit (Kalshi nets an event's positions, so a whole basket ties up no "
                                           "capital however far out it settles).", min=0.0, max=2000.0, step=1.0)
    quote_unnetted: bool = param("Trade baskets Kalshi doesn't net (spread/total ladders, watch.py's families). "
                                 "Their cash stays locked to settlement, so the scan lists only those settling "
                                 "within config_pm.json's days_till_expiry. Off: recorded only.")
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
    model_series: list[str] = param("Series or groups (S:Esports) whose quotes take their edge from the "
                                    "exit-cost model (exit_model.json, refitted nightly by the shadow check) "
                                    "instead of min_edge (+ live_edge_add). A bare name or * is live quotes, "
                                    "which take their size from the model too; NAME:pre / *:pre is pre-game "
                                    "quotes, which keep quote_size. Widening and shrinking act at once; "
                                    "narrowing and growing wait (MODEL_*_HOLD_S). Empty = off.")
    size_table: list[str] = param("Same names as model_series: live quotes there are cut to the size the "
                                  "size table (size_table.json, refitted nightly by the shadow check) values "
                                  "most at or under their size, from the other legs' depth near their tops "
                                  "and how far under our leg's touch the quote rests. Empty = off.")
    model_shadow: list[str] = param("Same names as model_series: the model's pick is journaled (model_quote, "
                                    "shadow) beside the config edge, which the quote keeps. For comparing before "
                                    "a group moves to model_series. Empty = off.")
    live_take_size: int = param("Most bundles per take (or flip) on a live event, when take_live_events is on.",
                                min=1, max=10000, step=1)
    live_order_groups: bool = param("Put a live event's quotes in one Kalshi order group (limit 1): once 2 "
                                    "contracts match in 15 s, Kalshi cancels the rest inside the matching "
                                    "engine, before the rest of a sweep lands, and exits go out without "
                                    "waiting on our own cancels.")
    # Scored on fill_context 11-02..04: sports, others' depth -50% in 2 s with a
    # 2 s hold skipped $60 of losing fills for $30 of winners, 1.7% of quote
    # time; live elections about even (most losses there gave no warning).
    pull_drop: float = param("Pull a live basket's quotes on its other legs when a leg it would complete with "
                             "moves away within pull_window_s: its ask rises, or others' size at that ask falls by "
                             "this fraction of its peak there (0.5 = halved). 0 = off.", min=0.0, max=1.0, step=0.05)
    pull_window_s: float = param("Seconds a completion leg's ask is watched over for the pull.", min=0.05,
                                 max=30.0, step=0.05)
    pull_hold_s: float = param("Seconds a pulled side stays unquoted after the last pull signal.", min=0.0,
                               max=60.0, step=0.5)
    pull_only: list[str] | None = param("Series, events or groups (S:Soccer) the pull runs in: a basket is "
                                        "covered when any of its markets is. null or empty: every market.",
                                        editable=False)
    near_min_size: int = param("Pre-game baskets: a second quote one tick under an arb quote that the other "
                               "legs hold under quote_size at its own price. It takes what is left of "
                               "quote_size, sized to the bundles they complete a tick lower, and is placed "
                               "only when that is at least this many bundles (in the deep quote's place on "
                               "that leg), so it needs quote_size above this. 0 = off. Replayed on prints "
                               "11-05..12, ones of 1,000+ made $3,245 on 23 fills with one loser; smaller "
                               "ones lost $334.", min=0, max=10000, step=100)
    deep_min_gap_ticks: int = param("A basket rests a second, deep quote one tick over the next bid behind each "
                                    "arb quote, where a sweep through the arb quote lands, up to the arb "
                                    "quote's own cap (live_quote_size / quote_size) and the room left after "
                                    "it. This is the least gap, in cents (not a tapered market's 0.1c ticks), "
                                    "between the arb quote and that price; a smaller gap gains too little "
                                    "over the arb quote to be worth the order.", min=1, max=99, step=1)
    depth_age_s: float = param("New and grown quotes are held to the other legs' touch depth that has rested "
                               "this long (an existing quote keeps its size); 0 = off. Live fills 11-02..10 "
                               "over 50 lots made +2.1c/lot when that depth (5 s) covered them, -3.0c/lot "
                               "(-$322, 87 arbs) when it didn't.", min=0.0, max=10.0, step=0.5)
    deep_live: bool = param("Deep and reach quotes on live baskets. Off: they rest pre-game only (near quotes are "
                            "pre-game only either way). Real fills 11-09..19: live deep fills made +$23 on 1,430 "
                            "lots and held the worst tails (KXNHL2P -$39, KXHIGHTSFO -$11), pre-game ones +$387 on "
                            "10,444.")
    lazy_below_top: bool = param("Book updates that leave a market's best prices and the sizes at them unchanged "
                                 "(only levels under the top moved) re-evaluate live and quoted baskets at most "
                                 "every 0.1 s, as quiet ones already do. Top moves, size changes at the top and "
                                 "baskets being exited stay immediate.")
    skip_beyond_reach: bool = param("A book update that leaves the top's prices and sizes alone and changes no "
                                    "level a live evaluation reads (within 2c of the best, then down to "
                                    "live_quote_size under it) marks none of the market's baskets, when all of "
                                    "them are live. Baskets being exited are still marked.")
    hot_event_s: float = param("Seconds a live basket stops quoting after a recorded print in its event that a "
                               "20-lot quote at a 5c edge would have lost over 2c/lot on. 0 = off (hot events "
                               "are still journaled).", min=0.0, max=3600.0, step=30.0)
    hot_event_only: list[str] | None = param("Series, events or groups (S:Esports) hot_event_s covers: a basket "
                                             "is covered when any of its markets is. null or empty: every market.",
                                             editable=False)
    dormant_skip: bool = param("Shard 0 stops quoting series on the dormant list (dormant.py: 100k+ write "
                               "tokens over 3 days, no finished arb) until a print through the price it would "
                               "quote wakes the event for 15 min. Off: the summary and journal (dormant_wake) "
                               "say what it would skip.")
    second_level_size: bool = param("Two-leg baskets: hold every quote (arb, near, deep) to what the other leg "
                                    "still completes, without a loss, with its best ask level gone. In a "
                                    "two-outcome market the trader who fills the quote takes that level in "
                                    "the same trade. Every sweep recorded 11-04..13 replayed: pre-game "
                                    "two-leg quotes made -$179 as sized and +$399 held this way; baskets of "
                                    "3+ legs lost $100-150 by it and are left alone.")
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
    # 11-22 ARBS0B in-play: 4,900 grows/h, a fifth cut back within 3 s (each a
    # 10-token amend that loses the queue); a 2-3 s hold cost 11-18 filled lots a day.
    live_grow_hold_s: float = param("A live quote is grown (to 2x+ what rests) only once its room has stayed that "
                                    "big this long, unless it is repriced anyway. 0: at once.", min=0.0, step=0.5)


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
    record_only: list[str] | None = param("Series, events or groups (S:Hockey) still scanned and watched, so "
                                          "their prints are recorded for the shadow check and the exit model, "
                                          "but never quoted or taken. Unlike exclude, they take scan budget. "
                                          "A name covers pre-game and in-play; NAME:pre or NAME:live only that "
                                          "phase (in-play from the event's scheduled start, scan.live_*). "
                                          "null: none, or config.json's list in ARBS3's file (its $base), so "
                                          "both Kalshi shards watch the same markets.", editable=False)


@section
class ScanParams:
    enable: bool = param("Scan Kalshi every interval_s for MECNET / DIRECNET events and trade their baskets "
                         "alongside the hand spec.")
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
    live_before_settle_h: float = param("Hours before expected settlement an event without a kickoff turns live "
                                        "(live size, edge, exits). Non-sports arbs started within "
                                        "6h of settlement netted -$3 over 10-28..11-09, then -$179 on 11-10; "
                                        "0 = off.", min=0.0, max=72.0, step=1.0)
    live_early_h: dict[str, float] = param("Series -> hours before its listed start its events turn live. "
                                           "Lower-tier tennis starts before its listed time: pre-game ITF "
                                           "quotes lost $44 (11-11 doubles) and $44 (11-12 singles, 19 min "
                                           "before the listed start) to single fills.", editable=False)


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
    balance_poll_s: float = param("Seconds between shard balance reads, for trading.max_dollars_auto; "
                                  "between them cash moves with our own fills and settlements.",
                                  min=1.0, step=1.0, editable=False)
    meta_refresh_s: float = param("Seconds between market status / settlement reads.", min=30.0, step=30.0,
                                  editable=False)
    max_order_errors: int = param("Consecutive order errors that stop new takes and quotes until a clean "
                                  "response.", min=1, max=100, step=1, editable=False)
    liquidity_log_s: float = param("Seconds per row of the per-market liquidity log "
                                   "(data/live/ARBS/liquidity/).", min=30.0, step=30.0, editable=False)
    feed_down_pull_s: float = param("Pull every quote once the Kalshi websocket has been down this long.",
                                    min=0.0, step=1.0, editable=False)
    feed_ready_per_market: bool = param("After a Kalshi reconnect, a basket trades (and a feed-down pull leaves its "
                                        "quotes) once each of its markets has its fresh snapshot, instead of "
                                        "waiting for every market to be resubscribed.", editable=False)
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
