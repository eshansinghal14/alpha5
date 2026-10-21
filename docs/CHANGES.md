# Change log

Reverse chronological. One section per material change. Entries before
2026-10-09 name paths that no longer exist (see the reorg entry).

## 2026-10-21 — Race: held_vol high side only, held_vol_edge, quote_markets (HYPE off)

Race real edge fills 10-21 00:31-02:48 UTC (10 events, 113 fills): the
held_vol pull blocked quoting 25% of armed time, all of it on the 0.5x
floor (held rows median realized/held 0.65x, never above 2x), while fills
on a held vol still ran -8.9c/lot at 10s (22 fills) and HYPE lost -$27 of
+$8.5.

- `pricing.held_vol_min_ratio` (dashboard, null = off): the low-side pull
  is now its own optional field; `held_vol_ratio` is the high side only.
  Set to null.
- `pricing.held_vol_edge` (dashboard): extra $ edge on a coin's market,
  takes and quotes, while that coin prices on a held vol (`Fair.edge_add`,
  added to the applied `constant_edge`). Set to 0.01. Held-vol fills
  10-19..10-21 were +0.84c/lot at 10s (+-0.93, 303 fills) against -0.05c on
  live implied, so this is a hedge for the bad stretch, not a fitted cost.
- `pricing.quote_markets` (dashboard checkboxes, null = all): markets left
  out rest no quotes (edge, LIP, LIP exit; `market_off` block) but are
  still priced. Set to BTC, ETH, SOL, XRP. HYPE edge fills: -$250 of the
  race's -$79 on 10-19 04:15..10-20 15:51, -$27 of +$8.5 on 10-21.

## 2026-10-20 — Dashboard performance data in its own process

The dashboard on EC2 sat at ~33% CPU and 2 GB RSS (2.5 GB peak) with the
browser reconnecting. Its perf refresh threads ran inside the Streamlit
process and held the GIL through cold journal passes: 64s on KXINXU and
over 11 minutes on KXCRYPTOLEAD15M (25k bbo + 25k decisions parts, 10
days), redone on every dashboard restart (18 restarts on 10-20).

- New `kalshi_viz/perf_worker.py` (unit `alpha5-perf`, nice 19,
  MemoryMax 1500M) refreshes every series every 30s, one thread each, and
  writes `data/live/<series>/perf/{fills,lip,prints}.parquet` + `meta.json`.
  The panel only reads those (`perf.read_result`); no refresh threads in
  Streamlit anymore (`ensure_background` / `IDLE_S` removed).
- `Perf` state is pickled to `perf/state.pkl` (first pass, then every 5
  min), so a restart only catches up. `perf.STATE_VERSION` invalidates it.
- Tunnel command gets `ServerAliveInterval=15` so idle drops don't
  silently kill the dashboard's websocket.

## 2026-10-21 — Race: implied_hold_s 600 -> 300

Race 10-21 00:35-06:00 UTC (hold 600, no HAR fallback, 5.4h priced, 266
real maker fills). Seconds classed by the oldest held coin's age:

| class | priced time | lots | mo1 / mo10 / mo60 c/lot | settled $/h |
|---|---|---|---|---|
| all implied | 55% | 1624 | -0.1 / +0.9 / +3.9 | +19.9 |
| held <= 300s | 36% | 1257 | -1.6 / -2.4 / +3.3 | +18.4 |
| held 300-600s | 8.9% | 119 | -5.2 / -8.0 / -17.2 (t -2.2..-2.5) | -70.1 |

The 300-600s band was 4% of lots and -$34 settled, so it goes. Only 9
fills; on Brier it priced no worse than Kalshi's mid (-0.0035, 13 events),
so the loss is in the fills, not a visibly wrong fair. Re-check after a
day at 300: time withheld (`no_implied_vol`) and the <=300s class.

## 2026-10-21 — Race: no HAR vol fallback; longer implied hold, guarded

Race 10-20 16:00-22:00 UTC (25 events, 336 real maker fills): seconds with
any coin on the HAR fallback were 34% of the time and -$71 of -$84 settled
(-$35/h vs -$10/h all-implied, +$10/h held); the only class where Kalshi's
mid beat our fair on Brier (+0.0052) and |fair - mid| 4.1c vs 2.7c. The
HAR rate ran 20-44% under the next live implied reading on every coin, and
switching to it cut vol to 0.70-0.90x. Fallback happens at each event's open
(15M mids ~0.5 carry no vol) and close (pinned), so the last reading is
usually 180-600s old: 87% of HAR seconds.

- `pricing.implied_fallback_realized` false: no implied or held vol ->
  fairs withheld (`no_implied_vol`), quotes pulled.
- `pricing.implied_hold_s` 180 -> 600. Holding 180-600s old was |ln| 0.23-0.28
  off the next reading vs HAR's 0.32-0.34. Not 900: only 3% more coverage,
  thinnest evidence, and 10-18/19 found long holds worse than HAR.
- New `pricing.held_vol_*` (dashboard): while a coin is on a held vol, its
  realized vol over 60s (10s returns, 1s bars) outside [0.5x, 2x] the held
  value pulls the race (`held_vol` quote block) for 30s. The 2x is the
  single-coin vol_burst calibration; 0.5x is uncalibrated. Decision rows log
  `x_held_vol_ratio`; summary has `held_vol`.

## 2026-10-20 — One write bucket per shard; HYPE priced on Binance's lead

- **Write budget.** Shard 2 was sliced per process: race 300/s (100%), the
  four single coins 60/s, BTC 30/s -- 390/s against Kalshi's 300, while total
  use averaged ~176/s (race 138, single coins ~27 + ~11). The race took 45
  429s on 10-20. Now every process pinned to a shard shares one bucket at the
  shard's full rate (`orders.SharedWriteBudget`: tokens + last refill in a
  16-byte file under `/dev/shm`, updated under `flock`; write groups use it
  too). `KALSHI_WRITE_SHARE` no longer applies to shard-pinned clients and is
  gone from the crypto units. New `quoting.max_write_tokens_per_s` (dashboard,
  live): optional per-series cap on creates/amends; cancels never wait on it.
  null everywhere.
- **HYPE on the perp nowcast.** New `pricing.perp_priced_markets` (race,
  dashboard checkboxes): those coins' x uses `perp_nowcast.increment` (spot +
  Binance's move since spot last moved, 2bp cap) instead of the spot mid, and
  their perp book moves wake the reprice. Start TWAP, CF basis and settlement
  stay on spot/CF. Race decisions 10-20 16:30-19:30 UTC: on HYPE a Binance
  increment over 1bp was followed by Coinbase the same way 73-75% of the time
  over 0.25-2s, covering 0.50-0.73 of it (slope 0.57-0.88); on SOL 28-56% /
  0.20-0.46, so SOL stays on spot. Coinbase HYPE prints ~9/min vs Binance
  ~900/min; receipt latency is not the issue (Coinbase HYPE p50 17ms, BTC 14ms).
  Set to ["HYPE"]. Decision rows log `x_priced_on` (latest | spot).
  First version woke the 5-coin reprice on every Binance HYPE touch change:
  live 19:51-20:00 UTC stale_signal 12.7% -> 25% of decisions, loop lag p95
  10 -> 55ms; set back to [] at 20:00 (stale 16.8%, p95 9.6ms). Now a perp
  move wakes it only once the increment has moved `live_feed.PERP_WAKE_BP`
  (0.5bp) since the last wake.

## 2026-10-20 — Cancels on their own lane; burst trips pull at once

Measured on the race, 10-20 15:15-18:15 UTC, orders journal (178k cancels):
decide -> send (`queued_s`) p50 0ms, p90 29ms; send -> ack on the loop p50
32ms, p90 64ms, of which the Kalshi round trip measured on the worker was
only p50 11ms. The extra time grew with writes in flight: 4ms with none, 14ms
with 1-2, 24-34ms with 3-12 (p90 31 -> 62ms). Cancels shared the loop's
default executor (8 threads) with creates, amends, REST polls and the feed's
parquet writes. Of 38 fills that came after we decided to cancel, 20 were
sent after the fill and the rest spent p50 34ms in that overhead.

