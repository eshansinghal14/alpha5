"""
Self-updating live chart: a Bokeh document that fetches its own data and
mutates its ColumnDataSources in place, instead of being re-rendered by
Streamlit.

Why this exists
---------------
Streamlit reruns the script on every refresh, and anything handed to
components.html gets a new `srcdoc`, which remounts the iframe. A remount
resets pan/zoom and blanks the panel -- unusable for watching a live
session. That's a Streamlit limitation, not a Bokeh one: partial component
updates are an open request (streamlit#12980), and Plotly's `uirevision`,
the usual workaround, has been broken in Streamlit since 1.10
(streamlit#5107, #8882).

So the data path goes around Streamlit entirely:
  1. build_live_html() emits a document with EMPTY, NAMED ColumnDataSources
     and no data baked in, so the html string is constant for a given
     (series, market) and Streamlit's diff never remounts it;
  2. an injected setInterval fetches a small JSON snapshot that run_live
     writes (see live_persist.write_stream_json);
  3. it assigns straight into `source.data` and emits a change, which Bokeh
     redraws without touching the view.

Zoom, pan and axis ranges survive, because nothing is rebuilt.

The document is only rebuilt when the user picks a different market -- a
deliberate action, where a redraw is correct rather than disruptive.
"""
import json

from bokeh.embed import file_html
from bokeh.layouts import column
from bokeh.models import ColumnDataSource, CustomJS, DatetimeTickFormatter, Range1d, Span
from bokeh.plotting import figure
from bokeh.resources import INLINE

from charts import (EDGE_CONST, GRID, NEON_BLUE, NEON_GREEN, NEON_ORANGE, NEON_PINK,
                    NEON_PURPLE, NEON_YELLOW, TEXT, _bounded_range, ci_band_label)

POLL_MS = 1500   # matches run_live's own flush cadence -- polling faster just refetches the same file


def _fig(x_range, height, title, y_range=None):
    # y_range omitted rather than passed as None -- bokeh's FigureOptions
    # rejects None outright instead of treating it as "use the default".
    kw = {"y_range": y_range} if y_range is not None else {}
    p = figure(height=height, sizing_mode="stretch_width", x_axis_type="datetime",
               x_range=x_range, title=title, **kw,
               tools="pan,wheel_zoom,box_zoom,reset", active_drag="pan", active_scroll="wheel_zoom")
    p.background_fill_color = None
    p.border_fill_color = None
    p.outline_line_color = None
    p.grid.grid_line_color = GRID
    p.grid.grid_line_alpha = 0.15
    p.axis.axis_line_color = GRID
    p.axis.major_tick_line_color = GRID
    p.axis.minor_tick_line_color = None
    p.axis.major_label_text_color = TEXT
    p.axis.axis_label_text_color = TEXT
    p.title.text_color = TEXT
    p.xaxis.formatter = DatetimeTickFormatter(
        hours="%H:%M", hourmin="%H:%M", minutes="%H:%M", minsec="%H:%M:%S", seconds="%H:%M:%S")
    p.toolbar.logo = None
    return p


def _style_legend(p):
    """Called after glyphs are added -- bokeh only creates a Legend when a
    glyph supplies legend_label, and setting legend props before that warns
    and silently does nothing."""
    if not p.legend:
        return
    p.legend.location = "top_left"
    p.legend.background_fill_alpha = 0.0
    p.legend.label_text_color = TEXT
    p.legend.border_line_alpha = 0
    p.legend.label_text_font_size = "9px"
    p.legend.click_policy = "hide"


