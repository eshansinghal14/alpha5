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

## Needs action
OMIT this section unless something needs the owner's action today (the hourly ALERT criteria). Then one bullet
per issue: what's wrong and what to do. Routine noise (one-off restarts, cleared audit re-checks, paused-market
warnings, old log lines, rate-limit warnings that didn't stop trading) is not worth mentioning.

## Ideas
At most THREE, and zero is fine. One well-tested idea beats three guesses. Earlier reports' ideas are at the end
of stdin: don't repeat one unless you have new evidence, and say if a past idea was adopted and how it's doing.

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

STATUS: ALERT only if something needs action today (see the hourly criteria); otherwise OK.
