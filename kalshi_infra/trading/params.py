"""
Config sections shared by every strategy. A series' top-level config is a
`section` with these as fields (plus its own `pricing` section); values live
in that series' config.json. See kalshi_infra.config for the loading rules.
"""
from __future__ import annotations

from kalshi_infra.config import APPLY_NEXT_EVENT, param, section  # noqa: F401

# Cheap-side LIP size tapers to 0 here. Public prints 10-15 17:00 to 10-16
# 12:00 UTC: makers resting at 0.20-0.45 made +0.1 to +3.4c/lot at 10s;
# 0.45-0.55 was the first adverse bucket (-1.3c).
LIP_TAPER_END = 0.50


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

    One LIP order per side, at others' best bid on that side, sized by that
    price: the cheap side of a longshot only (no expensive-side LIP). Our fair
    caps the price (lip_constant_edge). Rationale and backtest:
    kalshi_strategies/KXCRYPTOLEAD15M/research/LIP_REPORT.md."""
    enable_lip_quoting: bool = param(
        "Rest one LIP-farming quote per side, after the edge ladder in priority. Off cancels any "
        "LIP quote. LIP accrual is measured either way.")
    lip_cheap_max_price: float = param(
        "Full LIP size on a side whose best bid (own-side price, others' orders) is at or below "
        "this; above it the size tapers linearly to 0 at 0.50 (LIP_TAPER_END), so crossing the "
        "cutoff shrinks the order instead of cancelling it.", min=0.01, max=0.49, step=0.01)
    lip_cheap_lots: float = param(
        "LIP order size at or below lip_cheap_max_price, less any position already held on that "
        "side. 0 disables LIP quoting (the exit still offers LIP inventory back).", min=0, step=10.0)
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
    lip_min_time_left_s: float = param(
        "No LIP quote with fewer than this many seconds to close. The last 3 min lost money net "
        "in every cut of the 2026-10-11..14 backtest.", min=0, step=30)
    lip_hold_s: float = param(
        "A resting LIP quote is moved to a new price or size at most this often (one amend). "
        "Safety cancels (stale, band, time, LIP off) are never held.", min=0, max=60, step=0.5)

    def lots_at(self, price: float) -> float:
        """Order size for a LIP quote at own-side `price`: lip_cheap_lots up to
        lip_cheap_max_price, linear down to 0 at LIP_TAPER_END, 0 above."""
        if price <= self.lip_cheap_max_price + 1e-9:
            return self.lip_cheap_lots
        if price < LIP_TAPER_END - 1e-9:
            return self.lip_cheap_lots * (LIP_TAPER_END - price) / (LIP_TAPER_END - self.lip_cheap_max_price)
        return 0.0

    def max_lots(self) -> float:
        return self.lip_cheap_lots


SHARED_SECTIONS = ("quoting", "trading_time", "risk", "runtime", "lip")


def check_strategy_config(cfg) -> None:
    """A series config must carry every shared section under these names."""
    missing = [s for s in SHARED_SECTIONS if not hasattr(cfg, s)]
    if missing:
        raise TypeError(f"{type(cfg).__name__} is missing shared config sections: {missing}")

