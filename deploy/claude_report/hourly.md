HOURLY CHECK of the last hour. Be brief: this runs every hour and only an ALERT is emailed.

STATUS: ALERT only for something seriously wrong that the owner should act on now, e.g.:
- a trading service down, crash-looping, or its summary.json stale (> 2 min) while it should be trading
  (the coin race and ARBS trade around the clock);
- armed (arm_requested) but not live for more than a few minutes, a halt, a loss-limit trip, real_lock set;
- repeated order errors, failed cancels, or 429 storms; resting quotes that may be stuck on the book;
- a position audit / reconcile mismatch that persists across checks (a single "re-checking next audit" is normal);
- a loss in the hour well beyond normal (crypto events normally swing about -$40 to +$30; ARBS bursts a few $);
- memory available under ~700 MB or disk over 90%.
- LIPFARM (cheap/expensive-side LIP quoting, alpha5-lipfarm): read data/live/lipfarm_monitor.log (a
  5-minute watchdog; a SHUTDOWN line means it disarmed the farm), data/live/LIPFARM/status.json and the
  fills in data/live/LIPFARM/live/state.json. ALERT on a SHUTDOWN, on any fill marked worse than about -$20,
  on the farm armed but quoting 0 markets for 15+ minutes, or on its status.json going stale. For a bad
  fill, say what happened in the market (Kalshi trades around the fill time), whether the sentinel,
  2-tick dump or chase fired, and what parameter or rule would have avoided it.
Normal noise (not alerts): ARBS "market paused/closed" warnings, an occasional fast-move unwind, and
anything about series and services other than the coin race, ARBS and LIPFARM.

Earlier alerts (newest last) are listed below. Don't repeat an alert for the same unresolved issue more than
once every 3 hours unless it got worse; say "STILL:" when you do.

If STATUS is OK, the body is one or two lines. If ALERT, the body says what is wrong, the evidence (numbers,
log lines), what it is costing, and what the owner should check or do.
