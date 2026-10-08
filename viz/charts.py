"""
Bokeh figure builders for the SPXW/KXINX dashboard -- one linked column of
three figures (build_dashboard_figure) so panning/zooming any one panel
(quotes, position, PnL) moves all three together, via a single Range1d
object shared as all three figures' x_range.

Switched here from Plotly (see git history for pan_component.py, now
deleted) specifically because hand-rolled wheel-event JS on top of it never
got pan/zoom feel and hard axis bounds both right at once -- every new edge
case (sensitivity, an axis escaping its clamp on zoom but not pan, a
left-bound-too-far bug) needed a fresh custom fix. Bokeh's Range1d(bounds=
(lo, hi)) enforces "never pan or zoom past this" NATIVELY for every
interaction (drag-pan, scroll-zoom, box-zoom alike) with zero custom JS, and
wheel-zoom/drag-pan are built-in tools -- this is what actually retires that
whole class of bug instead of patching it again.

Transparent background + neon palette, per request. Rendered to a
standalone HTML fragment (render_dashboard_html) embedded via
streamlit.components.v1.html, the same mechanism the old Plotly version
used, so this still doesn't depend on Streamlit's own (version-pinned)
st.bokeh_chart integration.
"""
import pandas as pd
from bokeh.embed import file_html
from bokeh.layouts import column
from bokeh.models import CustomJS, DatetimeTickFormatter, Range1d, Span
from bokeh.plotting import figure
from bokeh.resources import INLINE

NEON_BLUE = "#00e5ff"     # SPXW-implied prob line
NEON_GREEN = "#39ff14"    # Kalshi bid / bought-yes fills
NEON_PINK = "#ff2079"     # Kalshi ask / sold-(bought-no) fills
NEON_PURPLE = "#bf40ff"   # net position / edge: position lean
NEON_YELLOW = "#e5ff00"   # edge: CI (vol-scaled) band
NEON_ORANGE = "#ff9500"   # edge: fee component
EDGE_CONST = "#d8d8d8"    # edge: flat constant floor
GRID = "#888888"
TEXT = "#9a9a9a"


def ci_band_label(series_or_event: str | None) -> str:
    """Legend label for the CI (vol-scaled) edge band. The band's meaning
    differs by strategy: for the SPX quoter it's the S&P-implied spread; for
    the Coin Race (KXCRYPTOLEAD15M) it's `ci_vol_multiplier * theo_vol`,
    a crypto theo-vol CI, not an S&P spread."""
    if series_or_event and "KXCRYPTOLEAD15M" in series_or_event:
        return "edge: crypto spread (CI)"
    return "edge: S&P spread (CI)"

def _bounded_range(start, end, lo, hi) -> Range1d:
    """Range1d hard-limited to [lo, hi], but via a JS-side per-edge clamp
    on 'start'/'end' instead of Bokeh's own Range1d(bounds=...) kwarg --
    confirmed directly that native `bounds=` combined with wheel_zoom's
    zoom-around-cursor computation can reject an ENTIRE zoom-out step (not
    just clip the one edge that would exceed it) whenever the cursor sits
    near an edge already at its bound, which reads as "zooming out does
    nothing" right when the user is closest to an edge and most wants it
    to work. Clamping each edge independently here instead means a
    bounded edge just stays pinned while the other keeps moving, same as
    pan/box-zoom/wheel-zoom's native per-edge clamping minus that
    reject-the-whole-update bug -- so zooming out near one bound keeps
    going until the OTHER edge also reaches its own bound (the full data
    range visible), instead of stalling early.
    """
    r = Range1d(start=start, end=end)
    clamp = CustomJS(args=dict(lo=lo, hi=hi), code="""
        const r = cb_obj;
        if (r._clamping) return;
        r._clamping = true;
        let start = r.start, end = r.end;
        if (start < lo) start = lo;
        if (end > hi) end = hi;
        if (start !== r.start) r.start = start;
        if (end !== r.end) r.end = end;
        r._clamping = false;
    """)
    r.js_on_change("start", clamp)
    r.js_on_change("end", clamp)
    return r