- Cancels run on their own 16-thread pool (`engine.CANCEL_WORKERS`) and
  their own `requests.Session` with a 16-connection pool, warmed at client
  build (a cold connection is ~105ms vs ~34ms warm). Orphan and sweep cancels
  use it too. Bucket and priority rules are unchanged.
- `create_ack` / `cancel_ack` journal rows add `pickup_s` (submit -> worker
  start) and `done_wait_s` (worker done -> callback on the loop), so the next
  measurement can tell queueing from the round trip from loop lag.
- A pulling `spot_burst` trip now cancels from the trip callback
  (`SpotFeed._pull` -> `model.pull_if_blocked` -> `engine.pull_burst`), ahead
  of the reprice and quoter pass that used to send the cancels (race reprice
  p95 8ms + dispatch p95 4ms). Each model checks its own `quote_block` first,
  so single coins with `burst_own_coin_only` still pull only on their own coin.
  Reason in the journal and counts: `pull_burst`.
- Venue drain applies every queued frame's trips before any of its books.
- `auth.sign` accepts an Ed25519 key. On the box, RSA-PSS takes 1.32ms per
  sign and holds the GIL (a spinning main thread ran at half speed beside a
  signing thread); Ed25519 takes 0.044ms. Using it needs a new Ed25519 API
  key in Kalshi settings and `KALSHI_PRIVATE_KEY_PATH` pointed at it; nothing
  else changes.

## 2026-10-20 — Bybit dropped; dashboard refreshes only what is viewed

- Bybit is no longer run (`LEADING_VENUES`, `PERP_SOURCES`, `perp_nowcast.PERPS`):
  slowest perp to us (p50 113-145ms vs Binance ~75ms, OKX ~80-120ms), second
  heaviest feed. The nowcast checks Binance against OKX alone: more than
  CHECK_BP apart -> spot unchanged (one checker can't say which is wrong);
  Binance stale -> OKX. `perp_move` now needs Binance and OKX to agree (was 2
  of 3). 6 h replay 10-20 (SOL/BTC/HYPE): the check fired 73-122/h, ~0.2s
  each, and spot 1s later sided with the others 66-85% of the time.
- Dashboard: a series' perf refresh thread stops once its panel hasn't been
  rendered for 120s (`perf.IDLE_S`) and restarts on the next render; every
  series ever viewed used to refresh every 30s (8 threads, dashboard at
  70-110% CPU). Decisions parts' theo is read once per part and cached
  (re-reading was ~31% of the dashboard's CPU).

## 2026-10-20 — Feed load cuts; all five single coins in one process

- Perp book updates whose touch prices are unchanged are dropped at the
  adapter, except one per 0.25 s per coin (`venues.PERP_SAME_TOUCH_S`). Binance
  bookTicker is ~711 msg/s on BTC for ~6 mid changes/s; the perp books only
  feed the nowcast mid and the recorder. Probe 10-20 16:19 UTC (bare listener
  beside the live venue child, same trade ids): Binance prints held in our
  process p50 6ms / p90 178 / p99 685ms over the network's 73 / 99 / 154ms,
  rising with burst size (busiest quarter p90 396ms).
- Hyperliquid is no longer run (`LEADING_VENUES`): prints reached us p50
  ~400ms after trade time vs Binance ~80ms, and it only pulled HYPE.
- A venue child under `CRYPTO15M_PERSIST=0` no longer records books, venue
  prints or perp prints (they were written twice, once per trading process).
- `alpha5-crypto15m` runs KXETH15M, KXSOL15M, KXXRP15M and KXHYPE15M in one
  process (one SpotFeed, one venue child, one 20% write bucket), in place of
  `alpha5-crypto15m-sol`; `alpha5-crypto15m-btc` runs KXBTC15M alone (10%).
  All five in one process ran at 71% CPU with loop lag p95 75ms in shadow
  (10-20 16:45 UTC): BTC's Kalshi book changes touch ~300/s vs 20-77/s for
  each of the others, each running the full dispatch.
- The multi-second Coinbase print holds seen in the probe were the race
  process's Advanced Trade / Exchange sockets reconnecting (recv timeout
  after a ConnectionClosed, 1 s retry): 1-2 per 15 min event 07:15-10:00 UTC,
  none since; a 4 min re-probe at 16:33 had p99 85ms, max 140ms.

## 2026-10-20 — Coinbase book back on Advanced Trade

- `coinbase.EX_BOOK = False`. Live 05:29-05:45 UTC on the Exchange
  `level2_batch` book, the race's signal age went p50 10 -> 24ms, p99 55 ->
  98ms, and decisions over the 30ms limit 14% -> 40% (over 60ms 0.7% -> 6%).
  Its ~50ms batches leave the book up to a batch old between messages, and the
  race's signal age is the oldest coin's newest book. Exchange `matches` still
  carry prints. The Exchange-book code stays for a later try (e.g. with the
  batch age not counted against the limit).
- Same 30 min recording (05:15-05:45 UTC), each candidate vs the real CF index
  after the model's 30s basis, rms bp: BTC Coinbase alone 0.47, median of
  6 venues 0.33; ETH 0.73 vs 0.53; XRP 0.66 vs 0.43. The other venues' median
  alone (0.39 / 0.56 / 0.47) also beat Coinbase alone, and "Coinbase unless
  > X bp from the others" was no better than Coinbase alone at any X. The
  multi-venue median stays for BTC/ETH/XRP.

## 2026-10-20 — Coinbase book from the Exchange feed

- `CoinbaseAdapter` builds its book views from the Exchange feed's
  `level2_batch` (top 50 levels) while it is synced, keeping Advanced Trade
  `level2` running as the fallback (used again the moment the Exchange
  connection drops). It also subscribes `ticker`: the touch after each match,
  held like a through-the-touch print until the book reaches it.
  `coinbase.EX_BOOK = False` reverts the book to Advanced Trade. Same-window
  test 10-20 02:27 UTC, touch-change delay p50/p90: BTC 45/186ms on Advanced
  Trade vs 15/41ms, SOL 24/54 vs 24/52, HYPE 31/54 vs 29/56. Smoke test on the
  box, 90 s: every view from the Exchange book, none crossed or empty, mid
  matching Advanced Trade's to 0.00bp p50 / 0.3-0.6bp p99.

## 2026-10-20 — KXSOL15M prices on the latest-mover nowcast

- New `pricing.perp_mode` (CRYPTO15M, live-editable): with `perp_pricing` on,
  `"latest"` prices on spot + Binance's move since spot last moved
  (`PerpNowcast.increment`), `"gap"` on the previous Binance-vs-spot gap;
  spot when the chosen one has no reading. `"latest"` for every coin (only
  KXSOL15M has `perp_pricing` on). `x_priced_on` is now spot / perp / latest.
  KXSOL15M 10-20 03:00-04:12 UTC, 7,837 informed takers (Kalshi mid >= 1c
  their way in 5s), our receipt clock: half-move latest -10ms, gap +20ms,
  spot -5ms; at the print latest had 0.20c of the move done, gap 0.01c (it sat
  0.08-0.1c against the taker for the second before, pulling back spot-led
  moves). Paired on 2,555 takers: latest earlier than gap on 40%, later on 9%.

## 2026-10-20 — Latest-mover nowcast (shadow); cap stays 2bp

- `PerpNowcast.increment`: spot plus Binance's move since spot last moved,
  capped at `CAP_BP`; it resets on every spot move, so a spot-led move is
  never pulled back while Binance hasn't updated (nowcast() pulls it back by
  the gap). Shadow only: race decision rows `x_perp_inc_bp`, KXSOL15M rows
  `x_inc_bp` / `x_inc_prob` (fair priced the same way as `x_perp_prob`).
