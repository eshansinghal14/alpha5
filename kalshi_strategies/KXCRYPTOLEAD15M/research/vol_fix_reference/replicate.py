import numpy as np, json, pandas as pd, sys
COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'HYPE']
def load_comp(day):
    Z = {c: np.load(f'/home/user/workspace/val/comp_{c}_{day}.npz') for c in COINS}
    return Z[COINS[0]]['ts'], Z
def results(day):
    S = json.load(open('/tmp/settled.json')) if day >= '20260915' else None
    D = json.load(open(f'/home/user/workspace/val/kalshi/{day[:4]}-{day[4:6]}-{day[6:]}.json'))
    ev = {}
    for tk, v in D.items():
        m = v['market']; e = ev.setdefault(m['event_ticker'], {'close': m['close_time'], 'res': {}})
        e['res'][m['yes_sub_title']] = m['result']
    return ev
if __name__ == '__main__':
    day = sys.argv[1]; ts, Z = load_comp(day); ev = results(day); t0 = ts[0]
    for kind in ['mid', 'micro', 'cf']:
        for off in [0, 1]:
            ok = n = 0; margins = []
            for e in ev.values():
                c = int(pd.Timestamp(e['close']).timestamp()); s = c - 900
                y = np.array([e['res'].get(x) == 'yes' for x in COINS])
                if y.sum() != 1: continue
                i0, i1 = s - t0 - off, c - t0 - off
                if i0 < 60 or i1 > len(ts): continue
                r = np.array([np.mean(Z[x][kind][i1 - 60:i1]) / np.mean(Z[x][kind][i0 - 60:i0]) - 1 for x in COINS])
                ok += y[np.argmax(r)]; n += 1
                srt = np.sort(r); margins.append((srt[-1] - srt[-2]) * 1e4 if y[np.argmax(r)] else -(srt[-1] - srt[-2]) * 1e4)
            print(day, kind, 'offset', off, f'winner match {ok}/{n} = {ok / n:.3f}', 'median miss margin bp', np.round(np.median([-m for m in margins if m < 0]), 2) if any(m < 0 for m in margins) else '-')