def _autoscale_y_to_window(x_range, y_range, times, values, include_zero: bool = False,
                            pad_frac: float = 0.08, min_span: float = 1.0):
    """Re-fits `y_range` to just the data inside `x_range` whenever the
    x-window changes, so panning/zooming the MAIN panel rescales this one
    automatically instead of leaving it flat against a whole-day extent.

    Motivation: position and PnL were both hard-bounded to their full-day
    range (the cap for position, the day's min/max for PnL), so zooming into
    a few minutes left the interesting movement as an invisible wiggle
    against a full-day scale, and the only fix was to zoom each panel's
    y-axis by hand -- which also broke the linked-x illusion, since those
    panels are supposed to be read-only followers of the main chart.

    `times` are Timestamps, converted here to epoch-ms to match what Bokeh
    puts in a datetime x_range JS-side.

    The last sample STRICTLY BEFORE the window is included in the min/max.
    Both series are step/level plots (net position is a cumsum, PnL is
    last-known mark-to-market), so the value drawn at the left edge comes
    from the most recent point before it -- a window containing no samples
    of its own still renders that carried-forward level, and ignoring it
    would scale the axis to nothing (or, mid-plateau, to a range that
    doesn't contain the line actually on screen).

    include_zero defaults to FALSE: forcing y=0 into view defeats the point
    of autoscaling once the series is far from zero (a window sitting at
    -30..-25 PnL would be squashed into the bottom sixth of an axis stretched
    up to 0, which is the exact problem this function exists to fix). The
    y=0 Span simply scrolls off-screen when the data isn't near it. pad_frac
    adds breathing room; min_span floors the y-extent so a dead-flat stretch
    doesn't collapse to a zero-height axis.
    """
    if len(times) == 0:
        return
    t_ms = [pd.Timestamp(t).value / 1e6 for t in times]
    vals = [float(v) for v in values]
    cb = CustomJS(
        args=dict(xr=x_range, yr=y_range, t=t_ms, v=vals,
                  include_zero=include_zero, pad_frac=pad_frac, min_span=min_span),
        code="""
        const lo_x = xr.start, hi_x = xr.end;
        let lo = null, hi = null, carried = null;
        for (let i = 0; i < t.length; i++) {
            if (t[i] < lo_x) { carried = v[i]; continue; }   // see docstring
            if (t[i] > hi_x) break;
            if (lo === null || v[i] < lo) lo = v[i];
            if (hi === null || v[i] > hi) hi = v[i];
        }
        if (carried !== null) {
            if (lo === null || carried < lo) lo = carried;
            if (hi === null || carried > hi) hi = carried;
        }
        if (lo === null) return;    // window is entirely before any data -- leave as-is
        if (include_zero) { if (lo > 0) lo = 0; if (hi < 0) hi = 0; }
        let span = hi - lo;
        if (span < min_span) {
            const mid = (lo + hi) / 2;
            lo = mid - min_span / 2;
            hi = mid + min_span / 2;
            span = min_span;
        }
        const pad = span * pad_frac;
        yr.start = lo - pad;
        yr.end = hi + pad;
    """)
    x_range.js_on_change("start", cb)
    x_range.js_on_change("end", cb)


def _new_figure(x_range, height: int, title: str, output_backend: str = "canvas") -> figure:
    p = figure(
        height=height, sizing_mode="stretch_width", x_axis_type="datetime", x_range=x_range,
        title=title, tools="pan,wheel_zoom,box_zoom,reset",
        active_drag="pan", active_scroll="wheel_zoom",
        output_backend=output_backend,
    )
    p.background_fill_color = None
    p.border_fill_color = None
    p.outline_line_color = None
    p.grid.grid_line_color = GRID
    p.grid.grid_line_alpha = 0.15
    p.axis.axis_line_color = GRID
    p.axis.major_tick_line_color = GRID
    p.axis.minor_tick_line_color = None
    p.axis.axis_label_text_color = TEXT
    p.axis.major_label_text_color = TEXT
    p.title.text_color = TEXT
    # A FRESH instance per figure, not a shared module-level constant --
    # confirmed directly reusing one DatetimeTickFormatter object across
    # all three figures crashes at render time ("Models must be owned by
    # only a single document") once all three get added to the same
    # Bokeh Document, unlike Range1d (shared_x below), which Bokeh's own
    # linked-panning idiom explicitly supports sharing.
    p.xaxis.formatter = DatetimeTickFormatter(
        hours="%b %d %H:%M", hourmin="%b %d %H:%M", minutes="%b %d %H:%M", minsec="%H:%M:%S", seconds="%H:%M:%S",
    )
    p.toolbar.logo = None
    return p


