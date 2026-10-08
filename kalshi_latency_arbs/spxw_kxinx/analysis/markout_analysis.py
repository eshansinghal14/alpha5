"""
Markout analysis for sp_options_lag_quoting.py: for every real fill the
strategy took, marks its P&L against the Kalshi market's own mid price
`MARKOUT_WINDOW_S` seconds later -- a standard "did we get picked off"
diagnostic for a take-only strategy (see sp_options_lag_quoting.py's module
docstring for why it's take-only: every fill is a taker cross by
construction, so markout is exactly the question "was the edge we thought
we saw at fill time still real a bit later, or did the market immediately
prove us wrong").

Plots the N worst `MARKOUT_WINDOW_S`-second markouts (10 minutes by
default): the fills that, in hindsight, look most like we traded right
before the market moved hard against the side we just took. Each plot
overlays BOTH of this strategy's two price sources -- Kalshi's own BBO
(bid/ask band + mid) and the SPXW option-implied probability + its
confidence band (from latency_arbs_functions.bucket_probabilities_ci via
precompute_atm_signal) -- around the fill, since the whole strategy thesis
is that the option side leads Kalshi's own quote; a bad markout plot is the
direct, visual way to see whether that lead actually existed at the moment
we traded, or whether we were reacting to a stale/noisy read of either
side.

Also plots, across the whole session:
  - net position by ticker (plot_position_over_day) -- the running
    yes_qty-no_qty from the same fills timeline, one line per ticker
    traded, with the configured max_position cap marked -- so a single
    glance shows how many buckets got traded, how each one's exposure
    evolved, and whether/when the cap actually bound.
  - cumulative PnL, ALL tickers combined (plot_pnl_over_day) -- marked to
    market throughout using Kalshi's own quotes for whatever's currently
    open (the same conservative formula core.Position.unrealized_pnl uses),
    not just PnL from tickers that have already settled -- see
    compute_pnl_over_day's docstring. Ends exactly at the real settled
    total from results.summary(), not an approximation.
  - drawdown (compute_drawdown/plot_drawdown) -- pnl minus its own running
    peak so far, off that same mark-to-market PnL series -- the standard
    peak-to-trough measure, so it reflects open-position swings too, not
    just realized drops. The always-on report bundles this with the worst
    markout trades into one PDF (drawdown.pdf, see generate_drawdown_report)
    rather than a lone PNG -- the trades most responsible for the session's
    worst drawdown points, right alongside the chart that shows them.

All plots for a given event_ticker land in analysis/{event_ticker}/ (see
day_dir) -- one folder per backtest day, not ticker-suffixed flat files.
generate_day_plots() produces the always-on set (position_over_day.png,
pnl_over_day.png, drawdown.pdf) and is called automatically by
run_backtest.run_one() after every backtest run (see its `auto_plot` param)
-- so a plain backtest leaves a visual record without a separate manual
step. Standalone per-trade markout PNGs (plot_worst_markouts) and a
standalone drawdown PNG (plot_drawdown) remain available for callers who
want those specifically instead of the combined PDF.

Usage (from this folder, or with it on sys.path):
    python markout_analysis.py [event_ticker ...] [n]
Defaults to KXINX-26AUG18H1600, n=5. One or more event tickers may be
passed (one full run per ticker); a trailing all-digit argument is read as
n instead of a ticker.
"""
import os
import sys

_ANALYSIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRATEGY_DIR = os.path.join(_ANALYSIS_DIR, "..")
_BACKTEST_DIR = os.path.join(_STRATEGY_DIR, "..", "..", "kalshi_backtest")
for _p in (_STRATEGY_DIR, _BACKTEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

import data
import engine
import results
import sp_options_lag_quoting as strat

# ---- house palette, matching kalshi_quoting_arbs/quoting_arb_lib.py ----
PLOT_BG = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
KALSHI_BLUE = "#2a78d6"
SIGNAL_ORANGE = "#eb6834"
FILL_GREEN = "#1baf7a"
FILL_RED = "#d1453b"
MARKOUT_RED = "#d1453b"

MARKOUT_WINDOW_S = 600  # 10 minutes
PLOT_LOOKBACK_S = 60
PLOT_LOOKAHEAD_S = 660  # 10min markout + a little margin so the markout point is never clipped
PNL_GRID_FREQ = "1min"


def _quotes_df(event_data, ticker: str) -> pd.DataFrame:
    """Kalshi's own BBO for `ticker`, long-form (time, bid, ask, mid) --
    the exact touch stream the strategy itself saw via ctx.book()."""
    quotes = event_data.quotes.get(ticker, [])
    if not quotes:
        return pd.DataFrame(columns=["time", "bid", "ask", "mid"])
    df = pd.DataFrame({
        "time": [q.time for q in quotes],
        "bid": [q.bid for q in quotes],
        "ask": [q.ask for q in quotes],
    }).sort_values("time")
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    return df


def _signal_df(signal_events: list[tuple], ticker: str) -> pd.DataFrame:
    """SPXW option-implied probability + CI for `ticker`, long-form (time,
    prob, prob_lo, prob_hi) -- filtered from precompute_atm_signal's full
    event list to whatever window this ticker was actually the ATM bucket
    (it's only ever a signal source while that's true, so this can have
    real gaps outside those windows -- see this module's docstring)."""
    rows = [
        {"time": t, "prob": p["prob"], "prob_lo": p["prob_lo"], "prob_hi": p["prob_hi"]}
        for t, p in signal_events if p.get("ticker") == ticker
    ]
    return pd.DataFrame(rows).sort_values("time") if rows else pd.DataFrame(
        columns=["time", "prob", "prob_lo", "prob_hi"])


def _window_with_anchor(df: pd.DataFrame, win_start: pd.Timestamp, win_end: pd.Timestamp) -> pd.DataFrame:
    """`df` (sorted by time) restricted to [win_start, win_end], anchored
    with the last known row strictly before win_start (its time reassigned
    to win_start) and extended with a closing copy of the last row at
    win_end -- so a step plot spans the FULL window using the correct
    last-known value even when nothing actually changed inside it (a real,
    common case for a touch that's sparser than the plot window -- not a
    data gap to paper over silently, just how a step function should be
    drawn from a last-observation-carried-forward series)."""
    if df.empty:
        return df
    before = df[df["time"] < win_start]
    inside = df[(df["time"] >= win_start) & (df["time"] <= win_end)].copy()
    parts = []
    if not before.empty:
        anchor = before.iloc[[-1]].copy()
        anchor["time"] = win_start
        parts.append(anchor)
    parts.append(inside)
    combined = pd.concat(parts, ignore_index=True)
    if not combined.empty and combined["time"].iloc[-1] < win_end:
        closer = combined.iloc[[-1]].copy()
        closer["time"] = win_end
        combined = pd.concat([combined, closer], ignore_index=True)
    return combined


def compute_markouts(result, event_data, markout_s: float = MARKOUT_WINDOW_S) -> pd.DataFrame:
    """One row per fill: entry price/side/time/ticker plus Kalshi's own mid
    price `markout_s` seconds later and the resulting per-contract P&L
    (yes-equivalent dollars) had we marked to that mid instead of holding to
    settlement. Negative = the market moved against the side we just took --
    adverse selection; positive = it moved in our favor.

    Mid price is read via a plain backward-looking lookup (last known quote
    at or before fill_time + markout_s) against event_data.quotes[ticker] --
    the same quote stream the strategy itself saw.
    """
    fills = results.fills_df(result)
    if fills.empty:
        return fills.assign(mid_at_markout=[], markout_pnl=[])

    quote_frames = {t: _quotes_df(event_data, t) for t in fills["ticker"].unique()}

    rows = []
    for f in fills.itertuples(index=False):
        qdf = quote_frames.get(f.ticker)
        if qdf is None or qdf.empty:
            continue
        target_time = f.time + pd.Timedelta(seconds=markout_s)
        idx = qdf["time"].searchsorted(target_time, side="right") - 1
        if idx < 0:
            continue
        mid_at_markout = qdf["mid"].iloc[idx]
        if pd.isna(mid_at_markout):
            continue
        markout_pnl = (mid_at_markout - f.price) if f.side == "yes" else ((1.0 - mid_at_markout) - f.price)
        rows.append({
            "ticker": f.ticker, "side": f.side, "price": f.price, "size": f.size,
            "time": f.time, "order_id": f.order_id, "liquidity": f.liquidity,
            "mid_at_markout": mid_at_markout, "markout_pnl": markout_pnl,
        })
    return pd.DataFrame(rows)


def _build_markout_figure(row, rank: int, event_data, signal_events: list[tuple],
                           markout_s: float, lookback_s: float, lookahead_s: float) -> "plt.Figure | None":
    """One markout trade's figure: Kalshi's own bid/ask band + mid AND the
    SPXW option-implied probability + CI band, both through a window
    straddling the fill, with the fill itself and the markout point marked
    directly. Returns None if there's nothing to plot (no quote/signal data
    in the window at all) -- shared by plot_worst_markouts (PNG-per-trade)
    and generate_drawdown_report (PDF pages) so both stay visually
    identical."""
    ticker = row["ticker"]
    win_start = row["time"] - pd.Timedelta(seconds=lookback_s)
    win_end = row["time"] + pd.Timedelta(seconds=lookahead_s)

    qdf = _quotes_df(event_data, ticker)
    qwin = _window_with_anchor(qdf, win_start, win_end)
    sdf = _signal_df(signal_events, ticker)
    swin = _window_with_anchor(sdf, win_start, win_end)
    if qwin.empty and swin.empty:
        return None

    fig, ax = plt.subplots(figsize=(9.5, 4.8), facecolor=PLOT_BG)
    ax.set_facecolor(PLOT_BG)

    if not qwin.empty:
        ax.fill_between(qwin["time"], qwin["bid"], qwin["ask"], color=KALSHI_BLUE, alpha=0.15,
                         step="post", zorder=1, label="Kalshi bid/ask")
        ax.step(qwin["time"], qwin["mid"], where="post", color=KALSHI_BLUE, linewidth=1.6,
                zorder=2, label="Kalshi mid")
    if not swin.empty:
        ax.fill_between(swin["time"], swin["prob_lo"], swin["prob_hi"], color=SIGNAL_ORANGE, alpha=0.18,
                         step="post", zorder=1, label="SPXW-implied CI")
        ax.step(swin["time"], swin["prob"], where="post", color=SIGNAL_ORANGE, linewidth=1.6,
                zorder=2, label="SPXW-implied prob")

    fill_color = FILL_GREEN if row["side"] == "yes" else FILL_RED
    ax.scatter([row["time"]], [row["price"]], color=fill_color, s=75, zorder=4,
               label=f"our fill ({row['side']} @ {row['price']:.2f})")
    markout_time = row["time"] + pd.Timedelta(seconds=markout_s)
    ax.scatter([markout_time], [row["mid_at_markout"]], color=MARKOUT_RED, marker="x", s=95, zorder=4,
               label=f"+{int(markout_s)}s mid ({row['mid_at_markout']:.2f})")
    ax.plot([row["time"], markout_time], [row["price"], row["mid_at_markout"]],
            color=MARKOUT_RED, linewidth=1.2, linestyle=(0, (3, 2)), zorder=3)
    ax.axvline(row["time"], color=INK_MUTED, linewidth=1.0, linestyle=(0, (1, 2)), zorder=1)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax.set_ylabel("yes price / implied probability ($)", color=INK_SECONDARY, fontsize=10)
    ax.set_ylim(-0.03, 1.03)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, loc="best", ncol=2)
    ax.set_title(
        f"#{rank} worst {int(markout_s)}s markout: {ticker}  "
        f"({row['side']} {row['size']:.0f} @ {row['price']:.2f} -> markout {row['markout_pnl']:+.3f}/contract)",
        color=INK_PRIMARY, fontsize=10.5, loc="left",
    )
    fig.tight_layout()
    return fig


