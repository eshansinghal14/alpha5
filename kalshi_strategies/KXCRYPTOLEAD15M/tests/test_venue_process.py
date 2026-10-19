"""Venue process lifecycle, without touching any exchange: the child's main
is swapped for a stub that ships fixed frames."""
import io
import sys
import time

import pytest

from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBus
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import venue_process as vp
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook

STUB = r"""
import sys, time
sys.path.insert(0, {root!r})
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import venue_process as vp
out = sys.stdout.buffer
for i in range({n}):
    now = time.time()
    vp._write_frame(out, ([("kraken", "BTC", "BTC/USD", ((100.0, 1.0),), ((100.2, 1.0),), now, None, False)],
                          [("hl_perp", "BTC", {{"price": "100", "size": "1", "trade_id": i}}, now, now)],
                          [("kraken", "BTC", now)]))
    time.sleep(0.02)
{tail}
"""


def _stubbed(monkeypatch, n=5, tail="sys.stdin.buffer.read()"):
    from kalshi_infra import REPO_ROOT
    code = STUB.format(root=str(REPO_ROOT), n=n, tail=tail)
    real_popen = vp.subprocess.Popen

    def popen(args, **kw):
        return real_popen([sys.executable, "-c", code], **kw)
    monkeypatch.setattr(vp.subprocess, "Popen", popen)