def build_dashboard_figure(quote_df: pd.DataFrame, fills_df: pd.DataFrame, pnl_df: pd.DataFrame,
                            event_ticker: str, market_ticker: str,
                            max_position: float | None = None,
                            prob_series: pd.DataFrame | None = None,
                            market_pnl_df: pd.DataFrame | None = None) -> column:
    """`quote_df`/`fills_df` must already be filtered to ONE market_ticker
    (the caller's job -- see viz/app.py's market selector) -- this figure
    deliberately never overlays multiple tickers on the same axes (that was
    a real complaint: e.g. AUG21 alone traded 16 tickers, all stacked on
    one position line was unreadable). PnL stays whole-day/all-tickers
    combined regardless of which market is selected, since that's a single
    line either way, not the multi-line clutter the market picker exists
    to avoid.

    `prob_series` (time, prob, prob_lo, prob_hi) is this market's FULL,
    continuous option-implied probability (see viz/data_loader.py's
    DayData.prob_grid) -- used for the implied-prob line instead of
    quote_df's own prob column, which only has rows for whenever this
    market happened to be the strategy's own ATM ticker and can have real
    multi-minute-plus gaps otherwise. Falls back to quote_df's ATM-scoped
    columns if not given. prob_lo/prob_hi aren't separately plotted -- the
    same ci_half is already shown as the "edge: S&P spread (CI)" band
    below (bid_reservation/ask_reservation, straight from debug_log --
    see _add_quote_panel), so a second band here would just be a
    duplicate."""
    if prob_series is None or prob_series.empty:
        prob_series = quote_df[["time", "prob", "prob_lo", "prob_hi"]]

    # Data extent bound on the shared x-axis -- union of quote_df, pnl_df,
    # and prob_series's own timestamps. pnl_df can legitimately be empty
    # too, not just fills_df -- confirmed directly on a real zero-fill day
    # (KXINX-26AUG25H1600): markout_analysis.compute_pnl_over_day returns
    # an empty frame when there's nothing to mark, and `.min()` on an
    # empty column returns a plain float NaN rather than NaT, which then
    # broke Python's builtin min() by mixing a float in with real
    # Timestamps ('<' not supported between float and Timestamp). Any
    # empty frame here just doesn't get a vote, same convention
    # fills_df's own empty case already used elsewhere in this file --
    # quote_df/prob_series are the only ones ever actually empty in
    # practice, but this guards all three uniformly rather than assuming.
    candidates = [df["time"] for df in (quote_df, pnl_df, prob_series) if not df.empty]
    x_min = min(s.min() for s in candidates)
    x_max = max(s.max() for s in candidates)
    shared_x = _bounded_range(x_min, x_max, x_min, x_max)

    p1 = _new_figure(
        shared_x, height=430,
        title=f"{event_ticker} -- {market_ticker}: bid/ask, SPXW-implied prob, edge demanded, fills",
        output_backend="webgl",  # this panel alone is ~9 series x thousands of points per day
    )
    _add_quote_panel(p1, quote_df, fills_df, prob_series,
                     ci_label=ci_band_label(event_ticker))

    # shared_x is passed into both so their y-axes re-fit to the visible
    # window (see _autoscale_y_to_window). The main panel deliberately keeps
    # its fixed [-0.03, 1.0] y-range: it's a probability/price axis whose
    # natural bounds ARE the full range, and it's the one panel the user
    # actually interacts with -- these two are followers.
    p2 = _new_figure(shared_x, height=220, title=f"{market_ticker}: net position")
    _add_position_panel(p2, fills_df, max_position, x_range=shared_x, x_min=x_min, x_max=x_max)

    # Title describes the market's OWN curve when there is one -- it's the
    # primary series, and titling the panel with the portfolio total is what
    # made the whole panel misread.
    if market_pnl_df is not None and not market_pnl_df.empty:
        pnl_title = (f"{market_ticker}: PnL (mark-to-market incl. open positions) "
                     f"-- settled ${market_pnl_df['pnl'].iloc[-1]:+.2f}")
        if not pnl_df.empty:
            pnl_title += f"   [all markets ${pnl_df['pnl'].iloc[-1]:+.2f}]"
    else:
        pnl_title = "Cumulative PnL, all markets combined (mark-to-market incl. open positions)"
        if not pnl_df.empty:
            pnl_title += f" -- settled ${pnl_df['pnl'].iloc[-1]:+.2f}"
    p3 = _new_figure(shared_x, height=220, title=pnl_title)
    _add_pnl_panel(p3, pnl_df, x_range=shared_x, x_min=x_min, x_max=x_max,
                    market_pnl_df=market_pnl_df, market_ticker=market_ticker)

    p1.yaxis.axis_label = "yes price / implied prob ($)"
    p2.yaxis.axis_label = "net position"
    p3.yaxis.axis_label = "PnL ($)"

    return column(p1, p2, p3, sizing_mode="stretch_width")


