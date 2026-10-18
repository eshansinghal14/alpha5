"""
What the single-coin series keep per coin across events, on the process's
one SpotFeed (KXCRYPTOLEAD15M/shared.py): the implied-vol EMA and the vol
pull holds. Kept on the feed so an event starts from the last one's state.
"""
from __future__ import annotations

from kalshi_strategies.CRYPTO15M.implied_vol import ImpliedVolEMA


def implied_ema(spot, coin: str) -> ImpliedVolEMA:
    st = spot.coin_state[coin]
    if "implied" not in st:
        st["implied"] = ImpliedVolEMA()
    return st["implied"]


def hold(spot, coin: str, key: str) -> float:
    """Monotonic time until which pull `key` holds for `coin` (0 = none)."""
    return spot.coin_state[coin].get(key, 0.0)


def extend(spot, coin: str, key: str, until: float):
    st = spot.coin_state[coin]
    st[key] = max(st.get(key, 0.0), until)


__all__ = ["implied_ema", "hold", "extend"]
