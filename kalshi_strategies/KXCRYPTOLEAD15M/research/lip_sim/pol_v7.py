"""Round 7b: order latency sensitivity on the candidate (place/cancel/move after LAT)."""
C = dict(cutoff=60, level="best", tau_min=180, N=0, size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 100)])
base = {}
for lat in (0.05, 0.10, 0.25, 0.50, 1.0):
    base[f"cand lat{int(lat * 1000)}"] = dict(C, lat=lat, sweep_pull=(0, 0.05, 3.0, 2))
    base[f"cand noswp lat{int(lat * 1000)}"] = dict(C, lat=lat)
POLICIES = {}
for k, v in base.items():
    POLICIES[k] = dict(v, cross_w=0.0)
    POLICIES[k + " |x20"] = dict(v, cross_w=0.2)
