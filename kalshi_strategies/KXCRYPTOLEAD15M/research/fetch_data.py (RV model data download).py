"""Download the data used in rv_model.ipynb.

1) Coinbase top-of-book quotes from Tardis.dev's free first-day-of-month files, resampled to a
   1-second mid (last quote in each second, forward-filled) -> data/s1/{COIN}_{YYYYMMDD}.parquet
2) Coinbase 1-minute candles (REST) for the time-of-day profile -> data/m1.pkl
"""
import os, json, time, subprocess, urllib.request, concurrent.futures as cf
import numpy as np, pandas as pd

COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'HYPE']
os.makedirs('data/s1', exist_ok=True)

def fetch_s1():
    days = [f'{y}/{m:02d}/01' for y in (2025, 2026) for m in range(1, 13) if (y, m) <= (2026, 10)]
    for d in days:
        for c in COINS:
            if c == 'HYPE' and d < '2026/03':   # HYPE-USD listed on Coinbase Feb 2026
                continue
            out = f'data/s1/{c}_{d.replace("/", "")}.parquet'
            if os.path.exists(out):
                continue
            f = f'data/{c}.csv.gz'
            subprocess.run(['curl', '-s', '-o', f, f'https://datasets.tardis.dev/v1/coinbase/quotes/{d}/{c}-USD.csv.gz'], check=True)
            parts = []
            for ch in pd.read_csv(f, usecols=['timestamp', 'ask_price', 'bid_price'], chunksize=2_000_000):
                ch = ch[(ch.bid_price > 0) & (ch.ask_price > ch.bid_price)]
                ch['sec'] = ch.timestamp // 1_000_000
                ch['mid'] = (ch.ask_price + ch.bid_price) / 2
                ch['spr'] = (ch.ask_price - ch.bid_price) / ch['mid']
                parts.append(ch.groupby('sec')[['mid', 'spr']].last())
            s = pd.concat(parts); s = s[~s.index.duplicated(keep='last')].sort_index()
            t0 = int(pd.Timestamp(d.replace('/', '-'), tz='UTC').timestamp())
            s.reindex(np.arange(t0, t0 + 86400)).ffill().to_parquet(out)
            os.remove(f); print(d, c, flush=True)

def fetch_m1(start='2025-01-01', end='2026-10-06'):
    a, b = int(pd.Timestamp(start, tz='UTC').timestamp()), int(pd.Timestamp(end, tz='UTC').timestamp())
    def get(p, s):
        u = f'https://api.exchange.coinbase.com/products/{p}/candles?granularity=60&start={s}&end={s + 299 * 60}'
        for i in range(6):
            try: return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'x'}), timeout=20))
            except Exception: time.sleep(1 + 2 * i)
        return []
    out = {}
    for c in COINS:
        rows = []
        with cf.ThreadPoolExecutor(4) as ex:
            for r in ex.map(lambda s: get(c + '-USD', s), range(a, b, 300 * 60)): rows += r
        df = pd.DataFrame(rows, columns=['t', 'l', 'h', 'o', 'c', 'v']).drop_duplicates('t').set_index('t').sort_index()
        out[c] = df['c']; print(c, len(df), flush=True)
    pd.DataFrame(out).to_pickle('data/m1.pkl')

if __name__ == '__main__':
    fetch_s1(); fetch_m1()