def render_dashboard_html(layout: column) -> str:
    return file_html(layout, INLINE, "SPXW latency-arb dashboard")


_MAX_DECISION_GAP_S = 60.0


def _break_on_gaps(df: pd.DataFrame, max_gap_s: float = _MAX_DECISION_GAP_S) -> pd.DataFrame:
    """Inserts an all-NaN row wherever consecutive `df["time"]` rows are
    more than `max_gap_s` apart, so a varea/line drawn over the result
    shows a genuine break instead of a straight line bridging the gap.
    `df` must already be sorted by time.

    Needed specifically for the edge bands (bid_reservation/ask_reservation
    -- see _add_quote_panel): quote_df only has a row whenever THIS market
    was the strategy's own current ATM ticker, so a stretch where ATM
    moved elsewhere can be many minutes wide. Confirmed directly on real
    data (KXINX-26AUG26H1600-B7662, 2026-08-26 18:44-18:49): a ~5-minute
    ATM-elsewhere gap left just two real decision points, and varea's
    default straight-line interpolation between them drew a shaded band
    that visually wandered away from the CONTINUOUS implied-prob line
    (prob_series, which keeps updating the whole time regardless of ATM
    status) -- reading as "the edge band isn't centered on the prob line"
    even though every actual DECISION point does satisfy
    bid_reservation <= prob <= ask_reservation. Same class of bug the
    prob line itself already avoids by plotting from prob_series instead
    of quote_df -- this is the band-shaped equivalent of that fix, since
    reservations have no continuous-grid source to fall back on (they're
    only ever computed AT a decision)."""
    gap_s = df["time"].diff().dt.total_seconds()
    break_after = gap_s > max_gap_s
    if not break_after.any():
        return df
    breaks = df[break_after].copy()
    breaks[[c for c in df.columns if c != "time"]] = float("nan")
    # Each break row sits just after the PRECEDING real point (midpoint in
    # time), so the line has genuinely ended before the gap rather than
    # touching either real point.
    prev_time = df["time"].shift(1)[break_after]
    breaks["time"] = prev_time + (df.loc[break_after, "time"] - prev_time) / 2
    return pd.concat([df, breaks]).sort_values("time").reset_index(drop=True)


