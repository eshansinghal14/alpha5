"""
HTML email for the scheduled Claude reviews (deploy/claude_report.sh): a
headline with the window's PnL, Claude's write-up (Markdown), then the
per-strategy tables and cumulative PnL / markout charts for reference. Covers
ARBS and the coin race only. Sent through SES as a raw MIME message so the
charts are embedded, not attached.

    python -m kalshi_infra.runtime.report_email --hours 24 --subject "..." --body reply.md \
        --sender a@b.com --to a@b.com [--status OK|ALERT] [--dry-run out.eml]
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import math
import os
import re
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

from kalshi_viz import arbs_pnl, perf  # noqa: E402

ET = "America/New_York"
# Perf-worker series shown in the email; ARBS comes from its ledger instead.
SERIES = {"KXCRYPTOLEAD15M": "Coin race"}
GREEN, RED, MUTED, TEXT, RULE = "#1a7f37", "#cf222e", "#656d76", "#1f2328", "#d8dee4"
LINE_COLORS = {"PnL (settled; open at mid)": "#0969da", "PnL incl. LIP": "#0969da", "1s markout": "#bf8700",
               "10s markout": "#8250df", "60s markout": "#1a7f37", "LIP reward": "#57606a",
               "total (settled + locked)": "#0969da", "settled": "#57606a"}
TD = f"padding:6px 12px;border-bottom:1px solid {RULE};text-align:right;white-space:nowrap"
TH = (f"padding:6px 12px;border-bottom:2px solid {RULE};text-align:right;white-space:nowrap;"
      f"color:{MUTED};font-weight:600;font-size:12px")
H2 = f"margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid {RULE};font-size:16px;color:{TEXT}"


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _money(x) -> str:
    if _isnan(x):
        return f'<span style="color:{MUTED}">–</span>'
    return f'<span style="color:{GREEN if x >= 0 else RED}">{"+" if x >= 0 else "−"}${abs(x):,.2f}</span>'


def _cents(x) -> str:
    if _isnan(x):
        return f'<span style="color:{MUTED}">–</span>'
    return f'<span style="color:{GREEN if x >= 0 else RED}">{x:+.1f}¢</span>'


def _table(header: list[str], rows: list[list[str]]) -> str:
    def cells(tag, style, row):
        return "".join(f'<{tag} style="{style}{";text-align:left" if i == 0 else ""}">{c}</{tag}>'
                       for i, c in enumerate(row))
    body = "".join(f"<tr>{cells('td', TD, r)}</tr>" for r in rows)
    return (f'<table style="border-collapse:collapse;font-size:13px;margin:4px 0 12px">'
            f"<tr>{cells('th', TH, header)}</tr>{body}</table>")


def _chart(curves: pd.DataFrame, title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 2.4), dpi=110)
    for name, g in curves.groupby("line", sort=False):
        t = pd.to_datetime(g.time, utc=True).dt.tz_convert(ET)
        ax.step(t, g.usd, where="post", label=name, lw=1.8 if "PnL" in name or "total" in name else 1.0,
                color=LINE_COLORS.get(name))
    ax.axhline(0, color="#8c959f", lw=0.6)
    ax.set_title(title, fontsize=10, loc="left", color=TEXT)
    # Whole-dollar ticks repeat ("$2 $2 $2") when the curves span only a few dollars.
    dec = 0 if curves.usd.max() - curves.usd.min() >= 20 else 2
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.{dec}f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ET))
    ax.tick_params(labelsize=8, colors=MUTED)
    ax.grid(alpha=0.2)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(0, -0.12), frameon=False, ncol=4)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def strategy_sections(hours: float) -> tuple[list[tuple[str, float]], str, list[tuple[str, bytes]]]:
    """([(strategy, window PnL $)], HTML of the reference tables, [(content id, png)])."""
    now = pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(hours=hours)
    totals, images, pnl_rows, mo_rows = [], [], [], []
    for d, name in SERIES.items():
        r = perf.read_result(d)
        if r is None or r.fills.empty:
            continue
        s = perf.summarize(r.fills, since)
        if not s.get("fills"):
            continue
        totals.append((name, s["pnl"]))
        stale = r.updated is not None and (now - r.updated) > pd.Timedelta("30min")
        label = name + (f'<br><span style="color:{RED};font-size:11px">perf data '
                        f'{(now - r.updated).total_seconds() / 3600:.1f}h old</span>' if stale else "")
        won = "–" if math.isnan(s["won"]) else f"{s['won']:.0%} of {s['events']}"
        pnl_rows.append([label, _money(s["pnl"]), _money(s["settled_usd"]), _money(s["open_usd"]),
                         f"{s['fills']:,}", won, _money(s["max_dd"])])
        mos = s["markouts"]
        for lab, m in mos.items():
            # "all" repeats maker or taker when every fill had the same role.
            if lab == "all" and any(k != "all" and v["fills"] == m["fills"] for k, v in mos.items()):
                continue
            mo_rows.append([f"{name} · {lab}", f"{m['fills']:,.0f}",
                            *(_cents(m[c]) for c in ("mo0", "mo1", "mo10", "mo60", "pnl_c"))])
        # No reward in the window: the LIP lines would just retrace PnL.
        lw = perf.window(r.lip, since)
        cv = perf.curves(r.fills, since, r.lip if len(lw) and lw.usd.sum() > 0 else None)
        if not cv.empty:
            images.append((f"chart-{d}", _chart(cv, f"{name}: cumulative $")))
    arbs = _arbs(since)
    if arbs is not None:
        total, row, cv = arbs
        totals.append(("ARBS", total))
        pnl_rows.append(row)
        if cv is not None:
            images.append(("chart-ARBS", _chart(cv, "ARBS: cumulative $ (settled + locked bundles)")))
    parts = []
    if pnl_rows:
        parts.append(_table(["", "PnL", "settled", "open", "fills", "events won", "max DD"], pnl_rows))
    if mo_rows:
        parts.append(f'<div style="color:{MUTED};font-size:12px;margin-top:14px">'
                     "Markouts, ¢ per contract after fees</div>")
        parts.append(_table(["", "fills", "at fill", "1s", "10s", "60s", "held to end"], mo_rows))
    return totals, "".join(parts), images


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
    total = float(last.total - base.total)
    incomplete = len(log) - len(done)
    fills = f"{len(log):,} bursts" + (f'<br><span style="color:{MUTED};font-size:11px">{incomplete} incomplete</span>'
                                      if incomplete else "")
    won = "–" if not len(done) else f"{(done.pnl > 0).mean():.0%} of {len(done)}"
    row = ["ARBS", _money(total), _money(last.settled - base.settled),
           _money((last.total - last.settled) - (base.total - base.settled)), fills, won, _money(None)]
    if w.empty:
        return total, row, None
    cv = pd.concat([pd.DataFrame({"time": [since] + list(w.time), "line": name,
                                  "usd": [0.0] + list(w[col] - base[col])})
                    for name, col in (("total (settled + locked)", "total"), ("settled", "settled"))])
    return total, row, cv


def _h(text: str) -> str:
    return f'<h2 style="{H2}">{html.escape(text)}</h2>'


def _headline(totals: list[tuple[str, float]], hours: float, status: str | None) -> str:
    now = pd.Timestamp.now(tz=ET)
    span = "Daily" if hours >= 20 else f"Last {hours:g}h"
    pill = ""
    if status:
        pill = (f'<span style="background:{RED if status == "ALERT" else GREEN};color:#fff;border-radius:10px;'
                f'padding:2px 9px;font-size:11px;font-weight:600;margin-left:8px">{html.escape(status)}</span>')
    out = (f'<div style="color:{MUTED};font-size:12px">{span} · {now:%a %b} {now.day}, '
           f'{now.hour % 12 or 12}:{now:%M %p} ET{pill}</div>')
    if not totals:
        return out
    total = sum(v for _, v in totals)
    out += (f'<div style="font-size:30px;font-weight:600;margin:6px 0 2px;color:{GREEN if total >= 0 else RED}">'
            f'{"+" if total >= 0 else "−"}${abs(total):,.2f}</div>')
    chips = "".join(f'<td style="padding:0 22px 0 0;font-size:13px;color:{MUTED}">{html.escape(n)} '
                    f'<b>{_money(v)}</b></td>' for n, v in totals)
    return out + f'<table style="border-collapse:collapse;margin:0 0 4px"><tr>{chips}</tr></table>'


def _style_markdown(body_md: str) -> str:
    # Python-Markdown nests a list only at a 4-space indent; Claude writes 2 or 3
    # under "1. ", which flattens the sub-bullets into the item's text.
    body_md = re.sub(r"(?m)^ {1,3}(?=[-*] )", "    ", body_md)
    out = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    # Gmail drops <style> blocks in some clients; inline the few that matter.
    return (out.replace("<table>", '<table style="border-collapse:collapse;font-size:13px;margin:6px 0 10px">')
               .replace("<th>", f'<th style="{TH}">').replace("<td>", f'<td style="{TD}">')
               .replace("<h2>", f'<h2 style="{H2}">')
               .replace("<h3>", '<h3 style="margin:16px 0 4px;font-size:14px">')
               .replace("<p>", '<p style="margin:6px 0">')
               .replace("<ul>", '<ul style="margin:6px 0;padding-left:20px">')
               .replace("<ol>", '<ol style="margin:6px 0;padding-left:20px">')
               .replace("<li>", '<li style="margin:0 0 8px">')
               .replace("<em>", f'<em style="color:{MUTED}">')
               .replace("<code>", '<code style="background:#f6f8fa;padding:1px 4px;border-radius:4px;font-size:12px">'))


def build(subject: str, body_md: str, hours: float, sender: str, to: str, charts: bool = True,
          status: str | None = None) -> MIMEMultipart:
    totals, tables, images = strategy_sections(hours) if charts else ([], "", [])
    imgs = "".join(f'<img src="cid:{cid}" style="display:block;max-width:100%;margin:8px 0" alt="{cid}">'
                   for cid, _ in images)
    page = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:{TEXT};'
            f'max-width:680px;font-size:14px;line-height:1.55">'
            f"{_headline(totals, hours, status)}{_style_markdown(body_md)}"
            f"{_h('Numbers') if tables else ''}{tables}{_h('Charts') if imgs else ''}{imgs}</div>")
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
    # CLI v2 takes the raw message base64-encoded; fileb:// isn't read inside
    # the Data= shorthand ("Invalid base64").
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"Data": base64.b64encode(msg.as_bytes()).decode()}, fh)
    try:
        r = subprocess.run(["aws", "ses", "send-raw-email", "--region", region, "--raw-message",
                            f"file://{fh.name}", "--output", "text"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"ses send-raw-email failed: {r.stderr.strip()}")
    finally:
        os.unlink(fh.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True, help="Markdown file (Claude's reply body)")
    ap.add_argument("--hours", type=float, default=24, help="window of the tables and charts")
    ap.add_argument("--sender", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--status", default=None, help="OK or ALERT, shown next to the date")
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--dry-run", default=None, help="write the .eml here instead of sending")
    args = ap.parse_args()
    with open(args.body, encoding="utf-8") as fh:
        body = fh.read()
    msg = build(args.subject, body, args.hours, args.sender, args.to, charts=not args.no_charts,
                status=args.status)
    if args.dry_run:
        with open(args.dry_run, "wb") as fh:
            fh.write(msg.as_bytes())
    else:
        send(msg)


if __name__ == "__main__":
    main()
