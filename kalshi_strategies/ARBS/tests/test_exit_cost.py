import json

import numpy as np
import pytest

from kalshi_strategies.ARBS import exit_cost as ec


def _f(edge_c, top=20.0):
    return {"top_r_min": top, "aged_frac": 1.0, "depth2_r": 30.0, "reach_c": 0.0, "edge_c": edge_c, "spread_c": 1.0,
            "n": 5.0, "unwind_top_r": 5.0, "n_other": 1, "stable": 1.0}


def test_fit_learns_an_additive_cost_and_days_add_up():
    a, b = ec.Stats(), ec.Stats()
    for _ in range(5000):
        a.add(_f(-1.0), 0.0)                      # no apparent edge: exits as planned
        b.add(_f(12.0), 7.0)                      # a 12c apparent arb: 7c lost on the exit
    a.merge(ec.Stats.from_json(json.loads(json.dumps(b.to_json()))))
    w = a.fit()
    assert a.n == 10000
    assert ec.predict(w, _f(-1.0)) == pytest.approx(0.0, abs=0.5)
    assert ec.predict(w, _f(12.0)) == pytest.approx(7.0, abs=0.5)


def test_fill_odds_are_the_share_of_prints_sweeping_that_deep():
    s = ec.Stats()
    for t in [0] * 6 + [1] * 3 + [5]:
        s.add_reach(True, t)
    assert s.fill_prob(True, 0) == 1.0
    assert s.fill_prob(True, 1) == pytest.approx(0.4)
    assert s.fill_prob(True, 2) == pytest.approx(0.1)
    assert s.fill_prob(False, 0) == 0.0               # no non-live prints seen


def test_quote_price_matches_the_replay():
    f = {**_f(0.0), "payout": 1.0, "plan": 0.50 + 0.07 * 0.5 * 0.5, "c0": 0.50, "touch": 0.45}
    assert ec.quote_price(f, 0.05) == pytest.approx(0.43)
    g = ec.at_price(f, 0.43)
    assert g["edge_c"] == pytest.approx(7.0) and g["reach_c"] == pytest.approx(2.0)
