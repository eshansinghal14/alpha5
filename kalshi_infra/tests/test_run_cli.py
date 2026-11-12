import argparse

import pytest

from kalshi_strategies import run


@pytest.fixture(scope="module")
def fmap():
    return run.folders()


def _resolve(fmap, series=None, only=None):
    return run.resolve(argparse.ArgumentParser(), series, only, fmap)


def test_folder_expands_to_all_its_series(fmap):
    s, svc = _resolve(fmap, ["ARBS"])
    assert s == ["ARBS", "ARBS3"] and svc == []


def test_only_runs_one_series(fmap):
    s, _ = _resolve(fmap, only=["ARBS3"])
    assert s == ["ARBS3"]


def test_crypto_has_no_services(fmap):
    s, svc = _resolve(fmap, ["KXCRYPTOLEAD15M"])
    assert s == ["KXCRYPTOLEAD15M"] and svc == []


def test_series_ticker_is_not_a_folder(fmap):
    with pytest.raises(SystemExit):
        _resolve(fmap, ["ARBS3"])


def test_dedup_and_case(fmap):
    s, _ = _resolve(fmap, ["arbs", "ARBS"])
    assert s == ["ARBS", "ARBS3"]


def test_series_without_a_write_group_run_apart(fmap):
    reg = run.registry()
    s, _ = _resolve(fmap, ["ARBS", "KXCRYPTOLEAD15M"])
    assert run.units_of(s, reg) == [["ARBS"], ["ARBS3"], ["KXCRYPTOLEAD15M"]]
