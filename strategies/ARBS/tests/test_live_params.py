import pytest

from strategies.ARBS.live_params import OFF, Level, LiveParams

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


def test_sizer_starts_small_grows_slowly_and_cuts_on_a_loss():
    from strategies.ARBS.live_params import LiveSizer
    s = LiveSizer(start=0.1, up=0.01, down=0.1)
    path = ("S:Table Tennis", "KXLIGAPROMATCH", "ev1")
    assert s.mult(path) == pytest.approx(0.1)                   # unseen sport: a tenth of size
    for _ in range(10):
        s.update(path, 3.0, 0.0)                                # ten +3c/lot wins
    grown = s.mult(path)
    assert 0.13 < grown < 0.14                                  # x exp(0.3)
    s.update(path, -20.0, 0.0)                                  # one -20c/lot fill
    assert s.mult(path) < grown * 0.2 * 0.2                      # group/series and event both cut x exp(-2)
    assert s.mult(("S:Table Tennis", "KXLIGAPROMATCH", "ev2")) == pytest.approx(grown * 0.135, rel=0.01)


def test_live_state_survives_a_restart():
    from strategies.ARBS.live_params import LiveSizer
    p = lp(z=0.0)
    feed(p, ("hockey", "ev"), 300, [10, 5], [-0.3, -0.1])
    q = lp(z=0.0)
    q.load(p.state())
    assert q.estimate(("hockey", "ev"), 0.0) == p.estimate(("hockey", "ev"), 0.0)
    s = LiveSizer()
    s.update(("g", "s", "e"), -10.0, 5.0)
    t = LiveSizer()
    t.load(s.state())
    assert t.mult(("g", "s", "e")) == s.mult(("g", "s", "e"))


def test_tte_buckets_size_near_arbs_down():
    from strategies.ARBS.live_params import tte_bucket
    assert tte_bucket(2.0) == ("<6h", 0.25)
    assert tte_bucket(12.0) == ("6-24h", 0.5)
    assert tte_bucket(48.0) == (">24h", 1.0)
    assert tte_bucket(None) == ("<6h", 0.25)
