# ARBS — CURRENT STATE 2026-11-12 ~17:40 UTC (read this first)

Cross-market basket arbitrage on Kalshi: buy a set of legs (a "basket") whose
payout is guaranteed whatever happens, for less than that payout. Live with
real money on the `ec2-live` box since 2026-10-28. Commits are on `ec2-live`.
The 11-04 sections further down are kept as history; this section supersedes
them where they differ.

## Where it stands (11-12 ~17:40 UTC)

- **Two processes, one service.** `alpha5-arbs` runs `run.py --series ARBS`,
  which starts each registered series as its own child: **ARBS** (Kalshi shard
  0, hand spec + scan, `config.json`, `data/live/ARBS`) and **ARBS3** (shard 3:
  tennis, basketball, baseball; scan only, `config_shard3.json`,
  `data/live/ARBS3`). Same code and rules; separate configs that are kept in
  step by hand (`scan.shards`, `scan.hand_spec`, `scan.max_markets` differ).
  The dashboard shows both on one ARBS page (`DASHBOARD_MERGED`).
- **PnL** (summary.json): ARBS settled +$2,273, locked +$1,173; ARBS3
  settled −$67 (two ITF tennis fills, −$44 each, see below), locked +$10.
- **Cash**: shard 0 $17.6k, shard 3 $9.0k (moved from shard 0 on 11-11,
  $3k + $6k). Quote room = shard cash − `cash_reserve` $1k.
- Both armed (`--resume-arm`). No halts.

## Polymarket US: third process ARBSPM (b965106, deployed 11-13 00:20 UTC, shadow)

- **What it trades** (`pm_scan.py`, `config_pm.json`, `data/live/ARBSPM`):
  Polymarket three-way soccer (complete set both ways, NO pairs), weather
  buckets for the six stations both exchanges list (NYC hi/lo, Chicago MDW hi/lo,
  MIA, LAX, SFO, PHL highs), and `cross_venue` baskets: one outcome on both
  exchanges (PM YES + Kalshi NO and the reverse, payout 1). Soccer games pair by
  both team names + date (~230 games/week); weather by station and bucket edges.
- **Plumbing**: Polymarket is shard `PM_SHARD` (100); a lowercase ticker is a
  slug (`venues.is_pm`). `infra/polymarket/orders.py` (client behind
  KalshiOrderClient's interface; NO = BUY_SHORT at 1 − p; a move is cancel +
  create), `infra/polymarket/feed.py` (whole books, offers → NO bids; private
  websocket for our fills and rejected/unfunded orders). Polymarket fills book
  up to each order's `cumQuantity` (websocket and reconcile can't double-book).
- **Rules that differ**: ARBSPM rests quotes on Polymarket legs only (a quote
  of its own on a Kalshi market ARBS trades could meet an ARBS IOC, which
  self-trade prevention cancels); a cross basket's Kalshi leg is taken. Each
  process's audit subtracts the other processes' Kalshi ledgers
  (`ARBS_PROCESSES`). $300 max, 5 lots, 3 days out, no live events, 100 write
  tokens/s = 10 of the key's 20 requests/s. Fee: taker 0.095·p(1−p), maker 0.
- **Not yet**: the Polymarket account has no funds (the 11-12 $8,000 deposit
  shows REJECTED), so arming refuses (zero balance). Unconfirmed in the docs and
  only checkable live: whether a modify keeps the id (we cancel + create
  instead), whether an IOC's synchronous executions always come back, how a
  post-only cross is reported, settlement timing for sports, and any in-play
  order delay. Arm with a small first session and watch `order_gone` rows,
  fills and the audit.

## What changed 11-05 → 11-12

- **Exit-cost model** (`exit_cost.py`, `e071bf6`): predicts what exiting a
  maker fill costs from the exit legs' books (top depth, depth that has held
  0.25 s, depth 2c deep, unwind top), live, time to settlement. Additive
  binned ridge on daily X'X/X'y; fill odds from how far prints swept below the
  touch; expected filled lots from print volumes. Fitted nightly on every
  saved day of both processes and written to `data/live/ARBS/exit_model.json`
  by the **daily shadow check** (`shadow_check.py`, timer
  `alpha5-arbs-shadow-check` 00:40 UTC; dashboard tab per shard). Validated:
  replay exits land ~30–90 ms after the print, and replay at 0.1 s is within
  ~0.15c/lot of our real fills; the model generalizes across groups where
  per-market learning didn't.
- **Model quoting on live events** (`aeb866a`, `4874964`, `7f1cf4a`):
  live quotes in `trading.model_series` take edge (3–20c grid) and size cap
  (5/20/50/100) from the model: the pair with the best fill odds × expected
  lots × predicted net. Sticky: widen only when the current edge is predicted
  to net under 0.5c a lot; narrow one step after a narrower pick worth 1.25×
  holds 120 s; shrink at once; grow one step after the bigger pick holds
  300 s; no positive value pulls the quote. Journal `kind: model_quote` (why:
  start/widen/narrow/shrink/grow), counter `model_changes`.
  **On**: S:Esports, S:Basketball, S:Football, S:Baseball, Climate and
  Weather. **Off: soccer** (11-12: the model quoted it 10–20c against a fixed
  5c that made +$72 on 73 live arbs the day before; 7.5c cut soccer fills
  85–90% on 11-04) and **all pre-game** (pre-game profit is >300-lot sweeps,
  +$198 of 7 fills 11-11..12; the model has no data above 100 lots). First
  results (11-11 23:39 → 11-12 16:00, CS2/NFL/NCAAMB only): +$18.75 on 22
  arbs, 2.3c/lot vs 2.0c/lot fixed the day before. Too little data to call.
