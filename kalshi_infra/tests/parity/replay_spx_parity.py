"""Replay a recorded SPX decision journal through the legacy strategy and the
new generic Quoter with an identical toy fill model; compare orders + logs."""
import dataclasses
import glob
import os
import sys
import types

import numpy as np
import pandas as pd

NEW = r"C:\Users\eshan\Desktop\Coding Projects\alpha5"
OLD = r"C:\Users\eshan\Desktop\Coding Projects\alpha5_legacy"
sys.path.insert(0, NEW)
for p in ("kalshi_latency_arbs/spxw_kxinx", "kalshi_latency_arbs", "kalshi_backtest", "kalshi_infra"):
    sys.path.append(os.path.join(OLD, p))

import sp_options_lag_quoting as old  # noqa: E402
from kalshi_backtest.core import Order, Portfolio  # noqa: E402
from kalshi_infra.trading.fair import Fair, FairUpdate  # noqa: E402
from kalshi_infra.trading.quoter import Quoter  # noqa: E402
from kalshi_infra.trading import params as P  # noqa: E402


@dataclasses.dataclass
class Book:
    ticker: str
    bid: float | None
    ask: float | None
    time: pd.Timestamp = None
    ladder: object = None


class Ctx:
    """Toy venue shared by both runs: takes fill if marketable against the
    current touch; resting quotes fill when a later touch crosses them."""

    def __init__(self):
        self.portfolio = Portfolio(fee_multiplier=0.07, maker_fee_multiplier=0.0)
        self.books: dict[str, Book] = {}
        self.resting: dict[int, Order] = {}
        self.orders: list[tuple] = []
        self._id = 0
        self.time = None
        self.last_event = None

    def book(self, t):
        return self.books.get(t)

    def position(self, t):
        return self.portfolio.position(t)

    def buy(self, ticker, side, size, price=None, order_type="limit", intent="take"):
        self._id += 1
        o = Order(self._id, ticker, side, order_type, price, size, self.time, intent)
        self.orders.append(("buy", ticker, side, round(price, 4), intent, str(self.time)))
        b = self.books.get(ticker)
        yes_px = price if side == "yes" else 1 - price
        crosses = b is not None and ((side == "yes" and b.ask is not None and b.ask <= yes_px + 1e-9) or
                                     (side == "no" and b.bid is not None and b.bid >= yes_px - 1e-9))
        if crosses:
            self.portfolio.record_fill(o, price, size, self.time, "taker")
        elif intent == "quote":
            self.resting[o.order_id] = o
        return o

    def cancel(self, oid):
        if self.resting.pop(oid, None) is not None:
            self.orders.append(("cancel", oid, str(self.time)))

    def set_book(self, ticker, bid, ask):
        self.books[ticker] = Book(ticker, bid, ask, self.time)
        for oid, o in list(self.resting.items()):
            if o.ticker != ticker:
                continue
            yes_px = o.price if o.side == "yes" else 1 - o.price
            if (o.side == "yes" and ask is not None and ask < yes_px - 1e-9) or \
               (o.side == "no" and bid is not None and bid > yes_px + 1e-9):
                self.resting.pop(oid)
                self.portfolio.record_fill(o, o.price, o.size, self.time, "maker")


