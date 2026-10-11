# Live trading architecture

The "why" behind `kalshi_infra/runtime/session.py` and what it drives.
Code-level details stay in the files.

## Layout

```
kalshi_strategies/run.py          launcher: --series A B ..., one child process per series
kalshi_strategies/<FOLDER>/       per-series pricing only (see adding-a-strategy.md)
kalshi_infra/runtime/session.py   rollover + per-event loops (series-agnostic)
kalshi_infra/trading/quoter.py    edge, takes, edge quotes, LIP quote (series-agnostic)
kalshi_infra/trading/engine.py    order resolution: shadow fills or real Kalshi orders
kalshi_infra/kalshi/              auth, REST, websocket feed, orders, fees, markets
kalshi_infra/runtime/writer.py    writer process: snapshots, chart streams, journal
kalshi_infra/runtime/persist.py   snapshot file formats the dashboard reads
kalshi_viz/app.py                 dashboard (reads only; never launches a session)
```

Per event the data path is:

```
series feeds -> wakeup -> model.reprice -> FairUpdate --+
Kalshi ws    -> book / trade ------------------------+--+--> Quoter -> LiveEngine -> shadow fill | Kalshi
```

Repricing is event-driven: the model wakes the reprice loop when a pricing
input changes (crypto: a coin's mid; SPX: an IB tick), and a burst is
coalesced into one reprice. Background loops: config hot-reload, arm/disarm,
position audit, quote-fill poll, a 1s flush to the writer. Only
`quote_fill_poll_s` and `control_poll_s` are configurable; the rest are
constants in `session.py` / `writer.py` / `kalshi/ws_feed.py`.

The trading process never serialises anything: it ships new rows to the
writer process once a second. On a thread, the pandas/parquet work held the
GIL long enough to stall the loop past `max_signal_age_s`.

## Running

```
python -m kalshi_strategies.run --series KXINX KXINXU KXCRYPTOLEAD15M
python -m kalshi_strategies.run --series KXINX --event-ticker KXINX-26OCT09H1600   # one event, no rollover
python -m kalshi_strategies.run --series KXINXU --live-trading                      # REAL orders from start
streamlit run kalshi_viz/app.py
```

Several series run as separate processes so one series' CPU stalls or
crash can't stall another.

If the dashboard shows "📡 Signal feed: IB Gateway reports broken ...", the
gateway has lost its own link to IB (codes 2110/2103/2157) while our socket
to it stays up. Nothing on our side fixes that; re-login or restart IB
Gateway. The feed reconnects by itself after 60s without ticks.

## Config

Each strategy folder has a tracked `config.json`; nothing tunable has a
default in code (`kalshi_infra/config.py` loads strictly: missing, unknown
or out-of-range fields fail). The dashboard edits the file; the running
session polls it every `runtime.control_poll_s`:

- fields marked `live` apply on the next decision;
- fields marked `next_event` (feed scope, MC draws, ...) apply at rollover;
- an invalid file is rejected whole, the running config is kept, and the
  error shows on the dashboard.

What is NOT in config, because Kalshi is the source of truth:

| Value | Source | When read |
|---|---|---|
| Fees | `GET /series/{s}` fee type + multiplier, `GET /events/fee_changes` override (`kalshi/fees.py`) | event start |
| LIP program terms | `GET /incentive_programs` (target, discount, reward, period, per-account cap) | event start |
| Write rate limit | `GET /account/limits` refill rate, split evenly across running series | event start |

## Modes

- **Shadow** (default): fills simulated via `kalshi_backtest.execution`,
  after the measured one-way send latency plus the dispatch's processing
  time. Nothing is sent to Kalshi.
- **Real** (`--live-trading`, or the dashboard checkbox): takes go out as
  IOCs, quotes as resting limits. Arming reconciles positions against
  Kalshi first and starts a fresh session ledger. Disarming pulls every
  resting quote, including ones a crashed process left behind.

The arm switch is `data/live/{series}/trading_mode.json`, separate from
`config.json` so a money-moving control never rides along with an edge
tweak. Only a request written after the process started counts — a
leftover `live_trading: true` must not re-arm on startup.

`trading_time` bounds when real orders may go out in each event:
`real_trading_delay_s` holds them for that long after the event's scheduled
open, and `real_trading_cutoff_s` disarms that long before close. Paper
continues throughout, and real trading resumes on its own (after the delay,
or at the next event) if the toggle is still on.

## Guards

| Trigger | Effect |
|---|---|
| PnL ≤ −`risk.max_loss_usd` on the worse of touch / fair marking | halt (one-way for the session); quotes pulled |
| Fair inputs older than `risk.max_signal_age_s` | no takes, quotes pulled; resumes after `fresh_recovery_s` of freshness (not applied at event start) |
| 5 consecutive real-order errors | halt new orders (auth/balance/market state) |
| Ledger disagrees with Kalshi `/positions` (audit) | halt |
| Fill outside its own limit price | halt (units / side-convention bug) |
| A market already past `max_position` grows further | halt |

All guards apply in shadow too, so shadow keeps predicting real.

## Quoting

Per market and side (`trading/quoter.py` docstring has the formula):

- **Take** when fair minus (constant edge + band + lean + Kalshi taker fee)
  clears the touch.
- **Edge quote** (`quoting.order_lots`): first tick outside fair minus
  (quoting edge + band + lean + maker fee), capped `max_improve_ticks`
  inside the near touch and a tick inside the far one.
- **LIP quote** (`lip.lip_lots`): exists only to earn LIP. Considers every
  level at or outside the edge quote's own reservation, finds the best
  LIP $/s there, and rests at the DEEPEST level still earning
  ≥ `prop_max_lip` of it with at least `lip_min_depth_ahead` contracts
  ahead (better prices plus the whole same-price queue). Size is cut to the
  fewest lots earning the same payout and bounded by `max_position`, since
  it can fill. Re-chosen every decision; a same-level size increase is an
  amend (Kalshi sends it to the back), anything else is cancel + new order,
  so it always sits at the back of the queue. Safety cancels bypass the
  write-rate limiter.

Every reason a side doesn't trade (`no_fair`, `stale_signal`,
`outside_atm_set`, or a series `side_block` string) is logged per decision
and counted on the dashboard.