- **Model quoting fixes** (`21ccc89`, live 11-12 19:32 UTC), from a churn
  check of 16:09–18:49 UTC (the model's own changes cause 2–3% of the writes on
  the legs it quotes, ~1.4 tokens/s on shard 0; pull cancels and their
  re-creates dominate, and live esports writes per resting leg-hour are the
  same as on fixed quoting):
  - A quote priced through another basket (`best_plan`) must clear the leg's
    model edge too (`min_need`). The other baskets were priced at the config
    edge (5c live) and undercut the model wherever a market side sits in 2+
    baskets: on weather (72% of model-quoted sides) 40% of new quotes for
    15–20c picks rested ~11c tighter and the quote flipped between the two
    prices (amends of 5c+ 5–7 → 31 per resting leg-hour, writes 220 → 333, same
    hours 11-11 vs 11-12). Soccer's hour on the model had the same leak (58%),
    so it was not a clean test of the model's soccer edges. Each alternate
    taking its own model pick was tried and dropped: a pick costs ~1.1 ms.
  - A held pick that can't be priced no longer restarts at the model's pick
    (586 times in 2.7 h, a narrow or grow with no hold, a quarter undone
    within minutes). Size shrinks at once; a narrower pick waits the 120 s hold
    and then moves to the widest edge that can be priced; size grows only if
    the bigger size can be priced at the held edge; the hold clocks reset on a
    real start. Such a leg rests no quote from that basket during the hold.
- **Weather edges, replay 11-05..12** (34.8k live weather prints our quotes
  met, 5-lot quote, pull rule applied): fixed 5c +$32, 4c +$32, 15c +$27
  (half of it 11-10), 20c +$20; the model's pick +$44 (fitted on these days).
  Even a 3c quote sits a median 9c under the touch on weather, so every fill
  is a sweep. Sweeps 20c+ under the touch lost 2–5c/lot for 2–5c quotes and
  made 6–10c/lot for 15–20c ones. The model picks 15–20c on 16% of prints;
  there 5c made +$2.1 on 76 fills, 15c +$11.2 on 40, 20c +$7.8 on 20.
- **Shard 0 since 11-13 00:19–00:26 UTC: `quote_size` 2000 and
  `near_min_size` 1000** (shard 3 unchanged: 1000 and 0). After it: 335 arb
  quotes at 2,000 ($431k resting in arb quotes against $16k of cash), ~235
  near quotes, median 1,800 lots ($225k resting), costing 4–5 write tokens/s;
  the write bucket stayed full while they went in. By 01:40 the evening's
  pulls had cut that to ~550 arb quotes (145 at 2,000) and ~85 near quotes.
  **`deep_quote_size` 1000 on shard 0 since 02:04 UTC**: deep quotes take
  `quote_size` as their cap, so at 2000 they rested up to 2,000 lots (19 of
  them) until then. First fill over 1,000 lots: 1,877 pre-game NO on
  KXSTREAMERAWARDS-VTUBER26-THE at 02:03:57, completed for +$55.20
  (2.9c/lot); no near fills yet.
- **Near quote** (`ab3e647`, `f1523b8`; `trading.near_min_size`, 0 = off,
  1000 is the size that held up). It takes only what the arb quote
  leaves of `quote_size`, so at `quote_size` 1000 it never appears; with a
  cap of its own it sat under every arb quote already at `quote_size`: on
  for 3 min on 11-12 (23:53–23:56 UTC) it placed ~700 quotes of 1,000 lots
  on the two shards, emptied shard 0's write bucket and cut the hot list
  from 1,510 to 1,106 baskets; no fills. Since then it comes last for
  writes: placed, repriced and grown only with half the write cap left in
  the bucket (`NEAR_RESERVE`), not charged to its basket's write cost, and
  a leg without one is checked for one every 2 s.
  On a pre-game basket a second quote one tick under
  each arb quote, in the deep quote's slot (journal `deep` + `near`, fills
  `fill_kind: deep`), sized to the bundles the other legs complete at that
  price beyond the arb quote's own. The arb quote is sized to the exit depth
  at exactly its price, often one level: 61 lots on Curaçao–Panama 11-12 with
  3,809 a cent lower, where one buyer sold 112,851 NO in four sweeps in two
  minutes and we bought 1,894 (+$108). Priced through the basket the arb
  quote is priced through; cut at once when the exits take under 80% of it,
  grown or repriced at most every 10 s; not held to `depth_age_s`.
  Since `19edc27` (11-13 02:00 UTC) a resting one stays where it is when the
  arb quote steps away, while under 5c (`deep_min_gap_ticks`) below it and
  the exits still cover it there; it is placed, repriced and grown only
  under an arb quote that has not moved for 10 s; and with no near plan it
  goes at once under an arb quote or in-play (where a deep quote could rest
  on its leg it used to wait out the deep cooldown at full size). Before,
  it was pulled each time the arb quote moved: 590 of 1,414 cancelled near
  quotes had rested under 10 s (shard 0, 00:20–01:43 UTC), 433 right after
  the arb quote stepped up, and a 1,998-lot one under a 2-lot arb quote
  hopping 46c/48c was re-placed 99 times. After (02:02–02:18): 42 of 264
  under 10 s, 4 after a step up. What is left: exit depth going, the arb
  quote losing its edge or its place on the hot list, and one leg
  (KXAAAGASM 3.80=>3.60) whose near quote went 1.3 s after each of 13
  placements with the arb quote unchanged, then held; cause not found.
- **Two-leg baskets and `trading.second_level_size`** (`fd35fa2`, deployed
  11-13 17:15 UTC, **off** on all three processes; the user decides when it
  goes on).
  - Why: 11-13 14:59 UTC shard 3 lost $118 on one pre-game fill (women's
    college basketball: a 2,356-lot sweep took our 200-lot NO bid and the one
    200-lot bid we planned to exit through; the price went from ~20c to
    ~80c). It is not the shard: both run the same rules and shard 3 sends
    exits as fast. It is the basket. Real arbs 11-05..13: 3+ legs made +$967
    pre-game (5.6c/lot, worst −$4.92) and +$424 in-play; two legs +$196 and
    −$85. In a two-outcome market YES on A is NO on B, so one trader takes
    our quote and our exit together; shard 3 is almost all two-leg.
  - The rule: a two-leg basket's quotes (arb, near, deep, the baskets a
    quote may be priced through, the ranking's size) are held to what the
    other leg still completes, without a loss, with its best ask level gone.
    Every recorded sweep 11-04..13 replayed (183k rows, dedup 3 s): pre-game
    two-leg −$179 as sized today, +$399 with the rule (both halves, no worse
    on 9 of 10 days, +$269 without the 3 best events; looser tolerances do
    worse in order). Real fills scaled to the smaller size: pre-game +$58 →
    +$150, in-play −$131 → +$274. It costs 3+ leg baskets $100–150, so it is
    two-leg only. Shard 3 pre-game stays negative even with it (−$72 replay).
  - Not the answer: more edge alone (5% → +$115, second half −$103; shard 3
    negative at every edge and size); exits taken 1 s later (−$255).
  - Before switching on: against the public books at 17:14 UTC it adds no
    orders and removes 102 of shard 0's 315 two-leg arb quotes (29 more cut;
    resting lots −20%) and 124 of shard 3's 1,179 (108 cut; lots −17%). Not
    measured: how often a resting quote is cancelled and re-placed because
    the level behind the best changed (a quote over its new size is
    cancelled, as today). Switch on one shard and read cancel reasons and
    quote lifetimes within the hour.
