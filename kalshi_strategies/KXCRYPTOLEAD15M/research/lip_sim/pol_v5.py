"""Round 5: price-dependent size on the wings. Cheap wing (own <= 0.20) is cheap
collateral; rich wing (>= 0.80) is 4-9x the collateral per lot and carries the
negative 10s markout. All t180 + sweep pull."""
SW = (0, 0.05, 3.0, 2)
C = dict(cutoff=60, level="best", tau_min=180, sweep_pull=SW, N=0)
base = {
    "cheap N400":               dict(C, size_rule=[(0.01, 0.20, 400)]),
    "cheap N800":               dict(C, size_rule=[(0.01, 0.20, 800)]),
    "cheap N400 + rich N100":   dict(C, size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 100)]),
    "cheap N400 + rich N200":   dict(C, size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 200)]),
    "cheap N600 + rich N150":   dict(C, size_rule=[(0.01, 0.20, 600), (0.80, 0.99, 150)]),
    "cheap N300 + rich N300":   dict(C, size_rule=[(0.01, 0.20, 300), (0.80, 0.99, 300)]),
    "rich N200":                dict(C, size_rule=[(0.80, 0.99, 200)]),
    "cheap(.25) N400 + rich(.75) N150": dict(C, size_rule=[(0.01, 0.25, 400), (0.75, 0.99, 150)]),
    "cheap(.15) N400 + rich(.85) N150": dict(C, size_rule=[(0.01, 0.15, 400), (0.85, 0.99, 150)]),
    "cheap N400 + rich N150, ref":       dict(C, level="ref", size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 150)]),
    "cheap N400 + rich N150, t120":      dict(C, tau_min=120, size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 150)]),
    "cheap N400 + rich N150, t300":      dict(C, tau_min=300, size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 150)]),
    "cheap N400 + rich N150, no swp":    dict({k: v for k, v in C.items() if k != "sweep_pull"},
                                              size_rule=[(0.01, 0.20, 400), (0.80, 0.99, 150)]),
}
POLICIES = {}
for k, v in base.items():
    POLICIES[k] = dict(v, cross_w=0.0)
    POLICIES[k + " |full"] = dict(v, cross_w=0.0, through_full=True)
    POLICIES[k + " |x20"] = dict(v, cross_w=0.2)
