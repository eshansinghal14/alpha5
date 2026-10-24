"""Book positions placed outside ARBS (by hand on Kalshi) into a basket of
the real ledger, at their real fill cost. Stop alpha5-arbs first: the
running process holds the ledger in memory and would overwrite the edit.

    python -m kalshi_strategies.ARBS.adopt <basket_id>            # dry run
    python -m kalshi_strategies.ARBS.adopt <basket_id> --write

Takes every account fill on the basket's tickers that no ledger basket
already holds, on the leg's side only.
"""
from __future__ import annotations

import argparse
import os
import time

from kalshi_infra.kalshi.orders import KalshiOrderClient
from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_strategies.ARBS.config import SPEC_PATH
from kalshi_strategies.ARBS.ledger import Ledger
from kalshi_strategies.ARBS.spec import load_spec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("basket_id")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    b = {x.basket_id: x for x in load_spec(SPEC_PATH)}[a.basket_id]
    led = Ledger(os.path.join(LIVE_ROOT, "ARBS", "ledger.json"))
    c = KalshiOrderClient.from_account(shard=None)
    held = led.net_by_ticker()
    for leg in b.legs:
        fills = [f for f in c.fills(leg.ticker) if f["ticker"] == leg.ticker and f["side"] == leg.side]
        qty = sum(f["size"] for f in fills)
        cost = sum(f["price"] * f["size"] + f["fee"] for f in fills)
        already = abs(held.get(leg.ticker, 0.0))
        if already > 1e-9:
            raise SystemExit(f"{leg.ticker}: ledger already holds {already}; adopt by hand")
        print(f"{leg.ticker} {leg.side}: {len(fills)} fills, {qty:g} contracts, ${cost:.4f} incl fees")
        if a.write and qty > 0:
            led.record(b.basket_id, b.tickers, leg.ticker, leg.side, qty, cost, time.time())
            for f in fills:
                led.seen_trades.add(f"{f['order_id']}:{f['trade_id']}")
    if a.write:
        led.save(time.time())
        pos = led.baskets[b.basket_id]
        print(f"written: {pos.bundles(b.tickers):g} bundles, cost ${pos.cost():.4f}, unhedged {pos.unhedged(b.tickers)}")
    else:
        print("dry run; --write to book it")


if __name__ == "__main__":
    main()