- **Exit model, checked 11-13** (the user asked why it missed these and
  whether to put every market on it):
  - It only prices in-play quotes (`model_pick` passes live) and all five
    large losses were pre-game. On those fills it predicts 1.5–3.3c of exit
    cost (real 8–63c): it predicts 2.7–3.9c for a two-leg pre-game fill
    whatever is behind the best exit level, where the replayed cost runs
    2.8c to 9.0c. It is fitted on every print at ≤100 lots with losses
    clipped at 50c, is additive (one −1.0/−1.5c adjustment for leg count),
    its `top_r_min` is ~1 for any quote sized to the best level, and
    **`spread_c` is always 0**: `exit_cost.features` subtracts the ask from
    itself and `live_features` passes 0.0. Not fixed.
  - In-play, each day scored by the model fitted on earlier days only (as
    the shadow check does), all sweeps 11-05..13, sizes 5–100: model $4,072
    vs fixed 5c×100 $3,048, ahead on 8 of 9 days; groups on it $1,385 →
    $1,721, groups not on it $1,664 → $2,351, soccer $959 → $1,402 (7 of 9
    days). Basketball slightly worse, football flat. Sizes over 100 in-play
    lose on two-leg baskets (one −$589). The replay matches real in-play
    fills on 3+ leg baskets (4.2 vs 3.6c/lot) and flatters two-leg ones by
    ~1.5c/lot. Real, in the five groups: +$75 on 277 arbs (1.6c/lot) in the
    34 h before, +$46 on 138 (2.7c/lot) in the 21 h since: about the same
    dollars an hour on 40% fewer lots.
  - Pre-game on the model: with its 100-lot grid the replay falls from
    $1,159 to $475 (without the 3 best events); with sizes to 1,000 it is
    $1,595, all of the gain on two-leg baskets (−$179 → +$412), which the
    size rule gets too, and 3+ leg baskets lose $200.
- **Sweep research 11-12** (every recorded print 11-05..12 that offered arb
  edge, replayed against the books before and 0.1 s after; scripts were in a
  session scratchpad, prefiltered rows in `~/scratch/sweeps` on the box):
  - Held up: the near quote where it would be 1,000+ lots, pre-game: $3,245
    on 23 fills over 18 events and all 8 days, one loser (−$6), positive in
    both halves, the same at 500 or 2,000, unchanged with exits at 1 s or half
    the fills. Real arbs of 500+ lots (16, 11-09..12) all made money, 5.1c/lot.
  - Did not hold up, not built: near quotes under 1,000 lots (−$334);
    quotes 5–15 ticks lower (−$717, in-play); the same quote in-play, always
    or for 5 min after a clean fill (negative on shard 3 and at larger size);
    asking more edge after a clean fill (a fraction of sizing up); standing
    down after a losing fill (on real fills −$34 10-30..11-04, −$2 11-05..08,
    +$184 11-09..12 of which +$203 is Liga Pro table tennis, now excluded).
  - True in all three periods on real fills, but no rule built on it yet:
    arbs within 5 min of one that made money made 3–9c/lot, of one that lost
    −3 to +0.4c/lot.
  - Deep quotes, real: 166 fills 10-30..11-12, about +$110 (5.6c/lot), for
    5–10 write tokens/s on shard 0 (7–11% of quote writes sent, mostly
    amends). About what those tokens would earn on the next baskets; left as
    they are.
- **`quote_size` replay** (pre-game prints that met our quote, shard 0,
  11-05..12, extra $ over as quoted; the exits complete under 1,000 at the
  arb quote's own price on 78% of them, so a higher `quote_size` only
  changes the rest): 2000 +$970, 3000 +$1,458, 5000 +$2,148, each positive
  in both halves and without its 3 best events (+$442/+$713/+$984), 94 fills
  changed, 24 losing, worst −$181 (a pre-game CS2 fill; esports −$194 and
  boxing −$67 in total at 3000, soccer +$1,168). The near quote on the rest
  of it adds +$260/+$354/+$778, resting on 9–14% of quoted prints. No extra
  orders for the bigger arb quote; the replay ignores `depth_age_s`.
- **Live flag** (`ab3e647`): an unwind that doesn't lose ("at plan") no
  longer flags its markets live; a fast move still does. Real arbs within 5
  min of a profitable unwind made 3.8c/lot (225), −4.1c after a losing one.
  The first Curaçao–Panama fill was such an unwind and left a 61-lot live
  quote for the second sweep.
