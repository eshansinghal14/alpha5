"""Venue-process burst triggers (LeadTrigger) and their path into SpotFeed."""
import time

import pytest

from kalshi_strategies.KXCRYPTOLEAD15M.prints import MOVE_SOURCE, TRIP_RESEND_S, LeadTrigger, PriceTrail
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config

PERPS = ("binance_perp", "bybit_perp", "okx_perp")


def test_price_trail_needs_a_full_window():
    tr = PriceTrail(0.5)
    tr.add("k", 10.0, 100.0)
    assert tr.move_bp("k", 10.3) is None              # no price from 0.5s ago yet
    tr.add("k", 10.6, 100.1)
    assert tr.move_bp("k", 10.6) == pytest.approx(1e4 * 0.000999500, rel=1e-3)
    tr.add("k", 11.5, 100.2)                          # 10.0 and 10.6 are both older than 11.0 now
    assert tr.move_bp("k", 11.5) == pytest.approx(1e4 * 0.000998503, rel=1e-3)


def test_volume_trip_onset_then_resends_while_over():
    lt = LeadTrigger(COINS, PERPS)
    lt.configure(vol_thr={"binance_perp": {"BTC": 1000.0}})
    assert lt.on_print("binance_perp", "BTC", 80000.0, 0.005, 1.00) == []          # $400
    rows = lt.on_print("binance_perp", "BTC", 80000.0, 0.01, 1.01)               # $1200 in 1s
    assert [(r[0], r[1], r[2], r[-1]) for r in rows] == [("volume", "binance_perp", "BTC", True)]
    assert lt.on_print("binance_perp", "BTC", 80000.0, 0.001, 1.02) == []          # still over, < resend gap
    rows = lt.on_print("binance_perp", "BTC", 80000.0, 0.001, 1.02 + TRIP_RESEND_S)
    assert len(rows) == 1 and rows[0][-1] is False                                 # resend, not an onset


def test_no_threshold_is_record_only():
    lt = LeadTrigger(COINS, PERPS)
    assert lt.on_print("okx_perp", "ETH", 2500.0, 1000.0, 1.0) == []


def test_hyperliquid_perp_and_spot_share_a_window():
    lt = LeadTrigger(COINS, PERPS)
    lt.configure(vol_thr={"hyperliquid": {"HYPE": 1000.0}})
    assert lt.on_print("hl_perp", "HYPE", 40.0, 15.0, 1.0) == []          # $600
    rows = lt.on_print("hl_spot", "HYPE", 40.0, 15.0, 1.2)               # $1200 across both
    assert rows and rows[0][1] == "hyperliquid"


def _move(lt, venue, coin, t0, p0, p1):
    lt.on_print(venue, coin, p0, 0.0, t0)
    return lt.on_print(venue, coin, p1, 0.0, t0 + 0.6)


def test_move_trigger_needs_two_venues_agreeing():
    lt = LeadTrigger(COINS, PERPS)
    lt.configure(move_thr={v: {"BTC": 3.0} for v in PERPS})
    assert _move(lt, "binance_perp", "BTC", 1.0, 80000.0, 80040.0) == []      # +5bp, one venue
    rows = _move(lt, "okx_perp", "BTC", 1.0, 80000.0, 80040.0)                 # second venue agrees
    assert [(r[0], r[1], r[2], r[-1]) for r in rows] == [("move", MOVE_SOURCE, "BTC", True)]
    assert rows[0][3] == pytest.approx(5.0, rel=0.01) and rows[0][4] == 3.0


def test_move_trigger_opposite_directions_do_not_agree():
    lt = LeadTrigger(COINS, PERPS)
    lt.configure(move_thr={v: {"ETH": 3.0} for v in PERPS})
    _move(lt, "binance_perp", "ETH", 1.0, 2500.0, 2502.0)                       # +8bp
    assert _move(lt, "okx_perp", "ETH", 1.0, 2500.0, 2498.0) == []              # -8bp


def test_move_trigger_off_without_thresholds():
    lt = LeadTrigger(COINS, PERPS)
    _move(lt, "binance_perp", "SOL", 1.0, 110.0, 111.0)
    assert _move(lt, "okx_perp", "SOL", 1.0, 110.0, 111.0) == []


