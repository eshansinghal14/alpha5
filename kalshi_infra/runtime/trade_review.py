"""
Periodic review of REAL fills: flags clearly bad trades and tallies causes.

    python -m kalshi_infra.runtime.trade_review --series KXCRYPTOLEAD15M --loop 1800

Writes data/live/{series}/trade_review.md (summary) and trade_review_flags.csv.
"""
from __future__ import annotations

import argparse
import os
import time

import pandas as pd

from kalshi_infra.api_functions import kalshi_get
from kalshi_infra.runtime.journal import read_journal
from kalshi_infra.runtime.persist import LIVE_ROOT

# A move against us within MARKOUT_S that is at least PICKOFF_C counts as
# being picked off, not noise; 0.10 is ~3x the typical 1-tick crypto spread.
MARKOUT_S = 10
PICKOFF_C = 0.10
_settle_cache: dict[str, str | None] = {}


def settlement(ticker: str) -> str | None:
    if _settle_cache.get(ticker) is not None:
        return _settle_cache[ticker]
    try:
        m = kalshi_get(f"/trade-api/v2/markets/{ticker}").get("market", {})
        r = m.get("result") or None if m.get("status") in ("finalized", "settled", "determined") else None
    except Exception:
        r = None
    _settle_cache[ticker] = r
    return r


