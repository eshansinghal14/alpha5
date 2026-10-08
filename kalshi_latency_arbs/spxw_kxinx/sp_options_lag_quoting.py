"""
S&P options-lag strategy: prices every Kalshi KXINX bucket off the SPXW/ES
option-implied probability and trades it against Kalshi's own book. Pricing
comes from latency_arbs_functions/pricing (Breeden-Litzenberger, model-free
-- see latency_arbs_functions.py's module docstring), never from Kalshi's
own book.

TAKING is one comparison per side, run on each wakeup:

    edge      = const_edge + ci_edge + fee_edge + lean_edge
    buy yes   when  prob - bid_edge >= market_ask
    sell yes  when  prob + ask_edge <= market_bid   (placed as a no-buy)

where each edge term is additive and non-negative (see QuotingConfig), so a
reservation can never land on the wrong side of fair value however loaded
the book is -- a property of the arithmetic, not a convention to maintain.

QUOTING (off unless `enable_quoting`) rests `quote_size` on each side at the
first tick outside that SAME edge plus a flat `quoting_edge`, and requotes
whenever the implied probability moves the level. Two things make it
different from a take rather than just a cheaper one:

  * a quote is only ever hit by someone who thinks it is wrong, whereas a
    take crosses a price that exists right now -- hence `quoting_edge` on
    top of the shared edge (constant + CI + lean) rather than in place of
    any of it. The FEE term is the one piece the two do not share: a take
    covers the taker fee at the touch it crosses, a quote only the maker
    fee it would actually pay;
  * a quote never crosses. Its level is capped a tick inside the opposite
    touch, so if the edge would put it through the book it rests just below
    the crossing level instead and the taking test -- which is priced with
    the taker fee -- is left to decide whether crossing is worth it;
  * being hit is information. After our own quote on a (ticker, side)
    trades, that side stays unquoted for `requote_delay_s`; requoting
    instantly into the level that just traded is how a quoter gets run over
    by one informed order.

**ATM selection is a data limit, not an edge filter.** By default every
market is priced and traded on its own merits -- gating on "closest to
50/50" buys nothing, since the edge test is per-market either way. But
`max_markets` caps how many buckets may be traded at once, keeping the N
closest to 50/50, because the live option feed can only carry so many
chains: a backtest trading 30 tickers off cached data describes a session
that cannot be run. Ranked per decision, so the set follows the market, and
a bucket that drops out has its resting quotes withdrawn.

The position cap and the inventory LEAN are both priced off an EFFECTIVE
position: confirmed fills, plus orders sent within `inflight_timeout_s`
(unconfirmed, see `_inflight`), plus whatever we have resting as a quote.
With a ~34ms round trip and a re-decision on every book tick, a market can
otherwise run from flat to its cap in one burst with zero lean charged on
any order in it (measured: 60% of same-ticker fills on 2026-08-28 landed
inside one round trip of the previous one) -- and a resting quote is exactly
the same unconfirmed exposure, just with no timeout on it.

Scope notes: no inventory flattening before settlement -- a filled position
rides to settlement, and leaning only slows accumulation. The class doesn't
care where its signal came from, so live trading swaps only the data source.

Runs on kalshi_backtest's engine via the strategy_fn(ctx) convention; see
run_backtest.py in this folder.
"""
import dataclasses
import math
import os
import sys

import pandas as pd

