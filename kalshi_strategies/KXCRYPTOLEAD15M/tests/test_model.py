"""CoinRaceModel gates and Fair shape, offline (no network)."""
import math
import time

import pandas as pd

from kalshi_infra.kalshi.markets import EventInfo
from kalshi_strategies.KXCRYPTOLEAD15M.cf_index import STALE_S, CfIndex
from kalshi_strategies.KXCRYPTOLEAD15M.composite import CompositeBar, CompositeBus
from kalshi_strategies.KXCRYPTOLEAD15M.cov_forecaster import ConstantCovForecaster
from kalshi_strategies.KXCRYPTOLEAD15M.model import CoinRaceModel
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.base import OrderBookLevel, VenueBook
from kalshi_strategies.KXCRYPTOLEAD15M.tests.conftest import config

PX = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 1.4, "HYPE": 84.0}


def _event(S, T):
    ms = [{"ticker": f"EV-{c}", "open_time": pd.Timestamp(S, unit="s", tz="UTC").isoformat(),
           "close_time": pd.Timestamp(T, unit="s", tz="UTC").isoformat()} for c in COINS]
    return EventInfo("EV", pd.Timestamp(S, unit="s", tz="UTC"), pd.Timestamp(T, unit="s", tz="UTC"), ms)


def _book(coin, mono, mult=1.0):
    p = PX[coin] * mult
    return VenueBook(venue="coinbase", product=f"{coin}-USD", coin=coin,
                     bids=(OrderBookLevel(p * 0.9999, 5.0),), asks=(OrderBookLevel(p * 1.0001, 5.0),),
                     receive_ts=mono)


def _cf_at(S, now, px=None, mono=None):
    """CF at `px` (default PX) for every second over [S-60, now), live as of `mono`."""
    cf = CfIndex(persist=False)
    mono = time.monotonic() if mono is None else mono
    for c in COINS:
        for t in range(int(S) - 60, int(now)):
            cf.add(c, t, (px or PX)[c], mono=mono)
    return cf


def _bars(m, now, mults=None):
    """Mid bars matching CF's level over the basis window, so basis = 0."""
    for c in COINS:
        f = m.bus.feeds[c]
        for t in range(int(now) - 40, int(now)):
            p = PX[c] * (mults or {}).get(c, 1.0)
            f._bars.append(CompositeBar(c, float(t), p, 1.0, 1, 0, False, p, 1))


def _model(now, **pricing):
    cfg = config(**pricing)
    S = int(now) - 300
    m = CoinRaceModel(_event(S, S + 900), cfg, cf=_cf_at(S, now), bus=CompositeBus(coins=COINS, persist=False),
                      forecaster=ConstantCovForecaster())
    _bars(m, now)
    return m


def _feed(m, mono, mults=None):
    for c in COINS:
        m.bus.observe(_book(c, mono, (mults or {}).get(c, 1.0)))


def test_fairs_sum_to_one_with_band():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic())
    fairs = m.reprice(now)
    assert set(fairs) == set(m.tickers) and all(f is not None for f in fairs.values())
    assert abs(sum(f.prob for f in fairs.values()) - 1.0) < 1e-9
    k = m.cfg.pricing.ci_vol_multiplier
    for f in fairs.values():
        tv = f.extras["theo_vol"]
        assert abs((f.hi - f.prob) - k * tv) < 1e-12 and abs((f.prob - f.lo) - k * tv) < 1e-12


def test_leader_is_priced_up():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic(), {"SOL": 1.01})
    fairs = m.reprice(now)
    assert max(fairs, key=lambda t: fairs[t].prob) == m.ticker_of["SOL"]


def test_down_coin_withholds_every_fair():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    for c in COINS[:-1]:
        m.bus.observe(_book(c, mono))
    fairs = m.reprice(now)
    assert all(f is None for f in fairs.values())
    assert m.withheld_reason.startswith("coin_down")
    assert m.signal_age_s() is None


def test_cross_coin_skew_withholds():
    now = time.time()
    m = _model(now, recovery_s=0.0, max_cross_coin_skew_s=0.5)
    mono = time.monotonic()
    for c in COINS:
        m.bus.observe(_book(c, mono - (2.0 if c == "BTC" else 0.0)))
    fairs = m.reprice(now)
    assert all(f is None for f in fairs.values()) and m.withheld_reason == "cross_coin_skew"


