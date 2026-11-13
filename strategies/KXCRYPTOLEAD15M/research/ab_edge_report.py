"""Result of the constant-edge A/B (ab_edge.py): per arm, lots and $ per event, 10s/60s markout and
settled c/lot with event-clustered standard errors, and the same per coin. Run from /opt/alpha5."""
import json

import numpy as np
import pandas as pd

LOG = "data/live/KXCRYPTOLEAD15M/ab_edge.jsonl"
arms = pd.DataFrame([json.loads(x) for x in open(LOG)])
arms = arms[~arms.stop].drop_duplicates("event_start", keep="last")
# The event ticker names its close: the event starting at T closes at T + 15 min.
arms["close"] = pd.to_datetime(arms.event_start, utc=True) + pd.Timedelta("15min")
f = pd.read_parquet("data/live/KXCRYPTOLEAD15M/perf/fills.parquet")
f = f.merge(arms[["close", "arm"]], on="close", how="inner")
f = f[~f.ticker.str.endswith("HYPE") & (f.close < f.time.max() - pd.Timedelta("2min"))]
f["coin"] = f.ticker.str.rsplit("-", n=1).str[-1]
done = arms[arms.close < f.time.max()]
n_ev = done.arm.value_counts()


def per_event(g, cols=("mo10", "mo60")):
    """c/lot by arm with an event-clustered SE; events with no fills count for volume only."""
    out = {}
    for arm, a in g.groupby("arm"):
        ev = a.groupby("close").apply(lambda e: pd.Series({
            "lots": e["size"].sum(), **{c: (e[c] * e["size"]).sum() for c in cols},
            "settled": e.pnl_c.sum()}))
        lots = ev.lots.sum()
        row = {"events": int(n_ev.get(arm, 0)), "lots/ev": lots / max(n_ev.get(arm, 1), 1)}
        for c in (*cols, "settled"):
            ratio = ev[c].sum() / lots
            # Ratio estimator SE, clustered by event.
            resid = ev[c] - ratio * ev.lots
            row[f"{c} c/lot"] = ratio
            row[f"{c} se"] = np.sqrt((resid ** 2).sum()) / lots
        row["settled $/ev"] = ev.settled.sum() / 100 / max(n_ev.get(arm, 1), 1)
        out[arm] = row
    return pd.DataFrame(out).T


pd.set_option("display.width", 220)
print(f"events: {n_ev.to_dict()}  fills: {len(f)}  through {f.time.max()}")
print(per_event(f).round(2).to_string())
for coin, g in f.groupby("coin"):
    print(f"\n{coin}")
    print(per_event(g).round(2).to_string())