def _spot(**pricing):
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    spot = SpotFeed(config(recovery_s=0.0, **pricing), persist=False, venues=False)
    spot.trips.persist = True
    return spot


def test_on_trip_pulls_and_logs_the_onset_only():
    spot = _spot()
    mono = time.monotonic()
    spot._on_trip("move", MOVE_SOURCE, "XRP", 6.0, 4.0, True, mono, None)
    assert spot.burst_until == pytest.approx(mono + spot.cfg.pricing.burst_hold_s) and spot.n_bursts == 1
    spot._on_trip("move", MOVE_SOURCE, "XRP", 6.5, 4.0, False, mono + 0.05, None)   # resend extends the pull
    assert spot.burst_until == pytest.approx(mono + 0.05 + spot.cfg.pricing.burst_hold_s) and spot.n_bursts == 1
    rows = spot.trips.drain()
    assert len(rows) == 1 and rows[0]["kind"] == "move" and rows[0]["ratio"] == pytest.approx(1.5)


class _Hooked:
    """A model attached to the feed with a pull hook (see SpotFeed._pull)."""

    def __init__(self, spot, block):
        self.cfg, self.spot, self.block = spot.cfg, spot, block
        self.pulled, self.woke = 0, 0

    def quote_block(self, ticker, now_s):
        return "spot_burst" if self.block and time.monotonic() < self.spot.burst_until else None

    def pull_if_blocked(self):
        if self.quote_block(None, time.time()):
            self.pulled += 1

    def _mid_moved(self):
        self.woke += 1


def test_pulling_trip_pulls_through_the_hook_before_the_reprice_wake():
    spot = _spot()
    pulls, quiet = _Hooked(spot, True), _Hooked(spot, False)
    spot.models += [pulls, quiet]
    spot._on_trip("move", MOVE_SOURCE, "XRP", 6.0, 4.0, True, time.monotonic(), None)
    assert pulls.pulled == 1 and pulls.woke == 1
    assert quiet.pulled == 0 and quiet.woke == 1          # its quote_block says no


def test_on_trip_respects_burst_pull_coins():
    spot = _spot()
    mono = time.monotonic()
    spot._on_trip("volume", "hyperliquid", "BTC", 2e5, 1e5, True, mono, mono)       # HL pulls HYPE only
    assert spot.burst_until == 0.0
    r = spot.trips.drain()[0]
    assert r["pulls"] is False and r["source"] == "hyperliquid"


def test_venue_thresholds_leave_out_too_hot_and_coinbase():
    spot = _spot()
    spot.calib["binance_perp"].live = {"BTC": 1e6}
    spot.calib["binance_perp"]._n = spot.calib["binance_perp"]._buf.shape[1]     # ready
    thr = spot._venue_thresholds()
    assert "coinbase" not in thr and thr["binance_perp"]["BTC"] == 1e6
    assert "BTC" in thr["hyperliquid"]          # static HL fallback (sent, unused: no HL adapter runs)


def test_hyperliquid_not_in_the_venue_plan():
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import venue_plan
    plan = venue_plan()
    assert "hyperliquid" not in plan and "bybit_perp" not in plan
    assert all(v in plan for v in ("binance_perp", "okx_perp"))


def test_perp_bbo_with_unchanged_touch_is_dropped_but_kept_alive():
    from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed import venues as vn
    ad = vn.BinancePerpAdapter(["BTC"])
    out = []
    ad._bbo("BTC", "100.0", "1", "100.1", "2", 1.00, None, out.append)
    ad._bbo("BTC", "100.0", "5", "100.1", "2", 1.05, None, out.append)            # size only: dropped
    assert len(out) == 1 and ad.n_bbo_dropped == 1
    ad._bbo("BTC", "100.0", "5", "100.1", "2", 1.00 + vn.PERP_SAME_TOUCH_S + 0.01, None, out.append)  # keep-alive
    ad._bbo("BTC", "100.05", "5", "100.1", "2", 1.30, None, out.append)           # touch moved
    assert len(out) == 3 and out[-1].best_bid() == 100.05
