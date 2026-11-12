import sys, numpy as np, pandas as pd
sys.path.insert(0,'.')
from pricer_ref import HAR, COINS
from rv2 import rv_features
from replicate import load_comp
day=sys.argv[1]; har=HAR(sys.argv[2]); ts,Z=load_comp(day)
for kind in ['mid','cf']:
    out={}
    for c in COINS:
        p=np.log(pd.Series(Z[c][kind]).ffill().bfill().values); res={}
        for name,(k,clip) in {'1s raw':(1,None),'10s clip6':(10,6)}.items():
            F=rv_features(p,k,clip); t=np.arange(3600,86400-900,30)
            lf=np.log(np.column_stack([F[W][t] for W in (60,900,3600)]))
            f=np.array([har.rate(v,300) for v in lf])*300; r2=(p[t+300]-p[t])**2
            res[name]=r2.mean()/f.mean()
        out[c]=res
    print(day,kind,'mean(r2)/mean(f) at h=300 (1.0 = calibrated)'); print(pd.DataFrame(out).round(2).to_string())
