"""Single-leg arb backtest over every basket in the spec, on 1-minute
Kalshi candles (candles.py).

Per basket and leg j, each minute:
  fair_j = payout - sum_{i != j} mid_i        (the price leg j "should" be)
  lag_j  = fair_j - ask_j                      (what buying j now is cheap by)

Entry: buy leg j at its ask (taker fee) when lag_j clears entry_edge, as a
fraction of the ask, and leg j is the lagging one by `lag_rule`:
  "thinnest" -- the leg with the least candle activity over lookback_min
                (the deep markets are the ones that are right);
  "stale"    -- the leg whose mid moved least over lookback_min while the
                basket's other legs moved (it didn't follow them).
Exit, first of:
  target   -- a resting sell at fair_j at entry (maker, fee 0 on these
              series) fills when the minute's best bid reached it;
  stop     -- the bid falls stop_loss below the bid at entry: sold at the
              bid (taker);
  max_hold -- held max_hold_min: sold at the bid (taker), or with
              complete_on_exit, completed into the full basket by taking
              the other legs when payout - other asks - fees beats the bid
              and is at least the entry (no loss); paid at settlement, and
              ties up the other legs' cost until then;
  end      -- data ends: marked at the bid, still open.

What a 1-minute close-and-extremes candle can't show (no depth, no queue)
makes this optimistic: entries and stops assume one contract fills at the
candle price, and a target fills on the minute's bid high touching it.
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import time

import numpy as np
import pandas as pd

from kalshi_infra import DATA_ROOT
from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.config import atomic_write_json
from kalshi_infra.kalshi.fees import fee
from kalshi_strategies.ARBS.config import SPEC_PATH
from kalshi_strategies.ARBS.single_leg import candles
from kalshi_strategies.ARBS.spec import Basket, load_spec

OUT_DIR = os.path.join(DATA_ROOT, "arbs_single_leg")
TAKER_RATE = 0.07      # every leg market's series, GET /series 2026-10-24 (quadratic x1, maker 0)
_EPS = 1e-9


@dataclasses.dataclass(frozen=True)
class Params:
    entry_edge: float = 0.10      # lag_j / ask_j
    lag_rule: str = "thinnest"
    lookback_min: int = 60
    stop_loss: float = 0.05       # dollars per contract below entry
    max_hold_min: int = 24 * 60
    complete_on_exit: bool = True
    min_price: float = 0.02       # skip asks below (tick-size noise)
    max_spread: float = 0.05      # skip a leg whose own spread is wider
    cooldown_min: int = 60        # after an exit, no re-entry on that basket+leg


@dataclasses.dataclass
class Trade:
    basket_id: str
    ticker: str
    side: str
    entry_ts: int
    entry: float                  # own-side price incl. taker fee
    target: float
    exit_ts: int
    exit: float                   # own-side proceeds per contract (net of fee); for "complete": payout - others
    reason: str
    pnl: float                    # per contract
    hold_min: float
    lag: float


def _tick(v) -> float:
    """A grid price (float32) back on the cent grid: 0.30 is stored as
    0.30000001, which the fee's ceiling would round up a hundredth of a cent."""
    return round(float(v), 2)


def _own(g: candles.Grid, ticker: str, side: str):
    """(bid, ask, bid_high, ask_low) arrays for buying/selling `side`."""
    if side == "yes":
        return g.bid[ticker], g.ask[ticker], g.bid_high[ticker], g.ask_low[ticker]
    return (1.0 - g.ask[ticker], 1.0 - g.bid[ticker], 1.0 - g.ask_low[ticker], 1.0 - g.bid_high[ticker])


def _activity(g: candles.Grid, ticker: str, lookback: int) -> np.ndarray:
    """Rolling count of minutes the book moved: a proxy for how live it is."""
    b, a = g.bid[ticker], g.ask[ticker]
    moved = np.zeros(len(b), dtype=np.float32)
    moved[1:] = ((b[1:] != b[:-1]) | (a[1:] != a[:-1])).astype(np.float32)
    c = np.cumsum(moved)
    out = c.copy()
    out[lookback:] = c[lookback:] - c[:-lookback]
    return out


def _move(mid: np.ndarray, lookback: int) -> np.ndarray:
    out = np.full(len(mid), np.nan, dtype=np.float32)
    out[lookback:] = np.abs(mid[lookback:] - mid[:-lookback])
    return out


