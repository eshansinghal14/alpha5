"""
Config sections shared by every strategy. A series' top-level config is a
`section` with these as fields (plus its own `pricing` section); values live
in that series' config.json. See kalshi_infra.config for the loading rules.
"""
from __future__ import annotations

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section  # noqa: F401

# Upper bound for lip_max_price. Public prints 10-15 17:00 to 10-16 12:00
# UTC: 0.45-0.55 was the first adverse maker bucket (-1.3c/lot at 10s).
LIP_TAPER_END = 0.50


def lip_yes_price(price: float, side: str) -> float:
    """An order's price in yes terms: a no bid at p is a yes offer at 1 - p."""
    return price if side == "yes" else 1.0 - price


@section
class QuotingParams:
    order_lots: float = param(
        "Contracts per take and per edge quote. Priced on edge alone; LIP plays no part.",
        min=0.01, step=1.0)
    max_position: float = param(
        "Cap on |yes - no| PER MARKET, on effective position (fills + in-flight + resting). "
        "Lowering it does not unwind, only caps growth.", min=0, step=1.0)
    constant_edge: float = param(
        "Flat $ edge floor on every take AND every resting quote. The one term that still bites "
        "when the band is tight and the touch is cheap.", min=0, max=0.5, step=0.005)
    constant_edge_ref_s: float | None = param(
        "Scale constant_edge by sqrt(this / seconds to close) once fewer than this many seconds "
        "are left (x1.73 at a third of it). A fixed-size spot shock moves a contested fair like "
        "1/sqrt(time left), the same way theo_vol grows; this is the part theo_vol doesn't see. "
        "Empty = constant_edge for the whole event.", min=1, step=30)
    edge_fraction: float = param(
        "Fraction of the distance from fair to the band edge (lo when buying, hi when selling) "
        "demanded as edge. 1.0 = only outside the band.", min=0, max=5, step=0.05)
    lean_dollars: float = param(
        "$ lean at full inventory, linear in effective position / max_position. Added to the side "
        "that grows the position, subtracted from the side that reduces it (that side's edge floors "
        "at 0, so it never quotes through fair).", min=0, max=0.5, step=0.005)
    max_markets: int | None = param(
        "Trade only the N markets with fair closest to 0.5 (null = all). Data/order-rate limit, "
        "not an edge filter.", min=1, step=1)
    atm_hysteresis: int = param(
        "A market in the traded set stays until it ranks below max_markets + this, so the edge "
        "markets don't flip on noise.", min=0, step=1)

    enable_taking: bool = param("Send takes (IOC orders that cross the touch). Off leaves quoting "
                                "untouched, so the strategy runs maker-only.")
    enable_quoting: bool = param("Rest quotes as well as taking. Off withdraws existing quotes on "
                                 "each market's next decision.")
    quoting_edge: float = param(
        "Extra $ edge for a RESTING quote, on top of constant_edge + band + lean (a quote pays the "
        "maker fee instead of the taker fee).",
        min=0, max=0.5, step=0.005)
    max_improve_ticks: int = param(
        "Max ticks the best quote may sit inside the near touch (others' orders, ours excluded). "
        "Caps edge handed over in a wide market (KXINX 2026-08-17 uncapped: 6% of quotes "
        "overshot best_bid+1 tick, p90 by 24c).", min=0, step=1)
    quote_levels_behind: int = param(
        "Extra order_lots levels per side, one tick apart, behind the best quote. 0 = one level. "
        "Every level counts toward max_position and the lean. Also caps how many edge orders a "
        "side may have live: levels left off the ladder are cancelled (most aggressive first) or "
        "moved onto it before any new level is created. Ones deeper than the ladder are trimmed "
        "at the lowest priority.", min=0, max=20, step=1)
    skip_quote_depth_ahead: float | None = param(
        "Don't add a ladder level when more than this many contracts (others', ours excluded) "
        "already rest at its price or better -- deep behind the touch it almost never fills, so it "
        "only spends write budget, and the queue in front moving would add and drop it over and over. "
        "A level already resting is never cancelled for going deep. null disables.",
        min=0, step=10.0)
    quote_ahead_grace_s: float = param(
        "How long a resting quote may sit more than max_improve_ticks above others' best (e.g. "
        "after the bid it improved on was pulled) before it's moved down to the ladder with one "
        "amend, or cancelled if the ladder is full. Filters touch flickers: a bid pulled and "
        "re-posted within this costs no write.", min=0, max=30, step=0.5)
    quote_keep_inside: float = param(
        "$ a RESTING quote may sit inside the edge before it's cancelled. 0 = cancel the moment the "
        "edge moves through it. New levels are never placed inside the edge. Each 0.01 saves a "
        "cancel + re-create when fair wobbles back.", min=0, max=0.1, step=0.005)
    cancels_token: float = param(
        "Write tokens quote creates and moves (10 each) must leave in the bucket, kept for cancels "
        "(2 each). Everything above it is quoting's. Cancels and takes may always use the whole "
        "bucket. 20 = 10 cancels; sparse cancels were mean 0.7/s, p99 8, max 14 live on 2026-10-11. "
        "In a write group (the S&P series) one reserve covers the shared bucket: the largest "
        "member's value.",
        min=0, step=2.0)
    requote_delay_s: float = param(
        "After our quote on a (market, side) is hit, that side adds no new levels this long "
        "(levels already up stay).", min=0, max=120, step=0.5)