def build_live_html(series: str, market: str, json_url: str,
                    x_start_ms: float, x_end_ms: float, max_position: float | None = None,
                    quoting: bool = False) -> str:
    """Standalone HTML for `market`'s live chart.

    Contains NO data -- every source starts empty and is filled by the
    poller. That's what keeps the string constant across Streamlit reruns
    (and therefore keeps the iframe mounted); it also means the panels are
    briefly blank until the first fetch lands.

    `x_start_ms`/`x_end_ms` fix the time axis to the session window so the
    range is stable at build time. A DataRange1d would auto-follow incoming
    data and keep yanking the view as points arrive, which is the same
    problem in a different costume.
    """
    quote_src = ColumnDataSource(dict(t=[], bid=[], ask=[], prob=[], br=[], ar=[], qb=[], qa=[]),
                                 name="quote_src")
    # One source per band, drawn outermost-first so each narrower band covers
    # the previous one's interior and only its own ring stays visible.
    fee_src = ColumnDataSource(dict(t=[], lo=[], hi=[]), name="fee_src")
    # Taker reservation (crypto only): dashed lines, not a band -- it moves
    # with the Kalshi touch via the fee and isn't what resting quotes use.
    taker_src = ColumnDataSource(dict(t=[], lo=[], hi=[]), name="taker_src")
    lipref_src = ColumnDataSource(dict(t=[], b=[], a=[]), name="lipref_src")
    ci_src = ColumnDataSource(dict(t=[], lo=[], hi=[]), name="ci_src")
    lean_src = ColumnDataSource(dict(t=[], lo=[], hi=[]), name="lean_src")
    const_src = ColumnDataSource(dict(t=[], lo=[], hi=[]), name="const_src")
    # One source per (side, liquidity): colour says which side we bought,
    # marker says whether a quote of ours was hit or we crossed to take.
    fills_yes_maker = ColumnDataSource(dict(t=[], p=[]), name="fills_yes_maker")
    fills_yes_taker = ColumnDataSource(dict(t=[], p=[]), name="fills_yes_taker")
    fills_no_maker = ColumnDataSource(dict(t=[], p=[]), name="fills_no_maker")
    fills_no_taker = ColumnDataSource(dict(t=[], p=[]), name="fills_no_taker")
    pos_src = ColumnDataSource(dict(t=[], v=[]), name="pos_src")
    pnl_src = ColumnDataSource(dict(t=[], v=[]), name="pnl_src")

    # Hard-bounded to [session start, now], clamped per-edge in JS. "now" is
    # read at clamp time since it moves. Per-edge clamp rather than bokeh's
    # `bounds=`, which rejects a whole zoom step near a bound (see
    # charts._bounded_range).
    xr = Range1d(start=x_start_ms, end=x_end_ms)
    clamp = CustomJS(args=dict(lo=x_start_ms), code="""
        const r = cb_obj;
        if (r._clamping) return;
        r._clamping = true;
        const hi = Math.max(Date.now(), lo + 1000);
        if (r.start < lo) r.start = lo;
        if (r.end > hi) r.end = hi;
        if (r.start >= r.end) r.start = Math.max(lo, r.end - 1000);
        r._clamping = false;
    """)
    xr.js_on_change("start", clamp)
    xr.js_on_change("end", clamp)

    # Same hard-bounded probability axis as the historical chart, via the
    # SAME helper -- reused rather than reimplemented so the two can't drift.
    # Everything on this panel is a probability or a price in [0, 1], so
    # panning outside it only ever shows empty space.
    p1 = _fig(xr, 430, f"{market}: Kalshi bid/ask, implied prob, edge demanded, fills",
              y_range=_bounded_range(-0.03, 1.0, -0.03, 1.0))
    crypto = "KXCRYPTOLEAD15M" in series
    p1.varea(x="t", y1="lo", y2="hi", source=fee_src, fill_color=NEON_ORANGE,
             fill_alpha=0.18,
             legend_label=("edge: quote reservation (net of LIP)" if crypto else "edge: fee"))
    if crypto:
        p1.step(x="t", y="lo", source=taker_src, mode="after", color=NEON_ORANGE,
                line_width=1.0, line_dash="dashed", alpha=0.7, legend_label="take reservation (incl. fee)")
        p1.step(x="t", y="hi", source=taker_src, mode="after", color=NEON_ORANGE,
                line_width=1.0, line_dash="dashed", alpha=0.7)
        p1.step(x="t", y="b", source=lipref_src, mode="after", color="#ffffff",
                line_width=1.0, line_dash="dotdash", alpha=0.6, legend_label="LIP reference price")
        p1.step(x="t", y="a", source=lipref_src, mode="after", color="#ffffff",
                line_width=1.0, line_dash="dotdash", alpha=0.6)
    p1.varea(x="t", y1="lo", y2="hi", source=ci_src, fill_color=NEON_YELLOW,
             fill_alpha=0.28, legend_label=ci_band_label(series))
    p1.varea(x="t", y1="lo", y2="hi", source=lean_src, fill_color=NEON_PURPLE,
             fill_alpha=0.30, legend_label="edge: position lean")
    p1.varea(x="t", y1="lo", y2="hi", source=const_src, fill_color=EDGE_CONST,
             fill_alpha=0.35, legend_label="edge: constant")
    p1.step(x="t", y="bid", source=quote_src, mode="after", color=NEON_GREEN,
            line_width=1.8, legend_label="Kalshi bid")
    p1.step(x="t", y="ask", source=quote_src, mode="after", color=NEON_PINK,
            line_width=1.8, legend_label="Kalshi ask")
    p1.step(x="t", y="prob", source=quote_src, mode="after", color=NEON_BLUE,
            line_width=2, legend_label="implied prob")
    # Our own resting quote levels, in YES terms. Dotted against the market's
    # solid touch lines; NaN where we weren't quoting that side, which bokeh
    # draws as a gap rather than a level we never had.
    #
    # Built only when the session is actually quoting. The document is emitted
    # once, before any data arrives, so it can't decide this from the data the
    # way charts.py does -- an empty pair of legend entries on every non-
    # quoting session would read as "we quoted somewhere off-screen".
    if quoting:
        p1.step(x="t", y="qb", source=quote_src, mode="after", color=NEON_GREEN,
                line_width=1.4, line_dash="dotted", legend_label="our quote (bid)")
        p1.step(x="t", y="qa", source=quote_src, mode="after", color=NEON_PINK,
                line_width=1.4, line_dash="dotted", legend_label="our quote (ask)")
    p1.scatter(x="t", y="p", source=fills_yes_maker, size=8, marker="circle",
               fill_color=NEON_GREEN, line_color="white", line_width=0.5,
               legend_label="quoted (bought yes)")
    p1.scatter(x="t", y="p", source=fills_no_maker, size=8, marker="circle",
               fill_color=NEON_PINK, line_color="white", line_width=0.5,
               legend_label="quoted (sold / bought no)")
    # x is line-only -- no fill -- so it takes line_color and extra weight.
    p1.scatter(x="t", y="p", source=fills_yes_taker, size=10, marker="x",
               line_color=NEON_GREEN, line_width=2, legend_label="took (bought yes)")
    p1.scatter(x="t", y="p", source=fills_no_taker, size=10, marker="x",
               line_color=NEON_PINK, line_width=2, legend_label="took (sold / bought no)")

    _style_legend(p1)

    p2 = _fig(xr, 200, f"{market}: net position")
    p2.step(x="t", y="v", source=pos_src, mode="after", color=NEON_PURPLE, line_width=1.8)
    if max_position:
        for lvl in (max_position, -max_position):
            p2.add_layout(Span(location=lvl, dimension="width", line_color=TEXT,
                               line_dash="dashed", line_width=1, line_alpha=0.5))

    p3 = _fig(xr, 200, f"{market}: PnL (this market only, mark-to-market)")
    p3.varea(x="t", y1=0, y2="v", source=pnl_src, fill_color=NEON_ORANGE, fill_alpha=0.12)
    p3.line(x="t", y="v", source=pnl_src, color=NEON_ORANGE, line_width=2)
    p3.add_layout(Span(location=0, dimension="width", line_color=TEXT, line_width=1, line_alpha=0.5))

    layout = column(p1, p2, p3, sizing_mode="stretch_width")
    html = file_html(layout, INLINE, f"{series} live -- {market}")

    poller = """
<script>
(function () {
  const URL = %s, MARKET = %s, POLL = %d, START = %f;
  // y-autoscale for the two follower panels, mirroring the historical
  // chart's behaviour -- a fixed range would flatten them as data arrives.
  function fit(fig, vals, minSpan) {
    if (!vals.length) return;
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    let span = hi - lo;
    if (span < minSpan) { const m = (lo + hi) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; span = minSpan; }
    const pad = span * 0.08;
    fig.y_range.start = lo - pad; fig.y_range.end = hi + pad;
  }
  // Follow the live edge until the user takes control. We remember the
  // range we last set ourselves; if it still matches, nobody has panned or
  // zoomed, so it's safe to advance the right edge to `now`. The moment it
  // differs the view is the user's and we stop touching it -- otherwise
  // auto-following would fight every drag, which is the same reset problem
  // this whole design exists to avoid.
  // Net position is a level that exists at every instant, not only at the
  // moments it changes: flat 0 from session start to the first fill, holding
  // its last value afterwards. Same extension charts._add_position_panel
  // does, but applied here rather than in the JSON because the right edge is
  // "now", which moves between writes.
  function extend(t, v, endMs) {
    t = (t || []).slice(); v = (v || []).slice();
    if (!t.length) return { t: t, v: v };
    if (t[0] > START) { t.unshift(START); v.unshift(0); }
    if (t[t.length - 1] < endMs) { t.push(endMs); v.push(v[v.length - 1]); }
    return { t: t, v: v };
  }
  let lastSet = null;
  function follow(doc) {
    const xr = doc.roots()[0].children[0].x_range;
    if (lastSet && (xr.start !== lastSet[0] || xr.end !== lastSet[1])) return;
    xr.start = START; xr.end = Date.now();
    lastSet = [xr.start, xr.end];
  }
  async function poll() {
    let j;
    try {
      const r = await fetch(URL + '?t=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) return;
      j = await r.json();
    } catch (e) { return; }               // a missed poll is harmless; try again next tick
    const doc = Bokeh.documents[0];
    if (!doc) return;
    follow(doc);   // before the data, so a missing market still advances the edge
    const m = (j.markets || {})[MARKET];
    if (!m) return;
    const put = (name, data) => {
      const s = doc.get_model_by_name(name);
      if (s) { s.data = data; s.change.emit(); }
    };
    put('quote_src', { t: m.q_t, bid: m.bid, ask: m.ask, prob: m.prob_on_q, br: m.br, ar: m.ar,
                       qb: m.qb || [], qa: m.qa || [] });
    put('fee_src',   { t: m.q_t, lo: m.br,     hi: m.ar });
    if (m.tbr) put('taker_src', { t: m.q_t, lo: m.tbr, hi: m.tar });
    if (m.lip_rb) put('lipref_src', { t: m.q_t, b: m.lip_rb, a: m.lip_ra });
    put('ci_src',    { t: m.q_t, lo: m.ci_bid, hi: m.ci_ask });
    put('lean_src',  { t: m.q_t, lo: m.l_bid,  hi: m.l_ask });
    put('const_src', { t: m.q_t, lo: m.c_bid,  hi: m.c_ask });
    put('fills_yes_maker', { t: m.fy_mt || [], p: m.fy_mp || [] });
    put('fills_yes_taker', { t: m.fy_tt || [], p: m.fy_tp || [] });
    put('fills_no_maker', { t: m.fn_mt || [], p: m.fn_mp || [] });
    put('fills_no_taker', { t: m.fn_tt || [], p: m.fn_tp || [] });
    const pos = extend(m.pos_t, m.pos_v, Date.now());
    put('pos_src', pos);
    put('pnl_src', { t: m.pnl_t, v: m.pnl_v });
    // panels 2 and 3 (index 1, 2 of the column layout)
    const figs = doc.roots()[0].children;
    if (figs && figs.length >= 3) {
      fit(figs[1], pos.v, 2);
      fit(figs[2], m.pnl_v || [], 1);
    }
  }
  poll();
  setInterval(poll, POLL);
})();
</script>
""" % (json.dumps(json_url), json.dumps(market), POLL_MS, x_start_ms)

    return html.replace("</body>", poller + "</body>")