def simulate_basket(b: Basket, g: candles.Grid, p: Params) -> list[Trade]:
    legs = [_own(g, l.ticker, l.side) for l in b.legs]
    n = len(g.ts)
    mids = np.stack([(bid + ask) / 2 for bid, ask, _, _ in legs])
    asks = np.stack([ask for _, ask, _, _ in legs])
    valid = ~np.isnan(mids).any(axis=0)
    tot = mids.sum(axis=0)
    if p.lag_rule == "thinnest":
        act = np.stack([_activity(g, l.ticker, p.lookback_min) for l in b.legs])
        lagger = np.argmin(act, axis=0)
    elif p.lag_rule == "stale":
        mv = np.nan_to_num(np.stack([_move(m, p.lookback_min) for m in mids]), nan=9.0)
        # Leg j lags when it moved least while the basket's legs moved at all
        # (NaN = before lookback_min of data, treated as "moved a lot").
        lagger = np.where(mv.max(axis=0) - mv.min(axis=0) > _EPS, np.argmin(mv, axis=0), -1)
        lagger[mv.max(axis=0) >= 9.0] = -1
    else:
        raise ValueError(f"lag_rule {p.lag_rule!r}")
    trades: list[Trade] = []
    for j, l in enumerate(b.legs):
        bid, ask, bid_high, _ = legs[j]
        fair = b.payout - (tot - mids[j])
        lag = fair - ask
        ok = (valid & (lagger == j) & (ask >= p.min_price) & (ask <= 0.99) & (ask - bid <= p.max_spread + _EPS)
              & (lag >= p.entry_edge * ask - _EPS))
        if p.lag_rule == "thinnest":
            ok &= np.arange(n) >= p.lookback_min
        i, cool_until = 0, -1
        cand = np.flatnonzero(ok)
        k = 0
        while k < len(cand):
            i = int(cand[k])
            if i <= cool_until:
                k += 1
                continue
            px = _tick(ask[i])
            entry = px + fee(px, 1.0, TAKER_RATE)
            target = round(min(0.99, float(fair[i])), 2)
            # From the bid we could sell at on entry, not the ask we paid: a
            # stop off the ask fired the next minute on any spread wider than
            # stop_loss (109 of 213 first-run trades, held 1 minute).
            stop = _tick(bid[i]) - p.stop_loss
            end = min(n - 1, i + p.max_hold_min)
            ex_i, ex, reason = end, None, None
            for t in range(i + 1, end + 1):
                if bid_high[t] >= target - _EPS:
                    ex_i, ex, reason = t, target, "target"
                    break
                if bid[t] <= stop + _EPS:
                    s = _tick(bid[t])
                    ex_i, ex, reason = t, s - fee(s, 1.0, TAKER_RATE), "stop"
                    break
            if reason is None:
                s = _tick(bid[end])
                if end == n - 1 and end - i < p.max_hold_min:
                    ex, reason = s, "end"
                else:
                    sell = s - fee(s, 1.0, TAKER_RATE)
                    ex, reason = sell, "max_hold"
                    if p.complete_on_exit:
                        # Buying the other legs now locks payout - others for
                        # this contract (paid at settlement); worth it over
                        # selling when that beats the bid and doesn't lose.
                        others = sum(_tick(asks[m][end]) + fee(_tick(asks[m][end]), 1.0, TAKER_RATE)
                                     for m in range(len(b.legs)) if m != j)
                        locked = b.payout - others
                        if not np.isnan(locked) and locked >= entry - _EPS and locked > sell:
                            ex, reason = locked, "complete"
            trades.append(Trade(basket_id=b.basket_id, ticker=l.ticker, side=l.side, entry_ts=int(g.ts[i]),
                                entry=round(entry, 4), target=target, exit_ts=int(g.ts[ex_i]), exit=round(ex, 4),
                                reason=reason, pnl=round(ex - entry, 4), hold_min=float(ex_i - i),
                                lag=round(float(lag[i]), 4)))
            cool_until = ex_i + p.cooldown_min
            k += 1
    return trades


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    closed = trades[trades.reason != "end"]
    cap = trades.entry
    return {
        "trades": int(len(trades)), "closed": int(len(closed)), "baskets": int(trades.basket_id.nunique()),
        "legs": int(trades.ticker.nunique()),
        "pnl_per_contract": round(float(trades.pnl.sum()), 4),
        "avg_pnl_c": round(100 * float(trades.pnl.mean()), 3),
        "return_on_entry": round(float(trades.pnl.sum() / cap.sum()), 4),
        "win_rate": round(float((trades.pnl > 0).mean()), 3),
        "median_hold_min": float(trades.hold_min.median()),
        "by_reason": {r: {"n": int(len(d)), "pnl": round(float(d.pnl.sum()), 4)} for r, d in trades.groupby("reason")},
        "worst_c": round(100 * float(trades.pnl.min()), 2), "best_c": round(100 * float(trades.pnl.max()), 2),
    }


