"""Saved burst thresholds: reused after a quick restart, ignored when stale."""
import numpy as np

from kalshi_strategies.KXCRYPTOLEAD15M.prints import (MIN_CALIB_S, SAMPLE_S, BurstCalibrator, load_thresholds,
                                                       save_thresholds)
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS


def test_roundtrip_and_age_cutoff(tmp_path):
    p = tmp_path / "th.json"
    save_thresholds({"hyperliquid": {"XRP": 150000.0, "BTC": 800000.0}}, path=p, now=1000.0)
    assert load_thresholds(path=p, now=1100.0, max_age_s=1800) == {"hyperliquid": {"XRP": 150000.0, "BTC": 800000.0}}
    assert load_thresholds(path=p, now=1000.0 + 1801, max_age_s=1800) == {}
    assert load_thresholds(path=p, now=900.0, max_age_s=1800) == {}       # written "in the future"


def test_merge_keeps_other_sources(tmp_path):
    p = tmp_path / "th.json"
    save_thresholds({"hyperliquid": {"XRP": 1.0}}, path=p, now=1000.0)
    save_thresholds({"coinbase": {"XRP": 2.0}}, path=p, now=4000.0)
    got = load_thresholds(path=p, now=4000.0, max_age_s=1800)
    assert got == {"coinbase": {"XRP": 2.0}}                               # hyperliquid now too old
    got = load_thresholds(path=p, now=4000.0, max_age_s=5000)
    assert got == {"coinbase": {"XRP": 2.0}, "hyperliquid": {"XRP": 1.0}}


def test_missing_or_corrupt_file_is_empty(tmp_path):
    assert load_thresholds(path=tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_thresholds(path=bad) == {}


def test_calibrator_prefers_live_then_saved_then_fallback():
    cal = BurstCalibrator(COINS, {"XRP": 5000.0})
    assert cal.threshold("XRP") == 5000.0 and cal.source == "fallback"
    cal.saved = {"XRP": 150000.0}
    assert cal.threshold("XRP") == 150000.0 and cal.source == "saved"
    for _ in range(int(MIN_CALIB_S / SAMPLE_S)):
        cal.sample({c: 1.0 for c in COINS} | {"XRP": 90000.0})
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert cal.ready and cal.source == "live"
    assert np.isclose(cal.threshold("XRP"), 90000.0)
