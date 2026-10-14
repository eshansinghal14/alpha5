# Adding a strategy

A strategy folder contains pricing only. Orders, edge, quoting, LIP,
guards, persistence and the dashboard are shared.

## 1. Folder

```
kalshi_strategies/<FOLDER>/
  __init__.py      SERIES = {"<TICKER>": Series(...)}   (registered automatically by run.py)
  config.py        <X>Config section: shared sections + your own `pricing` section
  config.json      every value, no defaults anywhere
  model.py         EventModel for one event
  backtest/ research/ tests/
```

One folder may serve several series tickers (KXINX serves KXINX, KXINXU
and KXINXDUD), with one complete `config.json` section per ticker.
`run --series <FOLDER>` runs all of them. A folder can also declare
`SERVICES = [Service(...)]`, processes its series share (KXINX's pricing
hub); the launcher starts and supervises them.

## 2. Config

```python
@section
class MyConfig:
    quoting: QuotingParams
    trading_time: TradingTimeParams
    risk: RiskParams
    runtime: RuntimeParams
    lip: LipSettings
    pricing: MyPricingParams     # your fields, declared with param(...)
```

Give each `param` a `help` that says what it does and, for anything
measured, what it was measured against and when. Mark fields that can't
change mid-event `apply=APPLY_NEXT_EVENT`. The dashboard form is generated
from these declarations.

## 3. Model

`kalshi_infra/runtime/series.py` (`EventModel`) and
`kalshi_infra/trading/fair.py` (`FairModel`) are the contract:

| Method | Returns |
|---|---|
| `tickers` | the event's market tickers to subscribe to |
| `reprice(now_s)` | `{ticker: Fair(prob, lo, hi) or None}`, or None if nothing changed |
| `on_market_event(ticker, now_s)` | cheap refresh on a Kalshi tick, or None |
| `signal_age_s()` | age of the newest input under the current fairs (None = unknown → stale) |
| `feed_age_s()`, `applied_age_s()` | stall attribution for the dashboard |
| `side_block(ticker, side, fair, now_s)` | a reason string to veto one side, or None |
| `summary_extras()` | series-specific scalars for summary.json |
| `feed_tasks()` | coroutines for your own feeds |
| `close()` | release feed resources |

The band (`lo`, `hi`) is what edge is measured against: wider band, more
edge demanded. `None` for a market means "no trustworthy fair" and pulls
its quotes.

## 4. Register

```python
SERIES = {"KXFOO": Series(ticker="KXFOO", config_cls=MyConfig, config_path=CONFIG_PATH,
                          config_key=None, pick_event=..., build_model=...)}
```

`pick_event(open_events, cfg, now)` chooses which open event to trade;
`build_model(event, cfg)` is async and returns the EventModel.

Then `python -m kalshi_strategies.run --series KXFOO` and the dashboard
picks it up.

## 5. Backtest

Drive the same `Quoter` through `kalshi_backtest.engine.Backtest`, feeding
your fairs as `FairUpdate` external events (see
`KXINX/backtest/run_backtest.py`). Fees come from
`kalshi.fees.for_event` exactly as live.
