"""
Plain-text digest of live trading over the last N hours, the input to the
scheduled Claude reviews (deploy/claude_report.sh): service state, each
series' live state, PnL and markouts (from the perf worker's output and the
ARBS ledger replay), box resources, and the log errors since the last run.

    python -m kalshi_infra.runtime.trading_digest --hours 24 --log-state /var/lib/alpha5-report/daily.logpos
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import re
import shutil
import subprocess

import pandas as pd

from kalshi_infra.runtime.persist import LIVE_ROOT
from kalshi_viz import arbs_pnl, perf

UNITS = ("alpha5-crypto", "alpha5-spx", "alpha5-arbs", "alpha5-ibgateway", "alpha5-perf", "alpha5-dashboard")
LOGS = ("run.out.log", "run.err.log", "spx.out.log", "spx.err.log", "arbs.out.log", "arbs.err.log")
_BAD = re.compile(r"ERROR|WARN|HALT|CIRCUIT|Traceback|STOP")
# Logs carry no timestamps; on a first run (no saved offset) only this much
# of each log's tail is read.
FIRST_RUN_TAIL = 2_000_000


def _f(x, fmt="+.2f") -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


def services() -> list[str]:
    out = ["## Services"]
    for u in UNITS:
        r = subprocess.run(["systemctl", "show", "-p", "ActiveState,SubState,NRestarts,ActiveEnterTimestamp", u],
                           capture_output=True, text=True)
        kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        out.append(f"- {u}: {kv.get('ActiveState')}/{kv.get('SubState')} restarts={kv.get('NRestarts')} "
                   f"since {kv.get('ActiveEnterTimestamp')}")
    return out


def live_state(since: pd.Timestamp, now: pd.Timestamp) -> list[str]:
    """One line per series whose summary was written inside the window."""
    out = ["## Live state (latest summary.json per series)"]
    days = {since.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")}
    latest: dict[str, str] = {}
    for p in sorted(glob.glob(os.path.join(LIVE_ROOT, "*", "*", "summary.json"))):
        series, day = p.split(os.sep)[-3:-1]
        if day in days:
            latest[series] = p
    for series, p in sorted(latest.items()):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            out.append(f"- {series}: unreadable summary ({e!r})")
            continue
        upd = pd.Timestamp(d["last_update"]) if d.get("last_update") else None
        if upd is None or upd < since:
            continue
        real = d.get("real") or {}
        errs = d.get("consecutive_order_errors", d.get("order_errors"))
        out.append(
            f"- {series} {d.get('event_ticker')}: summary age {(now - upd).total_seconds():.0f}s, "
            f"live={d.get('live_trading')} arm_requested={d.get('arm_requested')} halted={d.get('halted')} "
            f"{d.get('halt_reason') or ''} real_lock={d.get('real_lock')} blocked={d.get('blocked_reason')} "
            f"order_errors={errs} n_errors={real.get('n_errors')} failed_cancels={real.get('n_failed_cancels')} "
            f"reconcile_mismatches={real.get('reconcile_mismatches')} audit_diffs={real.get('last_audit_diffs')} "
            f"audit_diff={d.get('audit_diff')} pnl={json.dumps(d.get('pnl'))[:200]}")
    return out


def perf_section(since: pd.Timestamp, now: pd.Timestamp) -> list[str]:
    out = ["## Performance (perf worker; PnL = settled, else open at mid; markouts c/contract incl. fee)"]
    for d in sorted(glob.glob(os.path.join(LIVE_ROOT, "*", "perf"))):
        series = d.split(os.sep)[-2]
        r = perf.read_result(series)
        if r is None or r.fills.empty:
            continue
        df = r.fills
        s = perf.summarize(df, since)
        if not s.get("fills"):
            continue
        sp = perf.summarize_split(df, r.lip, since)
        age = "-" if r.updated is None else f"{(now - r.updated).total_seconds() / 60:.0f} min"
        out.append(f"\n### {series} (perf data age {age}{', ERROR ' + r.error if r.error else ''})")
        out.append(f"PnL ${s['pnl']:+.2f} (settled ${s['settled_usd']:+.2f}, open ${s['open_usd']:+.2f} on "
                   f"{s['unsettled']} fills), LIP reward est ${_f(sp['lip'].get('reward'))}, "
                   f"PnL incl LIP ${_f(sp['all'].get('pnl_incl_lip'))}; {s['events']} events, "
                   f"{_f(s['won'], '.0%')} won, t={_f(s['t_event'])}; {s['fills']} fills ({s['taker']} taker), "
                   f"{s['contracts']:.0f} contracts, fees ${s['fees']:.2f}, max drawdown ${s['max_dd']:.2f}")
        out.append(pd.DataFrame(s["markouts"]).T.round(2).to_string())
        w = perf.window(df, since)
        usd = w.pnl_c * w["size"] / 100
        ev = w.assign(usd=usd).groupby("event").usd.sum().sort_values()
        out.append("worst events: " + ", ".join(f"{e} {v:+.2f}" for e, v in ev.head(5).items()))
        out.append("best events: " + ", ".join(f"{e} {v:+.2f}" for e, v in ev.tail(3)[::-1].items()))
        mk = w.assign(usd=usd, mkt=w.ticker.str.rsplit("-", n=1).str[-1]).groupby("mkt").agg(
            fills=("usd", "size"), pnl=("usd", "sum"), mo10=("mo10", "mean"), mo60=("mo60", "mean"))
        if len(mk) <= 30:
            out.append("by market suffix:\n" + mk.round(2).sort_values("pnl").to_string())
        days = df[df.time >= now - pd.Timedelta(days=7)]
        if len(days):
            daily = days.assign(usd=days.pnl_c * days["size"] / 100, day=days.time.dt.strftime("%m-%d")) \
                .groupby("day").agg(fills=("usd", "size"), pnl=("usd", "sum"), mo10=("mo10", "mean"))
            out.append("last 7 days (UTC):\n" + daily.round(2).to_string())
    return out


def arbs_section(since: pd.Timestamp, now: pd.Timestamp) -> list[str]:
    out = ["## ARBS (ledger replay: total = settled + locked complete bundles)"]
    try:
        curve = arbs_pnl.load("ARBS")
    except Exception as e:
        return out + [f"replay failed: {e!r}"]
    if curve.empty:
        return out + ["no live fills"]
    before = curve[curve.time < since]
    t0 = before.iloc[-1] if len(before) else None
    last = curve.iloc[-1]
    out.append(f"window PnL total ${last.total - (t0.total if t0 is not None else 0):+.2f}, "
               f"settled ${last.settled - (t0.settled if t0 is not None else 0):+.2f}; "
               f"capital held now ${last.capital:.2f}; all-time total ${last.total:+.2f}")
    days = max(2, math.ceil((now - since).total_seconds() / 86400) + 1)
    log = arbs_pnl.bundle_log("ARBS", days=days)
    if log.empty:
        return out
    log = log[log.time >= since]
    done = log[log.pnl.notna()]
    out.append(f"{len(log)} bursts ({len(log) - len(done)} left a basket incomplete), "
               f"complete-burst PnL ${done.pnl.sum():+.2f} on ${done.cost.sum():.2f} cost, {int((done.pnl < 0).sum())} losers")
    if len(done):
        by = done.groupby("series").agg(bursts=("pnl", "size"), pnl=("pnl", "sum"), cost=("cost", "sum"))
        out.append("by series (complete bursts), worst first:\n" + by.sort_values("pnl").head(15).round(2).to_string())
        cols = ["time", "series", "how", "cost", "pnl", "fills"]
        out.append("worst bursts:\n" + done.nsmallest(5, "pnl")[cols].to_string(index=False))
    if len(log) > len(done):
        out.append("incomplete bursts (latest 5):\n"
                   + log[log.pnl.isna()].head(5)[["time", "series", "how", "cost", "fills"]].to_string(index=False))
    return out


def box() -> list[str]:
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", open("/proc/meminfo").read(), re.M)) if os.path.exists("/proc/meminfo") else {}
    du = shutil.disk_usage(LIVE_ROOT)
    load = os.getloadavg() if hasattr(os, "getloadavg") else ("-",)
    avail = int(mem.get("MemAvailable", 0)) // 1024
    return ["## Box", f"- memory available {avail} MB of {int(mem.get('MemTotal', 0)) // 1024} MB; "
                      f"disk {du.used / du.total:.0%} used ({du.free / 2**30:.1f} GB free); load {load}"]


def log_errors(state_path: str | None) -> list[str]:
    """Error-ish lines appended since the last run's offsets, grouped by
    message with digits masked; offsets saved back to `state_path`."""
    out = ["## Log errors since last run (count x message; digits masked)"]
    try:
        with open(state_path) as fh:
            pos = json.load(fh)
    except (TypeError, OSError, ValueError):
        pos = {}
    new_pos = {}
    for name in LOGS:
        path = os.path.join(LIVE_ROOT, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        start = pos.get(name, max(0, size - FIRST_RUN_TAIL))
        if start > size:        # rotated or truncated
            start = 0
        new_pos[name] = size
        with open(path, "rb") as fh:
            fh.seek(start)
            text = fh.read(size - start).decode("utf-8", "replace")
        counts, sample = collections.Counter(), {}
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Traceback"):
                # Group by the exception line that ends the traceback.
                j = i + 1
                while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                    j += 1
                line = "Traceback -> " + (lines[j] if j < len(lines) else "?")
            elif line.startswith("[record]") or not _BAD.search(line):
                continue
            key = re.sub(r"\d+(\.\d+)?", "#", line)[:160]
            counts[key] += 1
            sample.setdefault(key, line[:300])
        if counts:
            out.append(f"\n{name} ({(size - start) / 1e6:.1f} MB new):")
            out += [f"  {n} x {sample[k]}" for k, n in counts.most_common(25)]
    if state_path:
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(new_pos, fh)
        os.replace(tmp, state_path)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--log-state", default=None, help="file holding each log's offset at the last run")
    args = ap.parse_args()
    now = pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(hours=args.hours)
    print(f"# Trading digest, last {args.hours:g}h ({since:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M} UTC)\n")
    for section in (services, lambda: live_state(since, now), lambda: perf_section(since, now),
                    lambda: arbs_section(since, now), box, lambda: log_errors(args.log_state)):
        try:
            lines = section()
        except Exception as e:
            lines = [f"(section failed: {e!r})"]
        print("\n".join(lines) + "\n", flush=True)


if __name__ == "__main__":
    main()
