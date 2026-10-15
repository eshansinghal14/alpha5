"""WriteGroup: several OrderManagers on one bucket, one reserve and one
priority queue."""
from kalshi_infra.kalshi.orders import WriteBudget
from kalshi_infra.tests.test_order_manager import Clock, D, Ex, ack_all
from kalshi_infra.trading import order_manager as om
from kalshi_infra.trading.order_manager import OrderManager, WriteGroup


class Shared:
    """One token count all members' executors debit."""

    def __init__(self, tokens):
        self.tokens = tokens


class SharedEx(Ex):
    def __init__(self, shared):
        super().__init__(bucket=None)
        self.shared = shared

    def _debit(self, n):
        self.shared.tokens -= n

    def bucket_tokens(self):
        return self.shared.tokens


def group(tokens, reserves):
    sh, c, g = Shared(tokens), Clock(), WriteGroup()
    ms = {}
    for name, r in reserves.items():
        ex = SharedEx(sh)
        m = OrderManager(ex, cancels_token=r, clock=c)
        g.attach(name, m)
        ms[name] = (m, ex)
    return g, ms, sh


def test_one_reserve_not_one_per_series():
    # 3 series, one 80-token bucket, reserve 20: six creates fit (80-20 = 60).
    # Split three ways (26.7 each) none would, the 2026-10-13 bug.
    g, ms, sh = group(80.0, {"A": 20.0, "B": 20.0, "C": 20.0})
    for name, (m, _) in ms.items():
        m.set_desired(name, "yes", D([0.41, 0.40, 0.39]))
    g.pump()
    assert sum(len(ex.creates) for _, ex in ms.values()) == 6
    assert sh.tokens >= 20.0


def test_touch_levels_of_every_series_go_first():
    g, ms, _ = group(50.0, {"A": 20.0, "B": 20.0, "C": 20.0})   # three creates fit
    for name, (m, _) in ms.items():
        m.set_desired(name, "yes", D([0.41, 0.40]))
    g.pump()
    created = [(n, q.price) for n, (_, ex) in ms.items() for q in ex.creates]
    assert sorted(created) == [("A", 0.41), ("B", 0.41), ("C", 0.41)]


def test_pump_on_any_member_runs_the_whole_group():
    g, ms, _ = group(100.0, {"A": 0.0, "B": 0.0})
    for name, (m, _) in ms.items():
        m.set_desired(name, "yes", D([0.41]))
    ms["A"][0].pump()
    assert len(ms["A"][1].creates) == 1 and len(ms["B"][1].creates) == 1


def test_cancels_drain_touch_first_across_series_before_any_create():
    g, ms, sh = group(4.0, {"A": 0.0, "B": 0.0})                 # two cancels' worth
    (ma, exa), (mb, exb) = ms["A"], ms["B"]
    ma.quotes[1] = om.Quote(1, "A", "yes", 0.39, 1.0, state=om.RESTING, exchange_id="x1")
    ma.quotes[2] = om.Quote(2, "A", "yes", 0.41, 1.0, state=om.RESTING, exchange_id="x2")
    mb.quotes[3] = om.Quote(3, "B", "yes", 0.31, 1.0, state=om.RESTING, exchange_id="x3")
    for m in (ma, mb):
        for q in list(m.quotes.values()):
            m._cancel_q[q.order_id] = "pull"
    mb.set_desired("B", "yes", D([0.30]))
    g.pump()
    # Touch levels of both series (A 0.41, B 0.31) cancel first; A's 0.39 waits;
    # nothing is created while a cancel is queued.
    assert {oid for oid, _ in exa.cancels} == {2} and {oid for oid, _ in exb.cancels} == {3}
    assert not exb.creates and ma._cancel_q


def test_reserve_is_the_largest_members():
    g, ms, _ = group(100.0, {"A": 10.0, "B": 30.0})
    assert g.reserve() == 30.0 and ms["A"][0].reserve() == 30.0
    g.detach("B")
    assert g.reserve() == 10.0 and ms["B"][0].reserve() == 30.0   # B back on its own


def test_detached_member_pumps_alone():
    g, ms, _ = group(100.0, {"A": 0.0, "B": 0.0})
    g.detach("B")
    ms["B"][0].set_desired("B", "yes", D([0.41]))
    ms["A"][0].set_desired("A", "yes", D([0.41]))
    ms["A"][0].pump()
    assert len(ms["A"][1].creates) == 1 and not ms["B"][1].creates