- Cap left at 2bp. SOL 10-20 00:25-02:50 UTC, 10ms grid, Binance trade input:
  |gap| > 2bp 22% of the time, but spot closed ~0% of gaps over 1bp within 1s
  (basis drift, not lead), and every cap above 0 predicted spot 200ms ahead
  worse (rms 0.51bp at 0 -> 1.28 at 2bp -> 2.04 at 5bp). Informed-taker
  half-move -20ms at 2bp vs -30 at 5bp. On the bookTicker input (3 min) the
  gap never exceeded 3bp, so the cap doesn't bind there at all.

## 2026-10-20 — Coinbase prints from the Exchange feed

- `CoinbaseAdapter` opens a second socket to Coinbase Exchange's public feed
  (`wss://ws-feed.exchange.coinbase.com`, `matches`): every print reaches
  `on_print` (bursts, PrintLog) and the held through-the-touch print from
  there. Advanced Trade `market_trades` are ignored while it is connected and
  carry prints again if it drops; trade ids are de-duplicated across both.
  The book stays on Advanced Trade `level2`. Same trade ids side by side,
  us-east-2 10-20 02:27 UTC, 5 min: Coinbase trade time -> receipt p50
  SOL 69 -> 10ms, BTC 70 -> 12, HYPE 56 -> 9 (each trade ~55ms sooner, p10
  17 / p90 98). Exchange `level2_batch` was no faster than Advanced Trade's
  level2 (SOL p50 24 vs 24ms) and full `level2` needs auth, so it isn't used.
  Smoke test on the box, new adapter, 90s: 677 prints, 0 duplicates, print
  delay p50 SOL 13ms / BTC 15ms.

## 2026-10-20 — Perp nowcast on Binance alone

- `perp_nowcast` uses Binance's book mid (net of its 30s basis) instead of
  the median of Binance/OKX/Bybit, which moved only when the second venue did.
  OKX/Bybit only veto: Binance is dropped for the median of all fresh venues
  when it is over `CHECK_BP` (3bp) from every other one; without a fresh
  Binance the median of the others needs 2, else spot. 9,523 informed
  KXSOL15M takers 10-20 00:25-02:15 UTC (Kalshi mid >= 1c their way in 5s,
  our receipt clock): half of each venue's move at Binance -10ms, OKX +15ms,
  Bybit +45ms, Coinbase spot -2.5ms; Binance 0.29bp done at the print vs
  0.16-0.18 for the others; first over Coinbase on 72% of paired moves.

## 2026-10-20 — Venue books sent at once; HYPE priced on the depth-weighted replica

- Venue process: a book whose touch (best bid/ask or resync state) changed is
  written on its own frame immediately and wakes the parent's drain, instead
  of waiting for the 50 ms `SHIP_S` batch and then the 50 ms `VENUE_DRAIN_S`
  tick (0-100 ms, ~50 ms mean). Size-only changes, prints and keep-alives
  still ride the batch (`venue_process.IMMEDIATE`). Perp book moves now also
  wake models that price on the perp nowcast (KXSOL15M); the race only logs it.
- `series.DEPTH_PRICED = {"HYPE"}`: HYPE's pricing mid, per message and in the
  1s bars, is the CF replica (`cf_rate.compute_cf_spot`) of its 25-level
  Coinbase view instead of the touch mid, so basis and vol are fitted on the
  same series. A held through-the-touch print still wins while it holds.
  Scored against the real CF index after the model's 30s basis, 10-18 10:14 ->
  10-20 00:34 UTC: 0.67bp rms vs 0.98bp for the touch mid, better in 8/8
  six-hour blocks. ~0.23 ms per HYPE message on the c7g box.

## 2026-10-20 — Perp nowcast on the perps' book mid

- `perp_nowcast` is fed each perp's top-of-book mid (Binance `bookTicker`,
  Bybit `orderbook.1`, OKX `bbo-tbt`) from `SpotFeed._on_book`, instead of
  the last trade of each 50 ms ship. Resyncing or crossed books are skipped;
  perps still never enter the spot median. The venue process no longer ships
  `perp_px`. Binance, 3 min us-east-2 recording 10-20 00:58 UTC: bookTicker
  and trades both arrive p50 ~72ms after Binance's transaction time; on a 50 ms
  grid the last trade reversed its previous step 4-6% of the time (bid/ask
  bounce; HYPE 4.7%) and the book mid 0% (HYPE 1.1%), 1 s moves the same.
  Affects the race's shadow `x_perp_*` and KXSOL15M's `perp_pricing`.

## 2026-10-20 — Databento removed; IB is the only option source

- Deleted `kalshi_infra/databento_functions.py` and everything that only
  existed to replay its SPXW history: the S&P backtest
  (`KXINX/backtest/`, `grid_pricing.py`, `signal_grid.py`,
  `ib_emulation.py`), `research/run_ib_sweep.py`,
  `research/spxw_kxinx_lag.ipynb`, `tests/parity/backtest_spx_parity.py`,
  and the unused `kalshi_infra/spx_cash.py`. `databento` dropped from
  `deploy/requirements-live.txt`.
- The Kalshi-side helpers the live S&P model used from it moved, unchanged,
  to `ib_functions`: `event_markets`, `bracket_listed_strikes`,
  `select_event_buckets`, `IB_MARKET_DATA_LINES` (same results on
  KXINXU-26OCT20H1600). Live S&P pricing is unchanged.

## 2026-10-20 — KXSOL15M prices on the perp nowcast

- New `pricing.perp_pricing` (CRYPTO15M, per coin, live-editable): the fair
  is priced with x moved by the perp nowcast's gap (and the not-yet-printed
  end seconds with it); plain spot when fewer than 2 perps are fresh. Implied
  vol and the market shift stay on the spot input. On for KXSOL15M only, as a
  live test of whether a faster theo makes single-coin quoting pay; off for
  the others. Decision rows carry `x_spot_prob`, `x_perp_prob`, `x_priced_on`.
  SOL 10-20 00:16-00:21 UTC: a perp reading on 97% of decisions (3 fresh
  venues 83%), |gap| p50 0.6bp / p90 1.4bp, |perp fair - spot fair| p50
  0.6c / p90 1.9c.

## 2026-10-19 — Venue reader fix; perp nowcast (shadow)

- `venue_process._read_frame` read a frame with one unbuffered `read(n)`,
  which returns one pipe read's worth (4096 bytes on Linux). A big frame (a
  24k prints/s Binance sweep at 07:32 UTC) came back short, was taken as EOF,
  and the reader thread exited while the child stayed alive. Every
  non-Coinbase venue then left the median for 11h44m (07:34-19:18 UTC, until
  the next restart): BTC/ETH 6 -> 1 venues, XRP 4 -> 1; perp pulls and perp
  prints stopped too. Reads now loop until the frame is complete, and a live
  child whose reader has ended is killed and restarted.
- `feed_problem` now reports a coin whose spot median has under half its
  configured venues for over 60s ("spot median thin: BTC 1/6 ..."). Nothing
  flagged the 10-19 outage.
- `perp_nowcast.py` (shadow only): the spot mid moved to where the perps
  (Binance/Bybit/OKX median, each net of its trailing 30s basis to our spot)
  already are, capped at 2bp, spot-only under 2 fresh venues. Logged on
  decision rows as `x_perp_gap_bp`, `x_perp_n` (race, per coin) and
  `x_perp_prob` / `x_perp_mid` (SOL). Never priced on.

## 2026-10-19 — KXSOL15M in its own process; other single coins off