def plot_worst_markouts(markouts: pd.DataFrame, event_data, signal_events: list[tuple], n: int = 5,
                         markout_s: float = MARKOUT_WINDOW_S,
                         lookback_s: float = PLOT_LOOKBACK_S, lookahead_s: float = PLOT_LOOKAHEAD_S,
                         out_dir: str | None = None) -> list[str]:
    """One PNG per worst-markout trade (see _build_markout_figure). Returns
    the list of saved PNG paths."""
    if out_dir is None:
        out_dir = _ANALYSIS_DIR
    os.makedirs(out_dir, exist_ok=True)
    worst = markouts.sort_values("markout_pnl").head(n).reset_index(drop=True)
    saved = []

    for i, row in worst.iterrows():
        fig = _build_markout_figure(row, i + 1, event_data, signal_events, markout_s, lookback_s, lookahead_s)
        if fig is None:
            continue
        out_path = os.path.join(out_dir, f"worst_markout_{i + 1}_{row['ticker']}.png")
        fig.savefig(out_path, dpi=140)
        plt.close(fig)
        saved.append(out_path)
        print(f"saved {out_path}")

    return saved


def plot_position_over_day(result, event_ticker: str, max_position: float | None = None,
                            out_path: str | None = None) -> str | None:
    """Running net position (yes_qty - no_qty, cumulative from the fills
    timeline) for every ticker actually traded, over the course of the
    session -- shows how the ATM bucket rotates through the day and each
    one's own position evolves, including whether/when it ever reaches the
    configured cap. One line per ticker on a single chart (not one figure
    per ticker, unlike the markout plots) since the point here is the
    whole day's shape, not any one instant."""
    fills = results.fills_df(result)
    if fills.empty:
        return None

    fig, ax = plt.subplots(figsize=(11, 5.5), facecolor=PLOT_BG)
    ax.set_facecolor(PLOT_BG)
    for ticker, g in fills.groupby("ticker", sort=False):
        g = g.sort_values("time")
        signed = g["size"].where(g["side"] == "yes", -g["size"])
        net = signed.cumsum()
        ax.step(g["time"], net, where="post", linewidth=1.3, alpha=0.85, label=ticker.split("-")[-1])

    if max_position:
        for level in (max_position, -max_position):
            ax.axhline(level, color=INK_MUTED, linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
    ax.axhline(0, color=AXIS, linewidth=0.8, zorder=1)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_ylabel("net position (yes_qty - no_qty)", color=INK_SECONDARY, fontsize=10)
    ax.set_title("Net position by ticker over the session", color=INK_PRIMARY, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=7.5, labelcolor=INK_SECONDARY, ncol=3, loc="best")
    fig.tight_layout()

    if out_path is None:
        out_path = os.path.join(_ANALYSIS_DIR, f"position_over_day_{event_ticker}.png")
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"saved {out_path}")
    return out_path


