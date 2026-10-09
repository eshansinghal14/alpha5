"""Full-engine parity: legacy SPOptionsLagQuotingStrategy (from the
alpha5_legacy worktree) vs the new Quoter, same cached day, same engine.

    python kalshi_infra/tests/parity/backtest_spx_parity.py KXINX-26AUG27H1600
"""
import os
import subprocess
import sys
import json

NEW = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OLD = os.path.join(os.path.dirname(NEW), "alpha5_legacy")

OLD_SNIPPET = r"""
import sys, os, json
sys.path[:0] = [os.path.join(r'{old}', p) for p in ('kalshi_latency_arbs/spxw_kxinx' if '{s}'=='KXINX' else 'kalshi_latency_arbs/spxw_kxinxu', 'kalshi_latency_arbs', 'kalshi_backtest', 'kalshi_infra')]
import run_backtest as rb, results
r, strat = rb.run_one('{et}', verbose=False, auto_plot=False)
print('JSON' + json.dumps({{k: (v if isinstance(v, (int, float, str)) or v is None else str(v)) for k, v in results.summary(r).items()}}))
print('ORDERS' + json.dumps([(o.ticker, o.side, round(o.price, 4), o.intent, str(o.decision_time)) for o in r.orders]))
"""

NEW_SNIPPET = r"""
import sys, json
sys.path.insert(0, r'{new}')
from kalshi_backtest import results
from kalshi_strategies.KXINX_U.backtest.run_backtest import run_one
r, q, _ = run_one('{et}', verbose=False)
print('JSON' + json.dumps({{k: (v if isinstance(v, (int, float, str)) or v is None else str(v)) for k, v in results.summary(r).items()}}))
print('ORDERS' + json.dumps([(o.ticker, o.side, round(o.price, 4), o.intent, str(o.decision_time)) for o in r.orders]))
"""


def _run(code, cwd):
    out = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(out.stderr[-3000:])
    lines = out.stdout.splitlines()
    summ = json.loads(next(l for l in lines if l.startswith("JSON"))[4:])
    orders = json.loads(next(l for l in lines if l.startswith("ORDERS"))[6:])
    return summ, orders


def main(et):
    s = et.split("-")[0]
    old = _run(OLD_SNIPPET.format(old=OLD, s=s, et=et), OLD)
    new = _run(NEW_SNIPPET.format(new=NEW, et=et), NEW)
    print(f"{et}: orders old={len(old[1])} new={len(new[1])} identical={old[1] == new[1]}")
    for k in sorted(set(old[0]) | set(new[0])):
        a, b = old[0].get(k), new[0].get(k)
        flag = "" if a == b else "   <-- differs"
        print(f"  {k:28s} {str(a):>22s} {str(b):>22s}{flag}")
    if old[1] != new[1]:
        i = next(i for i, (a, b) in enumerate(zip(old[1], new[1])) if a != b)
        print("  first divergence", i, old[1][i], new[1][i])


if __name__ == "__main__":
    for et in sys.argv[1:]:
        main(et)
