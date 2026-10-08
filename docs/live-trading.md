# Live trading architecture

Overview of `kalshi_latency_arbs/live/run_live.py` and the modules it
drives. Code-level details stay in the files themselves; this doc holds
the "why".

## Modes

- **Shadow** (default): fills simulated via `kalshi_backtest.execution`.
  Nothing sent to Kalshi. Still halts on the same guards as real mode
  so shadow keeps predicting what real would do.
- **Real** (`--live-trading`, or the dashboard "Send REAL orders"
  checkbox): takes go out as IOCs, quotes as resting limits (see
  `live_orders.py`). Arming reconciles positions against Kalshi first.
  Disarming pulls every resting quote before returning.

The dashboard writes `data/live/{series}/trading_mode.json`; the
process polls it (`trading_mode_loop`) and transitions via
`engine.set_live_trading()`.

## Guards (`LiveEngine.blocked_reason`)

| Trigger | What halts |
|---|---|
| `-max_loss_usd` on either marking convention | New orders; resting quotes pulled |
| Signal stale > `--max-signal-age-s` | New orders; resting quotes pulled |
| 5 consecutive real-order errors | New orders |
| Event about to close | New orders |

All of these halt in shadow mode too, so a shadow run keeps predicting
real behavior.

## Three clocks

The staleness guard reads one; the other two attribute stalls on the
dashboard.

| Clock | What it measures | Thread |
|---|---|---|
| `feed_age` | Last tick arrival from IB | IB callback (same loop) |
| `applied_age` | Last `drain()` call | Signal loop |
| `signal_age` | Last successful full recompute | Signal loop |

`signal_age` gates trading because it strictly dominates: no feed
means no recomputes, and a recompute that keeps throwing produces no
successful ones. The others split a stall into "feed vs. loop" so the
dashboard can show which side is behind.

## Snapshot cadence

- `FLUSH_INTERVAL_S = 3.0` — summary.json + journal cheap write.
- `HEAVY_FLUSH_EVERY = 10` — heavy parquet/stream rebuild (dashboard-
  only). 30s cadence.
- Flush skips if the previous write is still in flight (overwrite-in-
  place means a dropped frame costs nothing).

The CPU-bound pandas work runs on an executor but holds the GIL, so
`loop_lag_while_flushing` is tracked separately to quantify the
contention cost.

## Rollover

One process trades the series' current event, then the next, idling
over nights and weekends. Each event gets a fresh engine, ledger, and
day directory; the armed state carries across (`carry_arm`) unless
`--no-carry-arm`.

`EVENT_OPEN_ET = 09:00 ET` is the floor for opening a new event —
earlier than that, SPXW 0DTE isn't listed and `LiveIBFeed` would raise
at `qualifyContractsAsync`.

## Position audit

`position_audit_loop` is the only guard that uses ground truth
(Kalshi's own `/positions`) rather than our fill accounting. The
2026-08-28 incident where every other guard read numbers our ledger
produced (and ran to ~4x cap over ~1 min) is why this exists.

Runs on an executor every `AUDIT_INTERVAL_S = 5.0s`.

## Quote fills

Takes get fills piggybacked on the create-order response. A quote fill
arrives whenever someone crosses it, with no request to answer — so
`quote_fill_loop` polls Kalshi's fills endpoint every
`QUOTE_FILL_POLL_S = 1.0s`. The audit tolerance widens by the resting
size to absorb this lag.

## Deferred

- Deep reconnect robustness (basic retry only).
- Multi-event concurrency.
- IB-side historical data for backtest (Databento remains sole source).
