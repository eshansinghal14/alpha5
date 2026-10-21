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
    # The live adapter's Exchange connection, one socket: ordering between
    # its ticker and level2_batch messages is what the adapter sees.
    "ex_live": (EX_URL, _ex("matches", "heartbeat", "level2_batch", "ticker")),
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


def analyze_live(outdir):
    """The ex_live socket alone: ticker vs level2_batch on one connection.
      1. ticker touch first vs book first, and by how long (same coin, side, price)
      2. l2update `time` vs the ticker time of the touch it first shows
      3. how far book content trails the connection's newest exchange time
         (bounds how long an unchanged book can be called current)
      4. newest-of(ticker, book) touch vs the book alone, by exchange time."""
    import orjson, numpy as np, pandas as pd
    pd.set_option("display.width", 250)
    q = lambda s: pd.Series({"n": len(s), "p50": s.median(), "p90": s.quantile(.9), "p99": s.quantile(.99),
                             "p999": s.quantile(.999), "max": s.max()})
    books, last_b, last_t = {}, {}, {}
    rows_b, rows_t, lag, clock = [], [], [], -1.0
    for recv, m in _lines(os.path.join(outdir, "ex_live.txt")):
        if recv is None:
            books.clear(); last_b.clear(); clock = -1.0
            continue
        d = orjson.loads(m)
        ty = d.get("type")
        ets = _iso(d.get("time"))
        if ty in ("snapshot", "l2update"):
            coin = d["product_id"].split("-")[0]
            bk = books.setdefault(coin, _Book())
            if ty == "snapshot":
                bk.b.clear(); bk.a.clear(); bk.synced = True
                for p, s in d["bids"]:
                    bk.set("b", float(p), float(s))
                for p, s in d["asks"]:
                    bk.set("a", float(p), float(s))
            else:
                for s, p, z in d["changes"]:
                    bk.set("b" if s == "buy" else "a", float(p), float(z))
                if clock > 0:
                    lag.append((coin, (clock - ets) * 1e3))
            if bk.synced:
                tb, ta = bk.touch()
                pb, pa = last_b.get(coin, (None, None))
                for side, new, old in (("b", tb, pb), ("a", ta, pa)):
                    if new != old and new is not None:
                        rows_b.append((coin, side, recv, ets, new))
                last_b[coin] = (tb, ta)
        elif ty == "ticker":
            coin = d["product_id"].split("-")[0]
            try:
                tb, ta = float(d["best_bid"]), float(d["best_ask"])
            except (KeyError, TypeError, ValueError):
                continue
            pb, pa = last_t.get(coin, (None, None))
            for side, new, old in (("b", tb, pb), ("a", ta, pa)):
                if new != old:
                    rows_t.append((coin, side, recv, ets, new))
            last_t[coin] = (tb, ta)
        if ets is not None:
            clock = max(clock, ets)
    B = pd.DataFrame(rows_b, columns=["coin", "side", "recv", "ets", "price"]).sort_values("recv")
    T = pd.DataFrame(rows_t, columns=["coin", "side", "recv", "ets", "price"]).sort_values("recv")
    print(f"book touch changes {len(B)}, ticker touch changes {len(T)}")

    # 1. each book touch change: was the ticker there first?
    Tm = T.rename(columns={"recv": "recv_t", "ets": "ets_t"})
    r = pd.merge_asof(B, Tm, left_on="recv", right_on="recv_t", by=["coin", "side", "price"],
                      direction="backward", tolerance=1.0)
    r["lead_ms"] = (r.recv - r.recv_t) * 1e3
    r["first"] = np.where(r.lead_ms.notna() & (r.lead_ms > 0), "ticker", "book")
    print("\n=== 1. every book best-price change: ticker showed it first (within 1s)? ===")
    g = r.groupby("coin")
    t1 = pd.DataFrame({"n": g.size(), "ticker_first_%": g["first"].apply(lambda s: 100 * (s == "ticker").mean())})
    lead = r[r["first"] == "ticker"].groupby("coin").lead_ms.apply(q).unstack()
    print(t1.join(lead.add_prefix("lead_")).round(1).to_string())
    a = r[r["first"] == "ticker"].lead_ms
    print(f"ALL: ticker first {100 * (r['first'] == 'ticker').mean():.1f}% of {len(r)}; lead p50 {a.median():.1f}ms "
          f"p90 {a.quantile(.9):.1f}ms")
    # ticker changes the book never showed within 1s (intermediate touches inside one batch)
    Bm = B.rename(columns={"recv": "recv_b", "ets": "ets_b"})
    u = pd.merge_asof(T, Bm, left_on="recv", right_on="recv_b", by=["coin", "side", "price"],
                      direction="forward", tolerance=1.0)
    print(f"ticker touches the book showed later: {100 * u.recv_b.notna().mean():.1f}% of {len(u)}; "
          f"book already had it: see 1")

    # 2. book message time vs ticker time of the same touch
    m2 = u.dropna(subset=["recv_b"])
    print("\n=== 2. l2update `time` - ticker `time`, same touch, book later (ms): >= 0 means `time` "
          "is at or after the change ===")
    print(q((m2.ets_b - m2.ets) * 1e3).round(1).to_string())
    print(f"share < 0: {100 * ((m2.ets_b - m2.ets) < 0).mean():.2f}%  share < -5ms: "
          f"{100 * ((m2.ets_b - m2.ets) < -0.005).mean():.2f}%")

    # 3. content lag behind the connection clock
    L = pd.DataFrame(lag, columns=["coin", "ms"])
    print("\n=== 3. connection's newest exchange time - l2update time, on arrival (ms) ===")
    print(L.groupby("coin").ms.apply(q).unstack().round(1).to_string())
    print("ALL", q(L.ms).round(1).to_dict())

    # 4. exchange-time age of the touch: book alone vs newest of book/ticker, sampled every 10ms
    print("\n=== 4. touch information age (now - exchange time of newest touch info), 10ms grid (ms) ===")
    t0, t1_ = B.recv.min() + 5, B.recv.max()
    grid = np.arange(t0, t1_, 0.01)
    out = []
    for coin in sorted(B.coin.unique()):
        bb = B[B.coin == coin].drop_duplicates("recv", keep="last")
        tt = T[T.coin == coin].drop_duplicates("recv", keep="last")
        ib = pd.Series(bb.ets.values, index=bb.recv.values).cummax()
        it = pd.Series(tt.ets.values, index=tt.recv.values).cummax()
        gb = ib.reindex(grid, method="ffill").values
        gt = it.reindex(grid, method="ffill").values if len(tt) else np.full(len(grid), np.nan)
        both = np.fmax(gb, gt)
        out.append(dict(coin=coin, book_p50=np.nanmedian(grid - gb) * 1e3, both_p50=np.nanmedian(grid - both) * 1e3,
                        book_p90=np.nanpercentile(grid - gb, 90) * 1e3, both_p90=np.nanpercentile(grid - both, 90) * 1e3,
                        newer_from_ticker_pct=100 * np.nanmean(gt > gb)))
    print("(time since the last touch CHANGE, so quiet coins read old; compare columns, not levels)")
    print(pd.DataFrame(out).set_index("coin").round(1).to_string())

    # 5. does sharing the socket with level2_batch delay matches / ticker?
    def stream(fname, want):
        out = []
        for recv, m in _lines(os.path.join(outdir, fname)):
            if recv is None or f'"{want}"' not in m:
                continue
            d = orjson.loads(m)
            if d.get("type") == want:
                out.append((d["product_id"].split("-")[0], str(d.get("trade_id") or d.get("sequence")), recv,
                            _iso(d["time"])))
        return pd.DataFrame(out, columns=["coin", "key", "recv", "ets"])
    print("\n=== 5. same message on the shared live socket vs its own socket: live receipt - own (ms) ===")
    for want, own in (("match", "ex_matches.txt"), ("ticker", "ex_ticker.txt")):
        a, b = stream("ex_live.txt", want), stream(own, want)
        if want == "ticker":
            a["key"], b["key"] = a.ets.astype(str), b.ets.astype(str)
        j = a.merge(b, on=["coin", "key"], suffixes=("_live", "_own")).drop_duplicates(["coin", "key"])
        print(want, q((j.recv_live - j.recv_own) * 1e3).round(1).to_dict())
        print(f"  exchange time -> receipt p50: live {np.median(j.recv_live - j.ets_live) * 1e3:.1f}ms, "
              f"own socket {np.median(j.recv_own - j.ets_own) * 1e3:.1f}ms")


if __name__ == "__main__":
    if sys.argv[1] == "rec":
        record(float(sys.argv[2]), sys.argv[3])
    elif sys.argv[1] == "live":
        analyze_live(sys.argv[2])
    else:
        analyze(sys.argv[2])
