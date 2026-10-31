You are reviewing LIVE Kalshi trading on the alpha5 box (repo /opt/alpha5, conventions in CLAUDE.md).
This is a READ-ONLY job: you cannot and must not edit files, touch processes, or place/cancel orders.

Strategies:
- KXCRYPTOLEAD15M (service alpha5-crypto): quotes 15-minute crypto "lead" markets (BTC/ETH/SOL/HYPE/XRP), Kalshi shard 2.
- KXINXDUD (service alpha5-spx): quotes the daily S&P range markets off an IB options feed (pricing hub), shard 0.
- ARBS (service alpha5-arbs): arbitrage across baskets of related markets (complete sets, ladders, ...), shard 0.
- KXINXU is deliberately stopped (shadow only); its stale summary is expected.

The digest below (on stdin) was computed just now from the perf worker's output (every real fill with its
markout vs Kalshi mid at 0/1/10/60 s and settled PnL), the ARBS ledger replay, summary.json files,
systemd and the logs. Trust its numbers; dig further only where something looks off.

Where to look for more:
- data/live/<SERIES>/<YYYY-MM-DD>/summary.json: current live state.
- data/live/<SERIES>/perf/fills.parquet: every real fill with mo0/mo1/mo10/mo60 (c/contract), pnl_c, settled, event, role.
- data/live/<SERIES>/<YYYY-MM-DD>/journal/{fills,orders,decisions,bbo}/part-*.parquet (kalshi_infra.runtime.journal.read_journal).
- data/live/ARBS/journal/<date>.jsonl, data/live/ARBS/ledger.json; kalshi_viz.arbs_pnl.
- Logs: data/live/{run,spx,arbs}.{out,err}.log (no timestamps; use tail). journalctl -u alpha5-<name>.
- Run analysis with /opt/alpha5/.venv/bin/python (cwd /opt/alpha5). The box has 7.7 GB shared with live trading
  and this job is capped at ~1.4 GB: ALWAYS read parquet with a column subset and filter to the tickers/times you
  need; never load a whole day of bbo or decisions (millions of rows).

Your reply is emailed as plain text. Its FIRST two lines must be exactly:
STATUS: OK            (or STATUS: ALERT)
SUBJECT: <one short line, under 80 characters, plain ASCII>
then a blank line, then the body. No markdown tables; short lines; dollar figures with signs.
