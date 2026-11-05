"""Nearest-node routing for Kalshi's API: which IPs count as near, and the
connect hook's fallbacks."""
import socket

from kalshi_infra.kalshi import routing


def test_pick_near_keeps_the_fast_cluster_fastest_first():
    t = {"a": 1.27, "b": 0.53, "c": 0.58, "d": 1.91, "e": 0.98, "f": float("nan")}
    assert routing.pick_near(t, margin_ms=0.4) == ["b", "c"]
    assert routing.pick_near({}) == []


def test_hook_uses_preferred_ips_then_falls_back(monkeypatch):
    calls = []

    def orig(address, timeout=None, source_address=None, socket_options=None):
        calls.append(address[0])
        if address[0] == "bad":
            raise OSError("refused")
        return "sock"
    hook = routing._wrap(orig)
    monkeypatch.setattr(routing, "_preferred", ["bad", "good"])
    monkeypatch.setattr(routing, "_rr", iter([0, 0, 0]))
    assert hook((routing.HOST, 443), 5) == "sock" and calls == ["bad", "good"]
    calls.clear()
    assert hook(("other.example.com", 443), 5) == "sock" and calls == ["other.example.com"]   # other hosts untouched
    calls.clear()
    monkeypatch.setattr(routing, "_preferred", ["bad"])
    assert hook((routing.HOST, 443), 5) == "sock" and calls == ["bad", routing.HOST]          # all failed: plain DNS
