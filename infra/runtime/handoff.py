"""Hot handoff: a running series hands its resting quotes and positions to a
freshly started copy of itself (new code) instead of cancelling them.

Files under the live root:
  HANDOFF.json                      deploy -> parent: {"id", "series": [..] | "all", "at"}
  HANDOFF.status.json               parent's progress (deploy/arbs_handoff.sh prints it)
  <SERIES>/handoff/<id>.ready       standby: warm
  <SERIES>/handoff/<id>.go          parent -> old process: hand over now
  <SERIES>/handoff/<id>.state.json  old process: quote table, exit and arm state
  <SERIES>/handoff/<id>.handed      old process: state saved, exiting without cancels
  <SERIES>/handoff/<id>.taken       standby: now the active process
  <SERIES>/handoff/<id>.abort       parent -> standby: exit without writing anything

The parent side is Driver, stepped from launch_many's poll loop. A standby
only ever writes into its series' handoff dir until it has taken over.
"""
from __future__ import annotations

import json
import os
import time

from infra.config import atomic_write_json

REQUEST = "HANDOFF.json"
STATUS = "HANDOFF.status.json"
# Shard 3 and Polymarket first; shard 0's group leader (ARBS) last, so its
# scan and rebalancing move over once its siblings already run the new code.
ORDER = ("ARBS3B", "ARBS3", "ARBSPM", "ARBS0C", "ARBS0B", "ARBS")
READY_TIMEOUT_S = 600.0
HANDED_TIMEOUT_S = 10.0
# The standby's takeover targets 2 s; past this it is presumed stuck.
TAKEN_TIMEOUT_S = 30.0
EXIT_WAIT_S = 30.0


def hdir(root: str, series: str) -> str:
    return os.path.join(root, series, "handoff")


def path(root: str, series: str, hid: str, kind: str) -> str:
    return os.path.join(hdir(root, series), f"{hid}.{kind}")


def mark(root: str, series: str, hid: str, kind: str, **body):
    atomic_write_json(path(root, series, hid, kind), {"t": time.time(), **body}, indent=None)


def has(root: str, series: str, hid: str, kind: str) -> bool:
    return os.path.exists(path(root, series, hid, kind))


