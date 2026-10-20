# alpha5 docs

Long-form "why", migrations, and architecture notes that would otherwise
bloat code comments. See `../CLAUDE.md` for the comment-density policy:
docstrings and inline comments stay short (fact + reason), narrative goes
here.

## Index

- [`live-trading.md`](live-trading.md) — how a live session works: layout,
  config, what comes from Kalshi, modes, guards, quoting (edge + LIP
  quotes), rate limit, rollover.
- [`adding-a-strategy.md`](adding-a-strategy.md) — the contract a new
  `kalshi_strategies/<FOLDER>` implements.
- [`CHANGES.md`](CHANGES.md) — reverse-chronological change log. Add
  one dated section per material change; keep entries short, link to
  deeper write-ups here.
- [`ec2-spx.md`](ec2-spx.md) — running the S&P strategy and IB Gateway
  (docker, IBC) on the EC2 box next to crypto; sizing, 2FA, one dashboard.
- [`ib-migration.md`](ib-migration.md) — the S&P option feed: IB SPXW,
  the only option-data source. Live-feed architecture, IB contract spec,
  line budget.

## Quick start

```
python -m kalshi_strategies.run --series KXINX KXCRYPTOLEAD15M   # shadow; KXINX = all S&P series
streamlit run kalshi_viz/app.py
python -m pytest kalshi_infra/tests kalshi_strategies -q
```

All commands run from the repo root.

## Adding a doc

Keep docs focused. One topic per file. Favor:

- **Why** choices were made (the thing code can't say for itself)
- **What was measured** and against what (per CLAUDE.md numbers rule)
- **What is deliberately NOT built**, with a trigger that would change
  that
- Pointers back into code via `path/to/file.py:line`

Avoid turning docs into narratives. If an incident is the only reason a
value exists, that's one line in the code with a one-section-max
reference here. Don't retell the full debugging story.
