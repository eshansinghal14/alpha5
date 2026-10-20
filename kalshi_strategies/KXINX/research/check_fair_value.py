"""
Sanity-check the live IB pricing path against Kalshi's own market.

Runs the production LiveIBFeed -> compute_live_bucket_probs path (no
strategy, no orders) and every --interval-s compares each priced bucket
to Kalshi's REST BBO. Also reports the put-call-parity forward
F = K + C - P per strike, which should agree across strikes if the
option book is coherent.

Usage:
    python -m kalshi_strategies.KXINX.research.check_fair_value --series KXINXU [--seconds 60]
"""
import argparse
import asyncio
import statistics

import numpy as np
import pandas as pd

from kalshi_infra import keys
from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import load
from kalshi_infra.kalshi import markets as km
from kalshi_infra import ib_functions as ibf
from kalshi_strategies.KXINX import _pick_event
from kalshi_strategies.KXINX.config import CONFIG_PATH, HUB_KEY, HubConfig, SpxConfig
from kalshi_strategies.KXINX.ib_feed import HoldParams, LiveIBFeed
from kalshi_strategies.KXINX.live_pricing import compute_live_bucket_probs


def _kalshi_bbo(event_ticker: str) -> dict[str, tuple[float, float]]:
    data = kalshi_get("/trade-api/v2/markets", params={"event_ticker": event_ticker, "limit": 200})
    out = {}
    for m in data.get("markets", []):
        try:
            bid, ask = float(m.get("yes_bid_dollars")), float(m.get("yes_ask_dollars"))
        except (TypeError, ValueError):
            continue
        if 0 < bid < ask < 1:
            out[m["ticker"]] = (bid, ask)
    return out


def _parity_forwards(book) -> list[tuple[float, float]]:
    cm, _, _, pm, _, _ = book.snapshot_arrays()
    puts = dict(zip(book.put_strikes, pm))
    out = []
    for k, c in zip(book.call_strikes, cm):
        p = puts.get(k)
        if p is not None and not np.isnan(c) and not np.isnan(p):
            out.append((float(k), float(k + c - p)))
    return out


async def main(series: str, seconds: float, interval_s: float):
    cfg = load(SpxConfig, CONFIG_PATH, series)
    ev = _pick_event(km.open_events(series), cfg)
    if ev is None:
        raise SystemExit(f"no tradeable {series} event right now")
    event = ev.event_ticker
    h = load(HubConfig, CONFIG_PATH, HUB_KEY)
    scope = ibf.select_event_buckets(event, cfg.quoting.max_markets or 5)
    resolved = ibf.resolve_event_strikes(event, tickers=scope)
    # +10 so this never collides with the hub's clientId.
    feed = LiveIBFeed(resolved["expiry"], resolved["strikes"], client_id=keys.IB_CLIENT_ID + 10 + h.ib_client_id_offset,
                      hold=HoldParams(h.hold_baseline_s, h.hold_multiplier, h.hold_settle_s,
                                      h.hold_min_samples, h.hold_sample_s),
                      reconnect_delay_s=h.ib_reconnect_delay_s)
    feed.markets = resolved["markets"]
    print(f"[fair] {event}: scoped to {len(scope)} ATM buckets")
    task = asyncio.create_task(feed.run())
    loop = asyncio.get_running_loop()

    rows = []          # (ticker, lo, mid, hi, k_bid, k_ask)
    fwd_spreads = []
    try:
        await asyncio.sleep(5.0)   # connect + first quotes
        t_end = loop.time() + seconds
        while loop.time() < t_end:
            feed.drain()
            b = feed.book
            arr = b.snapshot_arrays()
            probs = compute_live_bucket_probs(feed.markets, b.call_strikes, arr[0], arr[1], arr[2],
                                              b.put_strikes, arr[3], arr[4], arr[5],
                                              monotone=cfg.pricing.monotone_levels)
            bbo = await loop.run_in_executor(None, _kalshi_bbo, event)
            fwds = _parity_forwards(b)
            if fwds:
                vals = [f for _, f in fwds]
                fwd_spreads.append(max(vals) - min(vals))
            print(f"\n[fair] {len(probs)} priced, {len(bbo)} quoted on Kalshi, "
                  f"forward {statistics.median([f for _, f in fwds]) if fwds else float('nan'):.2f}")
            print(f"  {'bucket':>12} {'lo':>6} {'mid':>6} {'hi':>6} | {'k_bid':>5} {'k_ask':>5} {'k_mid':>6} {'diff':>7}")
            for t in sorted(probs, key=lambda t: abs(probs[t][1] - 0.5))[:12]:
                lo, mid, hi = probs[t]
                if t not in bbo:
                    continue
                kb, ka = bbo[t]
                rows.append((t, lo, mid, hi, kb, ka))
                print(f"  {t.split('-')[-1]:>12} {lo:6.3f} {mid:6.3f} {hi:6.3f} | "
                      f"{kb:5.2f} {ka:5.2f} {(kb + ka) / 2:6.3f} {mid - (kb + ka) / 2:+7.3f}")
            await asyncio.sleep(interval_s)
    finally:
        feed.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    if not rows:
        print("\n[fair] no comparable buckets -- nothing to summarise")
        return
    diffs = [mid - (kb + ka) / 2 for _, _, mid, _, kb, ka in rows]
    inside = sum(kb <= mid <= ka for _, _, mid, _, kb, ka in rows)
    overlap = sum(hi >= kb and lo <= ka for _, lo, _, hi, kb, ka in rows)
    widths = [hi - lo for _, lo, _, hi, _, _ in rows]
    print(f"\n=== {event}: {len(rows)} bucket-samples ===")
    print(f"  mid inside Kalshi bid/ask:   {inside / len(rows):6.1%}")
    print(f"  CI overlaps Kalshi bid/ask:  {overlap / len(rows):6.1%}")
    print(f"  median (implied - k_mid):    {statistics.median(diffs):+.4f}   (signed: bias)")
    print(f"  median |implied - k_mid|:    {statistics.median(abs(d) for d in diffs):.4f}")
    print(f"  median CI width:             {statistics.median(widths):.4f}")
    if fwd_spreads:
        print(f"  parity forward spread across strikes (median): {statistics.median(fwd_spreads):.2f} pts")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", choices=("KXINXU", "KXINXDUD"), required=True)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval-s", type=float, default=5.0)
    args = ap.parse_args()
    asyncio.run(main(args.series, args.seconds, args.interval_s))
