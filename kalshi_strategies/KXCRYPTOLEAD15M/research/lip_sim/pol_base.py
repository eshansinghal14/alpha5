"""Baseline level/size grid, no gates. Cutoff 60s as live."""
POLICIES = {}
for N in (50, 100):
    for lvl in ("best", "ref", "ref1", "ref2", "ahead300", "ahead600"):
        POLICIES[f"{lvl} N{N}"] = dict(N=N, level=lvl, cutoff=60)
POLICIES["ref N100 res"] = dict(N=100, level="ref", cutoff=60, use_res=True)
POLICIES["ref N100 nocross"] = dict(N=100, level="ref", cutoff=60, cross=False)
POLICIES["best N100 nocross"] = dict(N=100, level="best", cutoff=60, cross=False)