@section
class TradingTimeParams:
    """When in each event REAL orders may go out. Outside it paper trading
    continues and the arm request is kept, so real trading resumes on its own."""
    real_trading_delay_s: float = param(
        "Hold REAL trading this long after the event opens (paper continues). Counted from "
        "Kalshi's scheduled open, so a process started mid-event isn't delayed again. 0 disables.",
        min=0, step=10)
    real_trading_cutoff_s: float = param(
        "Disarm REAL trading this long before close (paper continues). Re-arms next event if the "
        "dashboard toggle is still on. 0 disables.", min=0, step=10)


@section
class RiskParams:
    max_loss_usd: float = param(
        "Per event: halt once this event's PnL reaches -this on the worse of touch/fair marking. A real-"
        "money trip turns real trading off for this series until the process restarts (shadow continues; "
        "other series are unaffected). Also caps each event's worst-case settlement loss when sizing orders.",
        min=0, step=5)
    max_signal_age_s: float = param(
        "Pull quotes and stop taking when the fair's newest input is older than this; they return "
        "as soon as it's back under.", min=0.01, max=60, step=0.05)
    # Both checks: S&P decision rows 2026-10-15/16, >=10 min from close, read
    # band p99.99 <= 23c and outside-touch p99.99 <= 10.5c (wider only at the
    # 9:30 ET option open); the 10-16 17:41Z chain break read bands to 1.00
    # and fairs to 57c outside the touch.
    max_fair_band: float | None = param(
        "Block a market (no quotes, LIP or takes) while its fair band (prob_hi - prob_lo) is wider "
        "than this, and leave it out of the max_markets ranking so a broken fair near 0.5 can't pull "
        "it in. null = off.", min=0.01, max=1.0, step=0.05)
    max_fair_outside_touch: float | None = param(
        "Block a market while its fair sits more than this below Kalshi's bid or above its ask. "
        "null = off.", min=0.01, max=1.0, step=0.01)
    fair_suspect_hold_s: float = param(
        "Keep a market blocked this long after its last failed check. The 10-16 break flickered "
        "between bad and plausible-looking readings for up to 33s, and the plausible ones were up to "
        "19c from the Kalshi mid.", min=0, max=600, step=5)


@section
class RuntimeParams:
    quote_fill_poll_s: float = param(
        "REST backstop poll of the fills endpoint. Quote fills arrive on Kalshi's fill websocket; "
        "this only catches what a websocket gap dropped.", min=0.2, step=0.1)
    control_poll_s: float = param("config.json / trading_mode.json poll.", min=0.2, step=0.1)


