"""
One KXCRYPTOLEAD15M SpotFeed serving every single-coin series in the process.

SpotFeed attaches one model (the race's five-coin event); here each up/down
series has its own model on its own coin, so venue books and CF ticks wake
only that coin's model.
"""
from __future__ import annotations

from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed


class SharedSpotFeed(SpotFeed):
    def __init__(self, cfg, *, persist: bool = True, venues: bool = True):
        super().__init__(cfg, persist=persist, venues=venues)
        self.models: list = []
        # Monotonic time until which each coin's own bursts pull its quotes.
        self.burst_until_by_coin: dict[str, float] = {}

    def attach(self, model):
        p = model.cfg.pricing
        self.health.soft_stale_s, self.health.hard_stale_s = p.soft_stale_s, p.hard_stale_s
        self.health.recovery_s = p.recovery_s
        if model not in self.models:
            self.models.append(model)

    def detach(self, model):
        if model in self.models:
            self.models.remove(model)

    def _on_cf_tick(self, coin: str, ts_s: int):
        for m in self.models:
            if m.coin == coin:
                m.cf_ticked()

    def _on_book(self, vb):
        before = self.bus.mid_nowcast(vb.coin) if vb.coin in self.bus.feeds else None
        self.bus.observe(vb)
        if self.bus.mid_nowcast(vb.coin) != before:
            for m in self.models:
                if m.coin == vb.coin:
                    m._mid_moved()

    def _pull(self, coin: str, src: str, as_of: float):
        hold = self.cfg.pricing.burst_hold_s
        if as_of >= self.burst_until:
            self.n_bursts += 1
        self.burst_until = max(self.burst_until, as_of + hold)
        self.burst_coin = coin if src == "coinbase" else f"{coin}@{src}"
        self.burst_until_by_coin[coin] = max(self.burst_until_by_coin.get(coin, 0.0), as_of + hold)
        for m in self.models:
            m._mid_moved()
