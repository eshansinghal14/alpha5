"""Writes in flight (matched send/ack pairs only) at each cancel_sent, and the
cancel's loop-side overhead (wall - worker rtt) by that count. Also how fast
creates get cancelled after their ack (churn)."""
import glob, os, sys, time
import numpy as np, pandas as pd, pyarrow.parquet as pq
S = sys.argv[1] if len(sys.argv) > 1 else "KXCRYPTOLEAD15M"
H = float(sys.argv[2]) if len(sys.argv) > 2 else 3
DAY = time.strftime("%Y-%m-%d", time.gmtime())
J = f"/opt/alpha5/data/live/{S}/{DAY}/journal/orders/"
lo = time.time() - H * 3600
parts = []
for f in glob.glob(J + "*.parquet"):
    if os.path.getmtime(f) < lo - 60:
        continue
    try:
        t = pq.read_table(f, columns=["time", "event", "order_id", "ticker", "reason", "rtt_s", "live", "queued_s"]).to_pandas()
    except Exception:
        continue
    t["t"] = t.pop("time").astype("int64") / 1e9
    t = t[(t.t >= lo) & t.live]
    t["k"] = t.ticker.astype(str) + "#" + t.order_id.astype(str)
    parts.append(t[["t", "event", "k", "reason", "rtt_s", "queued_s"]])
o = pd.concat(parts, ignore_index=True)
o["event"] = o.event.astype(str)
kind = o.event.str.split("_").str[0]
o["kind"] = kind
o["phase"] = o.event.str.split("_").str[1]
o = o[o.phase.isin(["sent", "ack"])]
s = o[o.phase == "sent"].drop_duplicates(["k", "kind"])
a = o[o.phase == "ack"].drop_duplicates(["k", "kind"])
p = s.merge(a[["k", "kind", "t", "rtt_s"]], on=["k", "kind"], suffixes=("_s", "_a"))
p = p[p.t_a >= p.t_s]
print(S, f"{H}h: pairs", p.kind.value_counts().to_dict())
starts = np.sort(p.t_s.values); ends = np.sort(p.t_a.values)
c = p[p.kind == "cancel"].copy()
c["inflight"] = np.searchsorted(starts, c.t_s.values, side="left") - np.searchsorted(ends, c.t_s.values, side="right")
c["ov"] = (c.t_a - c.t_s - c.rtt_s_a) * 1e3
c["rtt"] = c.rtt_s_a * 1e3
print("in flight at cancel send (excl itself):", c.inflight.quantile([.1, .5, .9, .99]).to_dict(), "max", c.inflight.max())
c["bin"] = pd.cut(c.inflight, [-1, 0, 2, 4, 6, 8, 12, 20, 1000])
print(c.groupby("bin", observed=True).agg(n=("ov", "size"), ov50=("ov", "median"), ov90=("ov", lambda x: x.quantile(.9)),
                                           rtt50=("rtt", "median"), rtt90=("rtt", lambda x: x.quantile(.9))).round(1))
# per-second write burst size
sec = (p.t_s // 1).astype(int)
print("writes sent per second: p50/p90/p99/max", sec.value_counts().quantile([.5, .9, .99]).to_dict(), sec.value_counts().max())
# 50ms windows
w = (p.t_s // 0.05).astype(int).value_counts()
print("writes per 50ms window (nonempty): p50/p90/p99/max", w.quantile([.5, .9, .99]).to_dict(), w.max())
# churn: create ack -> cancel sent of the same order
cr = p[p.kind == "create"][["k", "t_s", "t_a"]]
cc = p[p.kind == "cancel"][["k", "t_s", "reason"]].rename(columns={"t_s": "t_c"})
j = cr.merge(cc, on="k")
j["life"] = j.t_c - j.t_a
print("create->cancel life after ack (s):", j.life.quantile([.1, .25, .5, .75, .9]).round(3).to_dict())
print("share of creates cancelled within 100ms of ack:", round((j.life < 0.1).mean(), 3),
      " | after_ack (cancel decided before ack):", round((j.reason.astype(str) == "after_ack").mean(), 3))
