"""How late does CME crypto futures data reach the box through IB Gateway?

rec DUR OUT: records front-month MBT/MET through IB tick-by-tick (BidAsk + AllLast,
4 of the 5 tick-by-tick slots) plus reqMktData for comparison. Every event is
stamped with box wall time (chrony-synced).
ana OUT (run on the desktop; databento is not installed on the box): pulls the same window's CME trades from Databento (match-engine
ts_event, about $0.01/hour) and pairs each IB AllLast print with its CME trade
on (price, size, order). recv - ts_event is IB's full transit. Subtracting it
from the direct-CME lead (ts_event-based) gives what is left via IB.

Needs the CME (Level 1) market data subscription on the IB account. Without it,
tick-by-tick fails with error 10189 and reqMktData with 354 (seen 2026-10-21)."""
import asyncio, json, os, sys, time
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../../../..")
from kalshi_infra import keys

ROOTS = ("MBT", "MET")
# The hub and its expiries use offsets 0..n, research uses +10, and the watchdog uses +50.
CLIENT_OFFSET = 60


async def rec(dur: float, out: str) -> None:
    from ib_async import IB, Future, StartupFetchNONE
    os.makedirs(out, exist_ok=True)
    ib = IB()
    rows, errs = [], []
    ib.errorEvent += lambda rid, code, msg, *a: errs.append((time.time(), rid, code, msg))
    await ib.connectAsync(keys.IB_HOST, keys.IB_PORT, clientId=keys.IB_CLIENT_ID + CLIENT_OFFSET,
                          readonly=True, fetchFields=StartupFetchNONE)
    meta = {}
    for root in ROOTS:
        cds = await ib.reqContractDetailsAsync(Future(symbol=root, exchange="CME", currency="USD"))
        c = sorted(cds, key=lambda d: d.contract.lastTradeDateOrContractMonth)[0].contract
        meta[root] = c.localSymbol
        for kind in ("BidAsk", "AllLast"):
            t = ib.reqTickByTickData(c, kind, 0, False)

            def on(tk, root=root, kind=kind):
                now = time.time()
                for x in tk.tickByTicks:
                    if kind == "BidAsk":
                        rows.append((now, root, "tbt_bbo", x.time.timestamp(), x.bidPrice, x.askPrice, x.bidSize, x.askSize))
                    else:
                        rows.append((now, root, "tbt_trade", x.time.timestamp(), x.price, np.nan, x.size, np.nan))
                tk.tickByTicks.clear()
            t.updateEvent += on
        m = ib.reqMktData(c, "", False, False)
        m.updateEvent += lambda tk, root=root: rows.append(
            (time.time(), root, "mkt", np.nan, tk.bid, tk.ask, tk.bidSize, tk.askSize))
    print(f"[ib_cme] recording {meta} for {dur:.0f}s", flush=True)
    await asyncio.sleep(dur)
    ib.disconnect()
    df = pd.DataFrame(rows, columns=["recv_ts", "root", "kind", "ib_ts", "px", "px2", "sz", "sz2"])
    df.to_parquet(f"{out}/ib.parquet")
    json.dump({"contracts": meta, "errors": errs[-50:]}, open(f"{out}/meta.json", "w"), default=str)
    print(df.groupby(["root", "kind"]).size().to_string(), flush=True)
    bad = [e for e in errs if e[2] in (354, 10189, 10190, 10197)]
    if bad:
        print("[ib_cme] market data errors:", bad[:4], flush=True)


def ana(out: str) -> None:
    import databento as db
    ib = pd.read_parquet(f"{out}/ib.parquet")
    meta = json.load(open(f"{out}/meta.json"))
    s0, s1 = ib.recv_ts.min() - 5, ib.recv_ts.max() + 5
    store = db.Historical(keys.DATABENTO_API_KEY).timeseries.get_range(
        dataset="GLBX.MDP3", schema="trades", symbols=list(meta["contracts"].values()), stype_in="raw_symbol",
        start=pd.Timestamp(s0, unit="s", tz="UTC"), end=pd.Timestamp(s1, unit="s", tz="UTC"))
    cme = store.to_df(map_symbols=True)
    cme["ts"] = cme.ts_event.astype("int64") / 1e9
    for root, sym in meta["contracts"].items():
        a = ib[(ib.root == root) & (ib.kind == "tbt_trade")].sort_values("recv_ts")
        b = cme[cme.symbol == sym].sort_values("ts")
        # IB's AllLast tick time is whole seconds, so match on price and size within the same second, in order.
        b = b.assign(sec=np.floor(b.ts), k=b.groupby([np.floor(b.ts), "price", "size"]).cumcount())
        a = a.assign(sec=np.floor(a.ib_ts), k=a.groupby([np.floor(a.ib_ts), "px", "sz"]).cumcount())
        m = a.merge(b, left_on=["sec", "px", "sz", "k"], right_on=["sec", "price", "size", "k"])
        lat = (m.recv_ts - m.ts) * 1e3
        print(f"{root}: {len(m)}/{len(a)} IB trades matched to CME; IB transit ms "
              f"p10 {lat.quantile(.1):.1f} p50 {lat.median():.1f} p90 {lat.quantile(.9):.1f} p99 {lat.quantile(.99):.1f}")
        for kind in ("tbt_bbo", "mkt"):
            g = ib[(ib.root == root) & (ib.kind == kind)].recv_ts.diff().dropna() * 1e3
            if len(g):
                print(f"  {kind}: {len(g)+1} updates, inter-arrival ms p10 {g.quantile(.1):.1f} p50 {g.median():.1f}")


if __name__ == "__main__":
    if sys.argv[1] == "rec":
        asyncio.run(rec(float(sys.argv[2]), sys.argv[3]))
    else:
        ana(sys.argv[2])
