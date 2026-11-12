"""Record CME crypto futures top-of-book from Databento's live feed, stamped
with box receive time, to measure how far CME leads the spot/perp venues the
race prices on (data/kalshi_crypto/perp_prints, prints/).

    python -m strategies.KXCRYPTOLEAD15M.research.latency.cme_live_rec [SYMBOLS...]

mbp-1 carries every top-of-book change and every trade (action "T"). Writes
data/kalshi_crypto/cme_rec/{day}/{ms}.parquet every FLUSH_S. With no symbols
given it follows each root's front month and the one after (venues.cme_front),
and exits when that set rolls so systemd restarts it on the new one."""
from __future__ import annotations

import os
import sys
import threading
import time

import pandas as pd

from infra import REPO_ROOT, keys
from strategies.KXCRYPTOLEAD15M.spot_feed.venues import cme_front

OUT = os.path.join(REPO_ROOT, "data", "kalshi_crypto", "cme_rec")
ROOTS = ["BTC", "MBT", "ETH", "MET", "SOL", "MSL", "XRP", "MXP"]
FLUSH_S = 60.0


def auto_symbols(now: float) -> list[str]:
    """Front month and the next for every root."""
    out = []
    for r in ROOTS:
        front = cme_front(r, now)
        nxt = cme_front(r, now + 40 * 86400)
        out += [front] + ([nxt] if nxt != front else [cme_front(r, now + 70 * 86400)])
    return out
UNDEF = 2**63 - 1


def main(symbols: list[str], auto: bool = False):
    import databento as db
    rows, lock, names = [], threading.Lock(), {}

    def on_record(r):
        now = time.time_ns()
        if isinstance(r, db.SymbolMappingMsg):
            names[r.instrument_id] = r.stype_out_symbol
            return
        if not isinstance(r, db.MBP1Msg):
            if isinstance(r, (db.ErrorMsg, db.SystemMsg)):
                print(f"[cme_rec] {type(r).__name__}: {getattr(r, 'err', None) or getattr(r, 'msg', '')}", flush=True)
            return
        lv = r.levels[0]
        px = lambda v: v / 1e9 if v != UNDEF else float("nan")
        with lock:
            rows.append((now, r.ts_event, r.ts_recv, r.ts_in_delta, r.instrument_id, str(r.action), str(r.side),
                         px(r.price), r.size, px(lv.bid_px), px(lv.ask_px), lv.bid_sz, lv.ask_sz, r.flags, r.sequence))

    client = db.Live(key=keys.DATABENTO_API_KEY)
    client.subscribe(dataset="GLBX.MDP3", schema="mbp-1", symbols=symbols, stype_in="raw_symbol")
    client.add_callback(on_record)
    client.start()
    print(f"[cme_rec] recording {len(symbols)} symbols to {OUT}", flush=True)
    cols = ["recv_ns", "ts_event", "ts_recv", "ts_in_delta", "instrument_id", "action", "side", "price", "size",
            "bid", "ask", "bid_sz", "ask_sz", "flags", "sequence"]
    while True:
        time.sleep(FLUSH_S)
        rolled = auto and auto_symbols(time.time()) != symbols
        with lock:
            batch, rows[:] = list(rows), []
        if batch:
            df = pd.DataFrame(batch, columns=cols)
            df["symbol"] = df.instrument_id.map(names)
            d = os.path.join(OUT, pd.Timestamp(batch[0][0], unit="ns", tz="UTC").strftime("%Y-%m-%d"))
            os.makedirs(d, exist_ok=True)
            df.to_parquet(os.path.join(d, f"{batch[0][0] // 1_000_000}.parquet"), index=False)
        if rolled:
            print("[cme_rec] front month rolled; exiting for a restart on the new contracts", flush=True)
            client.stop()
            return


if __name__ == "__main__":
    main(sys.argv[1:] or auto_symbols(time.time()), auto=not sys.argv[1:])
