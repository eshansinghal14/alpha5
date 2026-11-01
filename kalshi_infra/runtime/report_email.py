"""
HTML email for the scheduled Claude reviews (deploy/claude_report.sh): a
metrics table per strategy, the dashboard's cumulative PnL / markout curves
as inline charts, then Claude's write-up (Markdown) below. Sent through SES
as a raw MIME message so the charts are embedded, not attached.

    python -m kalshi_infra.runtime.report_email --hours 24 --subject "..." --body reply.md \
        --sender a@b.com --to a@b.com [--dry-run out.eml]
"""
from __future__ import annotations

import argparse
import html
import io
import math
import os
import subprocess
import tempfile
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown
import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from kalshi_infra.runtime.persist import LIVE_ROOT  # noqa: E402
from kalshi_viz import arbs_pnl, perf  # noqa: E402

ET = "America/New_York"
GREEN, RED, MUTED = "#1a7f37", "#cf222e", "#57606a"
LINE_COLORS = {"PnL (settled; open at mid)": "#0969da", "PnL incl. LIP": "#0969da", "1s markout": "#bf8700",
               "10s markout": "#8250df", "60s markout": "#1a7f37", "LIP reward": "#57606a",
               "total (settled + locked)": "#0969da", "settled": "#57606a"}
TD = "padding:4px 10px;border-bottom:1px solid #d0d7de;text-align:right;white-space:nowrap"
TH = TD.replace("text-align:right", "text-align:right;background:#f6f8fa;font-weight:600")