- `alpha5-crypto15m-sol` runs KXSOL15M alone at normal priority (resumes an
  arm like the race). `alpha5-crypto15m-shadow` (all five at below_normal) is
  removed: armed inside it, SOL's loop lag reached p95 1.1s / max 2.1s and
  30% of its decisions were stale_signal (16:00 UTC, load 7.6 on 4 cores);
  22 real lots, 30s markout -1.33c, 61% picked off.
- Same shard as the race, which can't see its writes: capped at
  `KALSHI_WRITE_SHARE` 0.2 (60 tokens/s).

## 2026-10-19 — Depth journal; single-coin 15M back in shadow

- New journal stream `depth`: resting size at the best 5 prices of each side
  (yes bids and no bids, own-side prices), one row per level whose size
  changed (size 0 = emptied). `bbo` only had the touch, so a quote resting
  behind it couldn't be placed in its queue. Replaying 657 real ETH orders
  through the paper fill rules filled 47 of 626 real lots: the model puts us
  behind everything displayed at our price, but the taker that hit us took
  a median 41% of it. This data is what a tested queue model needs.
- `alpha5-crypto15m-shadow` is back, all five coins, shadow only, never
  armed: research on a profitable quoting setup. Same box isolation as before
  (own process, below_normal, read-only on the spot files).
- `CRYPTO15M/research/shadow_tracker.py`, hourly on EC2
  (`alpha5-crypto15m-track.timer`, last 6h, `data/live/crypto15m_track/`):
  per coin, the public tape from the resting side by edge vs our theo, and
  our paper fills by edge and time to close. The tape view doesn't depend on
  the paper fill model.

## 2026-10-19 — Single-coin 15M crypto no longer run

- `alpha5-crypto` runs the Coin Race alone (`--only KXETH15M` dropped), and
  `alpha5-crypto15m-shadow` (BTC/SOL/XRP/HYPE paper) is removed. KXETH15M
  real, 10-18 21:18 -> 10-19 02:18 UTC: 562 maker lots, 30s markout
  -0.62c/lot, 43% picked off, settled -$7.23 over 13 events.
- The race keeps pricing on vols implied by those markets: `implied_rates.py`
  polls their Kalshi mids itself, so no CRYPTO15M process is needed.

## 2026-10-19 — Coin Race: market-implied shift restored

- `pricing.market_shift_alpha` 0.25 (with `_half_life_s` 60, `_fit_s` 10,
  `_max_bp` 5) and the model's Kalshi-book input are back; the fit now uses
  the bump deltas at the vols being priced (implied or HAR). Removed
  10-18 20:14 UTC on a settlement-accuracy replay that scored it a wash, but
  quoting did worse without it: 10-18 20:14 -> 10-19 02:32 UTC the share of
  event time with some market >=3c outside Kalshi's touch went 17% -> 29%, and
  edge fills taken then went +1.0c/lot (3-10c out) -> -0.5c at 10s. To be
  re-judged once implied vol (10-19 02:32) has a few hours: if the gap stays
  under ~15% of time without it, it can go again.

## 2026-10-19 — Dashboard: one page per series, grouped in the sidebar

- Replaces the KXINX+ / KXCRYPTO+ group pages (combined performance panel,
  series picker, right-hand nav). Each series is its own page again; the
  sidebar lists them under a heading per folder's `DASHBOARD_GROUP`
  (Crypto: the Coin Race, then the single-coin 15M series by
  `DASHBOARD_RANK`; S&P: KXINXU, KXINXDUD), one button each. Folders
  without a group go under Other.

## 2026-10-18 — Quoting: one level per side, amend-down, full write budget

- `quote_levels_behind` no longer adds ladder levels. Each side quotes ONE
  level; the setting is how many ticks behind the best price an earlier
  order may be left resting instead of being moved up (no write; it's back
  at the top if the price steps back). Configs at 1 (CRYPTO15M) now quote
  one level, not two.
- A quote pushed inside the edge is amended down to the new best price (10
  tokens, side stays quoted) instead of cancel (2) + create (10). It still
  cancels when the order isn't acked yet, the bucket can't cover the amend
  plus queued cancels, or we already rest at that price. KXINXU 10-16:
  201k `cancel:inside_edge` against 267k creates.
- `WRITE_HEADROOM` 0.8 -> 1.0: 300 tokens/s on the Advanced tier (900-token
  Kalshi bucket vs our one-second bucket); no 429 in any EC2 log 10-14..18.

## 2026-10-18 — S&P: KXINX range buckets no longer traded; one KXINX+ page

- `kalshi_strategies/KXINX` registers only KXINXU and KXINXDUD; the KXINX
  section of config.json is gone. It never ran armed: 10-14..10-16 shadow
  was 212 of 223 fills taker at -1.3c/contract 10s markout. Bucket pricing
  (`bucket_fairs`, grid/backtest) is kept for research.
- Dashboard (superseded 2026-10-19): the folder's series share one sidebar page, "KXINX+"
  (`DASHBOARD_NAME`). The performance panel pools both series' real fills
  (with a per-series split under "Markout by fill type"). Each series'
  status and theo/NBBO charts sit under its own header. A ticker slider in
  the sidebar picks which section the config form edits.

## 2026-10-18 — Crypto: market-implied shift removed

- The fair no longer leans toward the Kalshi mids (`market_shift_alpha`,
  `_half_life_s`, `_fit_s`, `_max_bp` and the model's Kalshi-book input are
  gone). Replay of the CF-index model, alpha 0 vs the live 0.25, every 5s of
  568 events 10-12 17:00..10-18 17:00 UTC: winner log loss 0.9707 vs 0.9707
  (diff -0.0000, 90% CI +-0.0022). The shift helped 7-15 min out (+0.003) and
  hurt 1-3 min (-0.004) and the last minute (-0.008), where it is frozen at a
  stale value; halves disagree in sign. It moved the fair 0.46c on average.

## 2026-10-18 — Order lifecycle journal (`orders` stream)

- New journal stream `data/live/{series}/{date}/journal/orders/`: one row per
  real create / amend / cancel sent and answered, with the round trip timed
  on the worker thread (`rtt_s`), `queued_s` on a cancel (decided -> sent),
  and every real fill with `state` and, if we had already decided to cancel
  that order, `cancel_decided_t` + `cancel_reason` (a fill that raced our
  cancel). Before this, cancel timing wasn't recorded anywhere: of 78
  pick-offs 10-18 09:00-17:40 UTC, 6 lost the race to a cancel, but how
  much of that is our send vs Kalshi's answer couldn't be told apart.
- Not changed, from the same review: holding a step toward the touch. Over
  1,974 edge fills (10-16 19:05 -> 10-18 17:40 UTC) fills within 0.25s of a
  book-driven step-up were picked off 30% of the time vs 20% after none,
  but still made +$6.34 at 10s; holding them 0.1-2s removed fills worth
  -$2 to +$22. Fair-driven step-ups within 1s: -$9.30 on 357 fills, about
  half of it HYPE, and not consistent per 0.25s bucket.

## 2026-10-18 — Crypto prices and settles on the CF index

- New `cf_index.py`: the CF Benchmarks settlement index for all five coins
  from Kalshi's relay (`cfbenchmarks_value` websocket, one whole-second tick
  per index; REST `/cfbenchmarks/values` fills gaps and seeds a restart).
  Persisted to `data/kalshi_crypto/cf_index/{coin}_{day}_{HH}.parquet`.