def test_shadow_group_shares_one_modelled_bucket():
    c = Clock()
    g = WriteGroup(sim_tokens_per_s=40.0, clock=c)
    ms = {}
    for name in ("A", "B"):
        ex = Ex(bucket=None)
        m = OrderManager(ex, cancels_token=20.0, clock=c, sim_bucket_tokens_per_s=26.7)
        g.attach(name, m)
        m.set_desired(name, "yes", D([0.41, 0.40]))
        ms[name] = (m, ex)
    g.pump()
    # 40 - 20 reserve = 20: two creates across BOTH members, not 0 each.
    assert sum(len(ex.creates) for _, ex in ms.values()) == 2
    assert ms["A"][0].sim_bucket is ms["B"][0].sim_bucket


def test_starved_shadow_member_does_not_block_a_live_one():
    """2026-10-15: shadow KXINX's modelled bucket sat below reserve + create,
    and its creates (first in the shared queue) stopped every pass before
    armed KXINXU's real creates: KXINXU quoted nothing on a full real bucket."""
    c = Clock()
    g = WriteGroup(sim_tokens_per_s=25.0, clock=c)          # modelled bucket < 20 + 10
    shadow_ex = Ex(bucket=None)
    shadow = OrderManager(shadow_ex, cancels_token=20.0, clock=c)
    g.attach("SHADOW", shadow)
    live_ex = Ex(bucket=80.0)                              # real bucket, full
    live = OrderManager(live_ex, cancels_token=20.0, clock=c)
    g.attach("LIVE", live)
    shadow.set_desired("S", "yes", D([0.41, 0.40]))
    live.set_desired("L", "yes", D([0.31, 0.30]))
    g.pump()
    assert not shadow_ex.creates                           # its own bucket is short
    assert [q.price for q in live_ex.creates] == [0.31, 0.30]


def test_two_live_members_still_share_one_queue():
    sh, cl, g = Shared(30.0), Clock(), WriteGroup()        # 30 - 20 reserve: one create
    exs = {}
    for name in ("A", "B"):
        ex = SharedEx(sh)
        m = OrderManager(ex, cancels_token=20.0, clock=cl)
        g.attach(name, m)
        m.set_desired(name, "yes", D([0.41]))
        exs[name] = ex
    g.pump()
    assert sum(len(ex.creates) for ex in exs.values()) == 1


def test_clients_share_one_write_budget():
    from kalshi_infra.tests.test_shard_billing import _client
    b = WriteBudget(50.0)                       # 40 usable
    a, c = _client(0), _client(0)
    for x in (a, c):
        x.budget, x._spent, x._refused, x._lock = b, {}, {}, __import__("threading").Lock()
    assert a.allow_now("order") and c.allow_now("order")
    assert a.allow_now("order") and c.allow_now("order")
    assert not a.allow_now("order")            # 40 spent between them
    assert abs(c.tokens_now()) < 1.0


def test_priority_member_gets_the_bucket_first():
    """KXINXDUD before KXINXU: with room for one create above the reserve,
    the high-priority member's touch level gets it even though the other
    member's level is nearer its own touch."""
    sh, cl, g = Shared(30.0), Clock(), WriteGroup()          # 30 - 20 reserve: one create
    lo_ex, hi_ex = SharedEx(sh), SharedEx(sh)
    lo = OrderManager(lo_ex, cancels_token=20.0, clock=cl)
    hi = OrderManager(hi_ex, cancels_token=20.0, clock=cl)
    g.attach("U", lo, priority=2)                           # attached first, lower priority
    g.attach("DUD", hi, priority=0)
    lo.set_desired("U", "yes", D([0.41]))
    hi.set_desired("D", "yes", D([0.31]))
    g.pump()
    assert [q.price for q in hi_ex.creates] == [0.31]
    assert not lo_ex.creates


def test_priority_member_second_level_beats_others_touch():
    sh, cl, g = Shared(40.0), Clock(), WriteGroup()          # two creates above the reserve
    u_ex, d_ex = SharedEx(sh), SharedEx(sh)
    u = OrderManager(u_ex, cancels_token=20.0, clock=cl)
    d = OrderManager(d_ex, cancels_token=20.0, clock=cl)
    g.attach("U", u, priority=2)
    g.attach("DUD", d, priority=0)
    u.set_desired("U", "yes", D([0.41, 0.40]))
    d.set_desired("D", "yes", D([0.31, 0.30]))
    g.pump()
    # Touch levels go first across members, DUD's before U's; then levels behind.
    assert [q.price for q in d_ex.creates] == [0.31] and [q.price for q in u_ex.creates] == [0.41]


def test_equal_priority_keeps_touch_first_interleave():
    sh, cl, g = Shared(50.0), Clock(), WriteGroup()          # three creates
    exs = {}
    for name in ("A", "B"):
        ex = SharedEx(sh)
        m = OrderManager(ex, cancels_token=20.0, clock=cl)
        g.attach(name, m)
        m.set_desired(name, "yes", D([0.41, 0.40]))
        exs[name] = ex
    g.pump()
    assert sorted(q.price for ex in exs.values() for q in ex.creates) == [0.40, 0.41, 0.41]