def review(series: str, dates: list[str], event: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`event` limits every stream to that event's tickers, which keeps it
    light enough to run on the live box."""
    fills, dec, bbo = [], [], []
    for d in dates:
        f = read_journal(series, d, "fills", ticker_prefix=event)
        if f.empty:
            continue
        if "mode" not in f.columns:
            continue
        seeded = f["seeded"].fillna(False).astype(bool) if "seeded" in f.columns else False
        fills.append(f[(f["mode"] == "real") & ~seeded])
        dec.append(read_journal(series, d, "decisions", ticker_prefix=event,
                                columns=["time", "ticker", "prob", "market_bid", "market_ask", "x_phase", "x_tau"]))
        bbo.append(read_journal(series, d, "bbo", ticker_prefix=event, columns=["time", "ticker", "bid", "ask"]))
    if not fills:
        return pd.DataFrame(), pd.DataFrame()
    f = pd.concat(fills).drop_duplicates(["order_id", "trade_id", "time", "price", "size"]).sort_values("time")
    dec = pd.concat(dec).sort_values("time")
    bbo = pd.concat(bbo).sort_values("time")
    bbo["mid"] = (bbo["bid"] + bbo["ask"]) / 2

    f["yes_px"] = f["price"].where(f["side"] == "yes", 1 - f["price"])
    f["dir"] = f["side"].map({"yes": 1, "no": -1})
    # Model state at the fill (last decision row at or before it).
    f = pd.merge_asof(f, dec, on="time", by="ticker", direction="backward")
    # Mid MARKOUT_S after the fill.
    later = f[["time", "ticker"]].copy()
    later["time"] = later["time"] + pd.Timedelta(seconds=MARKOUT_S)
    later = pd.merge_asof(later.reset_index(), bbo[["time", "ticker", "mid"]], on="time", by="ticker",
                          direction="backward").set_index("index")
    f["mid_after"] = later["mid"]
    f["markout"] = f["dir"] * (f["mid_after"] - f["yes_px"]) * f["size"]
    f["edge_at_fill"] = f["dir"] * (f["prob"] - f["yes_px"])

    f["result"] = f["ticker"].map(lambda t: settlement(t))
    payoff = f["result"].map({"yes": 1.0, "no": 0.0})
    f["settled_pnl"] = (f["dir"] * (payoff - f["yes_px"]) * f["size"] - f["fee"]).where(payoff.notna())

    reasons = []
    for r in f.itertuples():
        why = []
        if r.liquidity == "taker" and pd.notna(r.edge_at_fill) and r.edge_at_fill < 0:
            why.append("took against own model (negative edge at fill)")
        if r.liquidity == "maker" and pd.notna(r.markout) and r.markout / r.size <= -PICKOFF_C:
            why.append(f"stale quote picked off (mid moved >={PICKOFF_C:.2f} against in {MARKOUT_S}s)")
        if r.liquidity == "taker" and pd.notna(r.markout) and r.markout / r.size <= -PICKOFF_C:
            why.append(f"take immediately reversed (>={PICKOFF_C:.2f} against in {MARKOUT_S}s)")
        if pd.notna(r.x_tau) and r.x_tau < 60 and pd.notna(r.settled_pnl) and r.settled_pnl < 0:
            why.append("losing trade in final minute")
        if pd.notna(r.settled_pnl) and r.settled_pnl < 0 and pd.notna(r.edge_at_fill) and r.edge_at_fill < -0.05:
            why.append("settled loss AND model disagreed by >5c")
        reasons.append("; ".join(why))
    f["bad_reason"] = reasons
    bad = f[f["bad_reason"] != ""]
    return f, bad


def write_report(series: str, f: pd.DataFrame, bad: pd.DataFrame):
    out_dir = os.path.join(LIVE_ROOT, series)
    now = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    lines = [f"# Trade review {series} -- {now}", ""]
    if f.empty:
        lines.append("No real fills yet.")
    else:
        settled = f["settled_pnl"].notna()
        lines += [
            f"- Real fills: {len(f)} ({(f.liquidity == 'taker').sum()} taker, {(f.liquidity == 'maker').sum()} maker), "
            f"contracts {f['size'].sum():.1f}, fees ${f['fee'].sum():.2f}",
            f"- Settled PnL (settled fills only, {settled.sum()}): ${f.loc[settled, 'settled_pnl'].sum():.2f}",
            f"- {MARKOUT_S}s markout total: ${f['markout'].sum():.2f} "
            f"(taker ${f.loc[f.liquidity == 'taker', 'markout'].sum():.2f}, maker ${f.loc[f.liquidity == 'maker', 'markout'].sum():.2f})",
            f"- Flagged bad fills: {len(bad)}", "", "## Most common issues", ""]
        counts = bad["bad_reason"].str.split("; ").explode().value_counts()
        for reason, n in counts.items():
            sub = bad[bad["bad_reason"].str.contains(reason, regex=False)]
            lines.append(f"- **{reason}**: {n} fills, settled PnL ${sub['settled_pnl'].sum():.2f}, "
                         f"markout ${sub['markout'].sum():.2f}")
        lines += ["", "## By coin (flagged)", ""]
        if not bad.empty:
            by = bad.assign(coin=bad["ticker"].str.split("-").str[-1]).groupby("coin").agg(
                n=("size", "size"), settled=("settled_pnl", "sum"), markout=("markout", "sum"))
            lines.append(by.round(2).to_string())
        lines += ["", "## Worst 10 flagged fills", ""]
        worst = bad.assign(k=bad["settled_pnl"].fillna(bad["markout"])).nsmallest(10, "k")
        cols = ["time", "ticker", "side", "price", "size", "liquidity", "prob", "mid_after", "settled_pnl", "bad_reason"]
        lines.append(worst[cols].round(3).to_string(index=False))
        bad[cols + ["markout", "edge_at_fill", "x_tau"]].to_csv(os.path.join(out_dir, "trade_review_flags.csv"), index=False)
    with open(os.path.join(out_dir, "trade_review.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXCRYPTOLEAD15M")
    ap.add_argument("--since", default=None, help="first date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--loop", type=float, default=0, help="seconds between reviews; 0 = once")
    ap.add_argument("--event", default=None, help="print every real fill of one event with context, then exit")
    args = ap.parse_args()
    if args.event:
        d = pd.Timestamp.now(tz="UTC")
        dates = [(d - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d")]
        f, _ = review(args.series, dates, event=args.event)
        f = f[f["ticker"].str.startswith(args.event)] if not f.empty else f
        cols = ["time", "ticker", "side", "price", "size", "fee", "liquidity", "prob", "market_bid", "market_ask",
                "mid_after", "markout", "edge_at_fill", "x_tau", "x_phase", "result", "settled_pnl", "bad_reason"]
        with pd.option_context("display.width", 300, "display.max_rows", None, "display.max_columns", None):
            print(f[cols].round(4).to_string(index=False) if not f.empty else "no real fills")
        return
    start = args.since or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    while True:
        dates = [d.strftime("%Y-%m-%d") for d in pd.date_range(start, pd.Timestamp.now(tz="UTC").normalize().tz_localize(None))]
        try:
            f, bad = review(args.series, dates)
            write_report(args.series, f, bad)
            print(f"{pd.Timestamp.now(tz='UTC')} reviewed {len(f)} fills, {len(bad)} flagged", flush=True)
        except Exception as e:
            print(f"{pd.Timestamp.now(tz='UTC')} review failed: {e!r}", flush=True)
        if not args.loop:
            return
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
