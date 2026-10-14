# Live options feed: Databento ES → IB SPXW

**Date:** 2026-10-05
**Scope:** Live trading only. Backtest unaffected.

*Paths updated after the 2026-10-09 reorg, and again 2026-10-13: the feed
is `kalshi_strategies/KXINX/ib_feed.py`, owned by the shared pricing hub
(`KXINX/hub.py`), one IB connection for every S&P series.*

## What changed

The live option stream used to be:

- Databento Live client
- `GLBX.MDP3` dataset (ES futures options)
- `databento.Live` callback thread → `call_soon_threadsafe` into asyncio
- ES → SPX basis adjustment on every tick

It is now:

- `ib_async` client (fallback `ib_insync`)
- TWS or IB Gateway on localhost socket (`keys.IB_HOST:IB_PORT`)
- Native SPXW options from CBOE/OPRA
- Callbacks dispatch on the same asyncio loop as the signal loop — no
  thread bridge
- No basis adjustment — strikes are cash-SPX directly

## Why

- IB has native SPXW access with an OPRA subscription; the account's
  Databento license didn't cover live OPRA.
- Eliminating ES → SPX basis estimation removes a whole class of
  drift and the `rebase()` state machine the retired feed carried.
- IB is also where real orders will be routed; keeping the market data
  and the execution venue on the same account simplifies entitlements.

## Why keep Databento for backtest

IB historical option endpoints are slow and line-rate-limited; a full
0DTE chain backfill would take hours and bump into daily limits.
Databento's cmbp-1 OPRA pull remains the only practical bulk source.
`load_kxinx_event_option_quotes` is unchanged.

## Interface the live feed satisfies

`KXINX/hub.py` expects:

```python
class LiveIBFeed:
    def __init__(self, expiry, strikes, *, client_id, hold: HoldParams, reconnect_delay_s, label=None)
    async def set_strikes(self, strikes) -> bool   # resubscribe without a gap
    book: LiveOptionBook   # has call_strikes, put_strikes (np.ndarray),
                           # snapshot_arrays() -> 6-tuple (mid/bid/ask x call/put)
    def drain(self) -> int           # applies buffered ticks; returns N
    def records_per_s(self) -> float
    def seconds_since_record(self) -> float   # feed liveness clock
    def seconds_since_applied(self) -> float  # drain lag clock
    async def run(self) -> None               # keeps the client alive
```

See `kalshi_strategies/KXINX/ib_feed.py` for the implementation. Hold
and reconnect values come from the `hub` section of `KXINX/config.json`.

## IB contract specification

SPXW weekly options, cash-settled, PM-settled at 16:00 ET:

```python
Option(
    symbol="SPXW",
    lastTradeDateOrContractMonth="YYYYMMDD",
    strike=float,          # cash-SPX strike
    right="C" | "P",
    exchange="CBOE",
    multiplier="100",
    currency="USD",
    tradingClass="SPXW",   # distinguishes weeklies from the AM-settled monthly
)
```

Required market-data subscriptions on the IB account:

- **OPRA Options Top of Book** — SPXW option quotes
- *CBOE Streaming Market Indexes* (optional) — SPX cash print

## Line budget

IB's default `maxTicker` limit is 100 L1 market-data lines, one per
contract. See `databento_functions.IB_MARKET_DATA_LINES` and
`select_event_buckets`.

Measured 2026-08-29 (KXINX-26AUG28H1600):

| Selection | Contracts |
|---|---|
| 5 ATM KXINX buckets | 36 |
| 15 ATM KXINXU buckets | 34 |
| Full KXINX event (30 buckets) | 174 |
| Full KXINXU event (60 buckets) | 124 |

ATM buckets are the cheap case because they're contiguous and share
strike neighbors. Scoped selections (`tickers=` arg) must stay inside
the budget — exceeding doesn't error, it silently starves whichever
subscriptions came last.

## Connection model

TWS and IB Gateway accept API connections on these ports:

| Port | Service | Mode |
|---|---|---|
| 7496 | TWS | Live |
| 7497 | TWS | Paper |
| 4001 | Gateway | Live |
| 4002 | Gateway | Paper |

