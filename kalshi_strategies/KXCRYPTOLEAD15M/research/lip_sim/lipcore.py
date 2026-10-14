"""Shared helpers for the LIP research (not part of the repo)."""
import glob
import json
import os

import numpy as np
import pandas as pd

REPO = r"C:\Users\eshan\Desktop\Coding Projects\alpha5"
PDX = os.path.join(REPO, "data", "predexon", "KXCRYPTOLEAD15M", "orderbooks")
MKT = os.path.join(REPO, "data", "predexon", "KXCRYPTOLEAD15M", "event_markets")
TRD = os.path.join(REPO, "data", "kalshi_trades", "KXCRYPTOLEAD15M")
D = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "Temp", "opencode", "lipr")
DATA = os.path.join(D, "data")
T, DF, REWARD, PERIOD_S = 1000.0, 0.5, 20.0, 900
TICK = 0.01
COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE"]


def close_of(ev):
    return pd.Timestamp(pd.to_datetime(ev.split("-")[1], format="%y%b%d%H%M")).tz_localize(
        "America/New_York").tz_convert("UTC")


def events():
    out = []
    for p in sorted(glob.glob(os.path.join(PDX, "*.parquet"))):
        ev = os.path.basename(p)[:-8]
        out.append((ev, close_of(ev)))
    return sorted(out, key=lambda x: x[1])


def results(ev):
    p = os.path.join(MKT, f"{ev}.json")
    if not os.path.exists(p):
        return {}
    return {m["ticker"]: m.get("result") for m in json.load(open(p))}


def ladder(js, flip=False):
    """Predexon JSON levels -> {own-side price: size}. flip: yes asks -> no bids."""
    out = {}
    for lv in json.loads(js):
        s = float(lv["size"])
        if s > 1e-9:
            p = round(int(float(lv["price"])) / 100, 2)
            if flip:
                p = round(1 - p, 2)
            out[p] = out.get(p, 0.0) + s
    return out


def load_books(ev):
    ob = pd.read_parquet(os.path.join(PDX, f"{ev}.parquet"))
    ob = ob[ob.source == "websocket"].sort_values(["ticker", "time"])
    return ob


def load_prints(ev):
    p = os.path.join(TRD, f"{ev}.parquet")
    if not os.path.exists(p):
        return pd.DataFrame(columns=["ticker", "time", "yes_price", "count", "taker_side"])
    tr = pd.read_parquet(p)
    if len(tr):
        tr["time"] = pd.to_datetime(tr.time, utc=True, format="ISO8601")
    return tr.sort_values("time")


def score_side(bids, our_p=None, our_n=0.0, rule="whole"):
    """One side. bids: others {price: size}. Returns (our_score, all_score, qualifies, ref).
    rule='whole': CFTC text -- every order at the level where Target is reached counts.
    rule='back' : lip.py -- our size (at the back) counts only if size ahead < Target."""
    lv = dict(bids)
    if our_p is not None and our_n > 0:
        lv[our_p] = lv.get(our_p, 0.0) + our_n
    ps = sorted(lv, reverse=True)
    cum, ref, stop = 0.0, None, None
    for i, p in enumerate(ps):
        cum += lv[p]
        if ref is None and cum >= T / 5:
            ref = p
        if cum >= T:
            stop = i
            break
    if ref is None or stop is None:
        return 0.0, 0.0, False, ref
    allsc, oursc, before = 0.0, 0.0, 0.0
    for p in ps[:stop + 1]:
        w = DF ** max(0, int(round((ref - p) / TICK)))
        mine = our_n if (our_p is not None and abs(p - our_p) < 1e-9) else 0.0
        others = lv[p] - mine
        if rule == "back" and mine > 0 and before + others >= T - 1e-9:
            mine = 0.0
        allsc += (others + mine) * w
        oursc += mine * w
        before += lv[p]
    return oursc, allsc, True, ref
