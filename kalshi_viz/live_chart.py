"""
Self-updating per-market chart: a Bokeh document with EMPTY named sources
that polls its own JSON (kalshi_infra.runtime.persist.write_streams) and
mutates the sources in place.

Streamlit remounts a components.html iframe whenever its html string changes,
which resets pan/zoom; keeping the string constant per (series, market,
session) and moving the data path into the iframe sidesteps that.

Series-agnostic: every series' stream has the same schema.
"""
from __future__ import annotations

import json

from bokeh.embed import file_html
from bokeh.events import Reset
from bokeh.layouts import row
from bokeh.models import ColumnDataSource, CustomJS, DatetimeTickFormatter, LinearAxis, Range1d, Span
from bokeh.plotting import figure
from bokeh.resources import CDN

# BokehJS from cdn.bokeh.org (pinned to the installed version), cached by the
# browser once. INLINE embedded ~1.4 MB of JS in EVERY chart iframe: a
# 53-market KXINXU page was ~75 MB through the SSH tunnel (2026-10-14).
# Not self-hosted: Streamlit's static route serves .js as text/plain + nosniff,
# which browsers refuse to execute.
RESOURCES = CDN

POLL_MS = 1500   # ~ the runtime's flush cadence; faster just refetches the same file
CHART_HEIGHT = 360
# Pos/PnL sits to the right of a theo chart that fills the visible iframe, so
# it is reached by scrolling the iframe horizontally.
POSPNL_WIDTH = 620
IFRAME_HEIGHT = CHART_HEIGHT + 18   # + horizontal scrollbar

BLUE, GREEN, PINK, PURPLE = "#00e5ff", "#39ff14", "#ff2079", "#bf40ff"
YELLOW, ORANGE, CONST, GRID, TEXT = "#e5ff00", "#ff9500", "#d8d8d8", "#888888", "#9a9a9a"


def _fig(x_range, height, title, y_range=None):
    kw = {"y_range": y_range} if y_range is not None else {}
    p = figure(height=height, sizing_mode="stretch_width", x_axis_type="datetime", x_range=x_range,
               title=title, tools="pan,wheel_zoom,box_zoom,reset", active_drag="pan",
               active_scroll="wheel_zoom", **kw)
    p.background_fill_color = p.border_fill_color = None
    p.outline_line_color = None
    p.grid.grid_line_color, p.grid.grid_line_alpha = GRID, 0.15
    p.axis.axis_line_color = p.axis.major_tick_line_color = GRID
    p.axis.minor_tick_line_color = None
    p.axis.major_label_text_color = p.axis.axis_label_text_color = p.title.text_color = TEXT
    p.xaxis.formatter = DatetimeTickFormatter(hours="%H:%M", hourmin="%H:%M", minutes="%H:%M",
                                              minsec="%H:%M:%S", seconds="%H:%M:%S")
    p.toolbar.logo = None
    return p


def _legend(p):
    if not p.legend:
        return
    p.legend.location = "top_left"
    p.legend.background_fill_alpha = 0.0
    p.legend.label_text_color = TEXT
    p.legend.border_line_alpha = 0
    p.legend.label_text_font_size = "9px"
    p.legend.click_policy = "hide"


