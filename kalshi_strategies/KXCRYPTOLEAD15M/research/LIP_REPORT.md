# LIP quoting for KXCRYPTOLEAD15M: research report

**Date:** 2026-10-14
**Data:** 232 events, 2026-10-11 22:15 to 2026-10-14 08:00 UTC (1,158 market-periods)
**Status:** the recommended rule is implemented in `quoter._lip_target` with
the `lip.*` params below (2026-10-14). `lip.enable_lip_quoting` is still false.

---

## 1. Summary

The current LIP design picks a level from depth ahead and stays outside the edge
reservation. It doesn't hold up against this data. Three findings drive that:

1. **Quoting deeper doesn't make LIP safer.** Each tick behind the reference price
   halves the reward (DF 0.5). Adverse cost per lot gets *worse* with depth,
   because a deep order only fills when a sweep runs through the whole book.
   With 100 lots and no gates, moving from the best bid to one tick behind the
   reference price cut LIP from $2,023 to $377. Ten-second markout per lot went
   from -3.0c to -6.6c.
2. **Spot doesn't lead Kalshi by enough to pull in time.** Across 9,430 Kalshi
   repricings (≥4c in 1s), the fastest venue (Binance perp) led by a median of
   0.2s. It led by ≥0.5s in only 41% of cases. The coin that moved was the
   market's own coin only 21% of the time. Every spot, fair-move or Kalshi-move
   gate cut LIP faster than it cut adverse fills.
3. **What separates good from bad LIP is the market's price and the time left.**
   The level we quote at and the signals around it matter much less.
   - Markets priced below 20c or above 80c paid more LIP than they lost to
     pickoffs.
   - Markets in the 35-80c band lost money net. That band is where race flips
     move prices 10-30c.
   - The last 3 minutes lost money net in every cut.
   - LIP fills are still adverse everywhere: the mid moves against 73-81% of
     them and the total 10s markout is negative. The band only keeps the
     pickoff cost (about 0-3c/lot on the cheap side) below the LIP earned (§4).
   - Our own real maker fills show the same pattern: 10s markout is +4.0c/lot
     at own-side ≤20c and -2.1c to -2.7c/lot at ≥80c.

**Recommended rule:**
- Rest one LIP order per side at the **best bid** (not behind the reference price).
- Only on markets whose touch is **outside 20-80c**.
- Size by side cost: **400 lots on the cheap side (≤20c), 100 lots on the
  expensive side (≥80c)**.
- **Off in the last 3 minutes.**
- Not tied to the edge reservation.
- Optional: pull every LIP order in the event for 3s after any multi-level
  Kalshi print.

The table below covers the same 232 events. Fill model is explained in §3.
"Net" = LIP paid (after the $1 floor) + PnL of the LIP fills.

| | LIP paid | 10s markout | net (10s) | net (settled) | t (net 10s / event) |
|---|---|---|---|---|---|
| prints-only fills | $1,523 | -$257 | **+$1,267** | +$2,646 | 7.4 |
| stress: 20% of book crossings fill | $1,511 | -$710 | **+$801** | +$2,114 | 4.5 |
| stress: sweeps take the whole order | $1,419 | +$747 | **+$2,166** | +$4,367 | 6.6 |

- **Per event:** $5.46 net on 10s markout (prints model; $3.45 in x20), from
  $6.57 of LIP.
- **Market-periods paid:** 73% cleared the $1 floor.
- **Both halves of the sample:** positive (+$619 / +$648).
- **All 4 days:** positive.
- **Events positive:** 83%.
- **Worst event:** -$171 settled. The 5th-percentile event lost $65.
- **Collateral:** $723 at p95 across the five markets.

