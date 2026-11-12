DAILY REPORT for the last 24 hours, read by the owner at 8am ET.

Body, in this order, about 35 lines in total:

## Summary
Two or three one-line bullets: the day's verdict and what drove it. Lead with the conclusion.

## Coin race
Two to four bullets: what the markouts say about adverse selection, how today compares with the previous days in
the digest, and which coins differ. Say whether anything changed, not just what the numbers were.

## ARBS
Two to four bullets: where the money came from and where it leaked (which series, live vs pre-match, completion
slippage), compared with previous days.

Before writing this section, go through ARBS's trades for the day (data/live/ARBS/journal/<date>.jsonl and
kalshi_viz.arbs_pnl.bundle_log) and find the main drawdowns: the losing baskets and runs of losses that account
for most of the day's losses. For each one:
- Work out what happened: which legs filled, at what prices, what the book and the market did around the fill,
  and how completion or the exit went compared with the plan.
- Look back through the ARBS history (all the journal days and bundle_log) for the same market, series, and
  market type (live sports, esports, weather, long-dated political, ...) and the same failure shape.
- If it's a one-off with a sensible explanation (a news shock, a single thin book, a settlement quirk) and
  nothing like it shows up in the history, leave it out of the email.
- If it recurs, find out why. Is it something in our system (sizing against the thinnest leg, completion logic,
  pull timing, a pricing or edge rule), or is that market just adverse to quote (we get filled only when someone
  knows more)? Then make it an idea below. A system fix names the code path or config key. An adverse market gets
  a recommendation to exclude it, or to quote it smaller or wider, with the history that shows the pattern.

## Needs action
OMIT this section unless something needs the owner's action today (the hourly ALERT criteria). Then one bullet
per issue: what's wrong and what to do. Routine noise (one-off restarts, cleared audit re-checks, paused-market
warnings, old log lines, rate-limit warnings that didn't stop trading) is not worth mentioning.

## Ideas
At most THREE, and zero is fine. One well-tested idea beats three guesses. A recurring ARBS drawdown found
above takes priority over other ideas. Earlier reports and the owner's decisions on their ideas (replies to the
email: approved and applied, rejected, or a note) are at the end of stdin. Never re-propose a rejected idea unless
you have new evidence, and then say what's new. For an applied one, say how it's doing since.

Spend most of your effort here. Before proposing an idea:
- Read the strategy's HANDOFF and check it hasn't already been tried or rejected. If it has, only propose it
  again with a reason the earlier result no longer holds.
- Find the mechanism in the code: which config key or code path produces the behavior, and what the change is
  (e.g. `quoting.size_by_coin.SOL` 1.0 -> 0.5).
- Test it on the data: use several days of fills (fills.parquet keeps the history), not just today, and run the
  counterfactual where you can (e.g. PnL of the fills the change would have removed, by day). Give the $ per day
  it would have made or saved and how many fills that rests on.
- Name the cost: what the change gives up (volume, LIP reward, fills that were profitable).

Format each idea as:
1. **<the change, in one line, naming the config key or code path>**
   - Why: <the mechanism and the key number, one or two sentences>
   - Evidence: <the backtest or counterfactual you ran: days, fills, $/day; and the cost>
   - Confidence: high / medium / low, and what would change your mind

The owner approves ideas by replying to the email ("1 yes, 2 no"), and an approved config change is applied to
the live config.json automatically. So for each idea that is purely a config change, end the body with ONE block
listing exactly the change, numbered like the ideas (omit ideas that need code; omit the block if there are none):

```proposals
[{"n": 1, "series": "ARBS", "title": "Exclude KXVALORANTGAME",
  "changes": [{"key": "watch.exclude", "add": ["KXVALORANTGAME"]}]},
 {"n": 2, "series": "KXCRYPTOLEAD15M", "title": "Halve SOL size",
  "changes": [{"key": "quoting.size_by_coin.SOL", "set": 0.5}]}]
```
- "series" is ARBS or KXCRYPTOLEAD15M; "key" is the dotted path in that strategy's config.json, as it exists
  there now (no new keys). Use "set" for a value, and "add" / "remove" for list items.
- Propose only what you would be comfortable seeing applied live within minutes of a "yes".

STATUS: ALERT only if something needs action today (see the hourly criteria); otherwise OK.
