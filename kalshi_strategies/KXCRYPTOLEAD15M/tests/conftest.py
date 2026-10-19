import dataclasses

from kalshi_infra.config import load
from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig


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
