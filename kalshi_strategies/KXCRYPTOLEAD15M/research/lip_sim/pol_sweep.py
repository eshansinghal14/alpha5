"""Level x gate x timing sweep, N100. Each policy run twice: prints-only fills (main)
and with crossings at 20% weight (stress)."""
G_SPOT3 = ("spot", 3.0, 3.0, "z1")
G_SPOT4 = ("spot", 4.0, 3.0, "z1")
G_KAL3 = ("kalshi", 0.03, 5.0)
G_FAIR2 = ("fair", 0.02, 3.0)
base = {}
for lvl in ("best", "ref", "ref1"):
    B = dict(N=100, cutoff=60, level=lvl)
    base[f"{lvl}"] = B
    base[f"{lvl} t180"] = dict(B, tau_min=180)
    base[f"{lvl} spot3"] = dict(B, gates=[G_SPOT3])
    base[f"{lvl} spot4"] = dict(B, gates=[G_SPOT4])
    base[f"{lvl} kal3"] = dict(B, gates=[G_KAL3])
    base[f"{lvl} fair2"] = dict(B, gates=[G_FAIR2])
    base[f"{lvl} xgrp"] = dict(B, xgroup=(10, 10.0, 0.1))
    base[f"{lvl} m3"] = dict(B, fair_margin=0.03)
    base[f"{lvl} res"] = dict(B, use_res=True)
    base[f"{lvl} t180+spot4+xgrp"] = dict(B, tau_min=180, gates=[G_SPOT4], xgroup=(10, 10.0, 0.1))
    base[f"{lvl} t180+spot4+xgrp+m3"] = dict(B, tau_min=180, gates=[G_SPOT4], xgroup=(10, 10.0, 0.1),
                                             fair_margin=0.03)
for N in (50, 200):
    base[f"ref N{N}"] = dict(N=N, cutoff=60, level="ref")
    base[f"best N{N}"] = dict(N=N, cutoff=60, level="best")
POLICIES = {}
for k, v in base.items():
    POLICIES[k] = dict(v, cross_w=0.0)
    POLICIES[k + " |x20"] = dict(v, cross_w=0.2)
