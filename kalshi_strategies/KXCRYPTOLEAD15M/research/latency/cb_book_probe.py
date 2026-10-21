"""Which Coinbase feed shows a touch change first?

  python cb_book_probe.py rec DUR_S OUTDIR    record every feed below, raw
  python cb_book_probe.py ana OUTDIR          compare them

Each connection runs in its own process and only stamps + appends raw frames,
so one feed's parsing never delays another's stamp. The main comparison is
receipt-to-receipt for the same best-price change (clock independent); the
exchange-time -> receipt numbers depend on chrony and are secondary. The `_b`
duplicate connections give the noise floor between two sockets on one feed.
"""
import asyncio, glob, json, multiprocessing as mp, os, sys, time

AT_URL = "wss://advanced-trade-ws.coinbase.com"
EX_URL = "wss://ws-feed.exchange.coinbase.com"
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "HYPE-USD"]


def _at(*channels):
    return [{"type": "subscribe", "channel": c, "product_ids": PRODUCTS} if c != "heartbeats"
            else {"type": "subscribe", "channel": c} for c in channels]


def _ex(*channels):
    return [{"type": "subscribe", "product_ids": PRODUCTS, "channels": list(channels)}]


FEEDS = {
    # at_l2_a matches the live adapter's connection (level2 + heartbeats + market_trades).
    "at_l2_a": (AT_URL, _at("level2", "heartbeats", "market_trades")),
    "at_l2_b": (AT_URL, _at("level2")),
    "at_ticker": (AT_URL, _at("ticker")),
    "ex_l2b_a": (EX_URL, _ex("level2_batch", "heartbeat")),
    "ex_l2b_b": (EX_URL, _ex("level2_batch")),
    "ex_ticker": (EX_URL, _ex("ticker")),
    "ex_matches": (EX_URL, _ex("matches")),
}


def _record(name, url, subs, end, outdir):
    import websockets

    async def run():
        with open(os.path.join(outdir, f"{name}.txt"), "w", buffering=1 << 20) as f:
            while time.time() < end:
                try:
                    async with websockets.connect(url, max_size=None, ping_interval=20,
                                                  ping_timeout=15, open_timeout=15) as ws:
                        for s in subs:
                            await ws.send(json.dumps(s))
                        f.write("# connect\n")
                        while (left := end - time.time()) > 0:
                            try:
                                m = await asyncio.wait_for(ws.recv(), timeout=min(left, 10))
                            except asyncio.TimeoutError:
                                if time.time() < end:
                                    f.write("# reconnect timeout\n")
                                    break
                                return
                            f.write(f"{time.time():.6f} {m}\n")
                except Exception as e:
                    f.write(f"# reconnect {e!r}\n")
                    print(name, "reconnect", repr(e)[:100], flush=True)
                    await asyncio.sleep(0.5)

    asyncio.run(run())