Compare that with how we run now: our edge quotes alone accrue a median $0.02 per
market-period, and none cleared $1 on 10-12/13 (HANDOFF #5).

**Biggest caveats (§7):**
- Only 2.4 days of data, in one regime.
- The simulator can't model how other LIP farmers react to 400-lot orders.
- The fill model is calibrated on 1-lot quotes, not 400-lot ones.

Validate with a small live test before scaling (§8).

---

## 2. Program facts (from Kalshi, 2026-10-14)

- **Terms are identical on all 2,405 KXCRYPTOLEAD15M programs** from 10-10 to
  10-15: target 1,000, DF 0.5, $20 per market per 15-min period, no per-account
  cap. Source: `GET /incentive_programs`.
- **Payout floor:** a payout under $1.00 is not paid at all. Source: CFTC filing
  of 2026-07-15, "if the result is greater than or equal to $1.00".
- **Scoring:** the filing scores *all* bids at the level where cumulative size
  reaches Target. `lip.py` instead counts our size only if it sits ahead of
  Target. I scored both ways and the difference is ≤3%, so it doesn't matter
  for this decision.
- **Snapshot qualification:** both sides reached Target 84% of the time, so 16%
  of snapshots pay nobody.

### Book structure (per side, 3s grid, all 1,158 market-periods)

| | |
|---|---|
| Median depth | 5,000-5,800 contracts |
| Median size at the best bid | 120 (the repeating 120-lot ladder from HANDOFF #5) |
| Reference price position | at the best bid 31% of the time; median 2-4 ticks behind |
| Competition score, own-side price 15-95c | ~450-800 |
| Competition score, own-side price ≤15c | 4,000-11,000 (crowded) |

### $ per period for one order on one side, held all period

| level | 25 lots | 50 | 100 | 200 |
|---|---|---|---|---|
| best bid | $0.45 | $0.91 | **$2.54** | $4.87 |
| reference price | $0.44 | $0.84 | $1.53 | $2.62 |
| ref − 1 tick | $0.22 | $0.43 | $0.82 | $1.48 |
| ref − 2 ticks | $0.11 | $0.22 | $0.43 | $0.81 |

- **Clearing the $1 floor** takes about 50 lots per side on both sides, or about
  100 lots on one side.
- **The best bid always gets full weight.** Everything at or above the
  reference price scores 1.0x, and the best bid has only ~120 contracts ahead
  versus ~600 at the reference price.
- So `prop_max_lip` (pick the deepest level earning ≥70% of the best) ends up
  choosing the level that earns least per unit of risk.

---

## 3. Method

### Simulator (`research/lip_sim/sim.py`)

- **Orders:** one virtual LIP order per (market, side). All 5 markets of an
  event run on one clock.
- **Book:** Predexon ladders, websocket source only. Median 0.59s between
  snapshots. The `live_early` source disagrees on the touch 13-26% of the time,
  so I dropped it.
- **Fills:** from real Kalshi prints, grouped into sweeps.
  - We fill when a sweep prints *through* our price (the level including us was
    consumed).
  - We also fill when a print *at* our price exceeds the queue ahead of us.
  - We join the back of the displayed queue. The queue shrinks with prints and
    with displayed cancels; cancels are assumed to be ahead of us, which is
    pessimistic.
- **Latency:** 100ms for place, cancel and move.
- **Inputs:** fair, edge reservation and theo_vol come from the live decision
  journal (250ms bins).

### Calibration against our real fills

I replayed our own 1-lot edge quotes (top level, from the journal) and compared
the result with the 1,838 real maker fill events in the same 208 events.

- **Print-based sim fills:** a real fill landed within 2s 66-78% of the time.
- **Sim "book crossings"** (a snapshot shows the far side at or through our
  price): a real fill landed only 16-20% of the time.
  - That holds even when we were provably resting.
  - Only 28% of crossings had any print at or through our price.
  - Most crossings are orders pulled before they matched, or snapshot
    artefacts.
- **The sim is pessimistic on adverse selection.** It put the replayed quotes
  at -2.5c/lot of 10s markout. The real fills were +1.3c/lot.

### Three fill models used throughout

| model | what it assumes |
|---|---|
| **prints** (main) | fills from prints only |
| **x20** (stress) | prints, plus 20% of book crossings fill us |
| **full** (stress) | a sweep that prints through us takes our whole order, not just the volume it found below us |

The first two bracket the calibration. The third covers price-limited sweepers
that would have eaten all 400 lots.

### Spot features (`spot_feat.py`)

- 100ms grid of Coinbase, Binance, OKX, Bybit and Hyperliquid perp prints,
  pulled from EC2.
- Per coin: median-across-venues return over 0.5/1/2/5s, z-scored against its
  trailing 15 min.
- A max-across-venues version was dominated by single-venue bad prints
  (z up to 10⁴), so I used the median.

### Metrics

- **10s markout:** mid 10s after the fill versus fill price, on Predexon mids.
- **Settled:** PnL against Kalshi's result.
- 10s markout is the steadier of the two. Settled is a binary outcome on
  400-lot positions and is noisy.

---

## 4. What predicts an adverse LIP fill

### Unrestricted sim (best bid, 200 lots, no gates)

LIP accrued and pickoff cost in the same buckets, prints model:

| own-side price | LIP | 10s markout | net |
|---|---|---|---|
| ≤ 0.10 | $256 | +$89 | **+$346** |
| 0.10-0.20 | $429 | +$132 | **+$561** |
| 0.20-0.35 | $541 | -$282 | +$259 |
| 0.35-0.50 | $449 | -$560 | -$110 |
| 0.50-0.65 | $536 | -$1,432 | **-$896** |
| 0.65-0.80 | $838 | -$1,560 | **-$722** |
| 0.80-0.90 | $809 | -$1,116 | -$307 |
| 0.90-1.00 | $632 | -$610 | +$22 |

| time left | LIP | 10s markout | net |
|---|---|---|---|
| < 1 min | $135 | -$803 | **-$668** |
| 1-2 min | $231 | -$1,204 | **-$973** |
| 2-3 min | $316 | -$668 | -$351 |
| 3-5 min | $789 | -$569 | +$221 |
| 5-15 min | $3,018 | -$2,096 | +$922 |

- The same sign pattern holds in both stress models.
- In the x20 model, the 20-80c band is negative by $4-6k.

### Out-of-sample check on the price bands

I chose the profitable 5c buckets on the first half of events and scored them on
the second half, then did the reverse.

- **Prints model:** the band choice didn't matter much (a single overfit split
  either way).
- **x20 model:** the selection kept only 0-25c and 80-100c in both
  directions.
  - Test net: +$473 vs -$207 for quoting everywhere.
  - Reverse split: +$301 vs -$899.

### Our real maker fills agree (2,893 fills, 10-11 to 10-14, edge quotes)

| own-side price | ≤0.10 | 0.10-0.20 | 0.20-0.35 | 0.35-0.65 | 0.65-0.80 | 0.80-0.90 | ≥0.90 |
|---|---|---|---|---|---|---|---|
| 10s markout c/lot | +4.0 | +4.1 | +3.0 | +0.3 / +1.3 | -0.6 | -2.1 | -2.7 |

60-180s left: -2.5c and -0.6c/lot. More than 3 min left: +1.3c to +1.9c/lot.

### Features that did not separate good from bad fills

These either had no monotone effect or cost more LIP than they saved:

- **Spot |z| or |bp| at the fill:** not monotone in either fill type.
- **Our fair's move over the prior 1s:** no clear effect.
- **theo_vol:** a weak gradient (-4c to -10c/lot), nothing to cut on.
- **Edge (own-side fair minus our price):** not monotone.
  - Within the cheap wing, larger edge had *better* 10s markout.
  - But settled was worse, which points to longshots that later won.
- **Contracts ahead at placement:** looks like a signal (150-300 ahead is worst
  at -7.3c/lot), but it's a proxy for price. Deep queues only exist on the
  ≤15c side.
- **Coin:** HYPE is the only coin with negative settled PnL in the candidate
  run (-$323). SOL has the worst 10s markout (-$295). That's one cut, so don't
  act on it yet.

### LIP fills are adverse; the band only limits how adverse

Split of each candidate-rule fill's 10s markout into the spread captured (versus
the mid 1s before the fill) and the mid move over [t−1s, t+10s]:

