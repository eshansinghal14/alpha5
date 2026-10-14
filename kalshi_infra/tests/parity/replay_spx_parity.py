"""Reference-only: compares against the pre-reorg code in the alpha5_legacy git
worktree (commit 0a82f3c; recreate with `git worktree add ../alpha5_legacy 0a82f3c`).
Replay a recorded SPX decision journal through the legacy strategy and the
new generic Quoter with an identical toy fill model; compare the PRICING.

Since the order-manager rewrite (2026-10-10) the new quoter publishes a quote
ladder instead of placing one quote per side, so only takes and the
reservation/lean columns are expected to match. Quote orders and the
`quote_bid`/`quote_ask` columns are no longer comparable (the new ones are
the ladder's top level, capped against others' touch).

Since 2026-10-12 the lean is flat linear (no band term, no exponent) and
takes wait quoter.CONFIRM_S, so leans and take timing no longer match legacy."""
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
from kalshi_infra.kalshi.fees import schedule  # noqa: E402
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

    def buy(self, ticker, side, size, price=None, order_type="limit", intent="take", role="edge"):
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

    def amend(self, oid, size):
        return False

    def cancel(self, oid, force=False):
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
        order_lots=oc.quote_size, max_position=oc.max_position, constant_edge=oc.constant_edge,
        constant_edge_ref_s=None,
        edge_fraction=oc.edge_fraction, lean_dollars=oc.lean_dollars,
        max_markets=oc.max_markets, atm_hysteresis=oc.atm_hysteresis,
        enable_taking=True, enable_quoting=oc.enable_quoting, quoting_edge=oc.quoting_edge,
        max_improve_ticks=oc.max_improve_ticks, quote_levels_behind=0, cancels_token=0.0,
        requote_delay_s=oc.requote_delay_s)
    assert oc.fee_multiplier == 0.07 and oc.maker_fee_multiplier == 0.0 and oc.taker_fee_edge_multiplier == 1.0
    return types.SimpleNamespace(
        quoting=q, trading_time=P.TradingTimeParams(real_trading_delay_s=0, real_trading_cutoff_s=0),
        risk=P.RiskParams(max_loss_usd=1e9, max_signal_age_s=1e9),
        lip=P.LipSettings(enable_lip_quoting=False, lip_cheap_max_price=0.2, lip_cheap_lots=0.0,
                          lip_rich_min_price=0.8, lip_rich_lots=0.0, lip_fair_margin=None,
                          lip_min_payout_usd=0.0, lip_max_markets=None, lip_requeue_min_ahead=None,
                          lip_min_time_left_s=0.0, lip_hold_s=1.0))


# What Kalshi reports for KXINX/KXINXU: "quadratic" x 1 -> taker 0.07, maker 0.
FEES = schedule("quadratic", 1)


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
    new_s, new_ctx = Quoter(new_cfg(oc), NullModel(), FEES, close_time=None, record_debug=True), Ctx()

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
    # eff_yes/eff_no and the lean differ once quotes rest (different quote
    # placement), so they only match while neither side has filled a quote.
    cols = ["bid_reservation", "ask_reservation", "bid_final", "ask_final",
            "quote_bid_reservation", "quote_ask_reservation", "lean_bid", "lean_ask"]
    takes = lambda orders: [x for x in orders if x[0] == "buy" and x[4] == "take"]
    print(f"\n{series}: {len(df)} journal rows replayed -> old {len(o)} decisions, new {len(n)}")
    print(f"  takes: old {len(takes(old_ctx.orders))}  new {len(takes(new_ctx.orders))}  "
          f"identical={takes(old_ctx.orders) == takes(new_ctx.orders)}")
    print(f"  fills : old {len(old_ctx.portfolio.fills)}  new {len(new_ctx.portfolio.fills)}")
    if len(o) == len(n):
        for c in cols:
            a, b = o[c].astype(float).to_numpy(), n[c].astype(float).to_numpy()
            mism = ~((np.isnan(a) & np.isnan(b)) | (np.abs(a - b) < 1e-12))
            print(f"  {c:24s} mismatches: {int(mism.sum())}")
    else:
        print("  decision count differs")
    ot, nt = takes(old_ctx.orders), takes(new_ctx.orders)
    first = next((i for i, (x, y) in enumerate(zip(ot, nt)) if x != y), None)
    if first is not None:
        print("  first take divergence at", first, ot[first], nt[first])


if __name__ == "__main__":
    run("KXINX", 150_000)
    run("KXINXU", 150_000)