@section
class LipSettings:
    """Program parameters (target, discount, reward, period) come from Kalshi's
    /incentive_programs per market; these only shape our own LIP quote.

    One LIP order per side, at (or a few ticks behind) others' best bid on
    that side, on longshot markets only: both the yes bid (lip_bid_lots) and
    the yes offer, a no bid (lip_offer_lots), while the order's price in yes
    terms is under lip_max_price. Our fair caps the price (lip_constant_edge).
    Rationale and backtest: kalshi_strategies/KXCRYPTOLEAD15M/research/LIP_REPORT.md."""
    enable_lip_quoting: bool = param(
        "Rest one LIP-farming quote per side, after the edge ladder in priority. Off cancels any "
        "LIP quote. LIP accrual is measured either way.")
    lip_cheap_max_price: float = param(
        "Fixed LIP lots, both sides, while the order's price in yes terms (the yes bid; 1 - the no "
        "bid for the offer) is at or below this; above it each side keeps the $ worst case it had "
        "here (bid lots x this / y; offer lots x (1 - this) / (1 - y)), up to lip_max_price.",
        min=0.01, max=0.49, step=0.01)
    lip_max_price: float = param(
        "No LIP order (quote or exit) at a yes price -- the yes bid, or 1 - the no bid for the "
        "offer -- at or above this. Lower it to keep LIP off contested markets, where both sides "
        "were picked off 10-16 23:00-00:15 UTC.", min=0.05, max=LIP_TAPER_END, step=0.01)
    lip_offer_max_price: float = param(
        "Tighter cap for the LIP offer (the no bid) alone: no LIP offer, exit included, at a yes "
        "offer at or above this; the bid keeps lip_max_price. 10-17 05:15-13:30 UTC: the offer at "
        "a yes offer <= 0.20 accrued $15 for +$10.65 net of 10s markout, above 0.20 $21 for "
        "-$123 (88% of its fill lots).", min=0.05, max=LIP_TAPER_END, step=0.01)
    lip_markets: list[str] | None = param(
        "Markets that may LIP-quote, by the last part of the ticker (XRP for "
        "KXCRYPTOLEAD15M-26OCT171745-XRP). null = every market. A market left out quotes no LIP "
        "but still offers back LIP inventory it already holds (the exit).")
    lip_bid_lots: float = param(
        "LIP yes-bid size at or below lip_cheap_max_price, less any yes position already held; "
        "shrinks above it at constant $ risk. 0 = no LIP bid.", min=0, step=10.0)
    lip_offer_lots: float = param(
        "LIP yes-offer size (the no bid) at a yes offer at or below lip_cheap_max_price, less any no "
        "position already held; grows above it at constant $ risk, to x(1 - cutoff)/(1 - cap) at "
        "the offer's cap (the lower of lip_max_price and lip_offer_max_price). Worst case per lot is "
        "1 - the yes offer, so this side risks far more per lot than the bid. 0 = no LIP offer.",
        min=0, step=10.0)
    lip_constant_edge: float | None = param(
        "Edge the LIP bid must keep below own-side fair ($): a best bid above fair - this is capped "
        "there. 0 = never above fair. null = no fair check. Live 2026-10-14 22:22-22:55 UTC: 613 of "
        "1,109 LIP lots filled >2c above fair and carried $24.5 of the $28.2 10s markout loss.",
        min=0, max=0.5, step=0.005)
    lip_max_markets: int | None = param(
        "LIP on at most this many markets per event: the ones with the highest best-case period "
        "payout (see quoter.LIP_MIN_PAYOUT_USD). A member keeps its place unless another projects "
        "25% more, so the set doesn't churn writes. null = no limit.", min=1, step=1)
    lip_requeue_min_ahead: float | None = param(
        "Real mode: every lip_hold_s, read Kalshi's queue position for each LIP order. One with "
        "fewer than this many contracts ahead at its price, and others queued behind it, is "
        "cancelled and re-placed at the back -- fewest-ahead first, only with write tokens to "
        "spare above cancels_token. null = never.", min=0, step=10.0)
    lip_max_signal_age_s: float | None = param(
        "Pull every LIP order (quote and exit) while the fair's newest input is older than this; "
        "tighter than risk.max_signal_age_s, which still pulls everything. They return as soon as "
        "it's back under. null = only risk.max_signal_age_s.", min=0.001, max=60, step=0.005)
    lip_min_time_left_s: float = param(
        "No LIP order with fewer than this many seconds to close, the LIP exit included: inside it "
        "LIP inventory is held. The last 3 min lost money net in every cut of the 2026-10-11..14 "
        "backtest.", min=0, step=30)
    lip_hold_s: float = param(
        "A resting LIP quote is moved to a new price or size at most this often (one amend). "
        "Safety cancels (stale, band, time, LIP off) are never held.", min=0, max=60, step=0.5)

    def market_allowed(self, ticker: str) -> bool:
        return self.lip_markets is None or ticker.rsplit("-", 1)[-1] in self.lip_markets

    def max_price(self, side: str) -> float:
        """The yes price at or above which `side` ("yes" = bid, "no" = offer)
        rests no LIP order: lip_max_price, tightened by lip_offer_max_price
        for the offer."""
        return self.lip_max_price if side == "yes" else min(self.lip_max_price, self.lip_offer_max_price)

    def lots_at(self, price: float, side: str) -> float:
        """Order size for a LIP quote on `side` ("yes" = the bid, "no" = the
        offer) at own-side `price`, judged by that price in yes terms y: the
        side's lots up to lip_cheap_max_price, then the same $ worst case as at
        the cutoff (bid loses y/lot, offer loses 1 - y/lot), 0 at or above
        max_price(side). So the bid shrinks and the offer grows toward its cap."""
        edge = self.lip_cheap_max_price
        y = lip_yes_price(price, side)
        if y >= self.max_price(side) - 1e-9:
            return 0.0
        if y <= edge + 1e-9:
            return self.lip_bid_lots if side == "yes" else self.lip_offer_lots
        if side == "yes":
            return self.lip_bid_lots * edge / y
        return self.lip_offer_lots * (1.0 - edge) / (1.0 - y)

    def max_lots(self) -> float:
        """Largest LIP order size anywhere: the offer peaks just under its cap."""
        peak = max(self.max_price("no"), self.lip_cheap_max_price)
        return max(self.lip_bid_lots, self.lip_offer_lots * (1.0 - self.lip_cheap_max_price) / (1.0 - peak))


SHARED_SECTIONS = ("quoting", "trading_time", "risk", "runtime", "lip")


def check_strategy_config(cfg) -> None:
    """A series config must carry every shared section under these names."""
    missing = [s for s in SHARED_SECTIONS if not hasattr(cfg, s)]
    if missing:
        raise TypeError(f"{type(cfg).__name__} is missing shared config sections: {missing}")

