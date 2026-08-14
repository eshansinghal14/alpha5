"""Turn a BacktestResult (or several, across events) into plain DataFrames."""
import pandas as pd

from engine import BacktestResult


def fills_df(result: BacktestResult) -> pd.DataFrame:
    rows = [
        {"time": f.time, "ticker": f.ticker, "side": f.side, "price": f.price,
         "size": f.size, "fee": f.fee, "liquidity": f.liquidity, "order_id": f.order_id}
        for f in result.portfolio.fills
    ]
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True) if rows else pd.DataFrame(
        columns=["time", "ticker", "side", "price", "size", "fee", "liquidity", "order_id"])


def positions_df(result: BacktestResult) -> pd.DataFrame:
    rows = [
        {"ticker": p.ticker, "yes_qty": p.yes_qty, "yes_cost": p.yes_cost,
         "no_qty": p.no_qty, "no_cost": p.no_cost, "fees_paid": p.fees_paid,
         "settled": p.settled, "result": p.result, "realized_pnl": p.realized_pnl}
        for p in result.portfolio.positions.values()
    ]
    return pd.DataFrame(rows)


def summary(result: BacktestResult) -> dict:
    pos = positions_df(result)
    fills = fills_df(result)
    return {
        "event_ticker": result.event_ticker,
        "source": result.source,
        "fidelity": result.fidelity,
        "n_orders": len(result.orders),
        "n_fills": len(fills),
        "n_unresolved_orders": len(result.unresolved_orders),
        "n_open_resting_orders": sum(1 for t in result.queue_trackers if not t.done),
        "total_fees": result.portfolio.total_fees(),
        "total_realized_pnl": result.portfolio.total_realized_pnl(),
        "n_tickers_traded": len(pos),
        "sources_used": sorted(set(result.source_by_ticker.values())),
        "n_tickers_no_data": len(result.tickers_with_no_data),
    }


def summarize_many(results: list[BacktestResult]) -> pd.DataFrame:
    """One row per event -- for running crossing_arb (or any strategy) across
    many days and comparing aggregate PnL/fill-rate."""
    return pd.DataFrame([summary(r) for r in results])
