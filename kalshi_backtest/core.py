"""
Core data types shared across the backtest engine: market data events, orders,
fills, and per-ticker position/PnL accounting.

Prices are always dollars in [0, 1], quoted from the YES side unless a
function name says otherwise (e.g. a NO order's own price is in NO terms,
since that's what the venue actually quotes and settles against).
"""
import dataclasses
import math

import pandas as pd


@dataclasses.dataclass
class Quote:
    """Best-bid/ask (+ depth, when known) for one ticker at one instant.

    bid/ask are always YES-side prices (a NO market's own bid/ask, if that's
    what the source gives you, must be converted to YES terms before this is
    constructed -- see book.py). depth fields are None (not 0.0) when the
    source can't tell you depth at all -- e.g. Kalshi's 1-min candles have no
    depth info whatsoever. 0.0 means "known to be empty", None means "unknown".
    """
    ticker: str
    time: pd.Timestamp
    bid: float | None
    ask: float | None
    bid_depth: float | None
    ask_depth: float | None
    source: str       # "kalshi_ws" | "predexon" | "kalshi_1min"
    fidelity: str      # "l2" | "top_of_book" | "candle"


@dataclasses.dataclass
class Trade:
    """A print of an actual executed trade, YES-side price."""
    ticker: str
    time: pd.Timestamp
    price: float
    size: float
    taker_side: str | None
    source: str = "kalshi_trades"


@dataclasses.dataclass
class Order:
    order_id: int
    ticker: str
    side: str          # "yes" | "no" -- which contract this order buys
    order_type: str    # "limit" | "market"
    price: float | None  # required for "limit", ignored for "market" (in `side`'s own price terms)
    size: float
    decision_time: pd.Timestamp   # sim time the strategy call that created this fired at


@dataclasses.dataclass
class Fill:
    order_id: int
    ticker: str
    side: str
    price: float
    size: float
    time: pd.Timestamp
    fee: float
    liquidity: str    # "taker" | "maker"


def kalshi_fee(price: float, contracts: float, fee_multiplier: float = 0.07) -> float:
    """
    Kalshi's standard per-fill trading fee, rounded up to the nearest cent:
        fee = ceil(fee_multiplier * contracts * price * (1 - price) * 100) / 100

    fee_multiplier=0.07 is Kalshi's advertised *general* rate as of this
    writing -- NOT confirmed against a real fill/statement anywhere in this
    repo (unlike most other constants here). Some series (financial index
    markets, a few others) publish different, lower per-series multipliers,
    and maker fees are $0 on several series. Treat this as a placeholder
    worth checking against a real account statement before trusting absolute
    PnL, not just relative strategy comparisons.
    """
    if contracts <= 0:
        return 0.0
    raw = fee_multiplier * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


@dataclasses.dataclass
class Position:
    ticker: str
    yes_qty: float = 0.0
    yes_cost: float = 0.0     # total dollars paid for yes_qty (before fees)
    no_qty: float = 0.0
    no_cost: float = 0.0
    fees_paid: float = 0.0
    realized_pnl: float = 0.0  # only populated once/if settled
    settled: bool = False
    result: str | None = None  # "yes" | "no" once known

    def apply_fill(self, fill: Fill):
        if fill.side == "yes":
            self.yes_qty += fill.size
            self.yes_cost += fill.size * fill.price
        else:
            self.no_qty += fill.size
            self.no_cost += fill.size * fill.price
        self.fees_paid += fill.fee

    def settle(self, result: str):
        """result: 'yes' or 'no'. Realizes PnL for whatever's still held; idempotent."""
        if self.settled:
            return
        yes_payout = self.yes_qty * (1.0 if result == "yes" else 0.0)
        no_payout = self.no_qty * (1.0 if result == "no" else 0.0)
        self.realized_pnl = (yes_payout - self.yes_cost) + (no_payout - self.no_cost) - self.fees_paid
        self.settled = True
        self.result = result

    def unrealized_pnl(self, yes_bid: float | None, yes_ask: float | None) -> float:
        """Mark-to-market using what the inventory could currently be sold for
        (yes_qty at the current yes bid, no_qty at the current no bid = 1 - yes_ask) --
        conservative vs. marking at mid. Returns 0.0 for any leg whose exit
        price isn't currently known rather than guessing."""
        if self.settled:
            return 0.0
        pnl = -self.fees_paid
        if self.yes_qty:
            exit_px = yes_bid if yes_bid is not None else 0.0
            pnl += self.yes_qty * exit_px - self.yes_cost
        if self.no_qty:
            no_bid = (1.0 - yes_ask) if yes_ask is not None else 0.0
            pnl += self.no_qty * no_bid - self.no_cost
        return pnl


class Portfolio:
    def __init__(self, fee_multiplier: float = 0.07, maker_fee_multiplier: float | None = None):
        self.fee_multiplier = fee_multiplier
        self.maker_fee_multiplier = maker_fee_multiplier if maker_fee_multiplier is not None else fee_multiplier
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []

    def position(self, ticker: str) -> Position:
        if ticker not in self.positions:
            self.positions[ticker] = Position(ticker=ticker)
        return self.positions[ticker]

    def record_fill(self, order: Order, price: float, size: float, time: pd.Timestamp, liquidity: str) -> Fill:
        mult = self.fee_multiplier if liquidity == "taker" else self.maker_fee_multiplier
        fee = kalshi_fee(price, size, mult)
        fill = Fill(order_id=order.order_id, ticker=order.ticker, side=order.side,
                    price=price, size=size, time=time, fee=fee, liquidity=liquidity)
        self.position(order.ticker).apply_fill(fill)
        self.fills.append(fill)
        return fill

    def settle(self, results: dict[str, str]):
        for ticker, pos in self.positions.items():
            if ticker in results and results[ticker] is not None:
                pos.settle(results[ticker])

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values() if p.settled)

    def total_fees(self) -> float:
        return sum(p.fees_paid for p in self.positions.values())
