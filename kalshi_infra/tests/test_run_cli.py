import argparse

import pytest

from kalshi_strategies import run


@pytest.fixture(scope="module")
def fmap():
    return run.folders()


def _resolve(fmap, series=None, only=None):
    return run.resolve(argparse.ArgumentParser(), series, only, fmap)


def test_folder_expands_to_all_its_series_and_hub(fmap):
    s, svc = _resolve(fmap, ["KXINX"])
    assert s == ["KXINX", "KXINXU", "KXINXDUD"]
    assert [x.name for x in svc] == ["spx-hub"]


def test_only_runs_one_series_but_keeps_its_hub(fmap):
    s, svc = _resolve(fmap, only=["KXINXU"])
    assert s == ["KXINXU"] and [x.name for x in svc] == ["spx-hub"]


def test_crypto_has_no_services(fmap):
    s, svc = _resolve(fmap, ["KXCRYPTOLEAD15M"])
    assert s == ["KXCRYPTOLEAD15M"] and svc == []


def test_series_ticker_is_not_a_folder(fmap):
    with pytest.raises(SystemExit):
        _resolve(fmap, ["KXINXU"])


def test_dedup_and_case(fmap):
    s, _ = _resolve(fmap, ["kxinx", "KXINX"], ["KXINXDUD"])
    assert s == ["KXINX", "KXINXU", "KXINXDUD"]


def test_write_group_runs_as_one_process(fmap):
    reg = run.registry()
    s, _ = _resolve(fmap, ["KXINX", "KXCRYPTOLEAD15M"])
    assert run.units_of(s, reg) == [["KXINX", "KXINXU", "KXINXDUD"], ["KXCRYPTOLEAD15M"]]
    assert run.units_of(["KXINXU"], reg) == [["KXINXU"]]