## Rate limit

Kalshi meters writes in tokens (`GET /account/endpoint_costs`): create and
amend 10, cancel 2. `KalshiOrderClient` keeps a token bucket at this
process's share of the account refill rate. Orders that would overdraw it
are dropped (the quoter re-decides next tick); cancels always go through.

## Snapshot files

`data/live/{series}/{date}/`: `summary.json` every second;
`quote_df` / `fills_df` / `pnl_df` / `fair_df` parquets and the chart
streams every 3s when anything changed; `journal/` append-only parts
(every 30s) that survive restarts and arming. The dashboard reads the
session snapshot, not the decisions journal (that reaches hundreds of MB a
day). Per-market chart streams go to `kalshi_viz/static/{series}/` so
Streamlit serves them same-origin.

## Rollover

Scheduled, not polled: Kalshi lists upcoming events with their
`open_time` / `close_time`, so the session picks the next event, sleeps to
`series.trade_from` (crypto: Kalshi's open; KXINX/KXINXU:
`pricing.event_open_et` on the close day), and runs it to `close_time`. The
next event and its fees/write limits are prefetched 30s before close; LIP
programs are looked up at the open, concurrently with trading.

Each event gets a fresh engine, quoter, ledger and LIP tracker; armed state
carries over unless `--no-carry-arm`. Crypto's Coinbase feed, composite
bars, vol forecaster and feed health live for the whole process
(`KXCRYPTOLEAD15M/live_feed.py`), so an event goes live within a fraction
of a second of its open.

## Deliberately not built

- Shared risk across series (each process guards its own series).
- Deep reconnect robustness (basic retry only).
- Historical (backtest) mode in the dashboard.
- Multi-event concurrency within a series.