def test_wakeup_only_on_mid_change_and_age_follows_live_books():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    woke = []
    m.set_wakeup(lambda: woke.append(1))
    mono = time.monotonic()
    for c in COINS:
        m._on_book(_book(c, mono - 5.0))
    m.reprice(now)
    n = len(woke)
    m._on_book(_book("BTC", mono))            # same mid: no wakeup
    assert len(woke) == n
    # Fair is current (no mid moved), so its age tracks the books, which a
    # heartbeat freshens in place.
    for vbs in m.bus.venue_books_per_coin().values():
        for vb in vbs:
            vb.receive_ts = time.monotonic()
    assert m.signal_age_s() < 0.5
    m._on_book(_book("BTC", time.monotonic(), 1.01))   # mid moved
    assert len(woke) == n + 1
    assert m.signal_age_s() >= 4.9            # dirty: age of the inputs under the fair


def test_spot_feed_carries_state_across_events():
    """The session feed outlives events: a new event's model sees the same
    forecaster/health, gets wakeups once attached, and its tracker is seeded
    from bars sampled while it was being built."""
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    now = time.time()
    cfg = config(recovery_s=0.0)
    spot = SpotFeed(cfg, persist=False, venues=False)
    spot.forecaster = ConstantCovForecaster()
    spot.forecaster_warmed = True
    a = CoinRaceModel(_event(now - 300, now + 600), cfg, cf=spot.cf, spot=spot)
    spot.attach(a)
    woke = []
    a.set_wakeup(lambda: woke.append("a"))
    mono = time.monotonic()
    for c in COINS:
        spot._on_book(_book(c, mono))
    assert woke and a.forecaster is spot.forecaster and a.health is spot.health
    spot.detach(a)
    b = CoinRaceModel(_event(now - 60, now + 840), cfg, cf=spot.cf, spot=spot)
    spot.attach(b)
    b.set_wakeup(lambda: woke.append("b"))
    spot._on_book(_book("BTC", mono, 1.01))
    assert woke[-1] == "b"
    spot._on_cf_tick("BTC", int(now))          # a CF second wakes the attached event
    assert woke[-1] == "b" and b.tracker.cf is spot.cf


def _last_minute_model(now, mid_mult, cf_mult, **pricing):
    """30s from close. CF start at PX; CF over the 30 elapsed end seconds at
    PX * cf_mult; the mid at PX * mid_mult, with the same offset to CF over the
    basis window as now (so basis = ln(mid_mult / cf_mult))."""
    cfg = config(recovery_s=0.0, **pricing)
    T = int(now) + 30
    S = T - 900
    cf = CfIndex(persist=False)
    mono = time.monotonic()
    for c in COINS:
        for t in range(S - 60, int(now)):
            v = PX[c] * (cf_mult.get(c, 1.0) if t >= T - 60 else 1.0)
            cf.add(c, t, v, mono=mono)
    m = CoinRaceModel(_event(S, T), cfg, cf=cf, bus=CompositeBus(coins=COINS, persist=False),
                      forecaster=ConstantCovForecaster())
    for c in COINS:
        f = m.bus.feeds[c]
        # The basis window [now-30, now) is inside the end average, where CF
        # sits at cf_mult; the mid sits at mid_mult throughout.
        for t in range(int(now) - 40, int(now)):
            p = PX[c] * mid_mult.get(c, 1.0)
            f._bars.append(CompositeBar(c, float(t), p, 1.0, 1, 0, False, p, 1))
    _feed(m, mono, mid_mult)
    return m


def test_last_minute_prices_the_cf_leader_not_the_mid_leader():
    """Mid says HYPE by 1bp; CF (what settles) says BTC by 1bp; the basis
    puts the mid on CF's level, so the fair follows CF: 26OCT092330's shape."""
    now = time.time()
    mid = {"HYPE": 1.0011, "BTC": 1.0010}
    cf = {"HYPE": 1.0010, "BTC": 1.0011}
    m = _last_minute_model(now, mid, cf)
    fairs = m.reprice(now)
    p = {c: fairs[m.ticker_of[c]].prob for c in COINS}
    assert p["BTC"] > p["HYPE"] and p["BTC"] > 0.5
    assert fairs[m.ticker_of["BTC"]].extras["cf_end_n"] == 30