def _mark_series(tickers, grid: pd.DatetimeIndex, event_data,
                  prob_grid: pd.DataFrame | None) -> dict[str, pd.Series]:
    """Per-ticker "what is a YES contract worth right now" series on `grid`,
    used to mark open positions (a NO contract is worth 1 - this).

    Priority, per ticker:
      1. the SPXW/ES option-implied probability (`prob_grid`, i.e. probs_mid
         from compute_prob_grid) as of that instant,
      2. Kalshi's own bid/ask MID, where the model has no value,
      3. the last available mark, carried forward (and back-filled at the
         very start).

    Implied probability first because Kalshi's BBO on these buckets is
    frequently very wide, and marking a position against a wide book
    systematically understates it -- a NO bought at 0.98 marks at
    1 - yes_ask = 0.97 and shows a loss purely from the spread, even when
    the model and the eventual settlement both say it was fine.

    Crucially, a missing quote never marks a leg to ZERO. That was a real
    bug with real consequences: KXINX-26AUG26H1600-B7737 held 51 NO bought
    at 0.98 and settled NO for +$0.40, but 15 of its 249 grid points had a
    NaN yes_ask, and the old `(1 - yes_ask) if not NaN else 0.0` marked the
    whole leg worthless at each of them -- printing a -$50.57 trough on the
    PnL chart for a position that never lost money. Missing data means
    "unknown", so the last known mark is carried instead.
    """
    out: dict[str, pd.Series] = {}
    for ticker in tickers:
        implied = None
        if prob_grid is not None and ticker in prob_grid.columns:
            # asof-align the 2s pricing grid onto this (coarser) PnL grid.
            implied = prob_grid[ticker].reindex(
                prob_grid.index.union(grid)).ffill().reindex(grid)

        qdf = _quotes_df(event_data, ticker)
        if qdf.empty:
            kalshi_mid = pd.Series(float("nan"), index=grid)
        else:
            q = qdf.dropna(subset=["mid"]).set_index("time")["mid"]
            q = q[~q.index.duplicated(keep="last")].sort_index()
            kalshi_mid = q.reindex(q.index.union(grid)).ffill().reindex(grid)

        mark = implied if implied is not None else pd.Series(float("nan"), index=grid)
        mark = mark.where(mark.notna(), kalshi_mid).ffill().bfill()
        out[ticker] = mark.clip(0.0, 1.0)
    return out


