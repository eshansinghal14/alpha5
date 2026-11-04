# ARBS — CURRENT STATE 2026-11-04 ~17:00 UTC (read this first)

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
- **Config-safe deploy** (the box's `kalshi_strategies/ARBS/config.json` holds
  dashboard edits — never let ARBS see repo defaults):
  ```
  F=kalshi_strategies/ARBS/config.json; sudo systemctl stop alpha5-arbs
  old=$(git rev-parse HEAD); cp $F /tmp/arbs_config_save.json; git checkout -q $F
  git pull -q --ff-only origin ec2-live
  .venv/bin/python data/live/merge_cfg.py $F $old /tmp/arbs_config_save.json   # if config.json changed upstream
  # else: cp /tmp/arbs_config_save.json $F
  sudo systemctl start alpha5-arbs; sudo systemctl restart alpha5-dashboard  # dashboard if a config key changed
  ```
  A removed config key the box had edited is re-added by merge_cfg: delete it
  by hand before starting (happened with `max_quoted_baskets`).
- **Scanner-only changes need just `git pull`**: the scan runs as a fresh child
  process every `scan.interval_s` (`python -m kalshi_strategies.ARBS.scanner`).
- Tests: `python -m pytest -q kalshi_strategies/ARBS` (136). `test_slow_drift_keeps_resting`
  is flaky (Windows' ~15 ms clock with chase_window_s 0); rerun before believing it. **Gotcha**: `pytest | tail -1 && git commit`
  commits even when tests fail — check the output.
- Analysis on the box: run scripts with `PYTHONPATH=/opt/alpha5 .venv/bin/python`.
  - `data/live/arbs_watch.py` — health, PnL vs last run / night start, new
    alerts, untracked orders; **disarms** on −$30 from its start or −$15 between
    runs. State in `/tmp/arbs_watch_state.json` (delete to reset the baseline).
  - `data/live/arbs_daily_live_report.py` — which series the daily/hourly live
    rule catches and how they've done (see Proposed improvements).
- Disarm by hand: `request_trading_mode("ARBS", False, source="...")`
  (`kalshi_infra.runtime.control`), the same request the dashboard button writes.
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