Default in `keys.py` is 4002 (Gateway paper). The Gateway process holds
the account login; the Python client just needs host, port, and a
`clientId` the account isn't already using. Two clients sharing a
`clientId` disconnect the older.

The Gateway's own **Trusted IPs** setting must include whatever host
is dialing — `127.0.0.1` for the common case.

## Reconnect behavior

`LiveIBFeed.run()` reconnects on a dropped socket after
`pricing.ib_reconnect_delay_s`. During the gap no reprice succeeds, so
signal age climbs past `risk.max_signal_age_s` (0.2s for SPX) and the
quoter stops taking and pulls quotes well before the reconnect lands.

## Gotchas

- `qualifyContractsAsync` silently drops contracts the chain doesn't
  list; the two sides can end up with different strike sets, so the
  book rebuilds its ndarrays after qualification completes.
- IB uses `-1` and `NaN` interchangeably for "no quote yet". The book
  treats both as missing.
- `reqMktData` with `snapshot=False` is a streaming subscription;
  `snapshot=True` is a one-shot billed from a separate pool. Live uses
  streaming only.
- `genericTickList=""` is intentional — we don't want IB-computed
  greeks or any of the type-10+ augmentations. The Breeden-Litzenberger
  pricing is model-free and uses only bid/ask.

## Validation (2026-10-08)

Measured around 12:20–12:35 ET on a quiet midday session, Gateway live
port 4001.

**Account entitlements.** OPRA top-of-book is live. The SPX cash index is
delayed only (no CBOE Streaming Indexes subscription), which isn't
needed. It does cause error 10090 on every option request; that's now
filtered.

**Tick cadence** (`measure_ib_latency.py`, 22 ATM±5 contracts, 30s):

| Metric | Value |
|---|---|
| Aggregate price-moving ticks | 45/s |
| Inter-tick gap p10 / p50 / p90 / p99 | 78 / 203 / 1000 / 4218 ms |
| Contracts that ticked | 22 / 22 |

That's the per-contract gap between quote changes, not wire latency
(IB doesn't send exchange timestamps). The ~100ms floor matches IB's
documented options conflation, which `ib_emulation.IB_OPTIONS_CONFLATION_MS`
already models for the backtest.

**Fair value vs Kalshi** (`check_fair_value.py`, 40s, 10s samples):

| | KXINX (5 ATM) | KXINXU (15 ATM) |
|---|---|---|
| Contracts subscribed | 36 | 34 |
| Implied mid inside Kalshi bid/ask | 85% | 81% |
| CI overlaps Kalshi bid/ask | 100% | 92% |
| Median implied − Kalshi mid | −0.0003 | −0.0096 |
| Median \|implied − Kalshi mid\| | 0.007 | 0.010 |
| Median CI width | 0.019 | 0.016 |
| Put-call parity forward spread across strikes | 0.10 pts | 0.12 pts |

KXINX lines up with Kalshi with essentially no bias. KXINXU reads about
1¢ low across the ladder; the at-the-money threshold (T7784.9999) was 3–4¢
below Kalshi's bid the whole run, and Kalshi's own book had nearly
crossed there (0.50/0.51). The tight parity forward says the option book
is internally consistent, so this looks like a real disagreement, not a
data problem. That's what the strategy trades on, but **watch it**: if it
persists across sessions, it's a model bias. (The ladder-wide bias halt
that used to guard this was removed in the 2026-10-09 reorg.)

**Shadow run** (pre-reorg `run_live.run`, KXINX, 75s): connects, subscribes 36
contracts, no guard trips, signal age ~0.09s (guard: 0.2s), bias
check `systematic=false`, and it shuts down cleanly on cancel.

## What's deliberately NOT built yet

- Reconnect robustness past a basic retry (no exponential backoff, no
  snapshot replay after reconnect — the next tick re-fills the book).
- Multi-event concurrent streaming (one event per process).
- Historical option data via IB (Databento is the sole historical
  source; use its cache).