def read_json(p: str):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class Driver:
    """Parent side, one series at a time. `procs` is launch_many's list of
    [name, proc] (mutated in place when a series moves to its new process);
    `spawn(name, extra_args)` starts that series' child command plus args."""

    def __init__(self, root: str, procs: list, spawn, log=print, clock=time.monotonic):
        self.root, self.procs, self.spawn, self.log, self.clock = root, procs, spawn, log, clock
        # Ids already present at start are never run: a request left from
        # before a full restart must not hand over the fresh processes.
        self.seen = {x.get("id") for x in (read_json(os.path.join(root, REQUEST)),
                                           read_json(os.path.join(root, STATUS))) if x}
        self.hid: str | None = None
        self.queue: list[str] = []
        self.job: dict | None = None
        self.status: dict = {}

    def busy(self) -> bool:
        return self.job is not None or bool(self.queue)

    def _proc(self, name: str):
        return next((e[1] for e in self.procs if e[0] == name), None)

    def _replace(self, name: str, p):
        for e in self.procs:
            if e[0] == name:
                e[1] = p

    def _say(self, name: str, msg: str):
        self.log(f"[run] handoff {self.hid} {name}: {msg}")
        st = self.status.setdefault("series", {}).setdefault(name, {})
        st["phase"], st["at"] = msg, time.time()
        self._write()

    def _write(self):
        try:
            atomic_write_json(os.path.join(self.root, STATUS), self.status, indent=None)
        except OSError as e:
            self.log(f"[run] handoff status not written ({e!r})")

    def poll(self):
        if not self.busy():
            self._check_request()
        if self.job is None and self.queue:
            self._start(self.queue.pop(0))
        if self.job is not None:
            self._step()
            if self.job is None and not self.queue and self.status.get("state") == "running":
                self.status["state"] = "done"
                self._write()
                self.log(f"[run] handoff {self.hid}: done")

    def _check_request(self):
        req = read_json(os.path.join(self.root, REQUEST))
        if not req or not req.get("id") or req["id"] in self.seen:
            return
        self.hid = str(req["id"])
        self.seen.add(req["id"])
        # Only series whose runner implements the standby side (ORDER): any
        # other child given --standby would start trading beside the old one.
        names = [e[0] for e in self.procs if e[0] in ORDER]
        want = names if req.get("series") == "all" else [s.upper() for s in req.get("series") or []]
        unknown = [s for s in want if s not in names]
        self.status = {"id": self.hid, "state": "running", "started": time.time(), "series": {}}
        if unknown:
            self.status.update(state="failed", error=f"unknown series {unknown}")
            self._write()
            self.log(f"[run] handoff {self.hid}: refused, unknown series {unknown}")
            return
        rank = {s: i for i, s in enumerate(ORDER)}
        self.queue = sorted(want, key=lambda s: rank.get(s, len(ORDER)))
        self.status["queue"] = list(self.queue)
        self._write()
        self.log(f"[run] handoff {self.hid}: series in order {self.queue}")

    def _start(self, name: str):
        old = self._proc(name)
        if old is None or old.poll() is not None:
            self._fail(name, "not running; skipped", stop=False)
            return
        os.makedirs(hdir(self.root, name), exist_ok=True)
        sb = self.spawn(name, ["--standby", self.hid])
        self.job = {"name": name, "old": old, "standby": sb, "phase": "warm", "t": self.clock()}
        self._say(name, f"standby started (pid {sb.pid}), waiting for it to warm up")

    def _phase(self, phase: str):
        self.job["phase"], self.job["t"] = phase, self.clock()

    def _done(self, name: str, result: str):
        self.status["series"].setdefault(name, {})["result"] = result
        self._say(name, result)
        self.job = None

    def _fail(self, name: str, why: str, stop: bool = True):
        """The series stays on whatever process now runs it; the rest of the
        queue is dropped so a bad build stops at the first series."""
        self.status["series"].setdefault(name, {})["result"] = f"failed: {why}"
        self._say(name, f"failed: {why}")
        self.job = None
        if stop:
            self.queue = []
            self.status["state"] = "failed"
            self._write()

    def _cold_start(self, name: str, why: str):
        adopt = has(self.root, name, self.hid, "handed")
        p = self.spawn(name, ["--adopt", self.hid] if adopt else [])
        self._replace(name, p)
        self._fail(name, f"{why}; cold start (pid {p.pid}){' adopting the handed-over quotes' if adopt else ''}")

    @staticmethod
    def _gone(p) -> bool:
        return p is None or p.poll() is not None

    def _kill(self, p):
        if p is not None and p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    def _step(self):
        j = self.job
        name, old, sb, age = j["name"], j["old"], j["standby"], self.clock() - j["t"]
        R, hid = self.root, self.hid
        if j["phase"] == "warm":
            if self._gone(old):
                self._kill(sb)
                self._cold_start(name, f"old process exited ({old.returncode}) while the standby warmed")
            elif self._gone(sb):
                self._fail(name, f"standby exited ({sb.returncode}) before it was ready; old process continues")
            elif has(R, name, hid, "ready"):
                mark(R, name, hid, "go")
                self._phase("go")
                self._say(name, "standby ready; told the old process to hand over")
            elif age > READY_TIMEOUT_S:
                self._kill(sb)
                self._fail(name, f"standby not ready in {READY_TIMEOUT_S:.0f}s; killed it, old process continues")
        elif j["phase"] == "go":
            if has(R, name, hid, "handed"):
                self._replace(name, sb)
                self._phase("taking")
                self._say(name, "old process handed over; standby taking over")
            elif self._gone(sb) or age > HANDED_TIMEOUT_S:
                why = "standby exited" if self._gone(sb) else f"no hand-over in {HANDED_TIMEOUT_S:.0f}s"
                mark(R, name, hid, "abort")
                j["why"] = why
                self._phase("abort")
                self._say(name, f"{why}; told the standby to abort")
        elif j["phase"] == "abort":
            if has(R, name, hid, "taken") and not self._gone(sb):
                # It saw .handed before our abort and has taken over: keep it.
                self._replace(name, sb)
                self._phase("retire")
                self._say(name, "standby took over before the abort; waiting for the old process to exit")
                return
            if not self._gone(sb) and age < 5.0:
                return
            self._kill(sb)
            if has(R, name, hid, "handed") and not self._gone(old) and age < EXIT_WAIT_S:
                return                         # it handed over as we aborted: let it exit first
            if not self._gone(old) and not has(R, name, hid, "handed"):
                self._fail(name, f"{j['why']}; aborted, old process continues")
            else:
                self._kill(old)
                self._cold_start(name, f"{j['why']}; aborted after the old process stopped")
        elif j["phase"] == "taking":
            if has(R, name, hid, "taken"):
                self._phase("retire")
                self._say(name, "standby took over; waiting for the old process to exit")
            elif self._gone(sb) or age > TAKEN_TIMEOUT_S:
                why = f"standby exited ({sb.returncode})" if self._gone(sb) else \
                    f"standby did not take over in {TAKEN_TIMEOUT_S:.0f}s"
                self._kill(sb)
                self._kill(old)
                self._cold_start(name, why)
        elif j["phase"] == "retire":
            if self._gone(old):
                self._done(name, f"handed over (old exited {old.returncode}, new pid {sb.pid})")
            elif age > EXIT_WAIT_S:
                self._kill(old)
                self._done(name, f"handed over (old process killed after {EXIT_WAIT_S:.0f}s, new pid {sb.pid})")

    def stop(self):
        """Ctrl-C mid-handoff: the standby isn't in procs yet, and an old
        process already replaced there is out of the caller's reach."""
        if self.job is not None:
            for p in (self.job["standby"], self.job["old"]):
                if not any(e[1] is p for e in self.procs) and p.poll() is None:
                    p.terminate()