def compute_pnl_over_day(result, event_data, freq: str = PNL_GRID_FREQ,
                          prob_grid: pd.DataFrame | None = None,
                          tickers: list[str] | None = None) -> pd.DataFrame:
    """Time series of TOTAL PnL (all tickers combined) across the session,
    including currently-open positions marked to market -- not just PnL from
    tickers that have already settled.

    Open inventory is marked at the option-implied probability rather than
    at Kalshi's own touch (yes_qty at `mark`, no_qty at `1 - mark`) -- see
    _mark_series for the priority order and for why marking against Kalshi's
    frequently-wide BBO, and especially treating a missing quote as zero,
    produced large phantom losses.

    Pass `prob_grid` (probs_mid from compute_prob_grid /
    precompute_signal(return_grid=True)) to get that implied marking; without
    it this falls back to Kalshi's own mid, which is still strictly better
    than the old bid/no-bid marking but doesn't help where the book is wide.

    The final point is result.portfolio.total_realized_pnl() -- the real
    settled number, computed from actual settlement results (see
    core.Position.settle), NOT an extrapolation of the mark. So the curve
    always terminates at exactly what the day actually made or lost, and any
    gap between the last marked point and that final value is the mark
    being wrong rather than the settlement being approximated.
    """
    fills = results.fills_df(result)
    if tickers is not None:
        # Restricting to one market makes this that market's OWN curve, which
        # is what the dashboard needs alongside the per-market position panel
        # -- a whole-day total sitting under a single market's position reads
        # as that market's PnL and has been misread that way.
        fills = fills[fills["ticker"].isin(tickers)]
    if fills.empty:
        return pd.DataFrame(columns=["time", "pnl"])

    fills_sorted = fills.sort_values("time").reset_index(drop=True)
    tickers = fills_sorted["ticker"].unique()

    start = fills_sorted["time"].min().floor(freq)
    end = fills_sorted["time"].max().ceil(freq)
    grid = pd.date_range(start, end, freq=freq)
    marks = _mark_series(tickers, grid, event_data, prob_grid)

    state = {t: {"yes_qty": 0.0, "yes_cost": 0.0, "no_qty": 0.0, "no_cost": 0.0, "fees": 0.0} for t in tickers}
    fill_idx = 0
    n_fills = len(fills_sorted)

    rows = []
    for t in grid:
        while fill_idx < n_fills and fills_sorted.loc[fill_idx, "time"] <= t:
            f = fills_sorted.loc[fill_idx]
            s = state[f["ticker"]]
            if f["side"] == "yes":
                s["yes_qty"] += f["size"]
                s["yes_cost"] += f["size"] * f["price"]
            else:
                s["no_qty"] += f["size"]
                s["no_cost"] += f["size"] * f["price"]
            s["fees"] += f["fee"]
            fill_idx += 1

        total_pnl = 0.0
        for ticker, s in state.items():
            if s["yes_qty"] == 0.0 and s["no_qty"] == 0.0 and s["fees"] == 0.0:
                continue
            mark = marks[ticker].loc[t]
            pnl = -s["fees"]
            if s["yes_qty"]:
                pnl += s["yes_qty"] * mark - s["yes_cost"]
            if s["no_qty"]:
                pnl += s["no_qty"] * (1.0 - mark) - s["no_cost"]
            total_pnl += pnl
        rows.append({"time": t, "pnl": total_pnl})

    df = pd.DataFrame(rows)
    # Settle to the realized total for exactly the markets plotted, so a
    # per-market curve ends on that market's own settled number rather than
    # the whole portfolio's.
    included = set(tickers)
    final_pnl = sum(p.realized_pnl for t, p in result.portfolio.positions.items()
                    if p.settled and t in included)
    settle_row = pd.DataFrame([{"time": end + pd.Timedelta(seconds=1), "pnl": final_pnl}])
    return pd.concat([df, settle_row], ignore_index=True)