def test_end_seconds_not_yet_in_are_filled_with_x():
    """An elapsed end second whose CF tick hasn't landed counts at x, so the
    fair doesn't jump when it lands (a leader stays the leader)."""
    now = time.time()
    lead = {"SOL": 1.003}
    a = _last_minute_model(now, lead, lead)
    pa = a.reprice(now)[a.ticker_of["SOL"]].prob
    # Same state but the newest 3 end seconds haven't landed yet.
    b = _last_minute_model(now, lead, lead)
    for c in COINS:
        for t in range(int(now) - 3, int(now)):
            b.cf._v[c].pop(t)
        b.cf._newest[c] = int(now) - 4
    pb = b.reprice(now)[b.ticker_of["SOL"]].prob
    assert pa > 0.95 and abs(pa - pb) < 0.02


def test_basis_moves_mid_onto_cf_level():
    """Mid 2bp above CF all window with no real move: x = 0, not +2bp."""
    now = time.time()
    S = int(now) - 300
    cfg = config(recovery_s=0.0)
    m = CoinRaceModel(_event(S, S + 900), cfg, cf=_cf_at(S, now), bus=CompositeBus(coins=COINS, persist=False),
                      forecaster=ConstantCovForecaster())
    _bars(m, now, {"XRP": 1.0002})
    _feed(m, time.monotonic(), {"XRP": 1.0002})
    fairs = m.reprice(now)
    assert abs(m.basis_bp["XRP"] - 2.0) < 1e-3
    flat = _model(now, recovery_s=0.0)
    _feed(flat, time.monotonic())
    ff = flat.reprice(now)
    assert all(abs(fairs[m.ticker_of[c]].prob - ff[flat.ticker_of[c]].prob) < 1e-9 for c in COINS)


def test_no_cf_start_withholds():
    now = time.time()
    S = int(now) - 300
    cf = _cf_at(S, now)
    for t in range(S - 60, S):
        cf._v["ETH"].pop(t, None)
    m = CoinRaceModel(_event(S, S + 900), config(recovery_s=0.0), cf=cf,
                      bus=CompositeBus(coins=COINS, persist=False), forecaster=ConstantCovForecaster())
    _bars(m, now)
    _feed(m, time.monotonic())
    assert all(f is None for f in m.reprice(now).values()) and m.withheld_reason == "no_cf_start"


def test_stale_cf_is_a_stale_signal():
    """A CF feed past STALE_S reads as signal age over any sane
    max_signal_age_s, so the quoter pulls everything (stale_signal)."""
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    _feed(m, mono)
    m.reprice(now)
    assert m.signal_age_s() < 0.5
    for c in COINS:
        m.cf._last_rx_mono[c] = mono - (STALE_S + 1.0)
    assert m.signal_age_s() >= STALE_S + 1.0 - 1e-6
    assert m.signal_age_s() > m.cfg.risk.max_signal_age_s


def test_cf_age_under_threshold_does_not_add_to_signal_age():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    _feed(m, mono)
    m.reprice(now)
    for c in COINS:
        m.cf._last_rx_mono[c] = mono - 0.9            # between ticks: healthy
    assert m.signal_age_s() < 0.5


def test_no_side_blocks():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic())
    fairs = m.reprice(now)
    assert all(m.side_block(t, s, f, now) is None for t, f in fairs.items() for s in ("yes", "no"))