| side | fill model | spread captured | mid move | 10s markout | fills with mid against us |
|---|---|---|---|---|---|
| cheap ≤20c | prints | +3.4c/lot | −3.0c/lot | +0.4c/lot (+$106) | 73% |
| cheap ≤20c | x20 | +3.0c/lot | −3.5c/lot | −0.5c/lot (−$165) | 81% |
| expensive ≥80c | prints | +3.1c/lot | −5.8c/lot | −2.7c/lot (−$355) | 73% |
| expensive ≥80c | x20 | +3.0c/lot | −6.3c/lot | −3.3c/lot (−$532) | 81% |

- Nearly every LIP fill is a pickoff. Total 10s markout on the candidate rule
  is negative (−$257 prints, −$710 x20).
- The positive net comes entirely from LIP.
- The cheap side is about break-even on markout because longshots move only
  about as far as the spread. That is not a positive edge.
- The expensive side is clearly adverse. It is the weakest leg: start without
  it or keep it small.
- The +4c/lot real-fill figure in the table above is our edge quotes, which
  rest only with edge against fair. It doesn't carry over to LIP fills, which
  ignore fair.

### Why the price band works

At any moment the race usually has one or two contenders in 20-80c. A move in
any coin reprices *those* markets by 10-30c, and the sweeps that follow hit
everyone resting there. A 10c longshot rarely moves 4c in a second. It's also
cheap to hold (400 lots at 10c is $40 of collateral), and other farmers crowd
it, so it needs size to earn a share.

