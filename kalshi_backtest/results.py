"""Turn a BacktestResult (or several, across events) into plain DataFrames."""
import pandas as pd

from kalshi_backtest.engine import BacktestResult


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


def peak_net_position_df(result: BacktestResult) -> pd.DataFrame:
    """Per-ticker PEAK absolute net position (yes_qty - no_qty, running
    cumulative over the fills timeline) reached at ANY point during the
    run -- not the final settled position positions_df reports, which can
    understate it a lot: a strategy that buys yes up near a cap and later
    unwinds back toward flat by buying no ends up with a small/zero FINAL
    net position despite having genuinely carried much more risk at the
    peak. Also the only way to see a strategy's position-cap enforcement
    actually get outrun by latency (see SPOptionsLagQuotingStrategy's
    _requote docstring: the cap check runs at decision time off the
    then-current position, so several already-in-flight orders placed
    before a cap-driven pullback can still land afterward -- confirmed
    directly on KXINX-26AUG17H1600-B7762 with max_position=50: peak net
    position overshot to 59 before settling back)."""
    fills = fills_df(result)
    if fills.empty:
        return pd.DataFrame(columns=["ticker", "peak_abs_net_position", "peak_net_position", "time_of_peak"])
    rows = []
    for ticker, g in fills.groupby("ticker"):
        g = g.sort_values("time")
        signed = g["size"].where(g["side"] == "yes", -g["size"])
        net = signed.cumsum()
        idx = net.abs().idxmax()
        rows.append({
            "ticker": ticker,
            "peak_abs_net_position": float(net.abs().max()),
            "peak_net_position": float(net.loc[idx]),
            "time_of_peak": g.loc[idx, "time"],
        })
    return pd.DataFrame(rows).sort_values("peak_abs_net_position", ascending=False).reset_index(drop=True)


def summary(result: BacktestResult) -> dict:
    pos = positions_df(result)
    fills = fills_df(result)
    peak = peak_net_position_df(result)
    return {
        "event_ticker": result.event_ticker,
        "source": result.source,
        "fidelity": result.fidelity,
        "n_orders": len(result.orders),
        "n_fills": len(fills),
        "n_unresolved_orders": len(result.unresolved_orders),
        # Why they went unresolved. A non-zero no_l2_depth means quoting was
        # not simulated at all on this day, which is a different statement
        # from "quoting made no money" -- see engine's own warning.
        "unresolved_reasons": dict(result.unresolved_reasons),
        "quoting_simulated": result.unresolved_reasons.get("no_l2_depth", 0) == 0,
        "n_cancelled_orders": len(result.cancelled_order_ids),
        "n_open_resting_orders": sum(1 for t in result.queue_trackers if not t.done),
        "n_maker_fills": int((fills["liquidity"] == "maker").sum()) if not fills.empty else 0,
        "total_fees": result.portfolio.total_fees(),
        "total_realized_pnl": result.portfolio.total_realized_pnl(),
        "n_tickers_traded": len(pos),
        "sources_used": sorted(set(result.source_by_ticker.values())),
        "n_tickers_no_data": len(result.tickers_with_no_data),
        "max_abs_net_position": peak["peak_abs_net_position"].max() if not peak.empty else 0.0,
    }


def summarize_many(results: list[BacktestResult]) -> pd.DataFrame:
    """One row per event, for comparing a strategy's PnL/fill-rate across days."""
    return pd.DataFrame([summary(r) for r in results])