- **Shard 3 / ARBS3** (`129265b`, `96c9976`, `b25d9e7`, `65ef47c`): per-shard
  hot budget (Kalshi bills a shard's writes to that shard), cash only from the
  scanned shards, one unscoped balance read for every shard (`5937f88`; a
  second per-shard read 429'd every poll). ARBS excludes baskets on shards it
  doesn't scan. `scan.max_markets` 3700 on ARBS3 (watches ~2,800, every shard-3
  market that forms a basket); ARBS stays at 1500 because shard 0 is
  **write-bound** (hot list 250/250 tokens/s, ~360 quote creates refused a
  day) — more markets there only add candidates.
- **Live rules**:
  - `scan.live_before_settle_h` 6 (`2bfb2a9`): an event with no kickoff is
    live from 6 h before expected settlement (a weekly Hormuz ladder lost $108
    to one headline 1.7 h out, 11-10).
  - `scan.live_early_h` (`4e0dc10`): ITF / ITF women's / ATP and WTA
    Challenger singles live 2 h before their listed start (lower-tier tennis
    starts early: −$44 doubles 11-11, −$44 singles 11-12 19 min before start).
  - `trading.late_quote_cutoff_min` {S:Soccer 75, S:Tennis 0}: no live tennis
    quoting at all; soccer stops 75 min after kickoff.
  - Fast-move flags (unchanged): a basket exiting on a fast move marks its
    markets live until a clean fill.
- **Markets**: `watch.exclude` (series, events or clusters, never traded)
  = Liga Pro, ETTAN, NBA and NCAAMB race-to-10 (they close mid-game, so a
  broken basket can't be exited). `watch.record_only` (`3693525`; watched and
  recorded, never quoted) = hockey (NHL, AHL, KHL, NCAA), cricket (T20, ODI,
  Test, women's T20/ODI) and tennis doubles (ITF, ITF women's, Challenger,
  WTA): their prints build data to test them under the model. The user
  manages these lists by hand (declined an automated quarantine 11-12).
  **ITF singles (KXITFMATCH, KXITFWMATCH) record-only on shard 3 since 11-13
  06:46 UTC** (box config, the user's call; 144 quotes came off on the
  reload, no restart). Pre-game ITF made −$79.35 on 48 arbs 11-11..13, all of
  it three fills of 81–426 lots buying NO on the longshot at 86–92c into a
  650–4,800-lot sweep of that longshot (−$107.06; the other 45 made +$27.71).
  The third (−$19.17, 11-13 03:20 UTC) was 6 h before its listed start, so
  `live_early_h` did not cover it. The same fill shape on other two-outcome
  markets (boxing, streamer awards) made +$105.83 on 6 arbs. ITF only began
  trading 11-11, so three days is the whole record; shard 0 quotes no ITF
  singles.
- **Cash and reads** (`ec1f79c`, `a58612d`, `b7b4123`): cash is read from
  Kalshi once an hour per process (`runtime.balance_poll_s` 3600) and moved
  locally by our fills (cost + fees), unwinds (+$1 a pair) and settlements;
  each read journals the drift (`balance_drift`). Kalshi's balance does not
  drop for resting orders. A machine-wide read bucket
  (`orders.read_budget`, 300 tokens/s, 10 per read, a 429 empties it) counts
  every order client's authenticated GET; the balance read only goes out with
  100 tokens left after it. Summary `reads` shows tokens left and reads/s.
  Our counted reads are ~5–6/s per process, so earlier balance 429s were
  probably a per-endpoint limit or uncounted reads (scanner, other
  strategies).
- **LIP removed from ARBS** (`27f056d`): the LIP edge discount, LIP pricing
  and the LIP ranking are gone. LIPFARM was retired 11-12 and ARBS's
  `claimed.json` yielding removed with it.
- **CPU** (`ddd3ffc`): 85% → ~60% of a core for ARBS; ARBS3 ~40%.
- **Replay fixes** (`06397cf`): shadow fills exit by the cheaper of
  completing and unwinding at the 0.1 s book, as the runner does.
- **Hand spec**: settlement dates for 7 settled election series removed
  (`5b9fc46`); 8 remain (county/margin/vote-share, Brazil rate, streamer
  awards).

## Open items

1. **Model on soccer**: compare its soccer picks with our real soccer fills
   (it over-predicts exit cost on many-leg soccer baskets) before re-adding.
   Its one hour live (11-12 16:11–17:28) ran with the alternate-basket leak,
   so judge it on replay or a rerun, not on that hour.
2. **Model results by group**: nightly check; watch for fill rates dropping as
   they did on soccer (fills/h fell from 12.6 to 5 in the first hour with all
   groups on). Weather: with `21ccc89` quotes rest at the model's edge, so
   expect fewer weather fills; compare real fills at 15–20c with the replay
   above once there are some (replay is validated only on narrow-edge fills).
3. **Model's wide end**: it cannot tell 15c from 20c (exit loss is one bin
   above 10c of edge, fill odds stop falling at 30c under the touch), so
   wherever the quote is deep under the touch it takes the top of the grid;
   15c replayed better than 20c on those weather prints. And live weather legs
   spent 45% of leg-time at 20c and 11% at 15c (11-12 16:09–18:49) against 16%
   of prints in replay: widening is immediate, narrowing back from 20c takes
   2+ min a step. Finer bins above 10c, or narrowing straight to the pick
   after the hold, are the two things to test.
   **`quote_size` 2000 and near quotes on shard 0** (11-13): compare real
   fills over 1,000 lots, and real near fills (`near` quote rows, deep fills
   one tick under the arb fill), with the replay above before going to 3000;
   the replay's worst fill at this size was −$181 (pre-game CS2) and esports
   and boxing lost. Resting size is now ~40x the cash: several large fills at
   once could leave a completion short of cash (4,000 bundles is ~$8k of
   shard 0's $16k).
   **Quote price flips between baskets** on pre-game soccer legs (2-leg vs
   3-leg basket, ~18c apart): about half of 11-12's amends on CONCACAF legs
   were jumps of 5c+. Costs writes and queue place; not looked into.
4. **Pre-game on the model**: revisit after ~a week of live results; needs the
   size grid extended above 100 once there is data there.
5. **Record-only markets**: after a few days, rerun the excluded-markets test
   (`~/scratch/excl_model.py` on the box: replay at the model's pick vs fixed
   rules, per series × live) on hockey, cricket, doubles and ITF singles
   (record-only since 11-13). Live doubles
   already replayed positive under the model (+$2.8..11.6 over 2 days), but
   live tennis quoting is off.
6. **Shadow sizer** (`live_params_shadow`, `df2ebb6`/`b024127`) and
   `regime_shadow` still run in shadow; the model supersedes the sizer —
   candidate for removal.
7. **Pull rule** churn: ~3,900 pull cancels/h on live basketball (11-11);
   each costs queue place and write tokens on write-bound shard 0. Worth a
   look. On 11-12 a live esports leg had a queue-resetting write about every
   10 s, 177 pull cancels per resting leg-hour.
8. Startup: both processes arm at once; one balance read can 429 and delay
   arming ~1–3 min (seen 11-12 16:09). Staggering is a small fix.

## Ops (current practice; supersedes the 11-04 deploy recipe below)

- Box `ubuntu@3.131.100.179`, key `~/.ssh/alpha5-live-key.pem`, `/opt/alpha5`,
  `.venv/bin/python`. Log `data/live/arbs.out.log` (both processes print
  `[ARBS]`).
- **Deploy code by file path**, never `git pull`/checkout of strategy configs.
  Since the 11-12 rename the box is a detached checkout of `origin/ec2-live`
  with later files checked out on top: push, then on the box
  `git fetch origin ec2-live`, check `git diff --cached --stat origin/ec2-live`
  lists only the files you mean to change, and
  `git checkout origin/ec2-live -- <files>`. Then
  `sudo systemctl restart alpha5-arbs` (and `alpha5-dashboard` when config.py
  or the page changed — a stale dashboard reports "config.json is invalid" on
  new keys).
- **Config**: edit single values on the box (python json edit or sed) after
  copying both configs to `/tmp/before_<what>/`. A new required key must land
  with (or after) the code that defines it; a running process logs
  "REJECTED config change: unknown <key>" if the key lands first — harmless
  once restarted. Many trading params apply live without a restart.
- Bulk research on the box: `sudo systemd-run --uid=ubuntu --gid=ubuntu
  -p MemoryMax=2G -p Nice=19 …`, scripts in `~/scratch` (`/tmp/re.py`
  shadows the stdlib). Strategies have priority for CPU and memory.
- Tests: `python -m pytest -q strategies/ARBS` (231) and
  `infra/tests`. `test_slow_drift_keeps_resting` is flaky on Windows.

---

# ARBS — STATE 2026-11-04 ~17:00 UTC (history)

Cross-market basket arbitrage on Kalshi: buy a set of legs (a "basket") whose
payout is guaranteed whatever happens, for less than that payout. Live with
real money on the `ec2-live` box since 2026-10-28. Commits referenced below
are on `ec2-live`. The 2026-10-30 state further down is kept for history;
the election-night section below supersedes it where they differ.

## Where it stands (11-04 ~17:00 UTC)

- **PnL** (summary.json): settled +$627.62, locked +$504.22, upside +$32.50.
- **Capital**: $13.7k held cost across 154 baskets. Sized against shard 0's
  Kalshi cash (`max_dollars_auto`): cash $14.86k, room $13.86k after the $1k
  reserve. `max_dollars` 20000 is only the fallback.
- Armed (`--resume-arm` re-arms after a restart, ~40 s). No halts.

## Election night 11-03/04: what was done and learned

- **Sizing raised** for the election: quote_size 400, live_quote_size 100,
  live_take_size 10, deep_quote_size 50, live_edge_add 0.005, quote_edge_add 0.
  Live election quotes, 4 h before size 100 (06:13 UTC): +$127.60 on 182
  episodes, 6.7% return, 12% losing. After: +$26.09 on 51, 2.9%, 25% losing;
  the 51–100-lot fills +$13.10 (2.1%, 38% losing), one AZ05 fill −$10.15.
  Much of the drop is late-night close counts (sweeps both legs at once), not
  size: 1–25-lot fills fell from 9.9% to 4.9% too. Replay had predicted ~14%.
- **Sports excluded** (`watch.exclude`, 154 series/clusters) for the night:
  live sports quotes lost −$3.58 in the 4 h before. Re-enable / replace with
  something automatic (see Proposed improvements #10).
- **Capital "used" overstated**: max_dollars − held cost counted YES/NO pairs and
  event-netted positions Kalshi had already credited ($11.8k "used" vs $3.2k
  Kalshi exposure). Now `max_dollars_auto` (`1239067`): room = shard cash
  (Kalshi balance, read every `runtime.balance_poll_s` 5 s) + the basket's own
  resting quotes − `cash_reserve` $1000; falls back to max_dollars in shadow
  or after 6 missed reads. Dashboard "Capital used" tile shows shard cash.
  Open: if other strategies (S&P) trade shard 0, raise cash_reserve.
- **Maine Senate "−$112" on Kalshi** is Kalshi's per-market view; the
  baskets on it made +$39.79.
- **Pull signal research** (`~/an/tmp/pulls.py`, `lead.py`, `sig.py`,
  `sig2.py`): fill_context recorder reworked to ms resolution (`18b41e5`:
  price changes always open a sample, depth-only changes coalesce 10 ms;
  fills carry Kalshi's exch_t; order log has amend/cancel rows with the
  newest book time, so sent − book = our reaction, ~3.6 ms, RTT ~10 ms).
  Live elections: 20 of 28 losing fills (−$47 of −$68) had no move before the
  match itself (simultaneous cross-leg sweeps); nothing beats the base rate
  there. Sports (older, coarser data): others' depth at our price −50% within
  2 s, hold 2 s, would skip $60 of losses for $30 of wins, pulled 1.7% of
  the time. Implemented as the pull rule (below) to track live.
- **Race / order path**: legacy pre-10-30 order path (`142388b`) was no
  better against other makers (1 s markout −2.96¢ vs −1.91¢); back on the
  current path (`30d3042`).
- **Exit bugs fixed** (`86f37a9`): a broken basket's exit cost was net of
  today's broken_spent, which a partial completion raises, so retries read
  dollars off plan (vs_plan −7.92 at plan; a fake −$241 "worse than planned"
  total) and "at plan" re-fired. Now on the plan's basis. A missed exit IOC is
  retried on the next book update with no delay (a 50 ms→1 s backoff was
  tried and removed: fast retries finished better); after
  `completion.max_exit_misses` (5, dashboard) misses in a row the basket
  rests post-only bids instead, with an alert.
- **Leg-miss analysis** (`~/an/tmp/lm2..7.py`, 1,330 broken episodes
  11-02..04): about half of live breaks miss at least once; median 2 tries,
  0.06 s to whole. Live misses cost $185 beyond the first exit decision,
  vs $277 already lost at that decision; on 538 of 650 missed legs our book
  showed the size (lost the race in the same sweep). Retries paid a median
  3.5c over the first limit. 1-2c of IOC slack on retries est. +$5-10 per
  3 days; deeper sweeps, path switching, take order and chase_ticks showed
  nothing. The lever is not being filled into the sweep.
- **Unwind basis**: ledger legs keep lots; an unwind and
  broken_spent take the newest contracts (the fill that broke the basket),
  not the leg's average, so bundles still held keep their own cost.
- **Pull rule**: `trading.pull_drop` 0.5, `pull_window_s` 2,
  `pull_hold_s` 2 (on the dashboard form). On a live basket's quoted market
  side, others' size at the best bid falling under half its peak at that
  price within the window cancels the arb + deep quotes on that side for the
  hold. Journal rows `kind: "pull"` (ticker, side, price, depth, quoted
  baskets); cancels have reason `pull`; summary counter `pulls`. Track over
  the next days: fills avoided vs fill_context control rows; score per
  category (sports only once they're re-enabled).
- **Cheapest-basket completion** (replaced a short-lived cross_baskets,
  which the 11-02..04 data showed would never fire: breaks last ~0.06 s, so
  two are never open on one market). An arb quote on a market side is
  priced through whichever basket with that side allows the highest price
  (`best_plan`, up to `MAX_ALTS` 8 others; `via` maps the quote to it and
  that basket's book updates re-evaluate it). A fill is booked into the
  basket whose finish cost rises least at fill time (`book_target`; fill
  row `quoted_in` = the quoting basket, counter `alt_fills`), then completed
  there as usual. Estimated upper bound from the data: ~$140 per 3 days
  from completion alone, plus tighter quotes.
- **Maker completion (option 1) rejected**: resting the needed legs lost in
  every simulated rule; after our fill the needed legs' asks rise (non-live
  +0.6-1c within seconds, our size almost never trades at the rest price;
  live +2.5c). Exit fees + spread take ~78% of edge at mid (1.4c fees,
  3.6c spread, 1.4c net per bundle, 11-02..04) - but our fills carry
  information, so that spread isn't collectable. Scripts `~/an/tmp/mc*.py`.
- **Ops gotchas** learned: put box scratch scripts in `~/an/tmp` (a
  `/tmp/re.py` shadowed the stdlib); stream journals line by line (loading
  every day OOM-killed a script); orders gz may read EOF mid-append, retry;
  balance/positions calls share the Kalshi rate limit (429 seen).

## Box, deploy, monitoring

- Box `ubuntu@3.131.100.179`, key `~/.ssh/alpha5-live-key.pem`, repo `/opt/alpha5`.
  Units: `alpha5-arbs` (stdout → `data/live/arbs.out.log`), `alpha5-dashboard`.
- **Config-safe deploy** (the box's `strategies/ARBS/config.json` holds
  dashboard edits — never let ARBS see repo defaults):
  ```
  F=strategies/ARBS/config.json; sudo systemctl stop alpha5-arbs
  old=$(git rev-parse HEAD); cp $F /tmp/arbs_config_save.json; git checkout -q $F
  git pull -q --ff-only origin ec2-live
  .venv/bin/python data/live/merge_cfg.py $F $old /tmp/arbs_config_save.json   # if config.json changed upstream
  # else: cp /tmp/arbs_config_save.json $F
  sudo systemctl start alpha5-arbs; sudo systemctl restart alpha5-dashboard  # dashboard if a config key changed
  ```
  A removed config key the box had edited is re-added by merge_cfg: delete it
  by hand before starting (happened with `max_quoted_baskets`).
- **Scanner-only changes need just `git pull`**: the scan runs as a fresh child
  process every `scan.interval_s` (`python -m strategies.ARBS.scanner`).
- Tests: `python -m pytest -q strategies/ARBS` (136). `test_slow_drift_keeps_resting`
  is flaky (Windows' ~15 ms clock with chase_window_s 0); rerun before believing it. **Gotcha**: `pytest | tail -1 && git commit`
  commits even when tests fail — check the output.
- Analysis on the box: run scripts with `PYTHONPATH=/opt/alpha5 .venv/bin/python`.
  - `data/live/arbs_watch.py` — health, PnL vs last run / night start, new
    alerts, untracked orders; **disarms** on −$30 from its start or −$15 between
    runs. State in `/tmp/arbs_watch_state.json` (delete to reset the baseline).
  - `data/live/arbs_daily_live_report.py` — which series the daily/hourly live
    rule catches and how they've done (see Proposed improvements).
- Disarm by hand: `request_trading_mode("ARBS", False, source="...")`
  (`infra.runtime.control`), the same request the dashboard button writes.
- py-spy at `/tmp/pyspy/bin/py-spy` (lost on reboot) — how the ledger-write stall was found.

## How it works now

- **Baskets**: hand spec (`Kalshi arb baskets spec.json`) + scanner. Scanner
  lists every MECNET (NO on k likeliest pays k−1; complete sets) and DIRECNET
  (ladders, `ladder_span` 3) event on shard 0 every 120 s; selection keeps
  `days_till_expiry` ≤ 90 and the top `scan.max_markets` (1000) by 24h volume.
  Held baskets keep their definition in the ledger.
- **Live window** (`Basket.live_from`): sports/esports kickoff from Kalshi
  milestones (Sports + Esports categories); **daily/hourly series** (Kalshi
  series `frequency`) from `strike_date − period` unless the event has a
  kickoff; else earliest `occurrence_datetime`. Plus runtime **flags**: a basket
  that unwinds or exits on a fast move marks its markets live until a clean
  fill on them.
- **Live treatment**: `live_quote_size` (10), `live_edge_add` on quotes/takes,
  takes (`take_live_events`, on since 11-03 as a trial) capped at
  `live_take_size` (1), broken baskets exit at once,
  closing held bundles needs the full arb edge.
- **Takes**: non-live sends every leg at once; live (if enabled) sends the
  stalest book first and the rest only for what it filled. Cooldown 5 s per basket.
- **Quotes**: hot list = ranking by expected quote $/h (edge × size / time for
  recent flow to clear the queue ahead), filled while each basket's write cost
  fits the write budget (incumbents kept within 1.1×). Zero-flow baskets fill
  leftover budget. Size = largest the other legs' **depth** can complete at
  the required edge at the quote's price (not just their touch), capped by
  quote_size / live size / per-basket bundles / dollars; resting quotes grow in
  place when room doubles. Resting quotes are not counted as capital.
- **Broken basket exits** (`resolve`): cheaper of complete vs unwind; take at
  once on a fast move (`chase_ticks` within `chase_window_s`), when at least
  as good as planned, or on a live event; otherwise rest post-only bids.
- **Flips / early exit**: a held basket's flip (every leg's other side) is
  taken/quoted up to held bundles when its gap ≥ `exit_need`: full edge within
  `exit_ref_days`, scaled by ref_days/days beyond, floor `min_exit_edge`.
- **Write budget**: cap `max_write_tokens_per_s` 150 while KXINXU is in its
  session, `..._solo` 250 otherwise (reads KXINXU's summary.json).
- **Feed**: one websocket; snapshots refreshed in rolling batches of 50
  (delete+add on the orderbook sid) instead of reconnecting every 15 min.
  Feed down > 5 s pulls every quote (in batches of 20) then sweeps.
- **Pauses**: Kalshi exchange-wide `trading_active` false (Thursday 3–5am ET
  maintenance) blocks new orders with one alert; a per-market `trading_is_paused`
  holds that market 60 s.
- **Persistence**: `ledger.json` (positions, settled, 46 KB) saved per fill;
  `ledger_quote_ids.json` (quote id → basket, retired kept 1 h) saved once a second.
- **Startup sweep** cancels every resting order we placed (by quote id, any
  ticker), ~100/s with retries on 429.

## Config on the box (dashboard edits included, 11-04 ~17:00 UTC)

Always read the box's config.json, not the repo's.
trading: min_edge 0.03, quote_edge_add 0, min_exit_edge 0.01, exit_ref_days 7,
days_till_expiry 120, quote_size 400, quote_live_events true, take_live_events true,
live_edge_add 0.005, live_quote_size 100, live_take_size 10, live_order_groups true,
deep_quote_size 50, deep_min_gap_ticks 5, max_bundles_per_basket 1500, max_dollars 20000,
max_dollars_auto true, cash_reserve 1000, pull_drop 0.5, pull_window_s 2, pull_hold_s 2,
requote_min_ticks 1, requote_hold_up 0.01, requote_cooldown_s 2, take_cooldown_s 5.
completion: chase_ticks 3, chase_window_s 0.1, max_broken_baskets 3.
watch: exclude = 154 sports series/clusters (election night).
runtime: max_write_tokens_per_s 150, _solo 250, write_reserve_tokens 20, balance_poll_s 5,
feed_down_pull_s 5, record_prints true.

## Dashboard (ARBS page)

PnL chart (settled + locked, windowed); header Total PnL and Expected unrealized;
alerts with a **Clear (handled)** button (`data/live/ARBS/alerts_cleared.json`);
**Bundles bought** log, newest first, one row per burst of fills on a basket
(time ET, **tte**, series, basket, how, bundles, cost, pnl, return, fills — pnl
counts unwound pairs at $1 and whole bundles at payout); active bad-fill
cooldowns; resting quotes; ranking (quote $/h, wait, write cost);
**missed fills** (prints through our would-be price on non-live baskets outside
the budget); settled baskets. Times are America/New_York.

## What the data says (10-28 → 10-30)

- **PnL by market type** (bundle log): awards/culture/futures +$81.6 (3.1%,
  1 loser in 31), elections +$74.3 (3.5%, 1/102), live soccer +$32.3 (2.0%,
  75/193 losers), econ/polls +$22.4 (4.5%), live esports +$6.7, NFL +$5.3,
  college football +$4.7, NHL +$3.3 (0.5% on $721, 30/78 losers), gas +$0.5,
  same-day weather −$3.0, cricket −$26.9 (almost all MSTOR −$27.71 on night 1,
  before the live fixes). Non-live ≈ 3–4.5% per fill with almost no losers;
  live ≈ 1.4% with a third of fills losing.
- **Live by time to market close**: fills in the last 15 min were the most
  profitable (+4–5¢/bundle, few unwinds); early-game (60+ min left) weakest
  (+0.6¢, 31% unwound). Exits near the end slip most (median $0.49 worse in
  the last 5 min).
- **Takes**: 68 non-live takes, 9 missed a leg; 8 of those were 1–5 lots on
  thin TX bucket legs (0–1 tick from the limit), 1 was the 249-lot TXSEND-P4
  (P5 jumped 5 ticks, −$1.49). Edge rarely lasts past a take (3/68 retaken
  within 10 s). Decided: leave takes as they are (no thinnest-first, no
  chunking, no 1-tick slack — slack only pays a tick when a level refills).
- **Latency** after fixes: order RTT p50 10 ms / p90 30 ms; fill→exit decision
  p50 35 ms / p90 62 ms; Kalshi fill→booked ~30–60 ms.
- **Liquidity logs** (`data/live/ARBS/liquidity/`): sports books worsen ~21×/h,
  politics ~0.5×/h; 1,627 index-ladder markets in the feed, 92% never trade.
- **LIP**: 221 programs on our markets (~$17.5k/day pools) but at arb sizes
  (15–50 lots, capped by the other legs' depth) our share is ~$1/day; collecting
  it needs ~1,000 lots resting at the touch = market making, not ARBS.
- **Quote limits far below the best bid** on ladders / partial MECNET baskets
  are by design: those bundles pay more than their floor in some outcomes and
  the market prices that upside (e.g. RT MON 35/40 bundle ≈ $1.07 vs a $1 floor).

## Fixes made this session (by theme)

- Scanner: `0f08039`, shard 0 only `9bebe5c`, every relationship `9e91067`,
  120 s `e3c2783`, child process (OOM at 3.5 GB) `20f688b`, esports starts
  `7c80c7d`, daily/hourly live window `1eda1e7` + kickoff exception `6396726`.
- Broken baskets: cheaper exit + rest/chase `18e6bf8` `4031d42` `5965c69`,
  retry on change not timer `9246386`, marginal exit cost `b252d22`, take at
  plan / when not losing `f4023be` `d9e9528`.
- Live: no quoting → switch `b0418e8`, kickoff not end `43c96fe`, stalest-first
  + live cap `3322aaa`, take_live_events `b42213b`, live closes need full edge
  `0fa96cf`, flags + live_edge_add `3b6f8ac`.
- Ledger/audit: held baskets keep definitions `2be8d2f` `a3f3973`, audit on
  settled markets `3c92c6d` `16781d9` `4846d12`, quote ids out of the ledger
  `9bc94ec` `bc52390` (each fill rewrote 7 MB; a live exit waited 7 s).
- Ops: exchange pause `eb7c10a`, market pause hold `0442a11`, sweep by quote id
  + 429 retries `a0b182a`, ghost quotes after a feed-drop pull `b8fb147`
  (217 quotes tracked but cancelled), rolling snapshot refresh `a478f68`.
- Quoting: quote $/h ranking `a15470d` `8bfe38e`, write-budget hot list `0d4cec9`
  `838a7e0`, ranking every 10 s `ea2be93`, capital ignores resting quotes
  `93ee9ee`, depth sizing `9f0c6dc`, grow in place `8d3f064`, exit edge by
  expiry `64a6b04`, KXINXU-aware write cap `91f130d`.
- Dashboard: chart `9f51618`, alerts clear `d55462a`, bundle log `43388cf`
  `494a15a` `0e407af` `c2d6d38` `c2c7f2b`, missed fills `43388cf` `e878bdf` `47eeb32`.

## Proposed improvements (status, with evidence)

| # | Idea | Status | Evidence / next step |
|---|---|---|---|
| 1 | Pause quoting an event after a bad exit (`bad_fill_cooldown_s`) | **Removed** (was off at 0) | Replay: 60 s skipped 163 fills, saved $31.6 on 36 losers, gave up $53.1 on 127 winners. Shorter (0.25–10 s) is ≈ break-even to negative excluding weather. Caveat: that replay's episode accounting was flawed (it overstated weather losses); redo with bundle-log bursts before revisiting. |
| 2 | Same-day weather/gas treated as live | **Done** (`1eda1e7`) | Weather −$3.03 total, all from one SF fill after 9 pm local (−$5.05, 50 lots). Watch `arbs_daily_live_report.py`: it also catches Trump approval (daily, +$5.11, no losers) — consider an exception list (config) if it keeps making money as non-live. |
| 3 | Stop quoting a live basket when a leg is ≥97¢ / ≤3¢ | **Rejected** | Replay: would skip 33 bursts worth +$8.23 (21 winners). A leg at 0.99 is usually a lopsided game, not a decided one. If decided-game pick-offs matter, key on the book (one side empty / spread blown), not price. |
| 4 | NHL: bigger live edge or off | Open | 78 bursts, +$3.26 on $721 (0.5%), 30 losers. |
| 5 | Per-market live edge from replayed flow | **Shadow** (`runtime.live_params_shadow`) | `live_params.py` keeps decayed per-edge fill/PnL sums per sport → series → event (shrunk toward the parent, chosen by lower confidence bound, "off" a candidate), fed by `replay.py` scoring every recorded live print as a quote priced 20 ms before it, with the pull rule's gate. Walk-forward on 11-01..04 replays: 2,226 vs 1,640 for static 4c and 2,018 for the best static edge per group fit in hindsight, mostly election night; in sports alone ≈ per-group static. Summary `live_params`, journal `kind: live_params` every 5 min, fill_context rows carry `edge` and `shadow_edge`. Next: a week of print rows with `pre`/`pull`/`quote`, check the replay against our real fills per group, then decide on going live with min/max edge bounds. |
| 5b | Switch a non-live event to live settings on news, without waiting for a bad fill of ours | **Shadow** (`runtime.regime_shadow`) | `depth.MoveWatch` on others' mids: ≥2 of an event's markets each moving ≥2¢ within 10 s, or a market ≥5¢ off its mid 5 min ago. Journal `kind: regime_shadow` once per 30 min hold per event and signal, summary `regime_shadow` lists the events held now. Replay of 11-01..04: in-game sports 83–91% flagged vs <1% pre-game, election night 22–43% vs <1% on election day; non-live cost $4–20 over 4 days. No signal (these, other makers' markout, print rate) fired ahead of the one non-live news sweep (KXRT-CAT 11-03, 30¢ in one print), so this only catches what follows a first move; 31 of 38 real non-live losses were the first bad fill in their event. Next: a few weeks of fires against our fills and prints, then decide whether it drives live settings. |
| 5c | Soccer late-game quoting | **Stopgap** (`trading.late_quote_cutoff_min` {S:Soccer: 75}) | 11-01..04 soccer fills 75–120 min after kickoff: 1,110 arbs, −$47, the rest of the game positive; both 11-04 goal sweeps (−$34) fell there. Live soccer baskets stop quoting 75 min after `live_from`; exits and takes unchanged. Meant to be replaced by the live estimator learning game phase (a phase level did worse out of sample on 4 days; retry with more). |
| 6 | Per-event dollar cap | Open | ~$1.9k on TX Senate identities. |
| 7 | Generalize early exit to baskets without an exact flip | Deferred | Only ~$3 locked in such baskets; would need valuing their extra upside. |
| 8 | Scan everything (`scan.max_markets` removal) | Open (user ask) | Needs multi-connection websocket and series-level fee caching first. |
| 9 | Separate LIP market-making strategy | Idea | RT scores / Trump / Truth Social pools ~$100/day each; inventory risk. |
| 10 | Automated per-market size/edge (replace the sports exclude list) | Next (user ask, 11-05) | Size 100 lost return in close counts (AZ05 −$10.15, MN01 −$1.98); sports lost live. Score markets on recent fills/sweeps and set size + edge per market instead of hand lists. |
| 11 | Pull rule (others' depth at our bid −50%) | **Live, tracking** | See election-night section. Judge on journal `pull` rows vs fill_context after a few days; sports is where it should pay. |
| 12 | Exclude volatile close-count House races (AZ05) | Offered, not done | One AZ05 basket kept getting swept during counting. |
| 13 | Other arb markets after the election | Open (user ask) | Asked whether the strategy is "cooked" post-election; non-election baskets (awards, econ, weather, sports with fixes) carry it. |

## Open risks / watch items

- TX Senate settlement: identity holds under the rules (same margin definition,
  inclusive lower / exclusive upper, no rounding); breaks only if the candidate
  runs under a different party.
- Live repeated pick-offs on goal swings (PERPJE 45 bundles in ~1 s, Israeli
  league games 10-30 morning) — cost ~$8–12 that morning; see #1/#5.
- Hand-spec markets no longer listed (GOVPARTYME-26-*, a Brasileirão score
  market, NYC marathon ACOL) just sit idle.
- Weekly Kalshi maintenance Thursdays 3–5 am ET: handled (exchange pause).
- `data/live/ARBS/alerts_cleared.json` hides handled alerts on the page only;
  the journal keeps them.