---

## 5. Pull signals: none fast enough to justify their cost

### Lead-lag (`c_leadlag.py`, 9,430 Kalshi jumps)

| source | median lead | % of jumps led by ≥0.1s | % led by ≥0.5s |
|---|---|---|---|
| Binance perp | 0.2s | 53% | 41% |
| OKX perp | 0.1s | 50% | 38% |
| Bybit perp | 0.1s | 47% | 36% |
| Coinbase | 0.0s | 45% | 36% |
| HL perp | 0.0s | 46% | 37% |

- For driver moves above 3bp, the median lead falls to 0-0.1s.
- That leaves no time for a 50-100ms cancel to land.

### Gates on the candidate rule (prints model, net 10s markout vs +$1,267 ungated)

| gate | net 10s | effect |
|---|---|---|
| spot \|z\|≥4, any coin, 3s | +$903 | cost LIP $430, saved $69 adverse |
| spot \|z\|≥6 | +$1,087 | |
| any Kalshi mid ≥3c/1s, 5s | +$423 | |
| any fair move ≥2c/1s (earlier sweep, reference-price level) | LIP cut by ~99% ($1,240 → $12) | |

### Other ideas tested

- **Pull every LIP order in the event when ≥20 lots fill anywhere**
  (`xgroup`; Kalshi order groups do this exchange-side): about neutral, -$20.
  - Only 5% of fills see a fill on another market of the event within 5ms.
  - Fills arriving ≥100ms after the first fill in an event carry only 14% of
    the markout.
- **Pull every LIP order for 3s on any multi-level Kalshi print, 50ms
  reaction:** small gain.
  - Same 10s net.
  - Settled +$269.
  - x20 model +$128.
  - Optional.

### Order latency (candidate rule)

| latency | net 10s | LIP |
|---|---|---|
| 50ms | +$1,247 | |
| 100ms | +$1,267 | |
| 250ms | +$1,226 | |
| 500ms | +$1,249 | |
| 1s | +$751 | -30% |

Latency up to 0.5s doesn't matter, because there's no race to win.

---

## 6. Variants of the candidate rule

Prints model; x20 in parentheses.

| variant | LIP paid | net 10s | net settled | collateral p95 |
|---|---|---|---|---|
| **candidate:** best, ≤.20→400 / ≥.80→100, τ≥180s, sweep pull | $1,523 | +$1,267 (+$801) | +$2,646 | $723 |
| cheap side only, 400 | $540 | +$646 (+$367) | +$1,553 | $371 |
| cheap side only, 800 | $1,028 | +$1,173 (+$829) | +$2,094 | $670 |
| expensive side only, 200 | $1,186 | +$478 (+$142) | +$1,458 | $948 |
| ≤.20→600 / ≥.80→150 | $2,062 | +$1,646 (+$1,075) | +$3,283 | $1,033 |
| ≤.20→400 / ≥.80→200 | $2,257 | +$1,647 (+$1,034) | +$3,549 | $1,168 |
| bands .25/.75 | $2,303 | +$1,520 (+$617) | +$3,123 | $968 |
| bands .15/.85 | $1,266 | +$902 (+$508) | +$2,534 | $879 |
| τ ≥ 120s | $1,986 | +$1,510 (+$894) | +$3,014 | $957 |
| τ ≥ 300s | $1,367 | +$1,031 (+$589) | +$2,402 | $887 |
| hold 2s between moves | $1,578 | +$1,367 (+$795) | +$3,146 | $729 |
| never within 2c of our fair | $1,092 | +$1,055 (+$890) | +$1,140 | $578 |
| at reference price instead of best | $990 | +$898 (+$678) | +$683 | $809 |
| never inside the edge reservation (live rule) | $381 | +$401 (+$267) | +$654 | $530 |
| band on our fair instead of the market price | $1,359 | +$658 (+$154) | +$2,883 | $721 |

### Takeaways

- **The edge-reservation constraint in `quoter._lip_target` is the main
  reason LIP earns nothing today.** The reservation sits 3.5c+ inside fair, so
  it almost never lets us stand at the best bid.