_INFRA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "kalshi_infra")
_ARBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_BACKTEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "kalshi_backtest")
for _p in (_INFRA_DIR, _ARBS_DIR, _BACKTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import MAKER_FEE_MULTIPLIER, kalshi_fee

# Signal computation lives in kalshi_latency_arbs/spxw_signal.py, shared
# verbatim with spxw_kxinxu/kxinxu_lag_quoting.py. Re-exported here (not
# imported privately) so callers doing `import sp_options_lag_quoting as
# strat; strat.precompute_signal(...)` keep working.
# precompute_atm_signal is re-exported too, but is no longer used by this
# strategy -- see its own docstring.
from spxw_signal import (  # noqa: F401 -- re-exports, see above
    precompute_signal, precompute_atm_signal, compute_prob_grid,
)

# Kalshi's price increment. Quotes rest at a tick boundary, so this is the
# resolution of "the first level outside the edge we require".
TICK = 0.01
# A quote at 0 or 1 is not a real price on this venue.
MIN_QUOTE_PRICE = TICK
MAX_QUOTE_PRICE = 1.0 - TICK


@dataclasses.dataclass
class QuotingConfig:
    lot_size: float = 1.0
    # Cap on |yes_qty - no_qty| PER MARKET, not per event -- each bucket is an
    # independent bet, so an event-wide cap would let one crowded bucket
    # starve the rest.
    max_position: float = 10.0
    # Flat dollar floor, charged on both sides. The only term that doesn't
    # scale with anything: CI/fee/lean all approach zero exactly when the
    # model is confident and the touch is cheap, which is where a systematic
    # pricing bias would otherwise be enough on its own to trigger a take.
    # TAKER flat floor only -- quotes use quoting_edge instead (see _act).
    # 1c is Kalshi's tick, the smallest floor that can move a reservation
    # across a real price level.
    #
    # Earlier note, kept because it still applies to quoting_edge below at
    # 0.5c -- deliberately SUB-TICK. For a take this is fully effective: the
    # crossing test compares a continuous reservation against a 1c-quantised
    # touch, so half a cent genuinely moves whether we cross. For a resting
    # quote it is only partly effective, since _desired_quote rounds to the
    # first tick outside the reservation and half a cent crosses a tick
    # boundary only about half the time. Re-tune against measured markouts.
    constant_edge: float = 0.01
    # Fraction of the distance from prob out to the relevant bound (prob_lo
    # buying, prob_hi selling) demanded as edge. 1.0 = take only outside the
    # band entirely. The band is already the union of call- and put-side
    # bounds, so call/put disagreement widens this directly.
    edge_fraction: float = 1.0
    # Extra edge to GROW an existing position, as a multiple of that same
    # bound distance scaled by |net_position| / max_position. Never charged to
    # the reducing side, so unwinding never costs more edge than entering.
    lean_multiplier: float = 1.0
    # Flat dollar lean at full inventory, scaled by the same ratio but NOT by
    # the CI width. lean_multiplier alone ties the inventory brake to model
    # uncertainty, which are unrelated things: measured on the 2026-08-28
    # live session, T7719 had a band so tight that the largest lean the
    # multiplier could produce was 0.185c against 0.487c of slack, so the
    # market ran flat -> full cap in seconds and inventory never entered into
    # it. This term is to leaning what constant_edge is to the edge -- the
    # part that still bites when the model is confident.
    # 2c at full inventory. Sized against the incident above: the largest
    # lean the multiplier alone could produce there was 0.185c against 0.487c
    # of slack, so anything under ~0.5c does not change a decision. 2c is
    # twice constant_edge -- enough to actually stop accumulation at the cap,
    # while the convexity below keeps it nearly free at low inventory.
    lean_dollars: float = 0.02
    # Lean scales with (position/max_position) ** this. 1.0 is linear (the
    # original shape); 2.0 leans barely at all early and hard near the cap,
    # which is the asymmetry wanted -- inventory is only dangerous once it is
    # large, so charging edge proportionally from the first lot mostly just
    # suppresses good trades. Exposed rather than hardcoded so it can be
    # swept against markouts.
    lean_exponent: float = 2.0
    # Trade only the N markets whose implied probability sits closest to 0.5
    # right now; None = every market on its own merits.
    #
    # This is a DATA constraint, not a strategy preference. IB streams a
    # bounded number of option chains, so only a handful of Kalshi buckets can
    # be priced live at all -- a backtest trading 30 tickers off cached data
    # describes a session that cannot actually be run. Ranked per decision off
    # the latest signal for each ticker, so the set follows the market.
    #
    # 5 is what IB will carry for KXINX. KXINXU shares this class and its
    # 60-market ladder is capped the same way as a result -- set it back to
    # None for any run that is deliberately not feed-limited.
    max_markets: int | None = 5
    # A market already in the ATM set stays in until it ranks below
    # max_markets + this. Without it the 5th/6th buckets swapped places on
    # every tick of noise: measured 2026-10-08 on KXINXU, 27 membership
    # flips/min, each one pulling and re-placing both quotes.
    atm_hysteresis: int = 2
    # Must match the engine's own Portfolio (engine.Backtest also defaults to
    # 0.07). taker_fee_edge_multiplier of 1.0 is breakeven-after-fees; higher
    # demands margin beyond covering the fee.
    fee_multiplier: float = 0.07
    taker_fee_edge_multiplier: float = 1.0
    # How long a sent order keeps counting against the cap before we assume
    # we'd have seen its fill. Must exceed LatencyModel send_s (~0.034s
    # measured) plus processing. Too long just under-trades near the cap,
    # which is the safe direction.
    inflight_timeout_s: float = 0.5

    # --- quoting ---
    # Resting is a genuinely different risk profile from taking, and every
    # config and backtest written before this existed assumed take-only --
    # so results either side of this flag are not comparable.
    enable_quoting: bool = True
    # Extra flat edge a RESTING quote must clear, on top of constant + CI +
    # lean and the fee a MAKER actually pays -- not the taker fee. Operator-
    # set, not calibrated against anything yet; re-tune against quote markouts
    # once there are some.
    quoting_edge: float = 0.005
    # The fee a filled resting quote pays, as its own multiplier. Separate
    # from fee_multiplier because a quote and a take are charged differently:
    # the taking edge covers the taker fee at the touch it would cross, while
    # a quote should only demand cover for what a maker fill costs. Defaults
    # to the engine's own MAKER_FEE_MULTIPLIER (0.0 for KXINX/KXINXU, checked
    # against the published schedule -- see core.MAKER_FEE_MULTIPLIER).
    maker_fee_multiplier: float = MAKER_FEE_MULTIPLIER
    quote_size: float = 1.0
    # Cap on how far INSIDE the near touch a quote may be placed, in ticks.
    #
    # floor(reservation) is the MOST we would ever pay, and it was being used
    # as the target -- so in a wide market we posted at our own limit and kept
    # almost no edge. Measured on KXINX-26AUG17H1600: 6% of quotes sat above
    # best_bid+1tick, median 2c over but p90 24c and max 32c of edge handed
    # over, and the rate rises with width (0% at a 1-2c spread, 12% at 4c).
    #
    # Being nearer the touch is not free: a higher bid fills whenever any
    # seller crosses to it, so it has strictly MORE fill probability. What
    # makes the cap favourable in a wide market is adverse selection -- if
    # nobody has crossed a 0.40 bid, whoever comes 13c down to hit 0.54 is a
    # market order or someone informed, and 1c of edge is no cushion against
    # that flow while 14c is.
    #
    # None disables the cap (the old behaviour). 1 is maximally conservative:
    # improve the touch by exactly one tick and no more.
    max_improve_ticks: int | None = 1
    # Ticks of improvement required before abandoning a resting quote that is
    # still acceptable. Cancel/replace forfeits FIFO queue position, so
    # chasing the level by a tick trades a real asset (priority, earned by
    # waiting) for a nominal one. A quote that has become MORE conservative
    # than today's level is never cancelled for that reason alone -- it still
    # clears the edge, it just earns less.
    #
    # Motivated by measurement, not taste: with unconditional requoting the
    # 2026-08-24 backtest fired 145,134 orders for 155 fills (936 per fill)
    # and the QUIET 2026-08-26 session still managed 51,171 for 46 -- about
    # 6/sec sustained against Kalshi's ~10/sec limit, which is not
    # deployable regardless of what it does to PnL.
    requote_improve_ticks: float = 2.0
    # A quote younger than this is not replaced for drifting a tick or two
    # inside its reservation -- only if fair value crosses it outright (it
    # would then be buying above / selling below fair). Option-implied fair
    # moves ~0.3c per signal at ~11 signals/s, so without this every quote
    # was replaced every ~0.2s. Replaying the 2026-10-08 KXINX/KXINXU journals:
    # 41/s and 20/s orders at 0s, 8/s and 7/s at 2s; the cost is that ~1/3 of
    # KXINX decisions see a quote sitting inside its reservation
    # (p95 ~4c, never past fair).
    min_quote_life_s: float = 2.0
    # How long a (ticker, side) stays unquoted after our own quote there
    # traded. Whoever hit us had a reason, and the level they hit is the one
    # we'd otherwise requote at instantly.
    requote_delay_s: float = 1.0


class SPOptionsLagQuotingStrategy:
    """
    Stateful strategy_fn for kalshi_backtest.engine.Backtest.run. Pass
    `strategy.__call__` as strategy_fn (Backtest.run just calls it, so a
    callable object with state works like the plain-function convention in
    strategies/crossing_arb.py).

    State is: the latest option-implied signal per ticker, a short list of
    just-sent order sizes per (ticker, side) not yet aged out, and (when
    quoting) the one resting quote per (ticker, side) plus when that side
    last got hit. Position is read from ctx.position(ticker) each wakeup --
    that's the authoritative number, and nothing here shadows it with its
    own fill accounting.
    """

    def __init__(self, config: QuotingConfig = None, record_debug: bool = False):
        self.config = config or QuotingConfig()
        self.signals: dict[str, dict] = {}                    # ticker -> latest signal payload
        self._sent: dict[tuple[str, str], list[tuple]] = {}   # (ticker, side) -> [(sent_time, size)]
        # (ticker, side) -> {"order_id", "price", "size"} for the one quote
        # we believe is resting there. `price` is in that side's own terms.
        self._quotes: dict[tuple[str, str], dict] = {}
        # (ticker, side) -> don't quote again before this time (see _sync_quote_fills)
        self._quote_block_until: dict[tuple[str, str], pd.Timestamp] = {}
        self._fills_seen = 0
        self._atm_set: set[str] = set()
        self.record_debug = record_debug
        self.debug_log: list[dict] = []

    def __call__(self, ctx):
        """Evaluate the one ticker this wakeup concerns.

        A Kalshi quote/trade re-evaluates its own ticker against the last
        fair value computed for it, since the touch we'd cross can move with
        no new option-side data.
        """
        # Across ALL tickers, not just this wakeup's: a quote is hit whenever
        # someone crosses it, which is unrelated to whatever woke us. Keyed on
        # having quotes rather than on enable_quoting -- turning quoting off
        # doesn't retract what is already resting, so the bookkeeping for it
        # has to keep running until it is gone.
        if self._quotes:
            self._sync_quote_fills(ctx)
        else:
            self._fills_seen = len(ctx.portfolio.fills)
        evt = ctx.last_event
        if isinstance(evt, dict):
            ticker = evt.get("ticker")
            if ticker is None:
                return
            self.signals[ticker] = evt
        else:
            ticker = getattr(evt, "ticker", None)
            if ticker is None or ticker not in self.signals:
                return  # no fair value for this market yet -- nothing to compare against
        self._act(ctx, ticker)

    # -----------------------------------------------------------------
    # Quote state
    # -----------------------------------------------------------------

    def live_quote_order_ids(self) -> list[int]:
        """Order ids of every quote we believe is resting. Read by
        LiveEngine.pull_quotes to withdraw them from the market."""
        return [q["order_id"] for q in self._quotes.values()]

    def clear_quotes(self):
        """Forget every quote WITHOUT cancelling it. Called by the engine
        immediately after it has cancelled them itself, so the next decision
        quotes from scratch instead of believing in orders that are gone."""
        self._quotes.clear()

    def drop_quote(self, order_id: int):
        """Forget one quote that never reached the book -- rate limited,
        blocked by a guard, rejected by the venue.

        Without this the strategy prices every later decision against a
        quote that does not exist, and since it only requotes when the LEVEL
        changes, that market can sit unquoted indefinitely. The engine calls
        this from every path that drops an order (see
        LiveEngine._quote_not_resting).
        """
        for key, quote in list(self._quotes.items()):
            if quote["order_id"] == order_id:
                del self._quotes[key]
                return

    def _sync_quote_fills(self, ctx):
        """Retire any quote of ours that has traded and start its requote
        delay.

        Read off the portfolio's fill list rather than a callback: every
        fill in both engines funnels through Portfolio.record_fill, so this
        is the one signal that exists identically in the backtest and live.
        The index resets when the list SHRINKS -- LiveEngine.reset_session
        swaps in a whole new Portfolio when real trading is armed.
        """
        fills = ctx.portfolio.fills
        if len(fills) < self._fills_seen:
            self._fills_seen = 0
        if len(fills) == self._fills_seen:
            return
        by_order = {q["order_id"]: key for key, q in self._quotes.items()}
        for fill in fills[self._fills_seen:]:
            key = by_order.pop(fill.order_id, None)
            if key is None:
                continue
            self._quotes.pop(key, None)
            # Cancels whatever is left if this was a partial fill. A no-op
            # for the whole-lot case, which is the only one quote_size=1 can
            # produce -- but a stranded remainder would rest untracked
            # forever, which is the expensive way to find that out.
            ctx.cancel(fill.order_id)
            self._quote_block_until[key] = (fill.time
                                            + pd.Timedelta(seconds=self.config.requote_delay_s))
        self._fills_seen = len(fills)

    def _resting(self, ticker: str, side: str) -> float:
        q = self._quotes.get((ticker, side))
        return 0.0 if q is None else q["size"]

    def _inflight(self, ticker: str, side: str, now: pd.Timestamp) -> float:
        """Size sent on (ticker, side) within the last inflight_timeout_s,
        pruning older entries in passing.

        A taker order fills on arrival or is dropped, so we only need how much
        could still be unconfirmed -- not which order became which fill. That
        is why this is a size total rather than per-order tracking."""
        key = (ticker, side)
        recent = self._sent.get(key)
        if not recent:
            return 0.0
        cutoff = now - pd.Timedelta(seconds=self.config.inflight_timeout_s)
        recent = [(t, s) for t, s in recent if t > cutoff]
        self._sent[key] = recent
        return sum(s for _, s in recent)

    def _atm_tradeable(self, ticker: str) -> bool:
        """Whether `ticker` is currently among the `max_markets` buckets
        closest to 50/50, and therefore one we could actually price live.

        Ranked off `self.signals`, which holds only signals already seen, so
        this is causal -- the backtest and live make the same call at the same
        instant. Ties break on ticker so the set never flickers between two
        equidistant buckets on reordering alone. Members keep their place
        until they fall below max_markets + atm_hysteresis.
        """
        n = self.config.max_markets
        if n is None:
            return True
        ranked = sorted(
            (t for t, s in self.signals.items() if s.get("prob") is not None),
            key=lambda t: (abs(self.signals[t]["prob"] - 0.5), t))
        keep_within = set(ranked[:n + max(self.config.atm_hysteresis, 0)])
        members = [t for t in ranked if t in self._atm_set and t in keep_within]
        for t in ranked:
            if len(members) >= n:
                break
            if t not in members:
                members.append(t)
        self._atm_set = set(members[:n])
        return ticker in self._atm_set

    def _act(self, ctx, ticker: str):
        """One take/no-take decision per side for `ticker`.

        The fee is part of the edge, not a separate test applied at crossing
        time: with `edge = ci_edge + fee_edge + lean_edge` the crossing
        condition is simply "does our reservation clear the touch". An
        earlier version kept fee out of the reservation and re-applied it
        inside the crossing check, so the logged reservation prices didn't
        correspond to the prices the strategy would actually trade at.
        """
        cfg = self.config
        signal = self.signals[ticker]
        pos = ctx.position(ticker)
        now = ctx.time

        prob = signal["prob"]
        lo, hi = signal["prob_lo"], signal["prob_hi"]
        ci_half = max((hi - lo) / 2.0, 0.0)   # logged only; the edge uses the bounds directly

        # Each side measures to the bound that matters for it, NOT a symmetric
        # prob +/- ci_half. The band is asymmetric exactly during the spikes
        # this edge defends against: a pulled level once drove prob to 1.0
        # (clipped) with a band of [0.074, 1.0], where prob - ci_half lands on
        # the band's midpoint (0.537) rather than its floor, and cleared a
        # 0.14 ask. edge_fraction stays a dial: 1.0 = all the way to the bound.
        ci_edge_bid = cfg.edge_fraction * max(prob - lo, 0.0)
        ci_edge_ask = cfg.edge_fraction * max(hi - prob, 0.0)

        net_position = pos.yes_qty - pos.no_qty          # positive = net long yes
        max_pos = cfg.max_position
        inflight_yes = self._inflight(ticker, "yes", now)
        inflight_no = self._inflight(ticker, "no", now)

        # Lean is charged on the position this order would leave us with if
        # everything already sent fills -- NOT on net_position alone.
        #
        # net_position can't see orders still in flight, and the round trip is
        # ~34ms while the strategy re-decides on every book tick. Measured on
        # the 2026-08-28 live session: 60% of same-ticker fills landed within
        # one round trip of the previous one, so those orders were all priced
        # at the inventory we held BEFORE the burst -- lean was identically
        # zero for the whole run from flat to the cap. The cap already counts
        # in-flight size for exactly this reason (see _inflight); the lean
        # simply wasn't given the same treatment.
        #
        # Per side, like the cap: each side leans on its own worst case rather
        # than netting against the other's in-flight orders, which aren't
        # guaranteed to fill.
        #
        # A resting quote counts the same way. It is unconfirmed exposure
        # exactly like an in-flight order, except nothing ages it out -- it
        # sits there until someone hits it -- so leaving it out would let a
        # market carry a quote on each side plus a full cap of position.
        resting_yes = self._resting(ticker, "yes")
        resting_no = self._resting(ticker, "no")
        eff_yes = net_position + inflight_yes + resting_yes  # if everything on the yes side fills
        eff_no = net_position - inflight_no - resting_no     # if everything on the no side fills

        def _ratio(pos_qty: float) -> float:
            return 0.0 if max_pos <= 0 else max(-1.0, min(1.0, pos_qty / max_pos))

        # Charged only to the side that would ADD to the position.
        ratio_bid, ratio_ask = _ratio(eff_yes), _ratio(eff_no)
        lean_bid = ((cfg.lean_multiplier * max(prob - lo, 0.0) + cfg.lean_dollars)
                    * max(ratio_bid, 0.0) ** cfg.lean_exponent)
        lean_ask = ((cfg.lean_multiplier * max(hi - prob, 0.0) + cfg.lean_dollars)
                    * max(-ratio_ask, 0.0) ** cfg.lean_exponent)
        # Logged as the plain inventory ratio; the effective ones drive pricing.
        ratio = _ratio(net_position)

        book = ctx.book(ticker)
        market_bid = book.bid if book is not None else None
        market_ask = book.ask if book is not None else None

        # Fee is priced at the touch we'd actually cross, so each side gets
        # its own term (they differ whenever the book isn't symmetric).
        fee_bid = (kalshi_fee(market_ask, 1.0, cfg.fee_multiplier) * cfg.taker_fee_edge_multiplier
                   if market_ask is not None else 0.0)
        fee_ask = (kalshi_fee(market_bid, 1.0, cfg.fee_multiplier) * cfg.taker_fee_edge_multiplier
                   if market_bid is not None else 0.0)

        # The edge every order demands regardless of how it executes. The
        # fee is added per execution style below, because the two pay
        # different fees.
        base_bid_edge = cfg.constant_edge + ci_edge_bid + lean_bid
        base_ask_edge = cfg.constant_edge + ci_edge_ask + lean_ask
        bid_edge = base_bid_edge + fee_bid
        ask_edge = base_ask_edge + fee_ask
        bid_reservation = prob - bid_edge
        ask_reservation = prob + ask_edge

        # Outside the ATM set this market cannot be priced from the live
        # option feed at all, so neither side may take and any quote resting
        # here has to come down (handled below, via want=None).
        atm_ok = self._atm_tradeable(ticker)
        take_bid = atm_ok and market_ask is not None and bid_reservation >= market_ask
        take_ask = atm_ok and market_bid is not None and ask_reservation <= market_bid

        # Each side is gated on its OWN worst case only -- never netted
        # against the other side's in-flight orders, which would assume those
        # are guaranteed to fill and pull the position back. They aren't.
        # Same eff_yes/eff_no the lean above is priced off, so the cap and the
        # brake now act on one consistent view of the position.
        cap_allows_yes = eff_yes < max_pos - 1e-9
        cap_allows_no = eff_no > -max_pos + 1e-9

        bid_final = market_ask if (take_bid and cap_allows_yes) else None
        ask_final = market_bid if (take_ask and cap_allows_no) else None

        if bid_final is not None:
            ctx.buy(ticker=ticker, side="yes", size=cfg.lot_size,
                    price=round(bid_final, 2), order_type="limit")
            self._sent.setdefault((ticker, "yes"), []).append((now, cfg.lot_size))
        if ask_final is not None:
            # Selling yes at `ask_final` is buying no at 1 - ask_final -- the
            # same YES-side convention book.py/core.py use throughout.
            ctx.buy(ticker=ticker, side="no", size=cfg.lot_size,
                    price=round(1.0 - ask_final, 2), order_type="limit")
            self._sent.setdefault((ticker, "no"), []).append((now, cfg.lot_size))

        # After the takes, so the quote cap sees the size we just sent (the
        # takes are in self._sent by now and _manage_quotes re-reads it).
        quote_bid = quote_ask = None
        quoting_edge = cfg.quoting_edge if (cfg.enable_quoting and atm_ok) else 0.0
        # A quote carries the MAKER fee, not the taker fee the reservations
        # above are built on. Inheriting the taker fee made a quote demand
        # cover for a cost it never pays -- 2c of it at a 50/50 touch, which
        # is the whole quoting_edge again -- and pushed every quote a tick or
        # two further out than it needed to be. Priced at the pre-fee
        # reservation rather than the final quote price: the two differ by
        # less than a tick, and the exact level isn't known until _desired_quote
        # rounds it.
        # A quote's flat floor is quoting_edge IN PLACE OF constant_edge, not
        # on top of it: the two are independent operator choices (a resting
        # quote and a cross are different risks), and while quoting_edge was
        # additive "set the taker floor to 1c" silently moved the quoter's to
        # 1.5c. The scaling terms (CI, lean) are shared -- they describe the
        # market and the inventory, not which side of the spread we are on.
        q_base_bid = quoting_edge + ci_edge_bid + lean_bid
        q_base_ask = quoting_edge + ci_edge_ask + lean_ask
        q_fee_bid = kalshi_fee(prob - q_base_bid, 1.0, cfg.maker_fee_multiplier)
        q_fee_ask = kalshi_fee(prob + q_base_ask, 1.0, cfg.maker_fee_multiplier)
        quote_bid_reservation = prob - (q_base_bid + q_fee_bid)
        quote_ask_reservation = prob + (q_base_ask + q_fee_ask)
        # Still entered when quoting has just been turned OFF, so that any
        # quote left resting from before is cancelled rather than abandoned.
        if (cfg.enable_quoting and atm_ok) or resting_yes or resting_no:
            quote_bid, quote_ask = self._manage_quotes(
                ctx, ticker, quote_bid_reservation, quote_ask_reservation,
                market_bid, market_ask, net_position, max_pos, now,
                allowed=cfg.enable_quoting and atm_ok, prob=prob)

        if self.record_debug:
            self.debug_log.append({
                "time": now, "ticker": ticker, "prob": prob, "prob_lo": lo, "prob_hi": hi,
                "ci_half": ci_half, "ci_edge": ci_edge_bid, "ci_edge_ask": ci_edge_ask,
                "constant_edge": cfg.constant_edge,
                "bid_edge": bid_edge, "ask_edge": ask_edge,
                "lean_bid": lean_bid, "lean_ask": lean_ask,
                "taker_fee_bid": fee_bid, "taker_fee_ask": fee_ask,
                "net_position": net_position, "yes_qty": pos.yes_qty, "no_qty": pos.no_qty,
                # The position each side actually leaned on -- diverges from
                # net_position exactly during the bursts that motivated it.
                "eff_yes": eff_yes, "eff_no": eff_no, "ratio": ratio,
                "lean_dollars": cfg.lean_dollars,
                "event_kind": "signal" if isinstance(ctx.last_event, dict) else type(ctx.last_event).__name__,
                "event_ticker": ticker,
                "market_bid": market_bid, "market_ask": market_ask,
                "bid_reservation": bid_reservation, "ask_reservation": ask_reservation,
                "bid_final": bid_final, "ask_final": ask_final,
                "inflight_yes": inflight_yes, "inflight_no": inflight_no,
                "resting_yes": resting_yes, "resting_no": resting_no,
                "cap_allows_yes": cap_allows_yes, "cap_allows_no": cap_allows_no,
                # Quotes are logged in YES terms like every other price here,
                # so the chart can draw them on the same axis as the touch and
                # the reservations. The no-side quote actually sent is at
                # 1 - quote_ask.
                "atm_tradeable": atm_ok, "max_markets": cfg.max_markets,
                "maker_fee_bid": q_fee_bid, "maker_fee_ask": q_fee_ask,
                "quoting_edge": quoting_edge,
                "quote_bid_reservation": quote_bid_reservation,
                "quote_ask_reservation": quote_ask_reservation,
                "quote_bid": quote_bid, "quote_ask": quote_ask,
            })

    # -----------------------------------------------------------------
    # Quoting
    # -----------------------------------------------------------------

    def _manage_quotes(self, ctx, ticker: str, bid_reservation: float, ask_reservation: float,
                       market_bid, market_ask, net_position: float, max_pos: float,
                       now, allowed: bool = True,
                       prob: float | None = None) -> tuple[float | None, float | None]:
        """Bring both sides' resting quotes in line with the current
        reservations. Returns the (bid, ask) we ended up quoting, in YES
        terms, either entry None if that side isn't quoted.

        Cancel-then-replace, one order per side. There is a moment where the
        old quote is cancelled and the new one hasn't arrived, and a moment
        where neither has been confirmed -- both are the same unconfirmed
        window `_inflight` already exists for, and both err toward being
        flat rather than doubled up.
        """
        cfg = self.config
        # Re-read rather than reuse _act's: a take placed moments ago in this
        # same call has to count against the room a quote is allowed to use.
        base_yes = net_position + self._inflight(ticker, "yes", now)
        base_no = net_position - self._inflight(ticker, "no", now)

        # No usable touch on EITHER side: no data yet, or a ladder book.py has
        # distrusted (drift, or an impossible bid>ask). A quote's price is
        # built on being capped a tick inside the opposite touch, and there is
        # nothing here to cap against -- so withdraw rather than rest blind.
        # One-sided is fine and ordinary: tail buckets are quoted one-sided
        # all day, and that side still caps.
        # `allowed=False` (quoting switched off, or this market outside the
        # ATM set) still runs the whole pass -- both sides come out wanting
        # nothing, which is what cancels anything already resting here.
        have_touch = allowed and (market_bid is not None or market_ask is not None)

        # Room for the NEW quote, measured without our own current quote on
        # that side -- it is about to be cancelled and replaced.
        want_yes = self._desired_quote(
            "yes", bid_reservation, market_bid, market_ask,
            allowed=have_touch and base_yes + cfg.quote_size <= max_pos + 1e-9,
            blocked_until=self._quote_block_until.get((ticker, "yes")), now=now)
        want_no = self._desired_quote(
            "no", ask_reservation, market_bid, market_ask,
            allowed=have_touch and base_no - cfg.quote_size >= -max_pos - 1e-9,
            blocked_until=self._quote_block_until.get((ticker, "no")), now=now)

        # A tick, in the side's own price terms.
        improve = cfg.requote_improve_ticks * 0.01
        for side, want in (("yes", want_yes), ("no", want_no)):
            key = (ticker, side)
            live = self._quotes.get(key)
            if live is not None:
                if want is None:
                    stale = True                      # not allowed to quote here at all now
                else:
                    # Inside the edge we demand (stale), or the level has run
                    # far enough ahead to be worth our queue position (chase).
                    inside = live["price"] > want + 1e-9
                    chase = (want - live["price"]) >= improve - 1e-9
                    stale = inside or chase
                    young = now - live.get("placed", now) < pd.Timedelta(seconds=cfg.min_quote_life_s)
                    if stale and young and not self._past_fair(side, live["price"], prob):
                        stale = False
                if stale:
                    ctx.cancel(live["order_id"])
                    self._quotes.pop(key, None)
                    live = None
            if live is None and want is not None:
                order = ctx.buy(ticker=ticker, side=side, size=cfg.quote_size,
                                price=want, order_type="limit", intent="quote")
                self._quotes[key] = {"order_id": order.order_id, "price": want,
                                     "size": cfg.quote_size, "placed": now}

        yes_q = self._quotes.get((ticker, "yes"))
        no_q = self._quotes.get((ticker, "no"))
        return (None if yes_q is None else yes_q["price"],
                None if no_q is None else round(1.0 - no_q["price"], 2))

    @staticmethod
    def _past_fair(side: str, price: float, prob: float | None) -> bool:
        """Whether a resting quote (in its own side's terms) is at or beyond
        fair value -- buying yes at >= prob, or buying no at >= 1 - prob.
        Unknown fair counts as past it, so the quote is replaced."""
        if prob is None or math.isnan(prob):
            return True
        fair = prob if side == "yes" else 1.0 - prob
        return price >= fair - 1e-9

    def _desired_quote(self, side: str, reservation: float, market_bid, market_ask,
                       allowed: bool, blocked_until, now) -> float | None:
        """Where we want to rest on `side`, in that side's own price terms,
        or None for "don't quote here right now".

        The level is the first tick OUTSIDE the reservation -- rounded down
        for a yes buy, up for a yes sale -- not the nearest one: rounding to
        nearest would put half our quotes a tick inside the edge we just
        demanded, which is the one thing the edge exists to prevent.

        Capped a tick inside the opposite touch so a quote can never cross.
        If it were marketable, _act's taking test has already looked at it
        this same call, at a better price and with no exposure left resting.
        """
        if not self.config.enable_quoting or not allowed:
            return None
        if blocked_until is not None and now < blocked_until:
            return None
        n_improve = self.config.max_improve_ticks
        if side == "yes":
            yes_price = math.floor(reservation / TICK + 1e-9) * TICK
            # Never pay more than needed to sit n_improve ticks inside the
            # near touch (see max_improve_ticks). This is a CAP, not a target:
            # when the reservation is already below it we stay where we are,
            # including behind the touch, which is the correct answer -- being
            # first would breach the edge we just demanded.
            if n_improve is not None and market_bid is not None:
                yes_price = min(yes_price, market_bid + n_improve * TICK)
            if market_ask is not None:
                yes_price = min(yes_price, market_ask - TICK)
        else:
            yes_price = math.ceil(reservation / TICK - 1e-9) * TICK
            if n_improve is not None and market_ask is not None:
                yes_price = max(yes_price, market_ask - n_improve * TICK)
            if market_bid is not None:
                yes_price = max(yes_price, market_bid + TICK)
        yes_price = round(yes_price, 2)
        if not (MIN_QUOTE_PRICE <= yes_price <= MAX_QUOTE_PRICE):
            return None
        return yes_price if side == "yes" else round(1.0 - yes_price, 2)


# ---------------------------------------------------------------------------
# Diagnostics: where were we relative to the market, and why
# ---------------------------------------------------------------------------

def _classify(row, touch_col: str, final_col: str) -> str:
    # pd.isna, not `is None` -- once the touch lives in a DataFrame column a
    # missing value is float NaN, not Python None. This mattered once
    # book.L2Book.to_quote started reporting missing data (bid=ask=None) for
    # a distrusted/drifted book (see book.py): an `is None` check silently
    # misclassified every one of those rows as "cross".
    if pd.isna(row[touch_col]):
        return "no_book_data"
    return "cross" if row[final_col] is not None else "no_take"


def analyze_quote_placement(debug_log: list[dict]) -> pd.DataFrame:
    """Turns SPOptionsLagQuotingStrategy.debug_log (populated only with
    record_debug=True) into a per-decision classification of each side:
      - 'cross'        -- reservation cleared the touch and we took.
      - 'no_take'      -- no edge available against the current book.
      - 'no_book_data' -- no market_bid/market_ask known at that instant.

    Also adds `market_spread` and `edge_vs_half_spread`
    ((bid_edge+ask_edge)/2 - market_spread/2): positive means our required
    edge alone demands more room than the visible spread offers (hard to ever
    cross), negative that the spread is wide enough that clearing it doesn't
    take much. It averages bid_edge/ask_edge, which differ once a position is
    loaded -- use them directly for anything side-specific.

    Use like:
        strategy = SPOptionsLagQuotingStrategy(config, record_debug=True)
        bt.run(strategy.__call__, ...)
        df = analyze_quote_placement(strategy.debug_log)
        df[["bid_class", "ask_class"]].apply(pd.Series.value_counts)
        df[["ci_edge", "bid_edge", "market_spread", "edge_vs_half_spread"]].describe()
    """
    df = pd.DataFrame(debug_log)
    if df.empty:
        return df
    df["bid_class"] = df.apply(_classify, axis=1, args=("market_ask", "bid_final"))
    df["ask_class"] = df.apply(_classify, axis=1, args=("market_bid", "ask_final"))
    df["market_spread"] = df["market_ask"] - df["market_bid"]
    df["edge_vs_half_spread"] = (df["bid_edge"] + df["ask_edge"]) / 2.0 - df["market_spread"] / 2.0
    return df
