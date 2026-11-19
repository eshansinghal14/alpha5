"""The parent's hot-handoff state machine (infra/runtime/handoff.Driver) on fake child processes."""
import itertools
import json
import os

from infra.runtime import handoff as H

_pids = itertools.count(100)


class Proc:
    def __init__(self, args=()):
        self.args, self.pid, self.returncode = list(args), next(_pids), None
        self.killed = self.terminated = False

    def poll(self):
        return self.returncode

    def exit(self, code=0):
        self.returncode = code

    def kill(self):
        self.killed, self.returncode = True, -9

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode


class World:
    def __init__(self, root, names=("ARBS", "ARBS3")):
        self.root, self.t = str(root), 0.0
        self.procs = [[n, Proc()] for n in names]
        self.spawned = []
        self.d = H.Driver(self.root, self.procs, self.spawn, log=lambda m: None, clock=lambda: self.t)

    def spawn(self, name, extra):
        p = Proc(extra)
        self.spawned.append((name, p))
        return p

    def request(self, hid="h1", series="all"):
        with open(os.path.join(self.root, H.REQUEST), "w") as fh:
            json.dump({"id": hid, "series": series, "at": 0}, fh)

    def proc(self, name):
        return next(p for n, p in self.procs if n == name)

    def status(self):
        with open(os.path.join(self.root, H.STATUS)) as fh:
            return json.load(fh)

    def step(self, secs=0.5):
        self.t += secs
        self.d.poll()


def _hand_over(w, name, hid="h1"):
    """Drive one series through a clean handoff; returns (old, standby)."""
    old = w.proc(name)
    w.step()
    sb = w.spawned[-1][1]
    assert w.spawned[-1][0] == name and sb.args == ["--standby", hid]
    H.mark(w.root, name, hid, "ready")
    w.step()
    assert H.has(w.root, name, hid, "go")
    H.mark(w.root, name, hid, "handed")
    w.step()
    assert w.proc(name) is sb                      # in procs from the hand-over on
    H.mark(w.root, name, hid, "taken")
    w.step()
    old.exit(0)
    w.step()
    return old, sb


def test_series_go_one_at_a_time_in_order(tmp_path):
    w = World(tmp_path)
    w.request()
    _hand_over(w, "ARBS3")                         # shard 3 before shard 0's leader
    _hand_over(w, "ARBS")
    w.step()
    st = w.status()
    assert st["state"] == "done" and not w.d.busy()
    assert all(v["result"].startswith("handed over") for v in st["series"].values())


def test_an_id_runs_once_and_one_present_at_start_never(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request("old")
    w2 = World(tmp_path, names=("ARBS",))          # a parent started with the request already there
    w2.step()
    assert not w2.spawned
    w.request("new")
    _hand_over(w2, "ARBS", "new")
    n = len(w2.spawned)
    for _ in range(5):
        w2.step()
    assert len(w2.spawned) == n


def test_standby_never_ready_is_killed_and_the_old_process_continues(tmp_path):
    w = World(tmp_path)
    w.request()
    old = w.proc("ARBS3")
    w.step()
    sb = w.spawned[-1][1]
    w.step(H.READY_TIMEOUT_S + 1)
    assert sb.killed and w.proc("ARBS3") is old and old.returncode is None
    st = w.status()
    assert st["state"] == "failed" and not w.d.busy()     # ARBS is not attempted after a failure
    assert [n for n, _ in w.spawned] == ["ARBS3"]


def test_no_hand_over_in_time_aborts_and_the_old_process_continues(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request()
    old = w.proc("ARBS")
    w.step()
    sb = w.spawned[-1][1]
    H.mark(w.root, "ARBS", "h1", "ready")
    w.step()
    w.step(H.HANDED_TIMEOUT_S + 1)
    assert H.has(w.root, "ARBS", "h1", "abort")
    sb.exit(0)                                     # the standby saw the abort
    w.step()
    assert w.proc("ARBS") is old and w.status()["state"] == "failed" and len(w.spawned) == 1


def test_old_process_gone_without_hand_over_cold_starts(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request()
    old = w.proc("ARBS")
    w.step()
    H.mark(w.root, "ARBS", "h1", "ready")
    w.step()
    old.exit(1)                                    # crashed before writing .handed
    w.step(H.HANDED_TIMEOUT_S + 1)
    w.step(6.0)
    name, cold = w.spawned[-1]
    assert cold.args == [] and w.proc("ARBS") is cold      # an ordinary cold start (sweep and audit)


def test_standby_dying_after_the_hand_over_cold_starts_adopting(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request()
    old = w.proc("ARBS")
    w.step()
    sb = w.spawned[-1][1]
    H.mark(w.root, "ARBS", "h1", "ready")
    w.step()
    H.mark(w.root, "ARBS", "h1", "handed")
    old.exit(0)
    w.step()
    sb.exit(3)                                     # died before .taken
    w.step()
    cold = w.spawned[-1][1]
    assert cold.args == ["--adopt", "h1"] and w.proc("ARBS") is cold
    assert w.status()["state"] == "failed"


def test_a_late_hand_over_after_the_abort_is_adopted(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request()
    old = w.proc("ARBS")
    w.step()
    sb = w.spawned[-1][1]
    H.mark(w.root, "ARBS", "h1", "ready")
    w.step()
    w.step(H.HANDED_TIMEOUT_S + 1)                 # abort written
    H.mark(w.root, "ARBS", "h1", "handed")         # ...as the old process handed over
    sb.exit(0)
    old.exit(0)
    w.step()
    assert w.spawned[-1][1].args == ["--adopt", "h1"]


def test_ctrl_c_mid_handoff_stops_the_standby(tmp_path):
    w = World(tmp_path, names=("ARBS",))
    w.request()
    w.step()
    sb = w.spawned[-1][1]
    w.d.stop()
    assert sb.terminated
