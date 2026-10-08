"""
Smoke test + latency measurement for the IB SPXW option feed.

What we measure (per CLAUDE.md: say what it was measured against):
  * per-contract inter-tick cadence (seconds between consecutive updates
    where bid or ask actually moved)
  * aggregate records/s across the subscribed set
  * tickers that produced zero ticks over the window (= OPRA subscribed
    but strike simply didn't move)

IB does NOT send a server-side exchange timestamp on streaming option
quotes, so this is NOT absolute wire-to-process latency. It is the
"how fresh is my book" number the signal loop actually cares about,
directly comparable to LiveIBFeed.records_per_s().

Universe: ATM +/- 5 SPXW 0DTE strikes, both C and P (22 contracts, well
under IB's 100-line default). ATM is picked from DELAYED SPX cash so
this works without the CBOE Streaming Indexes subscription.

Usage:
    python -m kalshi_infra.measure_ib_latency [--seconds 30] [--atm 7770]
"""
import argparse
import collections
import datetime
import math
import os
import statistics
import sys
import time

from kalshi_infra import keys


def _resolve_atm(ib) -> float:
    """ATM strike via delayed SPX cash. Delayed because CBOE Streaming
    Market Indexes is a paid subscription and we don't need live SPX for
    strike selection -- the index moves <$5 per 2s snapshot anyway."""
    from ib_async import Index

    ib.reqMarketDataType(3)   # 3 = DELAYED
    spx = Index("SPX", "CBOE", "USD")
    ib.qualifyContracts(spx)
    t = ib.reqMktData(spx, "", snapshot=False, regulatorySnapshot=False)
    for _ in range(20):     # up to 4s of 200ms polls
        ib.sleep(0.2)
        if not math.isnan(t.marketPrice()):
            break
    ib.cancelMktData(spx)
    ib.reqMarketDataType(1)   # back to LIVE for the OPRA leg
    px = t.marketPrice()
    if math.isnan(px):
        raise RuntimeError("could not get even a DELAYED SPX print")
    return 5.0 * round(px / 5.0)