def test_spot_burst_pulls_quotes_for_every_market_then_expires():
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    now = time.time()
    cfg = config(recovery_s=0.0, burst_usd_1s={c: 1000.0 for c in COINS}, burst_hold_s=1.5)
    spot = SpotFeed(cfg, persist=False, venues=False)
    spot.forecaster = ConstantCovForecaster()
    m = CoinRaceModel(_event(now - 300, now + 600), cfg, cf=spot.cf, spot=spot)
    spot.attach(m)
    woke = []
    m.set_wakeup(lambda: woke.append(1))
    mono = time.monotonic()
    spot._on_print("BTC", {"price": "80000", "size": "0.005", "trade_id": 1}, mono, None)   # $400
    assert m.quote_block(m.ticker_of["ETH"], now) is None and not woke
    spot._on_print("BTC", {"price": "80000", "size": "0.01", "trade_id": 2}, mono + 0.2, None)  # $1200 in 1s
    assert woke
    assert all(m.quote_block(t, now) == "spot_burst" for t in m.tickers)
    spot.burst_until = time.monotonic() - 0.01
    assert m.quote_block(m.ticker_of["ETH"], now) is None


def _spot(**pricing):
    from kalshi_strategies.KXCRYPTOLEAD15M.live_feed import SpotFeed
    cfg = config(recovery_s=0.0, **pricing)
    spot = SpotFeed(cfg, persist=False, venues=False)
    spot.trips.persist = True          # buffer rows to drain(); nothing calls write() here
    return spot


def test_log_only_source_trips_are_logged_but_do_not_pull():
    """burst_pull_coins without HL BTC: an HL BTC crossing is a trip row with
    pulls=False and leaves quotes up; HL HYPE still pulls."""
    pull = {"coinbase": list(COINS), "hyperliquid": ["HYPE"]}
    spot = _spot(burst_pull_coins=pull)
    spot.calib["hyperliquid"].fallback = {"BTC": 1000.0, "HYPE": 1000.0}
    mono = time.monotonic()
    spot._on_print("BTC", {"price": "80000", "size": "0.02"}, mono, mono, venue="hl_perp")    # $1600
    assert spot.burst_until == 0.0 and spot.n_bursts == 0
    rows = spot.trips.drain()
    assert len(rows) == 1 and rows[0]["source"] == "hyperliquid" and rows[0]["pulls"] is False
    assert rows[0]["ratio"] >= 1.6 - 1e-9 and rows[0]["thr_source"] == "fallback"
    spot._on_print("HYPE", {"price": "40", "size": "50"}, mono, mono, venue="hl_perp")        # $2000
    assert spot.burst_until > mono and spot.n_bursts == 1
    assert spot.trips.drain()[0]["pulls"] is True


def test_trip_log_records_onsets_only():
    spot = _spot(burst_usd_1s={c: 1000.0 for c in COINS})
    mono = time.monotonic()
    for i in range(3):          # $1200 / $2400 / $3600 in the same second: one onset
        spot._on_print("BTC", {"price": "80000", "size": "0.015", "trade_id": i}, mono + 0.1 * i, None)
    spot._on_print("BTC", {"price": "80000", "size": "0.015", "trade_id": 9}, mono + 1.5, None)  # window reset: $1200
    rows = spot.trips.drain()
    assert [r["coin"] for r in rows] == ["BTC"]
    # Dropped back under, then over again: a new onset.
    spot._on_print("BTC", {"price": "80000", "size": "0.001", "trade_id": 10}, mono + 3.0, None)
    spot._on_print("BTC", {"price": "80000", "size": "0.015", "trade_id": 11}, mono + 3.1, None)
    assert len(spot.trips.drain()) == 1


def test_trip_log_write_roundtrip(tmp_path):
    from kalshi_strategies.KXCRYPTOLEAD15M.prints import TripLog
    log = TripLog(root=tmp_path)
    mono = time.monotonic()
    log.observe("okx_perp", "SOL", 5e5, 2e5, thr_source="live", pulls=True, as_of=mono - 0.08,
                exch_ts=time.time() - 0.2, now_mono=mono)
    assert log.write(log.drain()) == 1
    df = pd.concat(pd.read_parquet(p) for p in tmp_path.rglob("*.parquet"))
    assert df.iloc[0].source == "okx_perp" and abs(df.iloc[0].seen_delay_s - 0.08) < 1e-9
    assert abs(df.iloc[0].ratio - 2.5) < 1e-9


