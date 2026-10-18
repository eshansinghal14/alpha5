"""Settled real PnL for the per-event log line."""
import asyncio

import pytest

from kalshi_infra.runtime import session


def test_settled_pnl_pays_the_winning_side_net_of_cost_and_fees():
    pos = {"E-ETH": (40.0, 0.0, 28.0, 0.0, 0.10),        # long 40 yes, paid $28
           "E-XRP": (0.0, 30.0, 0.0, 23.0, 0.0)}         # long 30 no, paid $23
    assert session.settled_pnl(pos, {"E-ETH": "no", "E-XRP": "yes"}) == pytest.approx(-51.10)
    assert session.settled_pnl(pos, {"E-ETH": "yes", "E-XRP": "no"}) == pytest.approx(40 - 28 + 30 - 23 - 0.10)


def test_settled_pnl_waits_for_every_result():
    pos = {"E-ETH": (1.0, 0.0, 0.5, 0.0, 0.0), "E-XRP": (0.0, 1.0, 0.0, 0.5, 0.0)}
    assert session.settled_pnl(pos, {"E-ETH": "yes"}) is None
    assert session.settled_pnl(pos, {"E-ETH": "yes", "E-XRP": ""}) is None


def test_log_settled_retries_until_published(monkeypatch):
    calls = {"n": 0}

    def fake_get(path, max_retries=2):
        calls["n"] += 1
        done = calls["n"] > 2
        return {"market": {"status": "finalized" if done else "closed", "result": "no" if done else ""}}

    import kalshi_infra.api_functions as af
    monkeypatch.setattr(af, "kalshi_get", fake_get)
    monkeypatch.setattr(session, "SETTLE_POLL_S", (0, 0, 0))
    logged = []
    monkeypatch.setattr(session, "_log", lambda s, m: logged.append(m))
    asyncio.run(session._log_settled("S", "E", {"E-ETH": (2.0, 0.0, 1.2, 0.0, 0.0)}, 0.5))
    assert len(logged) == 1 and "settled -1.20" in logged[0] and "marked at disarm +0.50" in logged[0]
