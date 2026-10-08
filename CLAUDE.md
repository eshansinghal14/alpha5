# alpha5 conventions

## Comments

Comment density should scale with how surprising the code is, not with how
important it feels.

**Don't comment** straightforward code. No comment for a getter, a clamp, a
sort, a dataclass field whose name already says it, or a line that restates
what the code plainly does. If a reader who knows Python can see it, skip it.

**Do comment** the non-obvious:
- a workaround for a real bug, with the symptom that motivated it
- a choice that looks wrong until you know why (e.g. why the union of
  call/put bounds, not the average)
- a value calibrated from real data, with what it was calibrated against
- an invariant a future edit could silently break

Keep it to the fact and the reason. One or two sentences. A comment that
recounts the full debugging story, cites several ticker/date incidents, or
re-derives the arithmetic below it is too long — the reason survives, the
narrative doesn't. Prefer a short "why", not a transcript.

Same for docstrings: say what it returns and anything a caller could get
wrong. Don't restate the signature.

## Numbers

Any constant that came from a measurement (latency, fees, basis, thresholds)
should say what it was measured against and when, so it can be re-derived
rather than trusted on faith.