def test_signal_age_is_oldest_coins_newest_book():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    mono = time.monotonic()
    for c in COINS:
        m.bus.observe(_book(c, mono - (0.3 if c == "HYPE" else 0.0)))
    m.reprice(now)
    # (mono - 0.3) then (later - that) rounds to 0.2999999998 sometimes.
    assert m.signal_age_s() >= 0.3 - 1e-6


def test_burst_calibrator_tracks_its_own_quantile_and_falls_back_until_ready():
    import numpy as np
    from kalshi_strategies.KXCRYPTOLEAD15M import prints as P
    cal = P.BurstCalibrator(["BTC"], {"BTC": 123.0})
    assert cal.threshold("BTC") == 123.0                      # nothing sampled: static fallback
    n = int(P.MIN_CALIB_S / P.SAMPLE_S)
    rng = np.random.default_rng(0)
    cal.seed(rng.uniform(0, 1000, size=(n, 1)))
    assert cal.ready
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert 990 < cal.threshold("BTC") < 1000                  # p99.5 of U(0, 1000)
    cal.seed(rng.uniform(0, 10_000, size=(len(cal._buf[0]), 1)))   # volume regime 10x higher
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert cal.threshold("BTC") > 9_000


def test_burst_calibrator_keeps_last_value_when_a_coin_goes_quiet():
    import numpy as np
    from kalshi_strategies.KXCRYPTOLEAD15M import prints as P
    cal = P.BurstCalibrator(["XRP"], None)
    cal.seed(np.full((int(P.MIN_CALIB_S / P.SAMPLE_S), 1), 500.0))
    cal.adopt(cal.quantiles(cal.snapshot()))
    cal.seed(np.zeros((len(cal._buf[0]), 1)))
    cal.adopt(cal.quantiles(cal.snapshot()))
    assert cal.threshold("XRP") == 500.0                      # a zero quantile would trip on any print


def test_mis_scaled_saved_threshold_goes_record_only():
    """A saved $91 threshold against ~$1-5k of 1s notional trips ~every
    sample: ignored (None = no pulls from this source) instead of pulling every
    quote, and a sane one is kept."""
    import numpy as np
    from kalshi_strategies.KXCRYPTOLEAD15M import prints as P
    cal = P.BurstCalibrator(["ETH"], None)
    cal.saved = {"ETH": 91.0}
    assert cal.threshold("ETH") == 91.0                       # no samples yet: can't judge
    rng = np.random.default_rng(1)
    cal.seed(rng.uniform(1_000, 5_000, size=(int(P.SANITY_MIN_S / P.SAMPLE_S), 1)))
    assert cal.threshold("ETH") is None
    cal.saved = {"ETH": 4_990.0}                              # crossed ~0.25% of samples: fine
    assert cal.threshold("ETH") == 4_990.0


def test_detector_level_decays_without_new_prints():
    from kalshi_strategies.KXCRYPTOLEAD15M.prints import BurstDetector
    d = BurstDetector(["BTC"])
    assert d.add("BTC", 1000.0, 10.0) == 1000.0
    assert d.level("BTC", 10.5) == 1000.0
    assert d.level("BTC", 11.01) == 0.0


# --- market-implied shift ---------------------------------------------------

class _Bk:
    def __init__(self, ticker, bid, ask):
        self.ticker, self.bid, self.ask = ticker, bid, ask


def _quote_market(m, probs):
    for c, p in probs.items():
        m.on_book(_Bk(m.ticker_of[c], p - 0.01, p + 0.01))


def _converge(m, now, steps, dt):
    """Reprice forward in time without leaving the in-window phase; spot and
    CF are re-stamped each step so feed health and the basis stay valid."""
    fairs = None
    for k in range(steps):
        t = now + k * dt
        for c in COINS:
            f = m.bus.feeds[c]
            for s in range(int(t) - int(dt) - 1, int(t)):
                m.cf.add(c, s, PX[c], mono=time.monotonic())
                f._bars.append(CompositeBar(c, float(s), PX[c], 1.0, 1, 0, False, PX[c], 1))
        _feed(m, time.monotonic())
        fairs = m._price(t, None)
    return fairs