def _add_quote_panel(p: figure, quote_df: pd.DataFrame, fills_df: pd.DataFrame, prob_series: pd.DataFrame,
                     ci_label: str = "edge: S&P spread (CI)"):
    if quote_df.empty:
        return
    quote_df = _break_on_gaps(quote_df.sort_values("time").reset_index(drop=True))
    t = quote_df["time"]

    # SPXW-implied prob line comes from prob_series (the FULL, continuous
    # per-grid-tick signal -- see build_dashboard_figure's own docstring),
    # NOT quote_df -- quote_df is ATM-scoped (only has rows for whenever
    # this market was the strategy's own current ticker) and a naive
    # step-line over it visually bridges real gaps into a misleading flat
    # stretch whenever ATM moved to a different bucket for a while, even
    # though the option-implied probability itself was being computed
    # continuously the whole time. No separate CI band drawn here -- it's
    # the same ci_half already shown as the "edge: S&P spread (CI)" band
    # below.
    pt = prob_series["time"]

    # step(mode="after") == Plotly's old shape="hv": the value holds
    # constant from one grid tick until the next (a 2s grid here, not one
    # decision until the next), not a linear glide between them.
    p.step(x=pt, y=prob_series["prob"], mode="after", color=NEON_BLUE, line_width=2,
           legend_label="SPXW-implied prob")

    # "edge demanded", decomposed into its four additive components as nested
    # bands around `prob`. Accumulated outward in the strategy's own order --
    # constant, then lean, then CI, then fee -- so each band's OUTER boundary
    # is the running total up to that term and the visible ring is that term's
    # own contribution. The outermost boundary is therefore exactly
    # bid_reservation/ask_reservation.
    #
    # Per-side, not symmetric: lean is charged only to the side that would
    # grow the position, and each side's CI edge measures to its own bound
    # (prob->prob_lo buying, prob->prob_hi selling), so the two halves are
    # different widths whenever the book is loaded or the band is skewed.
    #
    # Drawn outermost-first so each narrower band covers the previous one's
    # interior. Linear interpolation between decision points (varea has no
    # step mode).
    col = quote_df.columns

    def _c(name, default=0.0):
        return quote_df[name].fillna(default) if name in col else pd.Series(default, index=quote_df.index)

    prob_at_decision = quote_df["prob"]
    const_e = _c("constant_edge")
    lean_b, lean_a = _c("lean_bid"), _c("lean_ask")
    ci_b = _c("ci_edge")
    ci_a = _c("ci_edge_ask") if "ci_edge_ask" in col else ci_b
    fee_b, fee_a = _c("taker_fee_bid"), _c("taker_fee_ask")

    # cumulative outer boundaries, innermost -> outermost
    c_bid, c_ask = prob_at_decision - const_e, prob_at_decision + const_e
    l_bid, l_ask = c_bid - lean_b, c_ask + lean_a
    ci_bid, ci_ask = l_bid - ci_b, l_ask + ci_a
    bid_reservation = quote_df["bid_reservation"]
    ask_reservation = quote_df["ask_reservation"]

    p.varea(x=t, y1=bid_reservation, y2=ask_reservation, fill_color=NEON_ORANGE, fill_alpha=0.18,
            legend_label="edge: fee")
    p.varea(x=t, y1=ci_bid, y2=ci_ask, fill_color=NEON_YELLOW, fill_alpha=0.28,
            legend_label=ci_label)
    p.varea(x=t, y1=l_bid, y2=l_ask, fill_color=NEON_PURPLE, fill_alpha=0.30,
            legend_label="edge: position lean")
    p.varea(x=t, y1=c_bid, y2=c_ask, fill_color=EDGE_CONST, fill_alpha=0.35,
            legend_label="edge: constant")

    # Kalshi bid/ask as two SEPARATE lines (not a shaded band).
    p.step(x=t, y=quote_df["market_bid"], mode="after", color=NEON_GREEN, line_width=1.8,
           legend_label="Kalshi bid")
    p.step(x=t, y=quote_df["market_ask"], mode="after", color=NEON_PINK, line_width=1.8,
           legend_label="Kalshi ask")

    # Where our own quotes were resting, in YES terms like everything else on
    # this axis (the no-side order actually sent is at 1 - quote_ask). Dotted
    # against the market's solid lines, in that side's colour. NaN wherever
    # the strategy wasn't quoting that side, which bokeh renders as a gap --
    # the right answer, since "no quote" is not a level.
    #
    # The renderer is omitted ENTIRELY, legend entry included, when the column
    # holds no real values -- quoting off, or a non-l2 day where a quote can
    # never rest (see engine's no_l2_depth warning). An always-present empty
    # legend entry reads as "we quoted and it's off-screen" rather than "there
    # was no quoting here".
    for name, color, label in (("quote_bid", NEON_GREEN, "our quote (bid)"),
                               ("quote_ask", NEON_PINK, "our quote (ask)")):
        if name in col and quote_df[name].notna().any():
            p.step(x=t, y=quote_df[name], mode="after", color=color,
                   line_width=1.4, line_dash="dotted", legend_label=label)

    if not fills_df.empty:
        # Two independent facts per fill, on two independent channels.
        #
        # COLOR = which side we bought (green=bought yes, pink=sold (bought
        # no)) -- the conventional buy/sell color scheme, per explicit
        # preference. Note this means the color does NOT match whichever
        # line the fill actually lands on (a yes-buy resolves at the ASK, a
        # no-buy at the BID -- see execution.resolve_marketable) --
        # confirmed directly against real fills that the PRICES themselves
        # are always correct regardless of this color choice.
        #
        # MARKER = how the fill happened: a circle for a quote of ours that
        # got hit, an x for a take that crossed the book.
        liquidity = (fills_df["liquidity"] if "liquidity" in fills_df.columns
                     else pd.Series("taker", index=fills_df.index))
        for side, color, side_label in (("yes", NEON_GREEN, "bought yes"),
                                        ("no", NEON_PINK, "sold / bought no")):
            for kind, kind_label, style in (
                    ("maker", "quoted", dict(marker="circle", size=7, color=color,
                                             line_color="white", line_width=0.5)),
                    # x is a line-only marker, so it takes line_color rather
                    # than a fill and needs the extra weight to read at all.
                    ("taker", "took", dict(marker="x", size=9, line_color=color, line_width=2))):
                sub = fills_df[(fills_df["side"] == side) & (liquidity == kind)]
                if sub.empty:
                    continue
                p.scatter(x=sub["time"], y=sub["yes_equiv_price"],
                          legend_label=f"{kind_label} ({side_label})", **style)

    # Top capped exactly at 1 (a price/probability is never meaningful
    # above it) -- small padding only at the bottom, around 0. Hard-bounded
    # via Range1d, same as the shared x-axis above -- can't pan or zoom past
    # either edge.
    p.y_range = _bounded_range(-0.03, 1.0, -0.03, 1.0)

    p.legend.location = "top_left"
    p.legend.click_policy = "hide"
    p.legend.background_fill_alpha = 0.0
    p.legend.label_text_color = TEXT
    p.legend.border_line_alpha = 0
    p.legend.label_text_font_size = "9px"