def build_live_html(series: str, market: str, json_url: str, x_start_ms: float, x_end_ms: float,
                    max_position: float | None, quoting: bool) -> str:
    """Standalone HTML for one market. Contains NO data (that's what keeps
    the string constant across reruns). The x-axis is fixed to [session
    start, now] and clamped per-edge in JS; DataRange1d would chase incoming
    points and yank the view."""
    src = {n: ColumnDataSource(d, name=n) for n, d in {
        "quote": dict(t=[], bid=[], ask=[], prob=[], qb=[], qa=[], lb=[], la=[]),
        "fee": dict(t=[], lo=[], hi=[]), "ci": dict(t=[], lo=[], hi=[]),
        "lean": dict(t=[], lo=[], hi=[]), "const": dict(t=[], lo=[], hi=[]),
        "band": dict(t=[], lo=[], hi=[]),
        "fy_m": dict(t=[], p=[]), "fy_t": dict(t=[], p=[]), "fn_m": dict(t=[], p=[]), "fn_t": dict(t=[], p=[]),
        "pos": dict(t=[], v=[]), "pnl": dict(t=[], v=[]),
    }.items()}

    xr = Range1d(start=x_start_ms, end=x_end_ms)
    clamp = CustomJS(args=dict(lo=x_start_ms), code="""
        const r = cb_obj; if (r._c) return; r._c = true;
        const hi = Math.max(Date.now(), lo + 1000);
        if (r.start < lo) r.start = lo; if (r.end > hi) r.end = hi;
        if (r.start >= r.end) r.start = Math.max(lo, r.end - 1000);
        r._c = false;""")
    xr.js_on_change("start", clamp)
    xr.js_on_change("end", clamp)
    yr = Range1d(start=-0.03, end=1.0, bounds=(-0.03, 1.0))

    p1 = _fig(xr, CHART_HEIGHT, "Kalshi touch, fair, edge demanded, fills", y_range=yr)
    p1.name = "theo"
    # Bands outermost-first so each narrower one covers the previous interior.
    p1.varea(x="t", y1="lo", y2="hi", source=src["fee"], fill_color=ORANGE, fill_alpha=0.18,
             legend_label="edge: taker fee (= take reservation)")
    p1.varea(x="t", y1="lo", y2="hi", source=src["ci"], fill_color=YELLOW, fill_alpha=0.28,
             legend_label="edge: band (edge_fraction x model band)")
    p1.varea(x="t", y1="lo", y2="hi", source=src["lean"], fill_color=PURPLE, fill_alpha=0.30,
             legend_label="edge: position lean")
    p1.varea(x="t", y1="lo", y2="hi", source=src["const"], fill_color=CONST, fill_alpha=0.35,
             legend_label="edge: constant")
    p1.step(x="t", y="lo", source=src["band"], mode="after", color=BLUE, line_width=0.8, alpha=0.5,
            line_dash="dashed", legend_label="model band")
    p1.step(x="t", y="hi", source=src["band"], mode="after", color=BLUE, line_width=0.8, alpha=0.5,
            line_dash="dashed")
    p1.step(x="t", y="bid", source=src["quote"], mode="after", color=GREEN, line_width=1.8, legend_label="Kalshi bid")
    p1.step(x="t", y="ask", source=src["quote"], mode="after", color=PINK, line_width=1.8, legend_label="Kalshi ask")
    p1.step(x="t", y="prob", source=src["quote"], mode="after", color=BLUE, line_width=2, legend_label="fair")
    if quoting:
        p1.step(x="t", y="qb", source=src["quote"], mode="after", color=GREEN, line_width=1.4,
                line_dash="dotted", legend_label="our quote (bid)")
        p1.step(x="t", y="qa", source=src["quote"], mode="after", color=PINK, line_width=1.4,
                line_dash="dotted", legend_label="our quote (ask)")
        p1.step(x="t", y="lb", source=src["quote"], mode="after", color=GREEN, line_width=1.0,
                line_dash="dashdot", alpha=0.6, legend_label="LIP quote (bid)")
        p1.step(x="t", y="la", source=src["quote"], mode="after", color=PINK, line_width=1.0,
                line_dash="dashdot", alpha=0.6, legend_label="LIP quote (ask)")
    # Fills: circle = maker (quoted), x = taker (took); green = bought yes, pink = sold / bought no.
    for name, color in (("fy_m", GREEN), ("fn_m", PINK)):
        p1.scatter(x="t", y="p", source=src[name], size=8, marker="circle", fill_color=color,
                   line_color="white", line_width=0.5)
    for name, color in (("fy_t", GREEN), ("fn_t", PINK)):
        p1.scatter(x="t", y="p", source=src[name], size=10, marker="x", line_color=color, line_width=2)
    _legend(p1)

    # Position on the left axis, PnL on the right: different units, one time axis.
    p2 = _fig(xr, CHART_HEIGHT, "net position (left) / PnL at fair, $ (right)", y_range=Range1d(-1, 1))
    p2.sizing_mode, p2.width = "fixed", POSPNL_WIDTH
    p2.name = "pospnl"
    p2.extra_y_ranges = {"pnl": Range1d(-1, 1)}
    p2.yaxis.axis_label = "contracts"
    pnl_axis = LinearAxis(y_range_name="pnl", axis_label="$")
    pnl_axis.axis_line_color = pnl_axis.major_tick_line_color = GRID
    pnl_axis.minor_tick_line_color = None
    pnl_axis.major_label_text_color = pnl_axis.axis_label_text_color = ORANGE
    p2.add_layout(pnl_axis, "right")
    p2.yaxis[0].major_label_text_color = p2.yaxis[0].axis_label_text_color = PURPLE
    p2.varea(x="t", y1=0, y2="v", source=src["pnl"], fill_color=ORANGE, fill_alpha=0.10, y_range_name="pnl")
    p2.line(x="t", y="v", source=src["pnl"], color=ORANGE, line_width=2, y_range_name="pnl", legend_label="PnL")
    p2.add_layout(Span(location=0, dimension="width", line_color=ORANGE, line_width=1, line_alpha=0.4,
                       y_range_name="pnl"))
    p2.step(x="t", y="v", source=src["pos"], mode="after", color=PURPLE, line_width=1.8, legend_label="position")
    if max_position:
        for lvl in (max_position, -max_position):
            p2.add_layout(Span(location=lvl, dimension="width", line_color=PURPLE, line_dash="dashed",
                               line_width=1, line_alpha=0.5))
    _legend(p2)

    # Bokeh's reset restores the x-range from page load, which is a stale
    # "now". Go back to the whole session and resume following the live edge.
    on_reset = CustomJS(code="if (window.a5Reset) window.a5Reset();")
    p1.js_on_event(Reset, on_reset)
    p2.js_on_event(Reset, on_reset)

    html = file_html(row(p1, p2, sizing_mode="stretch_width"), RESOURCES, f"{series} -- {market}")
    # The body is one viewport plus the pos/pnl chart wide, so the
    # stretch_width theo chart takes exactly the visible width.
    poller = """
<style>html, body { margin: 0; } body { width: calc(100vw + %dpx); overflow-y: hidden; }</style>
<script>
(function () {
  const URL = %s, POLL = %d, START = %f;
  function fit(range, vals, minSpan) {
    vals = vals.filter(v => v !== null && !Number.isNaN(v));
    if (!vals.length) return;
    let lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo;
    if (span < minSpan) { const m = (lo + hi) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; span = minSpan; }
    range.start = lo - span * 0.08; range.end = hi + span * 0.08;
  }
  // Position is a level at every instant: flat 0 before the first fill,
  // holding its last value up to now.
  function extend(t, v, end) {
    t = (t || []).slice(); v = (v || []).slice();
    if (!t.length) return { t: t, v: v };
    if (t[0] > START) { t.unshift(START); v.unshift(0); }
    if (t[t.length - 1] < end) { t.push(end); v.push(v[v.length - 1]); }
    return { t: t, v: v };
  }
  // Follow the live edge until the user pans/zooms, then leave the view alone.
  let lastSet = null;
  function follow(doc) {
    const xr = doc.get_model_by_name('theo').x_range;
    if (lastSet && (xr.start !== lastSet[0] || xr.end !== lastSet[1])) return;
    xr.start = START; xr.end = Date.now(); lastSet = [xr.start, xr.end];
  }
  async function poll() {
    let j;
    try { const r = await fetch(URL + '?t=' + Date.now(), { cache: 'no-store' }); if (!r.ok) return; j = await r.json(); }
    catch (e) { return; }
    const doc = Bokeh.documents[0]; if (!doc) return;
    follow(doc);
    const m = j.market; if (!m) return;
    // JSON has no NaN, so a gap (no quote, withheld fair) arrives as null, and
    // BokehJS draws null as 0 -- every pulled LIP quote dove to the axis.
    // NaN is what Bokeh breaks a line on.
    const gaps = a => (a || []).map(v => v === null ? NaN : v);
    const put = (n, d) => {
      const s = doc.get_model_by_name(n); if (!s) return;
      const out = {}; for (const k in d) out[k] = k === 't' ? (d[k] || []) : gaps(d[k]);
      s.data = out; s.change.emit();
    };
    put('quote', { t: m.q_t, bid: m.bid, ask: m.ask, prob: m.prob, qb: m.qb, qa: m.qa, lb: m.lb || [], la: m.la || [] });
    put('fee',   { t: m.q_t, lo: m.br, hi: m.ar });
    put('ci',    { t: m.q_t, lo: m.ci_bid, hi: m.ci_ask });
    put('lean',  { t: m.q_t, lo: m.l_bid, hi: m.l_ask });
    put('const', { t: m.q_t, lo: m.c_bid, hi: m.c_ask });
    put('band',  { t: m.q_t, lo: m.lo, hi: m.hi });
    put('fy_m', { t: m.fy_mt, p: m.fy_mp }); put('fy_t', { t: m.fy_tt, p: m.fy_tp });
    put('fn_m', { t: m.fn_mt, p: m.fn_mp }); put('fn_t', { t: m.fn_tt, p: m.fn_tp });
    const pos = extend(m.pos_t, m.pos_v, Date.now());
    put('pos', pos); put('pnl', { t: m.pnl_t, v: m.pnl_v });
    const pp = doc.get_model_by_name('pospnl');
    if (pp) { fit(pp.y_range, pos.v, 2); fit(pp.extra_y_ranges['pnl'], m.pnl_v || [], 1); }
  }
  window.a5Reset = function () {
    const doc = Bokeh.documents[0]; if (!doc) return;
    lastSet = null; follow(doc); poll();
  };
  poll(); setInterval(poll, POLL);
})();
</script>
""" % (POSPNL_WIDTH, json.dumps(json_url), POLL_MS, x_start_ms)
    return html.replace("</body>", poller + "</body>")