def test_market_shift_off_is_a_no_op():
    now = time.time()
    a = _model(now, recovery_s=0.0, market_shift_alpha=0.0)
    b = _model(now, recovery_s=0.0, market_shift_alpha=0.0)
    for m in (a, b):
        _feed(m, time.monotonic(), {"SOL": 1.0005})
    _quote_market(a, {"BTC": 0.6, "ETH": 0.1, "SOL": 0.1, "XRP": 0.1, "HYPE": 0.1})
    fa, fb = a.reprice(now), b.reprice(now)
    assert all(abs(fa[t].prob - fb[t].prob) < 1e-12 for t in fa)
    assert not a._shift.any()


def test_market_shift_moves_fair_toward_market_by_alpha():
    now = time.time()
    m = _model(now, recovery_s=0.0, market_shift_alpha=0.25, market_shift_half_life_s=1.0,
               market_shift_fit_s=0.5)
    _feed(m, time.monotonic())
    base = {c: m.reprice(now)[m.ticker_of[c]].prob for c in COINS}
    target = {"BTC": base["BTC"] + 0.15, "ETH": base["ETH"] - 0.05, "SOL": base["SOL"] - 0.05,
              "XRP": base["XRP"] - 0.03, "HYPE": base["HYPE"] - 0.02}
    _quote_market(m, target)
    # S = now - 300 and the end average starts at T - 60 = now + 540: stay before it.
    fairs = _converge(m, now, steps=20, dt=5.0)
    btc = fairs[m.ticker_of["BTC"]].prob
    # Toward the market, but only part of the way (alpha = 0.25).
    assert base["BTC"] < btc < target["BTC"]
    # Centred, then capped per coin: a capped coin leaves a small common offset,
    # which no winner probability depends on.
    assert abs(m._shift).max() <= m.cfg.pricing.market_shift_max_bp * 1e-4 + 1e-12
    assert m.summary_extras()["market_shift_bp"]["BTC"] > 0


def test_market_shift_frozen_in_last_minute():
    now = time.time()
    m = _model(now, recovery_s=0.0, market_shift_alpha=0.25, market_shift_half_life_s=1.0,
               market_shift_fit_s=0.5)
    _feed(m, time.monotonic())
    _quote_market(m, {"BTC": 0.6, "ETH": 0.1, "SOL": 0.1, "XRP": 0.1, "HYPE": 0.1})
    _converge(m, now, steps=20, dt=5.0)
    before = m._shift.copy()
    # Market flips hard inside the end average: the shift must not follow.
    _quote_market(m, {"BTC": 0.05, "ETH": 0.05, "SOL": 0.8, "XRP": 0.05, "HYPE": 0.05})
    for k in range(5):
        m._update_market_shift(m.window.end_avg_begin + 1 + k, None, None, 30.0, None)
    assert (m._shift == before).all()


def test_on_book_empty_market_is_dropped():
    now = time.time()
    m = _model(now, recovery_s=0.0)
    m.on_book(_Bk(m.ticker_of["BTC"], 0.3, 0.4))
    assert abs(m._kmid["BTC"] - 0.35) < 1e-12
    m.on_book(_Bk(m.ticker_of["BTC"], None, 0.05))   # no bid: still a price
    assert abs(m._kmid["BTC"] - 0.025) < 1e-12
    m.on_book(_Bk(m.ticker_of["BTC"], None, None))   # no market at all
    assert "BTC" not in m._kmid



def test_thin_spot_median_is_a_feed_problem_after_a_minute():
    """10-19 07:34-19:18 UTC every coin priced off Coinbase alone (venue
    reader died) and the dashboard said nothing."""
    from kalshi_strategies.KXCRYPTOLEAD15M import model as mod
    now = time.time()
    m = _model(now, recovery_s=0.0)
    _feed(m, time.monotonic())                       # coinbase only, for every coin
    assert "thin" not in (m.summary_extras()["feed_problem"] or "")   # not yet THIN_VENUES_S
    for c in m._thin_since:
        m._thin_since[c] -= mod.THIN_VENUES_S + 1
    fp = m.summary_extras()["feed_problem"]
    assert fp and "BTC 1/6" in fp and "ETH 1/6" in fp and "XRP 1/4" in fp
    assert "SOL" not in fp and "HYPE" not in fp      # Coinbase-only by design
