import json

import numpy as np
import pytest

from strategies.ARBS import exit_cost as ec


def _f(edge_c, top=20.0):
    return {"top_r_min": top, "aged_frac": 1.0, "depth2_r": 30.0, "reach_c": 0.0, "edge_c": edge_c, "spread_c": 1.0,
            "n": 5.0, "unwind_top_r": 5.0, "n_other": 1, "stable": 1.0, "live": 0.0, "tte_h": 30.0}


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


def test_in_play_and_time_to_settlement_are_inputs():
    s = ec.Stats()
    for _ in range(5000):
        s.add({**_f(1.0), "live": 0.0, "tte_h": 48.0}, 0.0)
        s.add({**_f(1.0), "live": 1.0, "tte_h": 2.0}, 4.0)      # same book, in play near settlement: costs more
    w = s.fit()
    assert ec.predict(w, {**_f(1.0), "live": 1.0, "tte_h": 2.0}) - ec.predict(w, {**_f(1.0), "live": 0.0, "tte_h": 48.0}) \
        == pytest.approx(4.0, abs=0.5)


def test_model_value_matches_predict_at_price_bit_for_bit():
    import math
    import random
    import numpy as np
    from strategies.ARBS import exit_cost as X

    rng = random.Random(5)
    st = X.Stats()
    models = [X.Model(np.array([rng.uniform(-5, 5) for _ in range(X.WIDTH)]), st, []) for _ in range(2)]

    def draw(edges):
        lo = edges[1] - 1 if math.isinf(edges[0]) else edges[0]
        hi = edges[-2] + 5 if math.isinf(edges[-1]) else edges[-1]
        return rng.uniform(lo - 0.5, hi + 0.5)

    for _ in range(300):
        f = {name: draw(edges) for name, edges in X.BINS.items()}
        f.update(payout=1.0, c0=rng.uniform(0.3, 0.9), touch=rng.uniform(0.05, 0.6), plan=rng.uniform(0.3, 0.95))
        for _ in range(6):
            m = models[rng.randrange(2)]                       # the weights change under a kept f
            x, live, size, mr = round(rng.uniform(0.01, 0.6), 2), rng.random() < 0.5, rng.choice([5.0, 50.0]), 0.0175
            g = X.at_price(f, x)
            net = 100 * (f["payout"] - x - f["plan"] - mr * x * (1 - x)) - X.predict(m.w, g)
            want = (st.fill_prob(live, g["reach_c"]) * st.expected_fill(live, size) * net, net)
            assert m.value(f, x, live, size, mr) == want


def test_live_bias_comes_off_live_nets_only_and_survives_the_model_file():
    import numpy as np
    from strategies.ARBS import exit_cost as X
    st = X.Stats()
    m = X.Model(np.zeros(X.WIDTH), st, ["ARBS:2026-11-18"], live_bias_c=1.5)
    f = {name: 1.0 for name in X.BINS}
    f.update(payout=1.0, c0=0.6, touch=0.3, plan=0.65)
    (_, live_net), (_, pre_net) = m.value(f, 0.30, True, 20.0), m.value(f, 0.30, False, 20.0)
    assert pre_net - live_net == 1.5
    back = X.Model.from_json(m.to_json())
    assert back.live_bias_c == 1.5 and back.value(f, 0.30, True, 20.0)[1] == live_net



def _mix_setup():
    import numpy as np
    from strategies.ARBS import exit_cost as ec
    rng = np.random.default_rng(1)
    w = rng.normal(size=ec.WIDTH)
    f = {"top_r_min": 2.0, "aged_frac": 1.0, "depth2_r": 3.0, "reach_c": 0.0, "edge_c": 0.0, "spread_c": 0.0,
         "n": 5.0, "unwind_top_r": 1.0, "n_other": 1, "stable": 1.0, "live": 1.0, "tte_h": 2.0,
         "touch": 0.40, "c0": 0.55, "plan": 0.56, "payout": 1.0}
    return ec, w, f


def test_expected_loss_is_the_at_price_loss_when_every_sweep_stops_at_our_price():
    ec, w, f = _mix_setup()
    st = ec.Stats()
    for _ in range(10):
        st.add_reach(True, 3)                     # every sweep 3 ticks: a quote 3 under fills only on those
    x = 0.37
    want = ec.predict(w, ec.at_price(f, x))
    assert abs(ec.expected_loss(w, st, f, x, True) - want) < 1e-9


def test_expected_loss_averages_the_sweeps_that_reach_our_price():
    ec, w, f = _mix_setup()
    st = ec.Stats()
    for t in (1, 3, 3, 12):                       # a quote 3 under fills on the 3, 3 and 12 tick sweeps
        st.add_reach(True, t)
    x = 0.37
    at = ec.predict(w, ec.at_price(f, x))
    deep = ec.predict(w, {**ec.at_price(f, x), "reach_c": 12.0, "edge_c": 100 * (1.0 - x - 0.55) + 9.0})
    mixed = (2 * at + deep) / 3
    got = ec.expected_loss(w, st, f, x, True)
    assert abs(got - ((1 - ec.REACH_MIX) * at + ec.REACH_MIX * mixed)) < 1e-9


def test_expected_loss_is_the_at_price_loss_pre_game():
    ec, w, f = _mix_setup()
    st = ec.Stats()
    for t in (1, 3, 3, 12):
        st.add_reach(False, t)
    x = 0.37
    assert abs(ec.expected_loss(w, st, f, x, False) - ec.predict(w, ec.at_price(f, x))) < 1e-9


def test_bin_bisect_matches_the_scan():
    import math
    import random
    from strategies.ARBS import exit_cost
    rng = random.Random(0)
    for edges in exit_cost.BINS.values():
        fin = [e for e in edges if math.isfinite(e)]
        vals = [math.inf, -math.inf, math.nan] + [e + d for e in fin for d in (-1e-9, 0.0, 1e-9, 0.5)]
        for v in vals + [rng.uniform(min(fin) - 5, max(fin) * 2 + 5) for _ in range(300)]:
            scan = next((i for i in range(len(edges) - 1) if v <= edges[i + 1]), len(edges) - 2)
            assert exit_cost._bin(edges, v) == scan, (edges, v)