async def _auth_check(outdir):
    """Exchange full/level2 (unbatched) without credentials: record what it says."""
    import websockets
    out = {}
    for ch in ("level2", "full", "level3"):
        try:
            async with websockets.connect(EX_URL, max_size=None) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": [ch]}))
                out[ch] = [json.loads(await asyncio.wait_for(ws.recv(), 5))]
        except Exception as e:
            out[ch] = repr(e)
    with open(os.path.join(outdir, "auth_check.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("auth check:", json.dumps(out)[:600], flush=True)


def record(dur, outdir):
    os.makedirs(outdir, exist_ok=True)
    asyncio.run(_auth_check(outdir))
    end = time.time() + dur
    ps = [mp.Process(target=_record, args=(n, u, s, end, outdir)) for n, (u, s) in FEEDS.items()]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    print("done", {n: os.path.getsize(os.path.join(outdir, f"{n}.txt")) >> 20 for n in FEEDS}, "MiB")


# ---------------------------------------------------------------- analysis

def _iso(s):
    import datetime as dt
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def _lines(path):
    with open(path) as f:
        for ln in f:
            if ln.startswith("#"):
                yield None, None
                continue
            t, _, m = ln.partition(" ")
            yield float(t), m


class _Book:
    def __init__(self):
        from sortedcontainers import SortedDict
        self.b, self.a = SortedDict(), SortedDict()
        self.synced = False

    def set(self, side, p, q):
        d = self.b if side == "b" else self.a
        if q == 0:
            d.pop(p, None)
        else:
            d[p] = q

    def touch(self):
        return (self.b.peekitem(-1)[0] if self.b else None, self.a.peekitem(0)[0] if self.a else None)


def _book_changes(name, path):
    """Rows (coin, side, recv, price, event_ts) each time a best price changes, plus
    message rows (coin, recv, exch_ts) for receipt-delay stats. Advanced Trade's
    message `timestamp` is its send time (a flat ~6.6ms before receipt, 10-21),
    so its book age comes from the newest per-update `event_time` instead."""
    import orjson
    books, last, ch, msgs = {}, {}, [], []
    at = name.startswith("at_")
    for recv, m in _lines(path):
        if recv is None:          # reconnect: wait for the next snapshot
            for bk in books.values():
                bk.synced = False
            continue
        d = orjson.loads(m)
        touched = set()
        if at:
            if d.get("channel") != "l2_data":
                continue
            ets = _iso(d.get("timestamp"))
            for ev in d.get("events") or []:
                coin = ev["product_id"].split("-")[0]
                bk = books.setdefault(coin, _Book())
                if ev["type"] == "snapshot":
                    bk.b.clear(); bk.a.clear(); bk.synced = True
                ups = ev.get("updates") or []
                for u in ups:
                    bk.set("b" if u["side"] == "bid" else "a", float(u["price_level"]), float(u["new_quantity"]))
                touched.add(coin)
                if ev["type"] == "update" and ups:
                    msgs.append((coin, recv, ets, _iso(max(u["event_time"] for u in ups))))
        else:
            ty = d.get("type")
            if ty not in ("snapshot", "l2update"):
                continue
            coin = d["product_id"].split("-")[0]
            bk = books.setdefault(coin, _Book())
            if ty == "snapshot":
                bk.b.clear(); bk.a.clear(); bk.synced = True
                for p, q in d["bids"]:
                    bk.set("b", float(p), float(q))
                for p, q in d["asks"]:
                    bk.set("a", float(p), float(q))
            else:
                for s, p, q in d["changes"]:
                    bk.set("b" if s == "buy" else "a", float(p), float(q))
                t = _iso(d.get("time"))
                msgs.append((coin, recv, t, t))
            touched.add(coin)
        for coin in touched:
            bk = books[coin]
            if not bk.synced:
                continue
            tb, ta = bk.touch()
            pb, pa = last.get(coin, (None, None))
            if tb != pb and tb is not None:
                ch.append((coin, "b", recv, tb))
            if ta != pa and ta is not None:
                ch.append((coin, "a", recv, ta))
            last[coin] = (tb, ta)
    return ch, msgs


def _ticker_changes(name, path):
    import orjson
    last, ch, msgs = {}, [], []
    at = name.startswith("at_")
    for recv, m in _lines(path):
        if recv is None:
            continue
        d = orjson.loads(m)
        if at:
            if d.get("channel") != "ticker":
                continue
            ets = _iso(d.get("timestamp"))
            rows = [(t["product_id"], t.get("best_bid"), t.get("best_ask")) for ev in d.get("events") or []
                    for t in ev.get("tickers") or []]
        else:
            if d.get("type") != "ticker":
                continue
            ets = _iso(d.get("time"))
            rows = [(d["product_id"], d.get("best_bid"), d.get("best_ask"))]
        for pid, bb, ba in rows:
            coin = pid.split("-")[0]
            if not bb or not ba:
                continue
            tb, ta = float(bb), float(ba)
            pb, pa = last.get(coin, (None, None))
            if tb != pb:
                ch.append((coin, "b", recv, tb))
            if ta != pa:
                ch.append((coin, "a", recv, ta))
            last[coin] = (tb, ta)
            msgs.append((coin, recv, ets, ets))
    return ch, msgs


def _prints(name, path):
    import orjson
    out = []
    for recv, m in _lines(path):
        if recv is None:
            continue
        d = orjson.loads(m)
        if name == "ex_matches":
            if d.get("type") == "match":
                out.append((d["product_id"].split("-")[0], str(d["trade_id"]), recv, _iso(d["time"])))
        elif d.get("channel") == "market_trades":
            for ev in d.get("events") or []:
                if ev.get("type") != "update":
                    continue
                for t in ev.get("trades") or []:
                    out.append((t["product_id"].split("-")[0], str(t["trade_id"]), recv, _iso(t["time"])))
    return out


def analyze(outdir):
    import numpy as np, pandas as pd
    pd.set_option("display.width", 250)
    q = lambda s: pd.Series({"n": len(s), "p10": s.quantile(.1), "p25": s.quantile(.25), "p50": s.median(),
                             "p75": s.quantile(.75), "p90": s.quantile(.9), "p99": s.quantile(.99)})
    C, M = {}, {}
    for name in ("at_l2_a", "at_l2_b", "ex_l2b_a", "ex_l2b_b"):
        c, m = _book_changes(name, os.path.join(outdir, f"{name}.txt"))
        C[name] = pd.DataFrame(c, columns=["coin", "side", "recv", "price"])
        M[name] = pd.DataFrame(m, columns=["coin", "recv", "exch", "event"])
        print(name, len(c), "touch changes", len(m), "book msgs", flush=True)
    for name in ("at_ticker", "ex_ticker"):
        c, m = _ticker_changes(name, os.path.join(outdir, f"{name}.txt"))
        C[name] = pd.DataFrame(c, columns=["coin", "side", "recv", "price"])
        M[name] = pd.DataFrame(m, columns=["coin", "recv", "exch", "event"])
        print(name, len(c), "touch changes", flush=True)

    def lead(a, b, tol=1.0):
        """For each best-price change on `b`, the nearest change to the same price
        on `a` (within tol s): ms that `b` showed it after `a` (+ = a first)."""
        A, B = C[a].sort_values("recv"), C[b].sort_values("recv")
        A = A.rename(columns={"recv": "ra"})
        r = pd.merge_asof(B, A, left_on="recv", right_on="ra", by=["coin", "side", "price"],
                          direction="nearest", tolerance=tol)
        r["ms"] = (r.recv - r.ra) * 1e3
        return r

    pairs = [("at_l2_a", "at_l2_b", "noise floor: two Advanced Trade level2 sockets"),
             ("ex_l2b_a", "ex_l2b_b", "noise floor: two Exchange level2_batch sockets"),
             ("at_l2_a", "ex_l2b_a", "MAIN: Exchange level2_batch after Advanced Trade level2"),
             ("ex_l2b_a", "at_l2_a", "same, matched from the Advanced Trade side (Exchange first = +)"),
             ("at_l2_a", "at_ticker", "Advanced Trade ticker after AT level2"),
             ("at_l2_a", "ex_ticker", "Exchange ticker after AT level2")]
    res = {}
    for a, b, title in pairs:
        r = lead(a, b)
        res[(a, b)] = r
        hit = r.ms.notna()
        print(f"\n=== {title}  ({b} - {a}, ms; matched {hit.mean():.0%} of {len(r)}) ===")
        g = r[hit].groupby("coin").ms
        t = g.apply(q).unstack()
        t["a_first"] = g.apply(lambda s: (s > 0.5).mean())
        t["b_first"] = g.apply(lambda s: (s < -0.5).mean())
        allr = q(r.ms[hit]); allr["a_first"] = (r.ms[hit] > .5).mean(); allr["b_first"] = (r.ms[hit] < -.5).mean()
        t.loc["ALL"] = allr
        print(t.round(2).to_string())

    # Per-coin change counts: batched feeds collapse intermediate touches.
    cnt = pd.DataFrame({n: C[n].groupby("coin").size() for n in C})
    print("\n=== best-price changes seen per feed (fewer = intermediate touches collapsed) ===")
    print(cnt.to_string())

    for col, title in (("exch", "message timestamp -> receipt (AT: send time; Exchange: batch time)"),
                       ("event", "newest book event_time in the message -> receipt (the book's age on arrival)")):
        print(f"\n=== {title}, ms; depends on box clock ===")
        rows = []
        for n, m in M.items():
            m = m.dropna(subset=[col])
            for coin, g in m.groupby("coin"):
                rows.append(dict(feed=n, coin=coin, **q((g.recv - g[col]) * 1e3)))
        print(pd.DataFrame(rows).set_index(["coin", "feed"]).sort_index().round(1).to_string())

    print("\n=== message gaps per coin (ms between book messages): how stale the book can sit ===")
    rows = []
    for n in ("at_l2_a", "ex_l2b_a"):
        for coin, g in M[n].groupby("coin"):
            s = g.recv.sort_values().diff().dropna() * 1e3
            rows.append(dict(feed=n, coin=coin, msgs_per_s=len(g) / (g.recv.max() - g.recv.min()), **q(s)))
    print(pd.DataFrame(rows).set_index(["coin", "feed"]).sort_index().round(1).to_string())

    P = {n: pd.DataFrame(_prints(n, os.path.join(outdir, f"{f}.txt")), columns=["coin", "tid", "recv", "exch"])
         for n, f in (("ex_matches", "ex_matches"), ("at_trades", "at_l2_a"))}
    J = P["ex_matches"].merge(P["at_trades"], on=["coin", "tid"], suffixes=("_ex", "_at"))
    J["ms"] = (J.recv_at - J.recv_ex) * 1e3
    print(f"\n=== prints, same trade id: AT market_trades receipt - Exchange matches receipt (ms), n={len(J)} ===")
    print(J.groupby("coin").ms.apply(q).unstack().round(1).to_string())
    for n, p in P.items():
        p = p.dropna()
        print(f"{n} trade time -> receipt p50/p90 by coin (ms):",
              {c: (round(g.median(), 1), round(g.quantile(.9), 1))
               for c, g in ((p.recv - p.exch) * 1e3).groupby(p.coin)})

    pd.concat({f"{a}|{b}": r for (a, b), r in res.items()}).to_parquet(os.path.join(outdir, "lead.parquet"))


if __name__ == "__main__":
    if sys.argv[1] == "rec":
        record(float(sys.argv[2]), sys.argv[3])
    else:
        analyze(sys.argv[2])
