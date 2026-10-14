"""LIP order simulator: one virtual LIP order per (market, side), all 5 markets of
an event on one clock, against Predexon ladders (~0.5s) and Kalshi prints.

Fill model (the part that matters for adverse selection):
  * Prints are grouped into sweeps (same ticker, timestamp, taker side). For our
    bid at own-side price p, volume that would have reached us is every print
    in the sweep at a price BELOW p (it went through our level, so the level
    including us was gone), plus, at p itself, the print size beyond our queue.
    This does not depend on snapshot timing: if the maker ahead pulled just
    before the sweep, the sweep prints deeper and fills us.
  * Virtual crossing: a snapshot whose far side would cross our price fills us
    for the crossing size (someone posted an ask at/through our bid -- with us
    there it would have traded). Dated 50ms after the previous snapshot.
  * Queue: back of the displayed level at placement; shrinks with prints at p
    and with displayed size decreases (cancels assumed ahead -> more fills).
Latency: place/cancel take effect LAT after the decision. Decisions run on each
snapshot of that market; gates run on their own intervals (cancel at gate start
+ LAT, no re-place until the gate ends).

LIP accrual: on each snapshot, our share per side (CFTC 'whole level' rule),
weighted by time to the next snapshot of that market (cap 5s). Payout per
market-period = floor_cent(avg (ys+ns)/2 * $20) if >= $1 else 0.
"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from lipcore import D, REWARD, T, TICK, ladder, load_books, load_prints, results, score_side  # noqa: E402

EV = os.path.join(D, "ev")
LAT = 0.10
COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE"]


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

class EventData:
    def __init__(self, ev, close):
        self.ev, self.close = ev, close
        self.t_open = close - pd.Timedelta(seconds=900)
        self.ok = True
        ob = load_books(ev)
        self.res = results(ev)
        self.markets = {}
        for tk, g in ob.groupby("ticker"):
            g = g.drop_duplicates("time", keep="last").sort_values("time")
            ts = g.time.astype("int64").to_numpy() / 1e9
            yb = [ladder(x) for x in g.yes_bids]
            nb = [ladder(x, flip=True) for x in g.yes_asks]
            mid = np.array([(max(a) + (1 - max(b))) / 2 if a and b else np.nan for a, b in zip(yb, nb)])
            self.markets[tk] = dict(t=ts, yb=yb, nb=nb, mid=mid)
        tr = load_prints(ev)
        self.sweeps = {}
        if len(tr):
            tr = tr.copy()
            tr["ts"] = tr.time.astype("int64") / 1e9
            for (tk, ts, ts_side), g in tr.groupby(["ticker", "ts", "taker_side"]):
                side = "yes" if ts_side == "no" else "no"      # taker sold yes -> yes bids hit
                pxs = [(round(p, 2) if side == "yes" else round(1 - p, 2), c)
                       for p, c in zip(g.yes_price, g["count"])]
                self.sweeps.setdefault(tk, []).append((ts, side, pxs))
            for tk in self.sweeps:
                self.sweeps[tk].sort(key=lambda x: x[0])
        p = os.path.join(EV, f"{ev}_dec.parquet")
        self.dec = {}
        if os.path.exists(p):
            dd = pd.read_parquet(p, columns=["time", "ticker", "prob", "quote_bid_reservation",
                                            "quote_ask_reservation", "x_theo_vol", "block_bid", "block_ask",
                                            "quote_bid", "quote_ask", "n_levels_bid", "n_levels_ask"])
            for tk, g in dd.groupby("ticker"):
                g = g.sort_values("time")
                self.dec[tk] = dict(t=g.time.astype("int64").to_numpy() / 1e9, prob=g.prob.to_numpy(float),
                                    rb=g.quote_bid_reservation.to_numpy(float),
                                    ra=g.quote_ask_reservation.to_numpy(float),
                                    tv=g.x_theo_vol.to_numpy(float),
                                    qb=g.quote_bid.to_numpy(float), qa=g.quote_ask.to_numpy(float),
                                    nb=g.n_levels_bid.to_numpy(float), na=g.n_levels_ask.to_numpy(float))
        p = os.path.join(EV, f"{ev}_spot.parquet")
        self.spot = pd.read_parquet(p) if os.path.exists(p) else None
        if self.spot is not None:
            self.spot_t = (self.spot.b.to_numpy() + 1) / 10.0     # known at bin end
        # merged timeline of snapshots
        ev_rows = []
        for tk, m in self.markets.items():
            for i, t in enumerate(m["t"]):
                ev_rows.append((t, tk, i))
        ev_rows.sort()
        self.timeline = ev_rows

    def dec_at(self, tk, t):
        d = self.dec.get(tk)
        if d is None:
            return None
        k = np.searchsorted(d["t"], t, side="right") - 1
        if k < 0 or t - d["t"][k] > 3.0:
            return None
        return k

    def mid_at(self, tk, t):
        m = self.markets[tk]
        k = np.searchsorted(m["t"], t, side="right") - 1
        return m["mid"][k] if k >= 0 else np.nan

    def spot_feats(self, t):
        """Spot features known at t: max over coins of |z| (0.5s/1s/2s), the coin, and
        max |bp| move over 1s; also own-coin values per coin dict."""
        if self.spot is None:
            return None
        k = np.searchsorted(self.spot_t, t, side="right") - 1
        if k < 0:
            return None
        r = self.spot.iloc[k]
        out = {}
        for w in ("05", "1", "2", "5"):
            zs = [r[f"{c}_z{w}"] for c in COINS]
            zs = [z if z == z else 0.0 for z in zs]
            out[f"zmax{w}"] = max(zs)
            out[f"zarg{w}"] = COINS[int(np.argmax(zs))]
            out.update({f"z{w}_{c}": z for c, z in zip(COINS, zs)})
            bps = [abs(r[f"{c}_r{w}"]) if r[f"{c}_r{w}"] == r[f"{c}_r{w}"] else 0.0 for c in COINS]
            out[f"bpmax{w}"] = max(bps)
        return out


def gate_intervals(ed, kind, thr, hold, field="z1"):
    """[(start, end)] when a gate is on, from the spot grid (any coin) or the fair."""
    iv = []
    if kind == "spot":
        if ed.spot is None:
            return None
        cols = [f"{c}_{field}" for c in COINS]
        z = np.nanmax(ed.spot[cols].to_numpy(dtype=float), axis=1)
        on = np.where(np.nan_to_num(z) >= thr)[0]
        ts = ed.spot_t
    elif kind == "fair":
        # any market's fair moved >= thr over the trailing 1s
        on_t = []
        for tk, d in ed.dec.items():
            t, p = d["t"], d["prob"]
            k = np.searchsorted(t, t - 1.0, side="left")
            dp = np.abs(p - p[np.clip(k, 0, len(p) - 1)])
            on_t += list(t[dp >= thr])
        ts = np.array(sorted(on_t))
        on = np.arange(len(ts))
    elif kind == "kalshi":
        on_t = []
        for tk, m in ed.markets.items():
            t, mid = m["t"], m["mid"]
            k = np.searchsorted(t, t - 1.0, side="left")
            dm = np.abs(mid - mid[np.clip(k, 0, len(mid) - 1)])
            on_t += list(t[np.nan_to_num(dm) >= thr])
        ts = np.array(sorted(on_t))
        on = np.arange(len(ts))
    else:
        return []
    for i in on:
        s = ts[i]
        if iv and s <= iv[-1][1]:
            iv[-1][1] = max(iv[-1][1], s + hold)
        else:
            iv.append([s, s + hold])
    return iv


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

def own_far(yb, nb, side):
    other = nb if side == "yes" else yb
    return round(1 - max(other), 2) if other else None


def choose_level(pol, bids, far, fair_own, res_own, theo):
    """Own-side price for our LIP order or None."""
    if not bids:
        return None
    best = max(bids)
    ps = sorted(bids, reverse=True)
    cum, ref = 0.0, None
    for p in ps:
        cum += bids[p]
        if cum >= T / 5:
            ref = p
            break
    if ref is None:
        return None
    lvl = pol["level"]
    if lvl == "best":
        p = best
    elif lvl.startswith("ref"):
        p = round(ref - int(lvl[3:] or 0) * TICK, 2)
    elif lvl.startswith("ahead"):
        # shallowest price with >= A contracts of others at better-or-equal prices
        A = float(lvl[5:])
        cum, p = 0.0, None
        for q in ps:
            cum += bids[q]
            if cum >= A:
                p = q
                break
        if p is None:
            return None
        p = min(p, ref)        # never in front of the reference price: no extra LIP there
    else:
        raise ValueError(lvl)
    if far is not None:
        p = min(p, round(far - TICK, 2))
    # value guards
    m = pol.get("fair_margin")
    if m is not None:
        if fair_own is None or fair_own != fair_own:
            return None
        th = theo if (theo is not None and theo == theo) else 0.0
        cap = fair_own - m - pol.get("theo_k", 0.0) * th
        p = min(p, math.floor(cap / TICK + 1e-9) * TICK)
    if pol.get("use_res"):
        if res_own is None or res_own != res_own:
            return None
        p = min(p, math.floor(res_own / TICK + 1e-9) * TICK)
    p = round(p, 2)
    if "bands" in pol and not any(a - 1e-9 <= p <= b + 1e-9 for a, b in pol["bands"]):
        return None
    lo, hi = pol.get("band", (0.01, 0.99))
    if pol.get("band_fair"):
        # band on our fair (own-side), not on the quote price
        if fair_own is None or fair_own != fair_own:
            return None
        if "bands_fair" in pol and not any(a - 1e-9 <= fair_own <= b + 1e-9 for a, b in pol["bands_fair"]):
            return None
    if not (lo - 1e-9 <= p <= hi + 1e-9):
        return None
    # don't go further than max_behind ticks behind the reference (no LIP there anyway)
    if ref - p > pol.get("max_behind", 3) * TICK + 1e-9:
        return None
    return p


# --------------------------------------------------------------------------
# sim
# --------------------------------------------------------------------------

def simulate(ed: EventData, pol: dict, log_fills=True, log_expo=False):
    global LAT
    LAT = pol.get("lat", 0.10)
    N = pol["N"]
    t_open = ed.t_open.timestamp() + pol.get("open_delay", 10)
    t_stop = ed.close.timestamp() - max(pol.get("cutoff", 60), pol.get("tau_min", 0))
    tau_max = pol.get("tau_max")
    gates = []
    for g in pol.get("gates", []):
        iv = gate_intervals(ed, *g)
        if iv is None:
            return None
        gates += [(s + LAT, e + LAT) for s, e in iv]
    gates.sort()
    gstarts = np.array([s for s, _ in gates]) if gates else np.array([])
    gends = np.array([e for _, e in gates]) if gates else np.array([])

    def gate_on(t):
        if not len(gstarts):
            return None
        k = np.searchsorted(gstarts, t, side="right") - 1
        if k >= 0 and gends[k] > t:
            return gends[k]
        return None

    def next_gate_start(t0, t1):
        if not len(gstarts):
            return None
        k = np.searchsorted(gstarts, t0, side="right")
        if k < len(gstarts) and gstarts[k] < t1:
            return gstarts[k]
        return None

    xgroup = pol.get("xgroup")      # (min lots filled in event, hold s, latency s): pull all LIP
    # sweep_pull: (lookback s, latency s, hold s). Any print on ANY market of the event
    # that trades through >= 2 price levels (a sweep) pulls every LIP order after
    # latency -- reacting to the Kalshi trade feed itself, not to spot.
    sweep_pull = pol.get("sweep_pull")
    if sweep_pull is not None:
        st = []
        min_lv = sweep_pull[3] if len(sweep_pull) > 3 else 2
        min_lots = sweep_pull[4] if len(sweep_pull) > 4 else 0
        for tk2, sw in ed.sweeps.items():
            for ts, side, pxs in sw:
                if len({px for px, _ in pxs}) >= min_lv and sum(c for _, c in pxs) >= min_lots:
                    st.append(ts)
        for s in sorted(st):
            gates.append((s + sweep_pull[1], s + sweep_pull[1] + sweep_pull[2]))
        gates.sort()
        gstarts = np.array([s for s, _ in gates]) if gates else np.array([])
        gends = np.array([e for _, e in gates]) if gates else np.array([])
    pos = {tk: 0.0 for tk in ed.markets}
    orders = {(tk, s): None for tk in ed.markets for s in ("yes", "no")}
    share_acc = {tk: 0.0 for tk in ed.markets}
    last_snap = {tk: None for tk in ed.markets}
    sweep_i = {tk: 0 for tk in ed.markets}
    fills, expo = [], []
    block_until = 0.0
    max_col = 0.0
    n_writes = 0
    acc_by = {}
    tot_t = 900.0

    def kill(o, t):
        if o is not None and o["dead"] is None:
            o["dead"] = t

    def do_fill(tk, side, o, q, t, how):
        nonlocal block_until
        o["size"] -= q
        pos[tk] += q if side == "yes" else -q
        if log_fills:
            fills.append(fill_row(ed, tk, side, o, q, t, how))
        if xgroup is not None and q >= xgroup[0]:
            until = t + xgroup[1]
            block_until = max(block_until, until)
            for k2, o2 in orders.items():
                if o2 is not None and o2 is not o and o2["dead"] is None:
                    o2["dead"] = t + xgroup[2]

    def process_sweeps(tk, upto):
        sw = ed.sweeps.get(tk, [])
        i = sweep_i[tk]
        while i < len(sw) and sw[i][0] < upto:
            ts, side, pxs = sw[i]
            i += 1
            o = orders[(tk, side)]
            if o is None or ts < o["live"] or (o["dead"] is not None and ts >= o["dead"]) or o["size"] <= 1e-9:
                continue
            p = o["p"]
            at = sum(c for px, c in pxs if abs(px - p) < 1e-9)
            through = sum(c for px, c in pxs if px < p - 1e-9)
            o["traded_at"] += at
            if through > 0:
                # through_full: the sweeper was price-limited, so it would have taken
                # our whole order, not just the volume it found below us.
                avail = o["size"] if pol.get("through_full") else through + max(0.0, at - o["queue"])
                o["queue"] = 0.0
                how = "through"
            else:
                avail = max(0.0, at - o["queue"])
                o["queue"] = max(0.0, o["queue"] - at)
                how = "at"
            q = min(avail, o["size"])
            if q > 1e-9:
                do_fill(tk, side, o, q, ts, how)
        sweep_i[tk] = i

    for (t, tk, i) in ed.timeline:
        m = ed.markets[tk]
        # 1) prints on this market up to this snapshot
        process_sweeps(tk, t)
        yb, nb = m["yb"][i], m["nb"][i]
        prev_t = last_snap[tk]
        # 2) crossing, queue, retire
        for side in ("yes", "no"):
            o = orders[(tk, side)]
            if o is None:
                continue
            tcross = (prev_t + 0.05) if prev_t is not None else t
            alive = t >= o["live"] and (o["dead"] is None or o["dead"] > tcross) and o["size"] > 1e-9
            if alive:
                other = nb if side == "yes" else yb
                cross_q = sum(s for q2, s in other.items() if round(1 - q2, 2) <= o["p"] + 1e-9)
                # Calibrated 2026-10-11..14 on our 1-lot edge quotes: only ~16% of
                # sim cross events coincided with a real fill (vs 66-78% for print
                # fills), so crosses are weighted, not taken whole.
                w = pol.get("cross_w", 0.0)
                if cross_q > 0 and tcross >= o["live"] and w > 0:
                    do_fill(tk, side, o, min(cross_q, o["size"]) * w, max(tcross, o["live"]), "cross")
                if o["size"] > 1e-9:
                    own = yb if side == "yes" else nb
                    o["queue"] = min(o["queue"], own.get(o["p"], 0.0))
            if o["size"] <= 1e-9 or (o["dead"] is not None and o["dead"] <= t):
                orders[(tk, side)] = None
        # 3) LIP accrual for [t, next snapshot of this market)
        nxt = m["t"][i + 1] if i + 1 < len(m["t"]) else ed.close.timestamp()
        dt = max(0.0, min(nxt, ed.close.timestamp()) - max(t, ed.t_open.timestamp()))
        dt = min(dt, 5.0)
        if dt > 0:
            sh = []
            q_all = True
            for side in ("yes", "no"):
                o = orders[(tk, side)]
                live = o is not None and t >= o["live"] and o["dead"] is None and o["size"] > 1e-9
                own = yb if side == "yes" else nb
                ours, alls, qq, _ = score_side(own, o["p"] if live else None, o["size"] if live else 0.0)
                q_all &= qq
                sh.append(ours / alls if (qq and alls > 0) else 0.0)
            if q_all:
                share_acc[tk] += (sh[0] + sh[1]) / 2 * dt
                tau_b = int((ed.close.timestamp() - t) // 60)
                for side, s_ in zip(("yes", "no"), sh):
                    o = orders[(tk, side)]
                    if s_ > 0 and o is not None:
                        kb = (tk, side, round(o["p"], 2), tau_b)
                        acc_by[kb] = acc_by.get(kb, 0.0) + s_ / 2 * dt
                if log_expo:
                    expo.append((tk, t, dt, sh[0], sh[1]))
        last_snap[tk] = t
        col = sum(o2["p"] * o2["size"] for o2 in orders.values() if o2 is not None and o2["dead"] is None)
        col += sum(abs(v) * 0.5 for v in pos.values())
        max_col = max(max_col, col)
        # 4) decide
        active = t_open <= t < t_stop and (tau_max is None or ed.close.timestamp() - t <= tau_max)
        g_end = gate_on(t)
        if g_end is not None or t < block_until:
            active = False
        k = ed.dec_at(tk, t)
        if pol.get("need_dec", True) and k is None:
            active = False
        d = ed.dec.get(tk)
        for side in ("yes", "no"):
            o = orders[(tk, side)]
            want = None
            if active:
                own = yb if side == "yes" else nb
                far = own_far(yb, nb, side)
                fair_own = res_own = theo = None
                if k is not None:
                    pr = d["prob"][k]
                    fair_own = pr if side == "yes" else 1 - pr
                    res_own = d["rb"][k] if side == "yes" else (1 - d["ra"][k])
                    theo = d["tv"][k]
                if pol["level"].startswith("dec"):
                    # replay of our live edge quote: top level (dec0) or one behind (dec1)
                    j = int(pol["level"][3:])
                    top = d["qb"][k] if side == "yes" else (1 - d["qa"][k])
                    nl = d["nb"][k] if side == "yes" else d["na"][k]
                    p = round(top - j * TICK, 2) if (top == top and nl == nl and nl > j) else None
                else:
                    p = choose_level(pol, own, far, fair_own, res_own, theo)
                held = pos[tk] if side == "yes" else -pos[tk]
                NN = N
                if p is not None and "size_rule" in pol:
                    key = fair_own if (pol.get("band_fair") and fair_own is not None) else p
                    NN = next((n for a, b, n in pol["size_rule"] if a - 1e-9 <= key <= b + 1e-9), 0)
                    if pol.get("band_fair") and NN == 0:
                        # fair band edges (0.20/0.80) are inclusive in bands_fair; map the rest
                        NN = pol["size_rule"][0][2] if key <= 0.5 else pol["size_rule"][-1][2]
                size = math.floor(NN - max(held, 0.0) + 1e-9)
                if pol.get("fixed_size"):
                    size = pol["fixed_size"]
                if p is not None and size >= 1:
                    want = (p, float(size))
            if o is not None and o["dead"] is None:
                if want is None:
                    o["dead"] = t + LAT
                elif abs(want[0] - o["p"]) > 1e-9 and t - o["placed"] >= pol.get("hold", 0.5):
                    o["dead"] = t + LAT
            if want is not None and orders[(tk, side)] is None:
                own = yb if side == "yes" else nb
                ahead = sum(s for q2, s in own.items() if q2 >= want[0] - 1e-9)
                n120 = sum(1 for q2, s in own.items() if q2 >= want[0] - 1e-9 and abs(s - 120) < 1.5)
                o = dict(p=want[0], size=want[1], queue=own.get(want[0], 0.0), live=t + LAT, dead=None,
                         placed=t, ahead0=ahead, n120=n120, traded_at=0.0)
                # a gate starting before this order's natural life ends will kill it
                orders[(tk, side)] = o
                n_writes += 1
            # schedule gate kills for live orders
            o = orders[(tk, side)]
            if o is not None and o["dead"] is None:
                gs = next_gate_start(t, nxt + 1e-9)
                if gs is not None:
                    o["dead"] = gs
    # trailing prints
    for tk in ed.markets:
        process_sweeps(tk, ed.close.timestamp() + 1)
    periods = []
    for tk in ed.markets:
        gross = share_acc[tk] / tot_t * REWARD
        pay = math.floor(gross * 100) / 100
        periods.append(dict(ev=ed.ev, ticker=tk, gross=gross, paid=pay if pay >= 1.0 else 0.0,
                            result=ed.res.get(tk), max_col=max_col, n_writes=n_writes))
    acc = [dict(ev=ed.ev, ticker=k[0], side=k[1], price=k[2], tau_min=k[3], lip_gross=v / tot_t * REWARD)
           for k, v in acc_by.items()]
    return dict(periods=periods, fills=fills, expo=expo, acc=acc)


def fill_row(ed, tk, side, o, q, t, how):
    yes_px = o["p"] if side == "yes" else round(1 - o["p"], 2)
    d = 1.0 if side == "yes" else -1.0
    res = ed.res.get(tk)
    r = dict(ev=ed.ev, ticker=tk, side=side, t=t, size=q, price=o["p"], yes_px=yes_px, how=how,
             tau=ed.close.timestamp() - t, rest=t - o["placed"], ahead0=o["ahead0"], n120=o["n120"],
             traded_at=o["traded_at"])
    for h in (1, 5, 10, 30, 60):
        r[f"mo{h}"] = d * (ed.mid_at(tk, t + h) - yes_px) * q
    r["mid_before"] = ed.mid_at(tk, t - 1.0)
    r["settled"] = d * ((1.0 if res == "yes" else 0.0) - yes_px) * q if res in ("yes", "no") else np.nan
    k = ed.dec_at(tk, t - LAT)
    if k is not None:
        dd = ed.dec[tk]
        r["fair"] = dd["prob"][k]
        r["theo"] = dd["tv"][k]
        k1 = np.searchsorted(dd["t"], t - 1.0 - LAT, side="right") - 1
        r["dfair1"] = dd["prob"][k] - dd["prob"][k1] if k1 >= 0 else np.nan
        ka = ed.dec_at(tk, t + 2.0)
        r["fair_after2"] = dd["prob"][ka] if ka is not None else np.nan
    sf = ed.spot_feats(t - LAT)
    if sf is not None:
        for w in ("05", "1", "2", "5"):
            r[f"zmax{w}"] = sf[f"zmax{w}"]
            r[f"bpmax{w}"] = sf[f"bpmax{w}"]
        r["zarg1"] = sf["zarg1"]
        r["zown1"] = sf[f"z1_{tk.rsplit('-', 1)[-1]}"]
    sa = ed.spot_feats(t + 2.0)
    if sa is not None:
        r["zmax2_after"] = sa["zmax2"]
    return r
