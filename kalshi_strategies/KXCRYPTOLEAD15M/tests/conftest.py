import dataclasses

from kalshi_infra.config import load
from kalshi_strategies.KXCRYPTOLEAD15M.config import CONFIG_PATH, CryptoConfig


def config(**pricing_overrides):
    """The real config.json with pricing fields overridden -- tests never
    rely on values hardcoded in Python."""
    cfg = load(CryptoConfig, CONFIG_PATH)
    cfg.pricing = dataclasses.replace(cfg.pricing, **pricing_overrides)
    return cfg


def pricing(**overrides):
    return config(**overrides).pricing
