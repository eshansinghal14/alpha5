# alpha5 docs

Long-form "why", migrations, and architecture notes that would otherwise
bloat code comments. See `../CLAUDE.md` for the comment-density policy:
docstrings and inline comments stay short (fact + reason), narrative goes
here.

## Index

- [`CHANGES.md`](CHANGES.md) — reverse-chronological change log. Add
  one dated section per material change; keep entries short, link to
  deeper write-ups here.
- [`ib-migration.md`](ib-migration.md) — switch of the live options
  stream from Databento ES-daily to IB SPXW (2026-10-05). Live-feed
  architecture, IB contract spec, line budget.
- [`live-trading.md`](live-trading.md) — overall run_live architecture:
  guards, modes, rollover, dashboard integration.

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