- Start and end = mean of the index's seconds over [S-60, S) and [T-60, T).
  On 10-12..18 that matched Kalshi's published expiration_value to <=0.02bp
  (XRP's is rounded to 4 dp); the race paid the exact-average winner where
  the rounded values disagreed (26OCT130400, 26OCT161015). Kalshi's WS
  quarter-hour average uses (S-60, S] and is 0.003-0.03bp off; not used.
- Pricing: x = ln(mid / CF start) - basis, basis = mean ln(mid / CF) over the
  last 30 CF seconds; last-minute I from CF seconds (an elapsed second whose
  tick hasn't landed counts at x). Replay 10-12 17:00..10-18 05:00 (516
  events): last-minute winner log loss 0.238 -> 0.115 on the held-out half,
  in-window unchanged (+0.0002, CI +-0.006). Shadow beside live 10-18
  08:39-10:15 UTC (6 settled events): last minute 0.284 -> 0.121, all
  0.987 -> 0.955; stale_signal share 7.1% vs live 7.9%; every start/end within
  0.02bp of expiration_value (XRP 0.33, its 4-dp rounding), 6/6 winners.
- CF ticks older than 2.5s count in full toward signal age, so a dead CF feed
  is `stale_signal` (every quote pulled). No start yet: fair withheld
  (`no_cf_start`) and a REST fill requested.
- Removed: the mid start TWAP and its backfill, the Kalshi-start anchor
  (`kalshi_start.py`, `kalshi_start_offset_events`), `basis_tie` and
  `basis_tie_gap`, the independent basis noise (`BASIS_SD_BP`,
  `APPLY_BASIS_SD`), `quote_before_start`.

## 2026-10-18 — Edge top quote holds its place when others join it

- Real mode: if our best edge quote was alone at its price on an earlier
  decision and others have since joined it there, the top stays at that
  price instead of stepping a tick over them (we're first in that queue, so
  the step only paid 1c/lot more). Someone bidding above us is still
  overtaken as before. 10-17 00:00 to 10-18 06:00 UTC: 5.7% of the ~9,800
  book-driven step-ups/h were this case; edge fills alone at the top ran
  +2.1 to +2.7c/lot at 10s vs +0.25c when sharing the top price.

## 2026-10-18 — spot_burst: leading-venue trips sent at once; perp price-move trigger

- Volume trips on Hyperliquid and the perps are detected in the venue process
  on every print (`prints.LeadTrigger`) and sent on their own frame, which
  wakes the trading loop's drain. They used to wait for the 50ms batch and
  the 50ms drain tick. The trading process still owns the thresholds
  (calibrators) and sends them down as control frames on stdin, and still
  decides whether a trip pulls (`burst_pull_coins`). Coinbase is unchanged
  (in-process). Replay 10-16 19:05 to 10-18 06:35 UTC, 50ms grid, perp trips
  seen 100ms late vs at once: same 10.5% pulled, maker $ on unpulled public
  prints +$918 -> +$2,560, big-move coverage 68% -> 80%, pick-offs within
  0.5s of a trip avoided 12 -> 37 of 159. Local smoke test: child receipt ->
  applied in parent p50 9ms.
- Perp price-move trigger, source `perp_move`: 2 of Binance/Bybit/OKX with a
  last-trade move over 0.5s at or over that venue's own p99.5 (2h rolling,
  60s refresh, seeded from perp_prints/, calibrated in the venue process),
  same direction. Same pull/hold as a volume trip. Same replay on top of the
  above: pulled 10.5% -> 11.6%, maker $ on unpulled prints +$2,560 ->
  +$3,222, the extra pulled time ran -6.5c/lot to makers, big moves covered
  80% -> 88%. Every window 0.4-0.75s x p99-p99.7 tried was positive; one
  venue alone fired on the 10-17 22:15:37 all-venue glitch on every coin,
  2-of-3 on two.
- Trip log rows carry `kind` (volume / move) and `level` (USD or bp);
  `seen_delay_s` is now receipt -> applied on the trading loop.
- `ALPHA5_DATA_ROOT` moves `data/` (for a shadow run beside live).

## 2026-10-18 — spot_burst trip log; Hyperliquid pulls on HYPE only

