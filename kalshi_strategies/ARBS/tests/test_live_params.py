import pytest

from kalshi_strategies.ARBS.live_params import OFF, Level, LiveParams

EDGES = (0.02, 0.05)


def lp(**kw):
    return LiveParams([Level(100.0, 1e9), Level(50.0, 60.0, by_time=True)], edges=EDGES, **kw)


def feed(p, path, n, fills, pnl, t0=0.0, dt=0.0):
    for i in range(n):
        p.observe(path, t0 + i * dt, fills, pnl)


def test_unseen_leaf_takes_its_parents_numbers():
    p = lp(z=0.0)
    feed(p, ("soccer", "ev1"), 200, [10, 5], [0.2, 0.25])
    a = p.estimate(("soccer", "ev1"), 0.0)
    b = p.estimate(("soccer", "ev2"), 0.0)
    assert [x for e in b for x in e] == pytest.approx([x for e in a for x in e])


def test_a_thin_child_is_shrunk_toward_its_parent():
    p = lp(z=0.0)
    feed(p, ("soccer", "ev1"), 1000, [10, 5], [0.2, 0.25])      # 2c and 5c a contract
    feed(p, ("soccer", "ev2"), 5, [10, 5], [-1.0, -0.5])        # 5 bad prints
    v = p.estimate(("soccer", "ev2"), 0.0)[0][0]
    parent = p.estimate(("soccer",), 0.0)[0][0]
    assert -0.1 < v < parent                                    # pulled down, nowhere near -1.0 a print


def test_off_wins_when_every_edge_loses_with_confidence():
    p = lp(z=2.0)
    feed(p, ("hockey", "ev"), 2000, [10, 5], [-0.3, -0.1])
    assert p.choose(("hockey", "ev"), 0.0) is OFF


def test_uncertain_gain_does_not_win_against_off():
    p = lp(z=2.0)
    for i in range(400):                                        # +1c a contract on average, very noisy
        x = 3.0 if i % 2 else -2.8
        p.observe(("esports", "ev"), 0.0, [10, 5], [x, x / 2])
    assert p.choose(("esports", "ev"), 0.0) is OFF


def test_hysteresis_keeps_a_close_second_from_flapping():
    p = lp(z=0.0, hysteresis=0.5)
    feed(p, ("x", "ev"), 500, [10, 5], [0.20, 0.10])
    assert p.choose(("x", "ev"), 0.0) == 0.02
    feed(p, ("x", "ev"), 50, [10, 5], [0.20, 0.30])              # 5c now slightly ahead
    assert p.choose(("x", "ev"), 0.0) == 0.02


def test_event_level_forgets_by_time():
    p = lp(z=0.0)
    feed(p, ("soccer", "ev"), 1000, [10, 5], [0.2, 0.25])
    feed(p, ("soccer", "hot"), 200, [10, 5], [-2.0, 0.5])        # a goal flurry: 2c edge gets run over
    assert p.choose(("soccer", "hot"), 0.0) == 0.05
    later = p.estimate(("soccer", "hot"), 3600.0)                # 60 event half-lives on
    assert later[0][0] == pytest.approx(p.estimate(("soccer",), 3600.0)[0][0], rel=1e-6)


def test_prune_drops_idle_events_and_their_choices():
    p = lp()
    feed(p, ("soccer", "old"), 10, [10, 5], [0.2, 0.25], t0=0.0)
    feed(p, ("soccer", "new"), 10, [10, 5], [0.2, 0.25], t0=5000.0)
    p.choose(("soccer", "old"), 0.0)
    p.choose(("soccer", "new"), 5000.0)
    assert p.prune(5000.0, 3600.0) == 1
    assert ("soccer", "old") not in p.nodes and ("soccer",) in p.nodes and ("soccer", "new") in p.nodes
    assert set(p.choice) == {("soccer", "new")}