def new_cfg(oc):
    q = P.QuotingParams(
        order_size=oc.quote_size, max_position=oc.max_position, constant_edge=oc.constant_edge,
        edge_fraction=oc.edge_fraction, lean_multiplier=oc.lean_multiplier, lean_dollars=oc.lean_dollars,
        lean_exponent=oc.lean_exponent, fair_clip_eps=0.0, inflight_timeout_s=oc.inflight_timeout_s,
        take_confirm_s=0.0, max_markets=oc.max_markets, atm_hysteresis=oc.atm_hysteresis,
        enable_quoting=oc.enable_quoting, quoting_edge=oc.quoting_edge,
        max_improve_ticks=oc.max_improve_ticks, requote_improve_ticks=oc.requote_improve_ticks,
        min_quote_life_s=oc.min_quote_life_s, requote_delay_s=oc.requote_delay_s)
    fees = P.FeeParams(taker_fee_multiplier=oc.fee_multiplier, maker_fee_multiplier=oc.maker_fee_multiplier,
                       taker_fee_edge_multiplier=oc.taker_fee_edge_multiplier)
    close = P.CloseParams(last_phase_s=0, last_phase_max_half_band=None, last_phase_otm_fair_max=None,
                          real_trading_cutoff_s=0, close_buffer_s=0)
    risk = types.SimpleNamespace(max_signal_age_s=1e9, near_expiry_s=0, max_signal_age_near_expiry_s=1e9,
                                 fresh_recovery_s=0.0)
    return types.SimpleNamespace(quoting=q, fees=fees, close=close, risk=risk)


class NullModel:
    def on_market_event(self, t, s): return None
    def signal_age_s(self): return 0.0
    def side_block(self, *a): return None


def run(series, n_rows):
    parts = sorted(glob.glob(os.path.join(NEW, f"data/live/{series}/2026-10-08/journal/decisions/part-*.parquet")))
    rows = []
    for p in parts:
        rows.append(pd.read_parquet(p))
        if sum(len(r) for r in rows) >= n_rows:
            break
    df = pd.concat(rows).sort_values("time").head(n_rows).reset_index(drop=True)

    oc = old.QuotingConfig()
    assert oc.lot_size == oc.quote_size
    old_s, old_ctx = old.SPOptionsLagQuotingStrategy(oc, record_debug=True), Ctx()
    new_s, new_ctx = Quoter(new_cfg(oc), NullModel(), close_time=None, record_debug=True), Ctx()

    for r in df.itertuples(index=False):
        for ctx in (old_ctx, new_ctx):
            ctx.time = r.time
            ctx.set_book(r.ticker, None if pd.isna(r.market_bid) else r.market_bid,
                         None if pd.isna(r.market_ask) else r.market_ask)
        if r.event_kind == "signal":
            old_ctx.last_event = {"ticker": r.ticker, "prob": r.prob, "prob_lo": r.prob_lo, "prob_hi": r.prob_hi}
            new_ctx.last_event = FairUpdate({r.ticker: Fair(r.prob, r.prob_lo, r.prob_hi)})
        else:
            old_ctx.last_event = new_ctx.last_event = old_ctx.books[r.ticker]
        old_s(old_ctx)
        new_s(new_ctx)

    o, n = pd.DataFrame(old_s.debug_log), pd.DataFrame(new_s.debug_log)
    cols = ["bid_reservation", "ask_reservation", "bid_final", "ask_final",
            "quote_bid_reservation", "quote_ask_reservation", "quote_bid", "quote_ask",
            "lean_bid", "lean_ask", "eff_yes", "eff_no"]
    print(f"\n{series}: {len(df)} journal rows replayed -> old {len(o)} decisions, new {len(n)}")
    print(f"  orders: old {len(old_ctx.orders)}  new {len(new_ctx.orders)}  identical={old_ctx.orders == new_ctx.orders}")
    print(f"  fills : old {len(old_ctx.portfolio.fills)}  new {len(new_ctx.portfolio.fills)}")
    if len(o) == len(n):
        for c in cols:
            a, b = o[c].astype(float).to_numpy(), n[c].astype(float).to_numpy()
            mism = ~((np.isnan(a) & np.isnan(b)) | (np.abs(a - b) < 1e-12))
            print(f"  {c:24s} mismatches: {int(mism.sum())}")
    else:
        print("  decision count differs")
    first = next((i for i, (x, y) in enumerate(zip(old_ctx.orders, new_ctx.orders)) if x != y), None)
    if first is not None:
        print("  first order divergence at", first, old_ctx.orders[first], new_ctx.orders[first])


if __name__ == "__main__":
    run("KXINX", 150_000)
    run("KXINXU", 150_000)