def plot_pnl_over_day(pnl_df: pd.DataFrame, event_ticker: str, out_path: str | None = None) -> str | None:
    """Cumulative PnL across the whole session -- mark-to-market throughout
    (including still-open positions), snapping to the real settled total at
    the end. See compute_pnl_over_day's docstring for exactly how each
    point is priced."""
    if pnl_df.empty:
        return None

    fig, ax = plt.subplots(figsize=(11, 5), facecolor=PLOT_BG)
    ax.set_facecolor(PLOT_BG)
    line_color = KALSHI_BLUE if pnl_df["pnl"].iloc[-1] >= 0 else FILL_RED
    ax.plot(pnl_df["time"], pnl_df["pnl"], color=line_color, linewidth=1.6, zorder=2)
    ax.fill_between(pnl_df["time"], 0.0, pnl_df["pnl"], color=line_color, alpha=0.10, zorder=1)
    ax.axhline(0, color=AXIS, linewidth=0.8, zorder=1)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_ylabel("cumulative PnL ($)", color=INK_SECONDARY, fontsize=10)
    ax.set_title(
        f"{event_ticker}: PnL over the session (mark-to-market incl. open positions; "
        f"settled total ${pnl_df['pnl'].iloc[-1]:+.2f})",
        color=INK_PRIMARY, fontsize=11, loc="left",
    )
    fig.tight_layout()

    if out_path is None:
        out_path = os.path.join(_ANALYSIS_DIR, f"pnl_over_day_{event_ticker}.png")
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"saved {out_path}")
    return out_path


def compute_drawdown(pnl_df: pd.DataFrame) -> pd.DataFrame:
    """Adds `running_peak` (cumulative max of `pnl` so far) and `drawdown`
    (`pnl - running_peak`, always <= 0) to `pnl_df` -- the standard
    peak-to-trough equity-curve measure, computed directly off the same
    mark-to-market PnL series compute_pnl_over_day produces (so it reflects
    open-position mark-to-market too, not just realized swings)."""
    if pnl_df.empty:
        return pnl_df.assign(running_peak=[], drawdown=[])
    df = pnl_df.copy()
    df["running_peak"] = df["pnl"].cummax()
    df["drawdown"] = df["pnl"] - df["running_peak"]
    return df


