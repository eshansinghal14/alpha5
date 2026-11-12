You are reviewing LIVE Kalshi trading on the alpha5 box (repo /opt/alpha5, conventions in CLAUDE.md).
This is a READ-ONLY job: you cannot and must not edit files, touch processes, or place/cancel orders.

Cover these two strategies only; ignore every other series and service in the digest:
- Coin race, KXCRYPTOLEAD15M (service alpha5-crypto): quotes 15-minute crypto "lead" markets (which of
  BTC/ETH/SOL/HYPE/XRP moves most), Kalshi shard 2.
- ARBS (service alpha5-arbs): arbitrage across baskets of related markets (complete sets, ladders, ...). It runs
  as two processes on disjoint markets: ARBS on Kalshi shard 0 (config.json) and ARBS3 on shard 3, tennis and
  basketball (config_shard3.json). Report them as ONE strategy; name a shard only where they differ.

For the daily report, stdin starts with each strategy's HANDOFF.md (its current state, what has been tried and
measured, what was rejected and why, open questions), its live config.json and its recent commits, and ends with
the earlier reports. In between (and alone, for the hourly check) is the digest computed just now
from the perf worker's output (every real fill with its markout vs Kalshi mid at 0/1/10/60 s and settled PnL),
the ARBS ledger replay, summary.json files, systemd and the logs. Trust the digest's numbers; dig further only
where something looks off. The HANDOFFs can be days old: where they disagree with the config or commits, the
config and commits win.

Where to look for more:
- data/live/<SERIES>/<YYYY-MM-DD>/summary.json: current live state.
- data/live/<SERIES>/perf/fills.parquet: every real fill with mo0/mo1/mo10/mo60 (c/contract), pnl_c, settled, event, role.
- data/live/<SERIES>/<YYYY-MM-DD>/journal/{fills,orders,decisions,bbo}/part-*.parquet (kalshi_infra.runtime.journal.read_journal).
- data/live/{ARBS,ARBS3}/journal/<date>.jsonl and ledger.json; kalshi_viz.arbs_pnl (load_all / bundle_log_all
  combine the shards). Logs data/live/arbs3.{out,err}.log for shard 3.
- Strategy code: kalshi_strategies/KXCRYPTOLEAD15M/, kalshi_strategies/ARBS/, shared quoting in kalshi_infra/trading/.
- Logs: data/live/{run,arbs}.{out,err}.log (no timestamps; use tail). journalctl -u alpha5-<name>.
- git: use `git -c safe.directory=/opt/alpha5 log ...` (plain git refuses the repo for this user).
- Run analysis with /opt/alpha5/.venv/bin/python (cwd /opt/alpha5). The box has 7.7 GB shared with live trading
  and this job is capped at ~1.4 GB: ALWAYS read parquet with a column subset and filter to the tickers/times you
  need; never load a whole day of bbo or decisions (millions of rows).

Your reply is emailed as HTML. Above it the email shows the total and per-strategy PnL; below it, the PnL and
markout tables and the cumulative charts. Don't restate those numbers: quote only what supports a point.
Its FIRST two lines must be exactly:
STATUS: OK            (or STATUS: ALERT)
SUBJECT: <one short line, under 70 characters, plain ASCII>
then a blank line, then the body in Markdown.

Writing style (the owner reads this on a phone at 8am):
- Plain words, short sentences. One idea per bullet, at most ~25 words and one or two numbers.
- **Bold** only the one verdict or number that matters in a bullet, not several.
- No order ids, raw timestamps, or file paths unless the owner needs them to act.
- Dollar figures with signs (+$12.30 / -$4.10); cents as 1.6c.
- A small Markdown table (at most 5 rows, 4 columns) only where comparing coins or series is clearer than prose.