def one_per_leg(trades: list[Trade]) -> list[Trade]:
    """One open position per (ticker, side): a leg in several baskets
    (KXMIDTERMMOV-TXSEND-P3 is in 3 monotonic ones) signals in all of them at
    once, and it's one contract to buy, not three. First signal wins."""
    out, busy_until = [], {}
    for t in sorted(trades, key=lambda t: (t.entry_ts, t.basket_id)):
        k = (t.ticker, t.side)
        if t.entry_ts <= busy_until.get(k, -1):
            continue
        out.append(t)
        busy_until[k] = t.exit_ts
    return out


def run(baskets: list[Basket], g: candles.Grid, p: Params) -> pd.DataFrame:
    trades = []
    for b in baskets:
        if all(l.ticker in g.bid for l in b.legs):
            trades += simulate_basket(b, g, p)
    cols = [f.name for f in dataclasses.fields(Trade)]
    return pd.DataFrame([dataclasses.asdict(t) for t in one_per_leg(trades)], columns=cols)


SWEEP = {
    "entry_edge": [0.05, 0.10, 0.20, 0.40],
    "lag_rule": ["thinnest", "stale"],
    "stop_loss": [0.03, 0.10],
    "max_hold_min": [6 * 60, 3 * 24 * 60],
    "complete_on_exit": [True, False],
    "max_spread": [0.02, 0.05],
}


def market_open_times(tickers: list[str]) -> dict[str, int]:
    out = {}
    for i in range(0, len(tickers), 100):
        for m in kalshi_get("/trade-api/v2/markets", params={"tickers": ",".join(tickers[i:i + 100]), "limit": 1000}) \
                .get("markets", []):
            if m.get("open_time"):
                out[m["ticker"]] = int(pd.Timestamp(m["open_time"]).timestamp())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=30.0, help="History to backtest, ending now.")
    ap.add_argument("--no-fetch", action="store_true", help="Use the candle cache as is.")
    ap.add_argument("--no-sweep", action="store_true", help="Only the default Params.")
    a = ap.parse_args(argv)
    t_run = time.time()
    baskets = load_spec(SPEC_PATH)
    tickers = sorted({t for b in baskets for t in b.tickers})
    end_ts = int(time.time()) // 60 * 60
    start_ts = end_ts - int(a.days * 86400)
    if not a.no_fetch:
        candles.fetch(tickers, start_ts, end_ts, opened=market_open_times(tickers))
    g = candles.grid(tickers, start_ts, end_ts)
    print(f"[single-leg] grid {len(g.ts)} minutes x {len(tickers)} markets", flush=True)
    combos = [Params()] if a.no_sweep else [
        Params(**dict(zip(SWEEP, vals))) for vals in itertools.product(*SWEEP.values())]
    results = []
    best = None
    for p in combos:
        tr = run(baskets, g, p)
        s = summarize(tr)
        results.append({"params": dataclasses.asdict(p), **s})
        if s.get("trades") and (best is None or s["pnl_per_contract"] > best[1]["pnl_per_contract"]):
            best = (p, s, tr)
        print(f"[single-leg] {p} -> {s.get('trades')} trades, pnl {s.get('pnl_per_contract')}", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    default_tr = run(baskets, g, Params())
    default_tr.to_parquet(os.path.join(OUT_DIR, "trades_default.parquet"), index=False)
    if best is not None:
        best[2].to_parquet(os.path.join(OUT_DIR, "trades_best.parquet"), index=False)
    atomic_write_json(os.path.join(OUT_DIR, "result.json"), {
        "run_at": pd.Timestamp.now(tz="UTC").isoformat(), "runtime_s": round(time.time() - t_run, 1),
        "window": {"start": pd.Timestamp(start_ts, unit="s", tz="UTC").isoformat(),
                   "end": pd.Timestamp(end_ts, unit="s", tz="UTC").isoformat(), "days": a.days},
        "n_baskets": len(baskets), "n_markets": len(tickers),
        "default": {"params": dataclasses.asdict(Params()), **summarize(default_tr)},
        "best": None if best is None else {"params": dataclasses.asdict(best[0]), **best[1]},
        "sweep": sorted(results, key=lambda r: -(r.get("pnl_per_contract") or -1e9)),
    }, indent=None)
    print(f"[single-leg] wrote {OUT_DIR}/result.json", flush=True)


if __name__ == "__main__":
    main()