def _build_drawdown_figure(dd_df: pd.DataFrame, event_ticker: str) -> "plt.Figure | None":
    """The drawdown chart itself (pnl - running peak so far) -- shows how
    far below its own high-water mark the mark-to-market PnL curve dipped,
    and when. Always <= 0 by construction; the title reports the worst
    (most negative) point reached. Shared by plot_drawdown (standalone PNG)
    and generate_drawdown_report (PDF page 1)."""
    if dd_df.empty:
        return None

    max_dd = dd_df["drawdown"].min()
    max_dd_row = dd_df.loc[dd_df["drawdown"].idxmin()]

    fig, ax = plt.subplots(figsize=(11, 4), facecolor=PLOT_BG)
    ax.set_facecolor(PLOT_BG)
    ax.fill_between(dd_df["time"], dd_df["drawdown"], 0.0, color=FILL_RED, alpha=0.25, zorder=1)
    ax.plot(dd_df["time"], dd_df["drawdown"], color=FILL_RED, linewidth=1.3, zorder=2)
    ax.scatter([max_dd_row["time"]], [max_dd], color=MARKOUT_RED, marker="x", s=80, zorder=3,
               label=f"max drawdown {max_dd:.2f}")
    ax.axhline(0, color=AXIS, linewidth=0.8, zorder=1)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_ylabel("drawdown from running peak ($)", color=INK_SECONDARY, fontsize=10)
    ax.set_title(
        f"{event_ticker}: drawdown over the session (max drawdown ${max_dd:.2f})",
        color=INK_PRIMARY, fontsize=11, loc="left",
    )
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, loc="lower left")
    fig.tight_layout()
    return fig


def plot_drawdown(dd_df: pd.DataFrame, event_ticker: str, out_path: str | None = None) -> str | None:
    """Standalone drawdown PNG (see _build_drawdown_figure). For the
    combined PDF (drawdown + worst markouts), see generate_drawdown_report."""
    fig = _build_drawdown_figure(dd_df, event_ticker)
    if fig is None:
        return None

    if out_path is None:
        out_path = os.path.join(_ANALYSIS_DIR, f"drawdown_{event_ticker}.png")
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"saved {out_path}")
    return out_path


def generate_drawdown_report(result, event_data, signal_events: list[tuple], event_ticker: str,
                              n: int = 5, markout_s: float = MARKOUT_WINDOW_S,
                              lookback_s: float = PLOT_LOOKBACK_S, lookahead_s: float = PLOT_LOOKAHEAD_S,
                              out_path: str | None = None, prob_grid: pd.DataFrame | None = None) -> str | None:
    """Single PDF: page 1 is the session drawdown chart, followed by one
    page per worst-`markout_s`-second markout trade (see
    _build_markout_figure) -- the drawdown's own worst points and the
    individual trades most responsible for adverse markouts, bundled into
    one document instead of scattered PNGs. Returns None (no file written)
    if there's no PnL series to plot at all."""
    pnl_df = compute_pnl_over_day(result, event_data, prob_grid=prob_grid)
    dd_df = compute_drawdown(pnl_df)
    dd_fig = _build_drawdown_figure(dd_df, event_ticker)
    if dd_fig is None:
        return None

    markouts = compute_markouts(result, event_data, markout_s=markout_s)
    worst = markouts.sort_values("markout_pnl").head(n).reset_index(drop=True)

    if out_path is None:
        out_path = os.path.join(day_dir(event_ticker), "drawdown.pdf")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with PdfPages(out_path) as pdf:
        pdf.savefig(dd_fig)
        plt.close(dd_fig)
        for i, row in worst.iterrows():
            fig = _build_markout_figure(row, i + 1, event_data, signal_events, markout_s, lookback_s, lookahead_s)
            if fig is None:
                continue
            pdf.savefig(fig)
            plt.close(fig)

    print(f"saved {out_path}")
    return out_path


def day_dir(event_ticker: str) -> str:
    """analysis/{event_ticker}/ -- the one folder every plot for a given
    backtest day lands in (worst-markouts, position, pnl, drawdown alike),
    so a run's full visual record is a single directory, not scattered
    ticker-suffixed files. Created on demand by whichever plot function
    writes into it first."""
    return os.path.join(_ANALYSIS_DIR, event_ticker)