- **Use the market's price for the band, not our fair.** Our fair's errors are
  exactly where we get picked off.
- **The expensive side is marginal on its own.** It mainly lifts the floor
  clearance for the period. Size it smaller than the cheap side.
- **τ ≥ 120s scored better than 180s here,** but 1-2 min was the worst bucket
  in every cut. Start at 180s.
- **A 2s hold** was slightly better and cut writes from ~460 to ~380 creates
  per event (~5 tokens/s against an 80 tokens/s bucket).

---

## 7. Caveats

0. **No market impact is modeled.** The sim adds our order to the historical
   book and replays real prints; everyone else behaves exactly as they did.
   - Our cheap-side 400 lots exceeds all taker volume that normally hits those
     bids in a period (~224 lots; p99 sweep 358). Sim fills are small: median
     8 lots, and only 2% took the whole order.
   - Not captured: flow that appears only because our size is there; makers
     stepping in front of us or leaning on us; extra price moves caused by our
     own fills.
   - Worst-case fill bound on the candidate rule (net 10s / net settled):

     | book crossings that fill us | net 10s | net settled |
     |---|---|---|
     | 0% | +$1,267 | +$2,646 |
     | 20% | +$801 | +$2,114 |
     | 50% | +$166 | +$1,399 |
     | 100% | −$690 | +$582 |
     | 100%, whole order on any sweep through us | +$589 | +$2,892 |

   - The live test must step size (100 → 200 → 400) and check whether fill
     volume at our price scales with our size.
1. **Competitive response.** At 400 lots on the cheap side we'd hold a large
   share of those markets' LIP pool. Other farmers can add size and dilute us.
   The sim holds their behaviour fixed.
2. **Fill model at size.** Calibration used 1-lot quotes. A 400-lot order may be
   targeted, and the "full" model only partly covers that. The sim was
   pessimistic on our real edge fills, but that may not carry over to size.
3. **Short sample.** 2.4 days of one regime. Per-event noise is large: the
   worst event was -$171 settled, and settled PnL on 400-lot longshots is
   lumpy.
4. **Snapshot resolution.** Predexon books are about 0.6s apart, so queue
   position and any crossing inside one snapshot gap are approximate.
5. **Risk-guard conflicts.** `risk.max_loss_usd` = 80 would have halted in
   3.4% of events on settled PnL (4.3% in x20, 12% in "full"). It never
   triggers on 10s markout. The `max_position` cap (10) is bypassed by `lip_lots` in
   `engine`. LIP needs its own per-event loss and collateral budget.
6. **LIP unit still unverified against a payout.** The $20 assumes
   `period_reward` is in centi-cents (HANDOFF #3). Check it against the first
   real payout.

---

## 8. Suggested next steps

1. **Live A/B at small size.**
   - Cheap side only, 100 lots, own-side ≤20c, τ ≥ 180s, best bid.
   - About $10-20 of collateral per order.
   - Re-run the sim on the same events, then compare its fill count and markout
     with the real ones.
   - If real ≈ sim, step to 400 and add the expensive side at 100.
2. **Change `_lip_target`.**
   - Drop the `res`-based ceiling.
   - Choose the level as own-side best (capped one tick inside the far touch,
     as now).
   - Gate on the market's own-side touch band and on τ.
   - Size from a price→lots table.
   - Retire `lip_min_depth_ahead` and `prop_max_lip`, which the data says
     don't help.
   - Keep the stale/no-fair pulls.
3. **Add LIP risk params.** Per-event LIP loss stop and collateral cap,
   separate from `max_loss_usd`.
4. **Not yet tested:**
   - Improving the touch by one tick on the cheap side. Econ table: 200 lots
     at best+1 earn $6.13/period vs $4.87 at best. It will fill more.
   - Excluding HYPE.
   - Whether the band should key off the **event's** leader count rather than
     each market's price.

---

## Files

- **Scripts:** `research/lip_sim/`.
  - Paths point at `%TEMP%\opencode\lipr\`, which holds the pulled EC2 journals,
    spot/perp prints and per-event slices.
  - `condense.py` re-pulls the journals on EC2. Run it through
    `deploy/review_py.sh`.
- **Policy sets:** `pol_*.py`.
- **Run a set:** `python run_sim.py <set>`, then `python rep.py <set>`.
- **Bucket tables:** `bucket_rep.py`.
- **Robustness and out-of-sample checks:** `robust.py`.
- **Real-fill check:** `real_wing.py`.