- Every (source, coin) crossing of its burst threshold is a row under
  `data/kalshi_crypto/burst_trips/{day}/` (onsets only): trailing-1s $,
  threshold, ratio, threshold source (live/saved/fallback), whether it
  pulled, exchange and receipt time, and `seen_delay_s` (receipt to the
  handler: the venue process's batching for non-Coinbase sources).
  Decision rows carry `quote_block` (`spot_burst` while pulled); it was
  invisible before (priced rows with 0 levels).
- `pricing.burst_pull_coins`: per source, the coins whose trips pull quotes;
  the rest are logged only. Crypto: Hyperliquid on HYPE only, every other
  source on all five. Replay 10-16 19:05 to 10-18 06:35 UTC (live rule, 0.1s
  grid, 16,446 public Kalshi prints, 1,570 edge fills): time pulled only by
  a Hyperliquid trip (2.8%) had flow favouring makers, +1.96c/lot at 10s
  (BTC +9.6, XRP +4.8, ETH +2.5, SOL -0.6, HYPE -1.1). HL prints reach us
  ~0.38s after exchange time vs ~0.08s for Binance. Maker $ on unpulled
  prints +$2,627 -> +$2,895, pulled 12.8% -> ~10.5%, big-move coverage
  83% -> 81.5%; better in all four quarters. Requiring 2+ keys or 2-4x the
  threshold pulled less (3-6%) but every variant exposed worse flow
  (+$1,880 to +$2,040), so those were not taken.

## 2026-10-18 — LIP-only signal-age limit

- `lip.lip_max_signal_age_s` (dashboard; null = off): every LIP order, exit
  included, is pulled while the fair's signal age is over it and returns on
  the next decision under it; edge quotes keep `risk.max_signal_age_s`.
  Crypto 0.03. Replayed on decision rows (age, 10-17 00:00 to 10-18 02:50
  UTC, LIP quote up) and every LIP fill since 10-15 17:00, a fill counted as
  avoided if the gate was already off 30ms before it:
  10ms: 63% of LIP time off. 20ms: 32% off, avoids 7.1k lots at -2.7c/lot
  10s. 30ms: 15% off (15% of accrual), avoids 3.9k lots at -3.1c (-$120 of
  -$361); kept fills -1.6c. ~3,800 off-spells per LIP side-hour at 30ms, a
  cancel + create each: ~13 write tokens/s per resting LIP side. Holding LIP
  off for 0.1-1s after the age recovers took more time without picking out
  worse fills (avoided markout drifts back to the -1.9c average), so there
  is no hold. On ETH/BTC alone the avoided fills were about flat: the gate
  mainly protects SOL/XRP/HYPE.

## 2026-10-17 — A LIP fill clears that side's LIP target

- `OrderManager.on_fill`: a LIP fill drops the side's published target (the
  order's remainder keeps resting) until the quoter re-sizes it against the
  new position. The pump used to re-create a fully filled LIP order at the
  old size, or amend a partial back up to filled + target, in the gap.
  26OCT170115 SOL (05:02:26 UTC): a 97-lot no bid @0.54 filled, a second 97
  went up 76ms later and filled 70; 0 -> -170 in 0.1s and the position halt
  tripped. Fills arrive during stale_signal pulls, when the quoter isn't
  publishing, so the stale target could sit there for the whole pull.

## 2026-10-17 — LIP exit stops at the LIP time cutoff

- The LIP exit no longer bypasses `lip_min_time_left_s`: inside it no LIP
  order rests and LIP inventory is held. 26OCT162045 (00:43 UTC): the exit
  bid HYPE 0.48 x77 with 83s left, 9c over the edge ladder's threshold; it
  filled and fair fell to 0.09 within 17s (-48c/lot at 60s).

## 2026-10-17 (night) — pick which markets quote LIP

- `lip.lip_markets` (dashboard: one checkbox per market): only the ticked
  markets quote LIP; all ticked is saved as null (= every market). A
  market left out quotes no LIP, holds no `lip_max_markets` slot, but still
  offers back LIP inventory it already holds. Config lists of strings are now
  type-checked.
- 10-17 by coin (01:30-21:50 UTC): XRP earned $24.13 of the $44.59 payable
  (19 of 35 markets that reached $1) and its LIP fills lost $102.81 at 10s.

## 2026-10-17 (afternoon) — separate price cap for the LIP offer

- `lip.lip_offer_max_price` (dashboard): no LIP offer (the no bid), exit
  included, at a yes offer at or above it; the bid keeps `lip_max_price`. It
  can only tighten, never loosen, the offer's cap. Crypto: 0.20 (bid 0.35).
  10-17 05:15-13:30 UTC, splitting accrual by which side was resting: the
  offer at a yes offer <= 0.20 accrued $15 for +$10.65 net of 10s markout;
  above 0.20, $21 for -$123, on 88% of its fill lots. Reward per minute was
  flat ($0.04-0.08) at every price on both sides.

## 2026-10-17 — LIP steps back behind the best bid; `lip_max_price`; loss-limit marking

- A LIP quote rests at the lowest price within 3 ticks of others' best bid
  that keeps 98% of the reward rate it would earn at the best bid
  (`quoter._lip_step_back`; prefers the price already resting). Kalshi scores
  every bid at or above the reference price in full, so behind a thin best
  level a sweep must clear others first for the same reward. Live books
  10-17 00:30 UTC: one tick back kept >=95% of the reward on 55% of sides,
  two ticks on 21%. Exits never step back.
- `lip.lip_max_price` (dashboard; was the hardcoded 0.50 `LIP_TAPER_END`, now
  its upper bound): no LIP quote or exit at a yes price at or above it.
- Loss-limit PnL: matched yes/no pairs are valued at $1 (they pay exactly
  that), and a book side that has emptied marks at its last price, not $0.
  26OCT162015 read -$172.81 just after the 60s cutoff, as the books thinned,
  and tripped the -$120 limit; it settled -$13.75.

## 2026-10-16 (night) — constant-$ LIP sizing; exit = inventory; one LIP order per side; burst-threshold check

- LIP size above `lip_cheap_max_price` keeps the cutoff's $ worst case
  instead of tapering linearly: the bid is `lip_bid_lots x cutoff / y`, the
  offer `lip_offer_lots x (1 - cutoff) / (1 - y)`. The offer's per-lot loss is
  1 - y, so it now grows toward 0.50 rather than shrinking where it is
  cheapest to hold. Hard 0 at a yes price of 0.50 unchanged. `max_lots`
  (the position guard) is the offer's peak just under 0.50.
- The LIP exit is the inventory alone. Inventory plus band size flipped the
  position when the front-of-book exit was swept (ETH 10-16 14:23 UTC
  -70 -> +18; HYPE 07:32 -53 -> +101).
- No new LIP create on a side while that side's previous LIP order is still
  cancelling. ETH 14:24:07 UTC: a stale-cancel and a create raced, two no
  bids rested (41.65 @0.60, 38.65 @0.59) and both filled.
- Saved / fallback burst thresholds crossed by more than 5% of the last 5 min
  of samples are ignored (source record-only for that coin until its live
  quantile is ready). After the 18:34 UTC restart saved values ~1,000x too low
  (HL ETH $91/s) held every quote pulled for 12 minutes.
- Crypto params from bid+offer trading 13:18-18:48 UTC (24 LIP fills, 762
  lots): the offer leg lost $107 settled vs the bid's +$31 at similar 10s
  markout (-3.5c both); its $ risk per lot is ~4x the bid's. Markets reaching
  the $1 payout: 20% at offer 100-150 (avg 74 offer lots resting, $1.40
  payable/event over 7 events) vs 8% at offer 30 on the linear taper (avg 24
  lots, $0.50/event over 13 events). `lip_offer_lots` 60: under constant $ it
  rests ~65-80 lots at yes 0.15-0.30 (the 20% regime's sizes) for $54 worst
  case per market, 108 just under 0.50. `lip_bid_lots` 150 ($15),
  `lip_cheap_max_price` 0.10 kept.

## 2026-10-16 (evening, later) — LIP gate is the yes price; bid and offer on longshots

- The 0.50 gate, the band and the taper are now by each order's price in yes
  terms (the yes bid; 1 − the no bid for the offer), not its own-side price.
  So a longshot market (yes under 0.50) quotes both: the yes bid
  (`lip_bid_lots`) and the yes offer, a no bid near 0.80-0.90
  (`lip_offer_lots`). A favourite market quotes neither. The previous version
  gated the no bid on its own price, which put the offer on favourites only.
- The LIP exit follows the same gate: cheap-yes inventory is offered back as a
  no bid at a yes offer under 0.50 again.

## 2026-10-16 (evening) — per-side LIP sizes; nothing at or above 0.50

- `lip_cheap_lots` -> `lip_bid_lots` (yes bid) and `lip_offer_lots` (yes
  offer = no bid), each on a cheap own-side price only, same band and taper.
- Hard 0.50 cap on every LIP order. The LIP exit used to rest at any price:
  after a cheap yes fill it offered the inventory back as a ~0.89 no bid.
  Now it only rests under 0.50 (`lip_exit_over_half` when it can't), and the
  quote's position-netting can't add size back above 0.50 either.

## 2026-10-16 (late) — LIP cheap side only, tapered past the cutoff

- Expensive-side LIP removed: `lip_rich_min_price` and `lip_rich_lots` are
  gone (a config still carrying them is rejected as unknown keys). 10-16
  12:10 UTC: a 192-lot HYPE no bid at 0.81 was swept for 166 lots while the
  fair had moved under it (no fair 0.17-0.19, capped price 0.16 vs our 0.19
  for the 1.8s before the sweep), leaving us short 161 yes. Public prints
  10-15 17:00 to 10-16 12:00 UTC: makers at 0.80-1.00 ran -1.8 to +0.3c/lot
  at 10s. The LIP exit still offers inventory back on an expensive side,
  sized to the inventory alone.
- Cheap-side size tapers instead of stopping: `lip_cheap_lots` up to
  `lip_cheap_max_price`, linear to 0 at 0.50 (`params.LIP_TAPER_END`).
  Same prints: makers at 0.20-0.45 made +0.1 to +3.4c/lot at 10s, no worse
  than <=0.20; 0.45-0.55 was the first adverse bucket (-1.3c). A side
  crossing 0.20 is amended smaller rather than cancelled.

## 2026-10-16 (later) — one $1 LIP threshold; drop sides that add no payable cent

Armed 10-16 09:10-10:15 UTC, LIP was up 5.6% of its window and 72% of the
downtime was the payout floor; no market reached $1.

- `LIP_START_PAYOUT_USD` (x1.2 to start a quote) removed: one threshold,
  the $1 shortfall, to start and to keep. Every burst pull now cancels LIP,
  so the start margin was re-applied every ~11s.
- A LIP side (and an exit's band size) is dropped when it can't change the
  payout: once the market reaches $1 without it, it must add at least a cent
  (`LIP_PAYOUT_TICK_USD`; payouts round down to the cent) over the rest of
  the window. A side the market needs to reach $1 is kept and the floor
  judges the whole market. Cuts the cheap side behind 10k-125k-lot walls
  (SOL @0.01 at a 0.1% share) where the other side already pays.

## 2026-10-16 — spot_burst pulls LIP at once; LIP judged on markout

- `spot_burst` is now a hard pull for the LIP quote and the LIP exit, like
  the edge ladder: no `LIP_PULL_DEBOUNCE_S` hold. Over 10-15 17:00 to 10-16
  07:45 UTC, ~9 of 48 big LIP sweeps landed inside the 2s hold. The hold
  still covers the other soft pulls (payout floor, max-markets slot, band,
  fair cap).
- `pricing.burst_hold_s` 1.5 -> 0.5 (crypto). Replayed over every public
  Kalshi print 10-15 17:00 to 10-16 08:45 UTC: prints while the trailing-1s
  burst trigger is still over ran +1.1c/lot to the taker at 10s; every bucket
  from 0.25s to 5s after it ran against the taker. Time pulled 19.5% -> 12.6%
  in the replay. State-based release (stay out while any coin is in its top
  2-5% of 1s notional or spot is moving) did no better than fixed 0.5s.
- Dashboard LIP column leads with net = payable reward + 10s markout $ (60s
  and settled below). Settlement PnL is too noisy to judge LIP on. Every
  column's markout tile shows 1s, 10s and 60s.

## 2026-10-15 — LIP scorer aligned with the CFTC filing

Source: Appendix A of the 2026-07-15 LIP amendment (CFTC filing).

- `lip._score_side`: every bid at each level through the one that crosses
  Target scores in full. The back-of-queue rule (ours scored 0 when the size
  ahead already met Target, added 2026-10-09) is gone. The filing adds "all
  bids at the current bid price" with no time priority.
- A side whose best bid is at the highest price has no qualifying bids.
- `period_payout`: under $1.00 pays $0 (was rounded up to $1). The cent
  floor no longer loses a cent to float error.
- Per-account cap: `reward_rate` is uncapped. The quoter's projection caps
  accrued + future at `max_reward_usd` (`lip.remaining_cap_usd`) instead of
  capping each second's rate at cap/N.
- Dashboard LIP reward is now payable, not accrued: `perf.lip_frame` drops
  markets under $1.00 and rounds the rest down to the cent
  (`lip.payable_usd`), so Net, PnL incl. LIP, reward per lot and the chart
  line all exclude them. The header shows "LIP payable" with accrued under it;
  per-market tiles flag "under $1: pays $0". The perf panel is now three
  columns: overall / regular quoting / LIP quoting.

## 2026-10-14 (late) — LIP: fair margin, payout floor, max markets, re-queue

After the first live LIP session (22:22-23:00 UTC): 1,109 LIP lots filled,
4-7x the backtest rate; 613 of them >2c above our fair carried $24.5 of the
$28.2 10s loss, and the 19:00 ET event's ~900 lots took the run past the
$80 halt.

- `lip_fair_margin`: most the LIP bid (the best bid) may sit above own-side
  fair; further above, that side gets no LIP. Not stepped down toward fair:
  behind the best bid earns a fraction of the LIP.
- $1 payout floor (hardcoded, `quoter.LIP_MIN_PAYOUT_USD`): no LIP on a
  market whose best case for the period (accrued + current $/s held to the
  LIP stop) is under $1, Kalshi's minimum payout.
- `lip_max_markets`: LIP only on the event's top markets by that projection.
- LIP now also pulls on `spot_burst`.
- `lip_requeue_min_ahead`: queue-position poll every `lip_hold_s` (real
  mode); orders near the front go to the back via a 0.01-lot size amend.
- Decision rows log `lip_size_*`, `lip_proj_usd`, `lip_ahead_*`.
- Crypto: margin 0.02, 2 markets, re-queue under 200 ahead.

## 2026-10-14 — LIP quote: best bid, price bands, no edge

Backtest and rationale: `kalshi_strategies/KXCRYPTOLEAD15M/research/LIP_REPORT.md`.

- `quoter._lip_target`: one order per side at others' best bid, sized by
  price band, off near close. No longer held outside the edge reservation:
  that rule left it almost never at the best bid, where the LIP is.
- `lip` params replaced: `lip_lots`, `prop_max_lip` and `lip_min_depth_ahead`
  are gone. New: `lip_cheap_max_price` / `lip_cheap_lots`,
  `lip_rich_min_price` / `lip_rich_lots`, `lip_min_time_left_s`.
  `lip_hold_s` and `enable_lip_quoting` are unchanged.
- Removed: `lip.choose_lip_level`, the queue-position poll
  (`session.lip_queue_loop`, `engine.check_lip_queue`, `lip_avoid`). The
  depth-ahead rule didn't protect against sweeps (HANDOFF #5).
- Crypto config: 0.20 → 400 lots, 0.80 → 100 lots, 180s. Still disabled.
  KXINX/KXINXU/KXINXDUD: new fields at 0 lots, disabled.
- `engine.position_limit` uses the largest LIP size. A 400-lot LIP fill can
  take a market past `max_position`, by design.

## 2026-10-12..14 — Crypto live tuning, audit race, signal age

Findings and numbers: `kalshi_strategies/KXCRYPTOLEAD15M/research/HANDOFF.md` §7 #5-#8.

- `quoting.enable_taking` (dashboard checkbox): off = maker-only. Takes
  lost money on every window tested; crypto runs with it off.
- `quoting.constant_edge_ref_s` (all series, default empty = off):
  constant_edge x sqrt(ref / time left) in the last ref seconds before close.
- `CONFIRM_S` (0.1s, measured send -> booked fill) and `TAKE_HOLD_S` (0.5s)
  replace `inflight_timeout_s` / `take_confirm_s`. The lean is flat linear
  (`lean_dollars` only; `lean_multiplier` / `lean_exponent` removed);
  `fair_clip_eps` is hardcoded.
- Crypto `pricing.theo_vol_reval_draws`: theo_vol by full revaluation over
  joint coin moves (0 = the old slope x 1-sigma). Fixes the last minute's
  missing tau/60 factor, which read 60/tau too high.
- Position audit compares Kalshi against our ledger before AND after the
  read and halts only after 2 consecutive disagreements. The four
  "ledger disagrees" halts were fills racing the read; the books matched.
- `risk.fresh_recovery_s` removed: quotes return as soon as the signal age
  is back under `max_signal_age_s`. `signal_age_s` is logged on every
  decision row.
- Dashboard: strategy performance panel (PnL with 1s/10s markout by
  window, lots filled, drawdown); only `max_markets` markets are charted
  when it's set.
- Overnight safety: balance watchdog (portfolio floor) runs on EC2; the
  review agent's EC2 queries run under a 1 GiB memory cap
  (`deploy/review_py.sh`).

## 2026-10-13 — S&P folder renamed, KXINXDUD, shared pricing hub

- `kalshi_strategies/KXINX_U` -> `kalshi_strategies/KXINX`. It now also
  serves KXINXDUD (one "above prior close" market per day).
- `run --series` takes folder names and runs all their series;
  `--only` names single series. `--series KXINX` = KXINX + KXINXU +
  KXINXDUD.
- One pricing hub process (`KXINX/hub.py`) replaces three IB connections:
  union subscription, one curve fit per tick batch, fairs streamed per
  series. IB/hold settings moved from each series' `pricing` section to
  `config.json["hub"]`.
- Combined risk: `hub.combined_max_loss_usd` caps the worst-case
  settlement loss across all S&P positions; sides that would worsen it are
  blocked (`combined_risk`).
- `pricing.monotone_levels` (default off): monotone P(S>K) before
  differencing into buckets.
- `ib_functions` strike candidates snapped to the $5 grid (a 7808.69
  boundary asked IB for 7803/7808/7813).
- Write groups: the three S&P series run in one process on one write
  bucket, one `cancels_token` reserve and one quote priority queue. The
  per-series split (26.7 tokens each) sat below reserve + one create, so no
  S&P quote was ever placed with three series running.

## 2026-10-09 — Reorg: kalshi_strategies + generic runtime + kalshi_viz

**Layout.** `kalshi_crypto/`, `kalshi_latency_arbs/`, `kalshi_main/` and
`viz/` are removed. Pricing lives in `kalshi_strategies/<FOLDER>/`
(`KXINX` serves KXINX and KXINXU; `KXCRYPTOLEAD15M`); everything else is
shared in `kalshi_infra/` (`kalshi/`, `trading/`, `runtime/`). One launcher:
`python -m kalshi_strategies.run --series ...`. One dashboard:
`streamlit run kalshi_viz/app.py`. All code is packages imported from the
repo root (no `sys.path` inserts); loose `kalshi_infra` scripts now run as
`python -m kalshi_infra.<script>`. See [live-trading.md](live-trading.md)
and [adding-a-strategy.md](adding-a-strategy.md).

**Config.** `config_override.json` and CLI tuning flags are gone. Each
strategy folder has a tracked `config.json`, loaded strictly with no code
defaults, edited by the dashboard and hot-reloaded (live fields now,
`next_event` fields at rollover).

**From Kalshi instead of config:**
- Fees, per event (series fee type/multiplier + event overrides). Measured
  on this account: 216 KXINX/KXINXU taker fills → 0.07·p(1−p); 10
  KXCRYPTOLEAD15M maker fills → 0. **Crypto taker fee changed from 1.0 to
  0.07·p(1−p):** the old crypto code read Kalshi's "multiplier 1" as the
  rate itself, so it demanded ~14× the real fee before taking.
- LIP program terms (target, discount, reward in centi-cents, period
  length, per-account cap).
- Write-rate budget (`/account/limits`), split evenly across running series.

**One quoter for every series** (`trading/quoter.py`). Replay against the
old SPX strategy: identical orders and fills on 150k recorded decisions per
series and on full backtest days (KXINX/KXINXU-26AUG27H1600). Behaviour
choices: SPX lean formula for both; `take_confirm_s` and
`min_quote_life_s` per series (crypto 0.75 / 0, SPX 0 / 2.0).

**New: LIP-farming quote** (`lip.lip_lots`, `prop_max_lip`,
`lip_min_depth_ahead`), separate from the edge quote (`quoting.order_lots`,
renamed from `quote_size`/`lot_size`). See live-trading.md § Quoting.

**Removed:** the crypto last-phase pull rules, the 10s pre-close order
stop, the near-expiry signal-age limit, the ladder-wide bias halt, and the
fees / most close / most risk parameters. Shadow send latency (34ms
measured) and the 5-error breaker are now fixed constants.

**Rollback:** commit `0a82f3c` is the full pre-reorg tree.

## 2026-10-08 — Quote churn, IB scope, shadow fills

Measured on today's journals (KXINX/KXINXU-26OCT08H1600). Before: KXINX
~20–40 orders/s and KXINXU ~11–20/s, against a 5/s-per-series budget.
After: both ~5–7/s in a live shadow run (4 min each, side by side).

Causes and fixes:

- **Fair value moves ~0.3¢ per signal at ~11 signals/s,** and every move
  that crossed a tick replaced the quote. New
  `QuotingConfig.min_quote_life_s = 2.0`: a quote younger than that is only
  replaced if fair value crosses it outright. The journal replay with this
  setting gave 41 → 7 orders/s (KXINX) and 20 → 7/s (KXINXU).
- **Option spread blowouts went straight into pricing** (98/min, p50
  125ms), swinging B7737's band to [0.12, 0.53]. `LiveOptionBook` now holds
  through them the same way the backtest's `hold_through_wide_spread`
  does: 3-min trailing median, 3×, 300ms settle.
- **The 5th/6th ATM buckets swapped places constantly** (27 flips/min on
  KXINXU). New `QuotingConfig.atm_hysteresis = 2`: a member stays until it
  ranks below `max_markets + 2`.
- **KXINXU was subscribed to 15 buckets but traded only 5.** The IB scope
  is now a contiguous window: the tradeable set plus a margin
  (`SCOPE_MARGIN_BUCKETS`). `run_live` pins `max_markets` to the tradeable
  count, and `rescope_loop` moves the window without reconnecting when ATM
  drifts. Lines: KXINX 48, KXINXU 30, 78 of 100 combined.
- **Shadow takes never used up Kalshi depth.** A 0.01-contract ask got
  "filled" 779 times in 4 minutes. `LiveEngine` now nets its own shadow
  takes against the depth at the touch until that depth changes.
- **`n_unaccounted` counted every shadow cancel** (tens of thousands per
  session). Shadow cancels are now their own outcome, so the invariant
  reads 0 again.
- **A `config_override.json` from September was silently applied** at
  every start. It's now ignored unless it's saved after the session starts.

## 2026-10-08 — `--series` accepts several series

- `kalshi_main/run_live.py --series KXINX KXINXU` (or `KXINX,KXINXU`)
  runs each series as its own child process, with output prefixed
  `[SERIES]`. The direct `kalshi_latency_arbs/live/run_live.py` takes the
  same form for the SPX series.
- `--max-orders-per-s` is now a total across all series: Kalshi's limit
  is per account, so it's split evenly (default 10/s → 5/s each for two).
- `--event-ticker` still needs exactly one series.
- Ctrl-C reaches every child. Each one pulls its quotes and exits; the
  parent waits up to 60s, then force-terminates whatever is left (and
  says to check Kalshi by hand).
- Ctrl-Break used to kill the process without running the quote-pull
  teardown on Windows. It now takes the same path as Ctrl-C.

## 2026-10-08 — IB feed wired into run_live; first live validation

- All IB connections are `readonly=True` (`keys.IB_READONLY`). IB is
  data-only; orders still go to Kalshi.
- `keys.IB_PORT` now 4001 (Gateway live), the port this account uses.
- `run_live` scopes the IB subscription to the ATM buckets
  (`ib_functions.DEFAULT_ATM_BUCKETS`: KXINX 5, KXINXU 15) and uses a
  separate clientId per series so both can run at once.
- `LiveIBFeed` refuses to start if the scope is over the 100-line budget.
- Error 10090 (no SPX index subscription) is filtered from the logs; the
  OPRA leg is unaffected.
- New tools: `kalshi_infra/measure_ib_latency.py`,
  `kalshi_latency_arbs/live/check_fair_value.py`.

Results: [ib-migration.md § Validation](ib-migration.md#validation-2026-10-08).

## 2026-10-05 — Live options feed: Databento ES → IB SPXW

**Summary.** Live-trading option feed switched from the retired
Databento ES-daily futures-options stream to native SPXW options on
Interactive Brokers TWS/Gateway. Historical option quotes (backtest
only) still come from Databento. ThetaData and the provider-selector
layer are removed entirely.

Details: [ib-migration.md](ib-migration.md).

### Files added

- `kalshi_infra/ib_functions.py` — IB contract helpers, strike
  resolution, deferred ib_async import.
- `kalshi_latency_arbs/live/live_ib_feed.py` — live SPXW feed
  implementing the `LiveIBFeed` interface `run_live.py` already
  expected.
- `docs/` — this directory (project-level docs).

### Files removed

- `kalshi_infra/thetadata_functions.py`
- `kalshi_infra/option_source.py`
- `kalshi_latency_arbs/live/__pycache__/live_databento_feed.cpython-312.pyc`
  (stale bytecode of the retired ES-daily feed).

### Files modified

- `kalshi_infra/keys.py` — added `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`,
  `IB_ACCOUNT` placeholders. IB uses no bearer token.
- `kalshi_latency_arbs/latency_arbs_functions.py` and
  `kalshi_latency_arbs/spxw_signal.py` — now import
  `databento_functions` directly (no `option_source` indirection).
- `kalshi_latency_arbs/live/run_live.py` — comments refreshed (no more
  ES/Databento-feed references); logic unchanged, already called
  `LiveIBFeed`.
- `viz/backtest_cache.py` — dropped `thetadata_functions.py` and
  `option_source.py` from the code-fingerprint globs; dropped
  `data/thetadata/` from the data-fingerprint globs.
- `kalshi_latency_arbs/live/live_context.py`,
  `kalshi_latency_arbs/live/live_persist.py`,
  `kalshi_latency_arbs/live/live_orders.py`,
  `kalshi_latency_arbs/live/live_kalshi_ws.py`,
  `kalshi_latency_arbs/live/live_signal.py` — stale ES/Databento
  comment references fixed.

### Comment policy

Per CLAUDE.md: docstrings and inline comments are being trimmed to
"fact + reason", with narrative moved to `docs/`. This pass trimmed
the files touched by the migration; others will be trimmed as they are
edited.