def generate_day_plots(result, event_data, event_ticker: str, signal_events: list[tuple] | None = None,
                        config: "strat.QuotingConfig | None" = None,
                        out_dir: str | None = None, n_worst_markouts: int = 5,
                        prob_grid: pd.DataFrame | None = None) -> dict[str, str | None]:
    """The plots every backtest run should leave behind, automatically:
    position-over-day, pnl-over-day (mark-to-market incl. open positions),
    and a drawdown.pdf (the session drawdown chart plus the `n_worst_markouts`
    worst individual markout trades, see generate_drawdown_report) -- saved
    into analysis/{event_ticker}/ (see day_dir). Called from
    run_backtest.run_one() after every run (see its `auto_plot` param), not
    just when this module is run by hand -- so a plain backtest run always
    leaves a visual record without a separate manual step.

    signal_events (from precompute_atm_signal) is required for the markout
    pages in drawdown.pdf -- pass None only if you don't have it and are
    fine with drawdown.pdf containing just the drawdown page (n_worst_markouts
    forced to 0 in that case, since a markout page needs the option-implied
    signal to plot against).

    Returns {"position_over_day": path, "pnl_over_day": path, "drawdown": path}
    (a value is None only if there were no fills to plot -- see the
    individual plot_*/generate_drawdown_report's own empty-input handling)."""
    if out_dir is None:
        out_dir = day_dir(event_ticker)
    os.makedirs(out_dir, exist_ok=True)
    max_position = config.max_position if config is not None else None

    position_path = plot_position_over_day(
        result, event_ticker, max_position=max_position,
        out_path=os.path.join(out_dir, "position_over_day.png"))
    pnl_df = compute_pnl_over_day(result, event_data, prob_grid=prob_grid)
    pnl_path = plot_pnl_over_day(
        pnl_df, event_ticker, out_path=os.path.join(out_dir, "pnl_over_day.png"))
    dd_path = generate_drawdown_report(
        result, event_data, signal_events or [], event_ticker,
        n=n_worst_markouts if signal_events else 0,
        out_path=os.path.join(out_dir, "drawdown.pdf"), prob_grid=prob_grid)
    return {"position_over_day": position_path, "pnl_over_day": pnl_path, "drawdown": dd_path}


def main(event_ticker: str, n: int = 5):
    config = strat.QuotingConfig()
    event_data = data.load_event_data(event_ticker)
    markets, signal_events, _grid, _lo, probs_mid, _hi = strat.precompute_signal(
        event_ticker, return_grid=True)
    strategy = strat.SPOptionsLagQuotingStrategy(config)
    bt = engine.Backtest()
    result = bt.run(strategy.__call__, event_ticker, event_data=event_data, external_events=signal_events)

    out_dir = day_dir(event_ticker)

    markouts = compute_markouts(result, event_data)
    total_fills = len(results.fills_df(result))
    print(f"{len(markouts)} fills with a computable {MARKOUT_WINDOW_S}s markout (of {total_fills} total fills)")
    print(markouts.sort_values("markout_pnl").head(n)[
        ["time", "ticker", "side", "price", "size", "mid_at_markout", "markout_pnl"]
    ].to_string(index=False))

    # drawdown.pdf (page 1 = drawdown chart, pages 2..n+1 = worst markouts)
    # plus the always-on position/pnl plots, all into the same day_dir --
    # see generate_day_plots's docstring. No separate plot_worst_markouts
    # call here: its PNGs would just duplicate the PDF's own pages.
    generate_day_plots(result, event_data, event_ticker, signal_events=signal_events,
                        config=config, out_dir=out_dir, n_worst_markouts=n, prob_grid=probs_mid)


if __name__ == "__main__":
    # Multiple event tickers may be passed (one day's worth of plots each,
    # all ticker-suffixed so they don't overwrite one another -- see
    # plot_pnl_over_day/plot_drawdown/plot_position_over_day). The last
    # argument is `n` (worst-markouts count) only if it parses as an int;
    # otherwise it's treated as another event ticker.
    _args = sys.argv[1:]
    _n = 5
    if _args and _args[-1].isdigit():
        _n = int(_args[-1])
        _args = _args[:-1]
    _event_tickers = _args or ["KXINX-26AUG18H1600"]
    for _et in _event_tickers:
        main(_et, n=_n)