def _money(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f'<span style="color:{MUTED}">-</span>'
    return f'<span style="color:{GREEN if x >= 0 else RED}">{"+" if x >= 0 else "-"}${abs(x):,.2f}</span>'


def _cents(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f'<span style="color:{MUTED}">-</span>'
    return f'<span style="color:{GREEN if x >= 0 else RED}">{x:+.2f}c</span>'


def _table(header: list[str], rows: list[list[str]]) -> str:
    th = "".join(f'<th style="{TH}{";text-align:left" if i == 0 else ""}">{h}</th>' for i, h in enumerate(header))
    body = "".join("<tr>" + "".join(f'<td style="{TD}{";text-align:left" if i == 0 else ""}">{c}</td>'
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<table style="border-collapse:collapse;font-size:13px;margin:6px 0 14px">' \
           f"<tr>{th}</tr>{body}</table>"


def _chart(curves: pd.DataFrame, title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(7.6, 2.8), dpi=110)
    for name, g in curves.groupby("line", sort=False):
        t = pd.to_datetime(g.time, utc=True).dt.tz_convert(ET)
        ax.step(t, g.usd, where="post", label=name, lw=1.6 if "PnL" in name or "total" in name else 1.1,
                color=LINE_COLORS.get(name))
    ax.axhline(0, color="#8c959f", lw=0.6)
    ax.set_title(title, fontsize=10, loc="left")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ET))
    ax.tick_params(labelsize=8)
    ax.grid(alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(fontsize=7, loc="upper left", frameon=False, ncol=3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def strategy_sections(hours: float) -> tuple[str, list[tuple[str, bytes]]]:
    """(HTML of the metrics tables, [(content id, png)]) for the window."""
    now = pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(hours=hours)
    parts, images = [], []
    summary_rows, mo_rows = [], []
    for d in sorted(os.listdir(LIVE_ROOT)):
        if not os.path.isdir(os.path.join(LIVE_ROOT, d, "perf")):
            continue
        r = perf.read_result(d)
        if r is None or r.fills.empty:
            continue
        s = perf.summarize(r.fills, since)
        if not s.get("fills"):
            continue
        sp = perf.summarize_split(r.fills, r.lip, since)
        stale = r.updated is not None and (now - r.updated) > pd.Timedelta("30min")
        name = d + (f' <span style="color:{RED}">(perf data {(now - r.updated).total_seconds() / 3600:.1f}h old)</span>'
                    if stale else "")
        summary_rows.append([name, _money(s["pnl"]), _money(s["settled_usd"]), _money(s["open_usd"]),
                             _money(sp["lip"].get("reward")), f"{s['fills']} ({s['taker']} taker)",
                             f"{s['events']}", "-" if math.isnan(s["won"]) else f"{s['won']:.0%}",
                             _money(s["max_dd"])])
        for lab, m in s["markouts"].items():
            mo_rows.append([f"{d} {lab}", f"{m['fills']:.0f}", *(_cents(m[c]) for c in ("mo0", "mo1", "mo10", "mo60",
                                                                                       "pnl_c"))])
        # No reward in the window: the LIP lines would just retrace PnL.
        lip = r.lip if len(perf.window(r.lip, since)) and perf.window(r.lip, since).usd.sum() > 0 else None
        cv = perf.curves(r.fills, since, lip)
        if not cv.empty:
            images.append((f"chart-{d}", _chart(cv, f"{d}: cumulative $ since {since.tz_convert(ET):%m-%d %H:%M} ET")))
    arbs = _arbs(since)
    if arbs is not None:
        row, cv = arbs
        summary_rows.append(row)
        if cv is not None:
            images.append(("chart-ARBS", _chart(cv, "ARBS: cumulative $ (settled + locked complete bundles)")))
    if summary_rows:
        parts.append(_h("PnL"))
        parts.append(_table(["strategy", "PnL", "settled", "open", "LIP est", "fills", "events", "won", "max DD"],
                            summary_rows))
    if mo_rows:
        parts.append(_h("Markouts (c/contract, fee incl.)"))
        parts.append(_table(["strategy / fills", "n", "mo0 (spread)", "1s", "10s", "60s", "PnL/contract"], mo_rows))
    return "".join(parts), images


def _arbs(since: pd.Timestamp):
    try:
        curve = arbs_pnl.load("ARBS")
    except Exception:
        return None
    if curve.empty:
        return None
    before = curve[curve.time < since]
    base = before.iloc[-1] if len(before) else pd.Series({"total": 0.0, "settled": 0.0})
    w = curve[curve.time >= since]
    days = max(2, math.ceil((pd.Timestamp.now(tz="UTC") - since).total_seconds() / 86400) + 1)
    log = arbs_pnl.bundle_log("ARBS", days=days)
    log = log[log.time >= since] if len(log) else log
    done = log[log.pnl.notna()] if len(log) else log
    last = curve.iloc[-1]
    row = ["ARBS", _money(last.total - base.total), _money(last.settled - base.settled),
           _money((last.total - last.settled) - (base.total - base.settled)), _money(None),
           f"{len(log)} bursts ({len(log) - len(done)} incomplete)", "-",
           "-" if not len(done) else f"{(done.pnl > 0).mean():.0%}", _money(None)]
    if w.empty:
        return row, None
    cv = pd.concat([pd.DataFrame({"time": [since] + list(w.time), "line": name,
                                  "usd": [0.0] + list(w[col] - base[col])})
                    for name, col in (("total (settled + locked)", "total"), ("settled", "settled"))])
    return row, cv


def _h(text: str) -> str:
    return f'<h3 style="margin:18px 0 4px;font-size:15px">{html.escape(text)}</h3>'


def _style_markdown(body_md: str) -> str:
    out = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    # Gmail drops <style> blocks in some clients; inline the few that matter.
    return (out.replace("<table>", '<table style="border-collapse:collapse;font-size:13px">')
               .replace("<th>", f'<th style="{TH}">').replace("<td>", f'<td style="{TD}">')
               .replace("<h2>", '<h3 style="margin:18px 0 4px;font-size:15px">').replace("</h2>", "</h3>")
               .replace("<ul>", '<ul style="margin:4px 0;padding-left:20px">'))


def build(subject: str, body_md: str, hours: float, sender: str, to: str, charts: bool = True) -> MIMEMultipart:
    tables, images = strategy_sections(hours) if charts else ("", [])
    now = pd.Timestamp.now(tz=ET)
    imgs = "".join(f'<img src="cid:{cid}" style="display:block;max-width:100%;margin:6px 0" alt="{cid}">'
                   for cid, _ in images)
    page = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2328;'
            f'max-width:820px;font-size:14px;line-height:1.45">'
            f'<div style="color:{MUTED};font-size:12px">alpha5 · last {hours:g}h to {now:%a %m-%d %H:%M} ET</div>'
            f"{tables}{_h('Charts') if imgs else ''}{imgs}{_h('Analysis')}{_style_markdown(body_md)}</div>")
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_md, "plain", "utf-8"))
    alt.attach(MIMEText(page, "html", "utf-8"))
    msg.attach(alt)
    for cid, png in images:
        part = MIMEImage(png, "png")
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(part)
    return msg


def send(msg: MIMEMultipart, region: str = "us-east-2") -> None:
    with tempfile.NamedTemporaryFile("wb", suffix=".eml", delete=False) as fh:
        fh.write(msg.as_bytes())
    try:
        subprocess.run(["aws", "ses", "send-raw-email", "--region", region, "--raw-message",
                        f"Data=fileb://{fh.name}", "--output", "text"], check=True, capture_output=True, timeout=60)
    finally:
        os.unlink(fh.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True, help="Markdown file (Claude's reply body)")
    ap.add_argument("--hours", type=float, default=24, help="window of the tables and charts")
    ap.add_argument("--sender", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--dry-run", default=None, help="write the .eml here instead of sending")
    args = ap.parse_args()
    with open(args.body, encoding="utf-8") as fh:
        body = fh.read()
    msg = build(args.subject, body, args.hours, args.sender, args.to, charts=not args.no_charts)
    if args.dry_run:
        with open(args.dry_run, "wb") as fh:
            fh.write(msg.as_bytes())
    else:
        send(msg)


if __name__ == "__main__":
    main()