def run(seconds: float, atm: float | None):
    from ib_async import IB, Option
    from kalshi_infra import ib_functions

    ib_functions.quiet_ib_logging()
    ib = IB()
    ib.connect(keys.IB_HOST, keys.IB_PORT, clientId=keys.IB_CLIENT_ID, timeout=5,
               readonly=keys.IB_READONLY)
    try:
        if atm is None:
            atm = _resolve_atm(ib)
            print(f"[measure_ib_latency] ATM resolved from DELAYED SPX: {atm}")
        else:
            print(f"[measure_ib_latency] ATM pinned to {atm} (via --atm)")

        today = datetime.date.today().strftime("%Y%m%d")
        strikes = [atm + 5.0 * d for d in range(-5, 6)]
        contracts = [
            Option("SPXW", today, k, r, "CBOE", "100", "USD", tradingClass="SPXW")
            for k in strikes
            for r in ("C", "P")
        ]
        qualified = ib.qualifyContracts(*contracts)
        qualified = [c for c in qualified if getattr(c, "conId", 0)]
        print(f"[measure_ib_latency] qualified {len(qualified)}/{len(contracts)} contracts "
              f"({today} SPXW, strikes {strikes[0]:.0f}-{strikes[-1]:.0f})")
        if not qualified:
            raise RuntimeError("no SPXW contracts qualified -- 0DTE chain not listed?")

        # Per-contract: (last_bid, last_ask, last_tick_t, [inter_tick_dts])
        state: dict[int, dict] = {}
        global_times: collections.deque = collections.deque()

        def on_tick(ticker):
            now = time.monotonic()
            cid = ticker.contract.conId
            s = state.setdefault(cid, {
                "localSymbol": ticker.contract.localSymbol,
                "strike": ticker.contract.strike,
                "right": ticker.contract.right,
                "last_bid": math.nan,
                "last_ask": math.nan,
                "last_t": None,
                "dts": [],
                "n_ticks": 0,
            })
            bid = ticker.bid if (ticker.bid is not None and ticker.bid > 0) else math.nan
            ask = ticker.ask if (ticker.ask is not None and ticker.ask > 0) else math.nan
            # Only count ticks where bid or ask changed. IB fires updateEvent
            # on sizes and greeks too; those aren't what the signal loop
            # repaints the book from.
            def _eq_nan(a, b):
                if math.isnan(a) and math.isnan(b):
                    return True
                return a == b
            moved = not (_eq_nan(bid, s["last_bid"]) and _eq_nan(ask, s["last_ask"]))
            if not moved:
                return
            s["last_bid"], s["last_ask"] = bid, ask
            if s["last_t"] is not None:
                s["dts"].append(now - s["last_t"])
            s["last_t"] = now
            s["n_ticks"] += 1
            global_times.append(now)

        tickers = []
        for c in qualified:
            tk = ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
            tk.updateEvent += on_tick
            tickers.append((c, tk))

        print(f"[measure_ib_latency] subscribed; collecting for {seconds:.0f}s...")
        t0 = time.monotonic()
        ib.sleep(seconds)
        total_wall = time.monotonic() - t0

        for c, _ in tickers:
            ib.cancelMktData(c)

        # Report
        print()
        print(f"=== IB SPXW latency smoke test, {today}, {len(qualified)} contracts, "
              f"{total_wall:.1f}s wall ===")
        total_ticks = sum(s["n_ticks"] for s in state.values())
        print(f"total price-moving ticks: {total_ticks}  "
              f"(aggregate: {total_ticks / total_wall:.1f} ticks/s)")

        active = [s for s in state.values() if s["n_ticks"] > 0]
        inactive = [c for c in qualified if c.conId not in state or state[c.conId]["n_ticks"] == 0]
        print(f"active contracts: {len(active)} / {len(qualified)}  "
              f"(inactive: {len(inactive)})")

        if active:
            all_dts = [dt for s in active for dt in s["dts"]]
            if all_dts:
                all_dts.sort()
                def pct(p):
                    return all_dts[min(len(all_dts) - 1, int(p * len(all_dts)))]
                print(f"\n-- inter-tick gap, pooled across active contracts "
                      f"(N={len(all_dts)}) --")
                print(f"  min:    {all_dts[0] * 1000:7.1f} ms")
                print(f"  p10:    {pct(0.10) * 1000:7.1f} ms")
                print(f"  p50:    {pct(0.50) * 1000:7.1f} ms")
                print(f"  p90:    {pct(0.90) * 1000:7.1f} ms")
                print(f"  p99:    {pct(0.99) * 1000:7.1f} ms")
                print(f"  max:    {all_dts[-1] * 1000:7.1f} ms")
                print(f"  mean:   {statistics.mean(all_dts) * 1000:7.1f} ms")

        # Per-contract table, sorted by tick count desc
        print(f"\n-- per-contract (top 10 by tick count) --")
        print(f"  {'strike':>7} {'R':>1}  {'n':>6}  {'rate/s':>7}  {'median_dt_ms':>12}")
        for s in sorted(active, key=lambda s: -s["n_ticks"])[:10]:
            rate = s["n_ticks"] / total_wall
            med = (statistics.median(s["dts"]) * 1000) if s["dts"] else float("nan")
            print(f"  {s['strike']:>7.0f} {s['right']:>1}  {s['n_ticks']:>6}  "
                  f"{rate:>7.2f}  {med:>12.1f}")

        if inactive:
            print(f"\n-- inactive strikes (no price-moving tick in {total_wall:.0f}s) --")
            inactive_syms = sorted({(c.strike, c.right) for c in inactive})
            print("  " + ", ".join(f"{k:.0f}{r}" for k, r in inactive_syms))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="How long to collect ticks for (default: 30).")
    ap.add_argument("--atm", type=float, default=None,
                    help="Pin ATM strike instead of resolving from SPX cash.")
    args = ap.parse_args()
    run(args.seconds, args.atm)