def _wait(cond, timeout=30.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_frame_roundtrip():
    buf = io.BytesIO()
    vp._write_frame(buf, ([1, 2], [], []))
    buf.seek(0)
    assert vp._read_frame(buf) == ([1, 2], [], [])
    assert vp._read_frame(buf) is None          # EOF


class _Trickle(io.RawIOBase):
    """A pipe that hands back at most `step` bytes per read, like an
    unbuffered Linux pipe under load."""

    def __init__(self, data: bytes, step: int = 4096):
        self._b, self._i, self._step = data, 0, step

    def readable(self):
        return True

    def read(self, n=-1):
        k = min(n, self._step, len(self._b) - self._i)
        out = self._b[self._i:self._i + k]
        self._i += k
        return out


def test_frame_bigger_than_one_pipe_read_is_reassembled():
    """10-19 07:34 UTC: a sweep's frame came back short, read as EOF, and the
    reader quit -- every non-Coinbase venue left the median for 11h44m."""
    buf = io.BytesIO()
    obj = ([("kraken", "BTC", i) for i in range(20_000)], [], [])
    vp._write_frame(buf, obj)
    inp = _Trickle(buf.getvalue())
    assert vp._read_frame(inp) == obj
    assert vp._read_frame(inp) is None


def test_truncated_frame_is_eof():
    buf = io.BytesIO()
    vp._write_frame(buf, ([1], [], []))
    assert vp._read_frame(_Trickle(buf.getvalue()[:-3])) is None


def test_dead_reader_restarts_a_live_child(monkeypatch):
    _stubbed(monkeypatch, n=1)                   # stays alive reading stdin
    p = vp.VenueProcess({"kraken": ["BTC"]})
    p.ensure_running()
    try:
        assert p.alive() and p.n_starts == 1
        p._reader_thread.join(0)
        p._reader_thread = type("Dead", (), {"is_alive": lambda self: False})()
        p.ensure_running()                       # kills the orphaned child
        assert not p.alive() and p.n_reader_restarts == 1
        p._next_try = 0.0
        p.ensure_running()
        assert p.alive() and p.n_starts == 2 and p.summary()["reader_alive"]
    finally:
        p.stop()


def test_books_prints_and_keepalive_reach_the_parent(monkeypatch):
    _stubbed(monkeypatch)
    p = vp.VenueProcess({"kraken": ["BTC"]})
    p.ensure_running()
    try:
        books, prints, alive = [], [], []
        assert _wait(lambda: p.drain(books.append, lambda *a: prints.append(a),
                                     lambda *a: alive.append(a)) or len(books) >= 5)
        vb = books[0]
        assert (vb.venue, vb.coin, vb.best_bid(), vb.best_ask()) == ("kraken", "BTC", 100.0, 100.2)
        # Wall-clock receipt converted to this process's monotonic clock.
        assert abs(time.monotonic() - vb.receive_ts) < 5.0
        assert prints and prints[0][4] == "hl_perp" and alive
    finally:
        p.stop()


TRIP_STUB = r"""
import sys, time, threading
sys.path.insert(0, {root!r})
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import venue_process as vp
out = sys.stdout.buffer
got = []
def rd():
    while True:
        m = vp._read_frame(sys.stdin.buffer)
        if m is None:
            return
        got.append(m)
threading.Thread(target=rd, daemon=True).start()
deadline = time.time() + 20
while not got and time.time() < deadline:
    time.sleep(0.01)
now = time.time()
thr = got[0]["vol"]["binance_perp"]["BTC"] if got else -1
vp._write_frame(out, ([], [], [], {{"trips": [("volume", "binance_perp", "BTC", 2.0 * thr, thr, True, now, now)]}}))
sys.stdin.buffer.read()
"""


def test_control_reaches_child_and_trip_frame_wakes_parent(monkeypatch):
    from kalshi_infra import REPO_ROOT
    code = TRIP_STUB.format(root=str(REPO_ROOT))
    real_popen = vp.subprocess.Popen
    monkeypatch.setattr(vp.subprocess, "Popen", lambda args, **kw: real_popen([sys.executable, "-c", code], **kw))
    p = vp.VenueProcess({"binance_perp": ["BTC"]})
    woke = []
    p.on_urgent = lambda: woke.append(time.monotonic())
    p.send_control({"vol": {"binance_perp": {"BTC": 1234.0}}})     # held, sent on start
    p.ensure_running()
    try:
        trips = []
        assert _wait(lambda: woke)
        p.drain(lambda vb: None, on_trip=lambda *a: trips.append(a))
        assert trips and trips[0][:6] == ("volume", "binance_perp", "BTC", 2468.0, 1234.0, True)
        assert abs(time.monotonic() - trips[0][6]) < 5.0
    finally:
        p.stop()


def test_dead_child_is_restarted_with_backoff(monkeypatch):
    _stubbed(monkeypatch, n=1, tail="")          # exits right after one frame
    clock = [0.0]
    p = vp.VenueProcess({"kraken": ["BTC"]}, clock=lambda: clock[0])
    p.ensure_running()
    assert _wait(lambda: not p.alive())
    p.ensure_running()                          # notices the exit, schedules a retry
    assert p.n_starts == 1 and p.last_exit is not None
    clock[0] = vp.RESTART_MIN_S - 0.01
    p.ensure_running()
    assert p.n_starts == 1                      # still backing off
    clock[0] = vp.RESTART_MIN_S + 0.01
    p.ensure_running()
    assert p.n_starts == 2
    p.stop()


def test_child_exits_when_parent_closes_the_pipe(monkeypatch):
    _stubbed(monkeypatch, n=1)
    p = vp.VenueProcess({"kraken": ["BTC"]})
    p.ensure_running()
    assert p.alive()
    p._proc.stdin.close()
    assert _wait(lambda: not p.alive())


def test_start_failure_leaves_pricing_running(monkeypatch):
    def boom(*a, **k):
        raise OSError("no python")
    monkeypatch.setattr(vp.subprocess, "Popen", boom)
    p = vp.VenueProcess({"kraken": ["BTC"]})
    p.ensure_running()                          # must not raise
    assert not p.alive() and p.drain(lambda vb: None) == 0


def _vb(venue, mid, ts):
    return VenueBook(venue=venue, product="BTC-USD", coin="BTC", bids=(OrderBookLevel(mid - 0.5, 1.0),),
                     asks=(OrderBookLevel(mid + 0.5, 1.0),), receive_ts=ts)


def test_median_uses_listed_venues_and_drops_stale_ones():
    bus = CompositeBus(coins=("BTC",), persist=False, venues={"BTC": ("coinbase", "kraken", "bitstamp")},
                       venue_stale_s=5.0)
    now = time.monotonic()
    bus.observe(_vb("coinbase", 100.0, now))
    bus.observe(_vb("gemini", 500.0, now))           # not listed for BTC here: ignored
    bus.observe(_vb("kraken", 102.0, now))
    assert bus.mid_nowcast("BTC") == pytest.approx(101.0)
    bus.observe(_vb("bitstamp", 104.0, now - 6.0))   # older than venue_stale_s: left out
    bus.observe(_vb("coinbase", 100.0, now))
    assert bus.mid_nowcast("BTC") == pytest.approx(101.0)
    bus.touch("bitstamp", "BTC", now)                # live connection, unchanged book
    bus.observe(_vb("coinbase", 100.0, now))
    assert bus.mid_nowcast("BTC") == pytest.approx(102.0)
