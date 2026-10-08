"""
Real-trading cutoff X minutes before expiry: disarms REAL orders only,
paper trading continues, and a dashboard re-arm inside the window is
ignored until the next event.
"""
import asyncio
import json
import os
import sys
import time
import types

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_PROJECT_ROOT,
           os.path.join(_PROJECT_ROOT, "kalshi_backtest"),
           os.path.join(_PROJECT_ROOT, "kalshi_latency_arbs", "live"),
           os.path.join(_PROJECT_ROOT, "kalshi_infra")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kalshi_crypto.live.run_live as rl
from kalshi_crypto.config import CoinRaceConfig, EDITABLE_FIELDS


class _Engine:
    def __init__(self):
        self.live_trading = True
        self.calls = []
    def set_live_trading(self, on):
        self.calls.append(on)
        self.live_trading = bool(on)
        return self.live_trading


def _strategy(tau_s, cutoff_min):
    cfg = CoinRaceConfig(real_trading_cutoff_min=cutoff_min)
    es = types.SimpleNamespace(window=types.SimpleNamespace(T=time.time() + tau_s))
    return types.SimpleNamespace(config=cfg, event_state=es, real_cutoff_hit=False)


def _run_once(engine, strat, tmp_path, monkeypatch, armed_file=None):
    path = tmp_path / "trading_mode.json"
    if armed_file is not None:
        path.write_text(json.dumps({"live_trading": armed_file}))
    monkeypatch.setattr(rl, "_trading_mode_path", lambda: path)
    monkeypatch.setattr(rl, "TRADING_MODE_POLL_INTERVAL_S", 0.01)

    async def go():
        t = asyncio.create_task(rl.trading_mode_loop(engine, strat))
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())


def test_field_is_dashboard_editable():
    assert "real_trading_cutoff_min" in EDITABLE_FIELDS
    assert CoinRaceConfig().real_trading_cutoff_min == 0.0


def test_disarms_real_inside_cutoff(tmp_path, monkeypatch):
    eng, strat = _Engine(), _strategy(tau_s=90, cutoff_min=2.0)
    _run_once(eng, strat, tmp_path, monkeypatch)
    assert eng.live_trading is False and strat.real_cutoff_hit


def test_leaves_real_armed_outside_cutoff(tmp_path, monkeypatch):
    eng, strat = _Engine(), _strategy(tau_s=600, cutoff_min=2.0)
    _run_once(eng, strat, tmp_path, monkeypatch)
    assert eng.live_trading is True and not strat.real_cutoff_hit


def test_zero_cutoff_never_disarms(tmp_path, monkeypatch):
    eng, strat = _Engine(), _strategy(tau_s=5, cutoff_min=0.0)
    _run_once(eng, strat, tmp_path, monkeypatch)
    assert eng.live_trading is True


def test_dashboard_rearm_ignored_inside_cutoff(tmp_path, monkeypatch):
    eng, strat = _Engine(), _strategy(tau_s=90, cutoff_min=2.0)
    _run_once(eng, strat, tmp_path, monkeypatch, armed_file=True)
    assert eng.live_trading is False
    assert True not in eng.calls[1:]


def test_dashboard_values_become_restart_defaults(tmp_path, monkeypatch):
    """A dashboard Apply (config_override.json) is the starting config on
    the next run_live start; non-editable keys are ignored and the
    widget's 0 for max_improve_ticks means uncapped (None)."""
    path = tmp_path / "config_override.json"
    path.write_text(json.dumps({"constant_edge": 0.03, "quote_size": 5.0,
                                "real_trading_cutoff_min": 2.0, "max_improve_ticks": 0,
                                "mc_n": 7}))
    monkeypatch.setattr(rl, "_config_override_path", lambda: path)
    cfg = rl.load_saved_config()
    assert cfg.constant_edge == 0.03 and cfg.quote_size == 5.0
    assert cfg.real_trading_cutoff_min == 2.0
    assert cfg.max_improve_ticks is None
    assert cfg.mc_n == CoinRaceConfig().mc_n          # not editable -> untouched


def test_no_saved_file_gives_dataclass_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "_config_override_path", lambda: tmp_path / "missing.json")
    assert rl.load_saved_config() == CoinRaceConfig()
