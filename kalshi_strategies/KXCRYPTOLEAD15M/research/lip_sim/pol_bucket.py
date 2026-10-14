"""Unrestricted 'best' (and ref) with only the cutoff, so per-bucket LIP and adverse
cost can be read off one run. Three fill models."""
POLICIES = {}
for lvl in ("best", "ref"):
    B = dict(N=200, cutoff=30, level=lvl, open_delay=0)
    POLICIES[f"{lvl} N200"] = dict(B, cross_w=0.0)
    POLICIES[f"{lvl} N200 |full"] = dict(B, cross_w=0.0, through_full=True)
    POLICIES[f"{lvl} N200 |x20"] = dict(B, cross_w=0.2)
    POLICIES[f"{lvl} N200 swp"] = dict(B, cross_w=0.0, sweep_pull=(0, 0.05, 3.0, 2))
    POLICIES[f"{lvl} N200 swp |full"] = dict(B, cross_w=0.0, through_full=True, sweep_pull=(0, 0.05, 3.0, 2))
    POLICIES[f"{lvl} N200 swp |x20"] = dict(B, cross_w=0.2, sweep_pull=(0, 0.05, 3.0, 2))
