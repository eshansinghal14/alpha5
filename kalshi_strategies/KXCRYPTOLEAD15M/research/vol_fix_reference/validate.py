import sys, json, numpy as np, pandas as pd
sys.path.insert(0, '.')
from pricer_ref import *
from replicate import load_comp, results
day = sys.argv[1]; har = HAR(sys.argv[2]); kind = 'mid'
ts, Z = load_comp(day); t0 = ts[0]
P = np.log(np.column_stack([pd.Series(Z[c][kind]).ffill().bfill().values for c in COINS]))
F = [rv_features(P[:, i], 10, 6) for i in range(5)]
logF = np.stack([np.log(np.column_stack([F[i][W] for W in (60, 900, 3600)])) for i in range(5)], 1)   # (T,5,3)
cidx, Rs = corr_series(P)
D = json.load(open(f'/home/user/workspace/val/kalshi/{day[:4]}-{day[4:6]}-{day[6:]}.json'))
mkt = {}
for tk, v in D.items():
    m = v['market']
    for cd in v['candles']:
        b, a = cd['yes_bid'].get('close_dollars'), cd['yes_ask'].get('close_dollars')
        if b is not None and a is not None: mkt[(m['event_ticker'], m['yes_sub_title'], cd['end_period_ts'])] = (float(b), float(a))
ev = results(day); pr = {nm: Pricer(har, nu=nu) for nm, nu in [('normal', None), ('t5', 5)]}
rows = []
for e_tk, e in ev.items():
    c = int(pd.Timestamp(e['close']).timestamp()); s = c - 900
    y = np.array([e['res'].get(x) == 'yes' for x in COINS], float)
    if y.sum() != 1 or s - t0 < 4 * 3600 or c - t0 > len(ts): continue
    i0 = s - t0; A0 = P[i0 - 60:i0].mean(0) if False else np.log(np.exp(P[i0 - 60:i0]).mean(0))
    for tau in list(range(840, 0, -60)) + [30, 10]:
        t = c - tau; it = t - t0
        x = P[it - 1] - A0                                       # last completed second
        k = 60 - tau if tau < 60 else 0
        I = (np.log(np.exp(P[c - t0 - 60:it]).mean(0)) - A0) * (k / 60.0) if tau < 60 and k > 0 else np.zeros(5)
        if tau < 60: I = (P[c - t0 - 60:it] - A0).sum(0) / 60.0
        R = Rs[cidx[cidx <= it][-1]]
        rec = {'ev': e_tk, 'tau': tau, 'y': y}
        for nm, p in pr.items():
            for vm in (0.5, 0.75, 1.0, 1.5):
                rec[f'{nm}_{vm}'] = p.fairs(x, I, tau, logF[it - 1], R, vm)[0]
        Rl = R.copy(); Rl[4, :4] = Rl[:4, 4] = 0.15
        user_vm = np.array([1.5, 2.1, 1.2, 1.0, 5.3])            # sd inflation seen at the logged tick
        rec['V_lowcorr'] = pr['normal'].fairs(x, I, tau, logF[it - 1], Rl, 1.0)[0]
        rec['V_uservol'] = pr['normal'].fairs(x, I, tau, logF[it - 1], R, user_vm ** 2)[0]
        rec['V_user_both'] = pr['normal'].fairs(x, I, tau, logF[it - 1], Rl, user_vm ** 2)[0]
        rec['V_hype4'] = pr['normal'].fairs(x, I, tau, logF[it - 1], R, np.array([1, 1, 1, 1, 16.]))[0]
        rec['V_nocorr'] = pr['normal'].fairs(x, I, tau, logF[it - 1], np.eye(5), 1.0)[0]
        q = [mkt.get((e_tk, cn, t)) for cn in COINS]
        if all(v is not None for v in q):
            mids = np.array([(b + a) / 2 for b, a in q]); rec['mkt'] = mids / mids.sum(); rec['mkt_spread'] = np.mean([a - b for b, a in q])
        rows.append(rec)
pd.to_pickle(rows, f'/home/user/workspace/val/rows_{day}.pkl'); print(len(rows), 'rows', len({r["ev"] for r in rows}), 'events')