def _add_position_panel(p: figure, fills_df: pd.DataFrame, max_position: float | None,
                         x_range=None, x_min=None, x_max=None):
    """`fills_df` is already single-market (see build_dashboard_figure) --
    one line, not one-per-ticker."""
    if fills_df.empty:
        return
    g = fills_df.sort_values("time")
    signed = g["size"].where(g["side"] == "yes", -g["size"])
    net = signed.cumsum()

    # Extend the step line across the FULL plot span rather than only the
    # first-fill..last-fill window. Net position is a level that exists at
    # every instant, not just at the moments it changes: it is flat 0 from
    # the session start until the first fill, and holds its final value from
    # the last fill onward. Plotting only the fill timestamps drew the line
    # as starting and stopping mid-chart, which reads as missing data rather
    # than "flat here" -- and left it visually unaligned with the quote and
    # PnL panels above/below, which do span the whole session.
    times = list(g["time"])
    vals = [float(v) for v in net]
    if x_min is not None and times and times[0] > x_min:
        times.insert(0, x_min)
        vals.insert(0, 0.0)          # no position before the first fill
    if x_max is not None and times and times[-1] < x_max:
        times.append(x_max)
        vals.append(vals[-1])        # mode="after" holds the last level to the end
    p.step(x=times, y=vals, mode="after", color=NEON_PURPLE, line_width=1.8)
    if max_position:
        for level in (max_position, -max_position):
            p.add_layout(Span(location=level, dimension="width", line_color=TEXT,
                               line_dash="dashed", line_width=1, line_alpha=0.6))

    # Autoscales to whatever the main panel's x-window shows, rather than
    # staying pinned to +/-max_position: the cap is often far larger than the
    # position actually reached in a given window, which flattened the line
    # into the middle of the axis. min_span=2 keeps a 1-lot move readable
    # without the axis collapsing onto it. Initial extent uses the same rule
    # as the callback (fit the data, don't force 0 in) so the first paint and
    # the first pan don't jump to different scales.
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 2.0)
    p.y_range = Range1d(start=lo - span * 0.08, end=hi + span * 0.08)
    if x_range is not None:
        # the EXTENDED series, so a window before the first fill or after the
        # last one still autoscales against the level actually drawn there
        _autoscale_y_to_window(x_range, p.y_range, times, vals, min_span=2.0)


