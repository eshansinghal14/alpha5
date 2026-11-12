import dataclasses

from infra.config import load
from strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig


def config(**pricing_overrides):
    """The real config.json with pricing fields overridden -- tests never
    rely on values hardcoded in Python. vol_source is realized unless a test
    asks for implied: implied withholds every fair until the single-coin 15M
    mids have been read, which these offline tests don't feed."""
    cfg = load(CryptoConfig, CONFIG_PATH)
    cfg.pricing = dataclasses.replace(cfg.pricing, **{"vol_source": "realized", "corr_fit": False,
                                                      **pricing_overrides})
    return cfg


def pricing(**overrides):
    return config(**overrides).pricing


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _cme_open(monkeypatch):
    """CME touches count whatever the wall clock says (cme_closed is tested on its own)."""
    from strategies.KXCRYPTOLEAD15M import perp_nowcast
    monkeypatch.setattr(perp_nowcast, "cme_closed", lambda t: False)