def _add_pnl_panel(p: figure, pnl_df: pd.DataFrame, x_range=None, x_min=None, x_max=None,
                    market_pnl_df: pd.DataFrame | None = None, market_ticker: str | None = None):
    if pnl_df.empty:
        return
    # The SELECTED market's own curve is the subject of this panel; the
    # all-markets total is context only. They were the other way round, which
    # put a whole-day total directly under a single market's position panel
    # and got read as that market's PnL -- a market that settled +$0.16 was
    # reported as losing money because the combined line was negative.
    primary = market_pnl_df if (market_pnl_df is not None and not market_pnl_df.empty) else pnl_df
    is_market = primary is not pnl_df
    final = primary["pnl"].iloc[-1]
    color = NEON_BLUE if final >= 0 else NEON_PINK

    # Same full-span extension as the position panel: PnL is 0 before the
    # first fill (nothing traded yet) and holds its settled value afterwards,
    # so the curve should run edge to edge rather than starting and stopping
    # mid-chart. compute_pnl_over_day's own grid deliberately starts at the
    # first fill, so this is a plotting concern, not a data one.
    def _span(df):
        tt = list(df["time"])
        vv = [float(x) for x in df["pnl"]]
        if x_min is not None and tt and tt[0] > x_min:
            tt.insert(0, x_min)
            vv.insert(0, 0.0)
        if x_max is not None and tt and tt[-1] < x_max:
            tt.append(x_max)
            vv.append(vv[-1])
        return tt, vv

    t, v = _span(primary)
    label = (f"{market_ticker.split('-')[-1]} only" if (is_market and market_ticker) else "all markets")
    p.varea(x=t, y1=[0.0] * len(v), y2=v, fill_color=color, fill_alpha=0.12)
    p.line(x=t, y=v, color=color, line_width=2, legend_label=label)

    # The selected market's OWN curve, on the same axes. Without it this panel
    # shows a whole-day total directly beneath a single market's position
    # panel, which reads as that market's PnL -- a real misreading (a market
    # that settled +$0.16 was reported as losing money because the combined
    # line below it was negative).
    if is_market and not pnl_df.empty:
        # All-markets total, kept only as faint context behind the market's
        # own line -- never the thing being read off this panel.
        mt, mv = _span(pnl_df)
        p.line(x=mt, y=mv, color=TEXT, line_width=1.2, line_dash=(6, 3), line_alpha=0.55,
               legend_label="all markets (context)")
        # Both lines share the axis, so the range must cover both. Merged and
        # re-sorted rather than concatenated -- _autoscale_y_to_window walks
        # the series in time order and stops at the window's right edge.
        merged = sorted(zip(t + mt, v + mv), key=lambda r: r[0])
        range_t = [r[0] for r in merged]
        range_v = [r[1] for r in merged]
    else:
        range_t, range_v = t, v
    p.add_layout(Span(location=0, dimension="width", line_color=TEXT, line_width=1, line_alpha=0.6))

    # Initial extent is the whole day (which is also the initial x-window);
    # from there it re-fits to whatever the main panel is showing, so zooming
    # into a drawdown fills the panel with that drawdown instead of leaving
    # it as a small wiggle against the full day's PnL range. Same fit-the-data
    # rule as the callback (see _autoscale_y_to_window on why 0 isn't forced
    # into view), so first paint and first pan agree.
    lo, hi = min(range_v), max(range_v)
    pad = (hi - lo) * 0.05 if hi > lo else 1.0
    p.y_range = Range1d(start=lo - pad, end=hi + pad)
    if x_range is not None:
        _autoscale_y_to_window(x_range, p.y_range, range_t, range_v, min_span=1.0)

    p.legend.location = "top_left"
    p.legend.background_fill_alpha = 0.0
    p.legend.label_text_color = TEXT
    p.legend.border_line_alpha = 0
    p.legend.label_text_font_size = "9px"
