"""CME futures as a shadow signal: front-month roll, Databento record
handling, its solo move trigger, and that it never touches pricing."""
import datetime as dt
import math
import types

import pytest

from kalshi_strategies.KXCRYPTOLEAD15M import perp_nowcast as pn
from kalshi_strategies.KXCRYPTOLEAD15M.prints import LeadTrigger
from kalshi_strategies.KXCRYPTOLEAD15M.series import COINS
from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venues import CmeFuturesAdapter, cme_expiry, cme_front

UNDEF = 2**63 - 1


def _ts(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()


def test_expiry_is_the_last_friday_at_16_utc():
    assert cme_expiry(2026, 10) == _ts("2026-10-30T16:00")
    assert cme_expiry(2026, 11) == _ts("2026-11-27T16:00")


def test_front_month_rolls_five_days_before_expiry():
    assert cme_front("MBT", _ts("2026-11-01T20:00")) == "MBTX6"
    assert cme_front("MBT", _ts("2026-11-22T15:59")) == "MBTX6"
    assert cme_front("MBT", _ts("2026-11-22T16:00")) == "MBTZ6"
    assert cme_front("SOL", _ts("2026-12-25T00:00")) == "SOLF7"


def _mapping(iid, sym):
    return types.SimpleNamespace(instrument_id=iid, stype_in_symbol=sym, stype_out_symbol=sym)


def _mbp(iid, action="A", side="N", price=UNDEF, size=0, bid=81000.0, ask=81005.0, bsz=3, asz=4, ts=1.5e18):
    lv = types.SimpleNamespace(bid_px=UNDEF if bid is None else int(bid * 1e9), ask_px=int(ask * 1e9),
                               bid_sz=bsz, ask_sz=asz)
    return types.SimpleNamespace(instrument_id=iid, action=action, side=side, price=price, size=size,
                                 sequence=7, ts_event=int(ts), levels=[lv])


def test_records_become_coin_unit_books_and_prints():
    ad = CmeFuturesAdapter(COINS)
    assert ad.coins == ["BTC", "ETH", "SOL", "XRP"]                       # no HYPE contract
    books, prints, ids = [], [], {}
    ad.on_print = lambda coin, t, as_of, exch, kind: prints.append((coin, t, kind))
    subs = {"MBTX6": "BTC"}
    ad._record(_mapping(11, "MBTX6"), ids, subs, 1.0, books.append)
    ad._record(_mbp(99), ids, subs, 1.0, books.append)                  # not subscribed: ignored
    ad._record(_mbp(11), ids, subs, 1.0, books.append)
    vb = books[-1]
    assert (vb.venue, vb.coin) == ("cme", "BTC")
    assert vb.best_bid() == pytest.approx(81000.0) and vb.bids[0].size == pytest.approx(0.3)   # 3 MBT = 0.3 BTC
    ad._record(_mbp(11, action="T", side="B", price=int(81005 * 1e9), size=2), ids, subs, 2.0, books.append)
    coin, t, kind = prints[-1]
    assert (coin, kind, t["side"]) == ("BTC", "cme", "BUY") and t["size"] == pytest.approx(0.2)
    n = len(books)
    ad._record(_mbp(11, bid=None), ids, subs, 3.0, books.append)        # one-sided book: no update
    assert len(books) == n


def test_direct_mode_splits_books_and_prints_between_the_two_passes():
    # Reader thread: books only. Loop: prints only. Each pass maps ids itself.
    ad = CmeFuturesAdapter(COINS)
    books, prints, tids, lids = [], [], {}, {}
    ad.on_print = lambda coin, t, as_of, exch, kind: prints.append(coin)
    subs = {"MBTX6": "BTC"}
    trade = _mbp(11, action="T", side="B", price=int(81005 * 1e9), size=2, bid=81001.0)
    for r in (_mapping(11, "MBTX6"), _mbp(11), trade):
        ad._record(r, tids, subs, 1.0, books.append, prints=False)
        ad._record(r, lids, subs, 1.0, books.append, books=False)
    assert [vb.best_bid() for vb in books] == [pytest.approx(81000.0), pytest.approx(81001.0)]
    assert prints == ["BTC"]
    err = types.SimpleNamespace(err="boom")
    assert ad._record(err, tids, subs, 1.0, books.append, prints=False) is None
    with pytest.raises(ConnectionError):
        ad._record(err, lids, subs, 1.0, books.append, books=False)


def test_cme_trips_alone_and_stays_out_of_the_perp_vote():
    lt = LeadTrigger(COINS, ("binance_perp", "okx_perp"), solo=("cme",))
    lt.configure(move_thr={"cme": {"BTC": 2.0}, "binance_perp": {"BTC": 2.0}, "okx_perp": {"BTC": 2.0}})
    assert lt.on_print("cme", "BTC", 81000.0, 0.1, 10.0) == []
    rows = lt.on_print("cme", "BTC", 81000.0 * math.exp(3e-4), 0.1, 10.6)   # +3bp over 0.5s
    assert [(r[0], r[1], r[2], r[-1]) for r in rows] == [("move", "cme_move", "BTC", True)]
    assert lt._agrees("BTC", 10.6) == []                                 # the perps haven't moved


def test_cme_is_tracked_for_logging_but_never_in_the_nowcast():
    n = pn.PerpNowcast()
    for i in range(30):
        n.on_spot("BTC", 81000.0)
        n.on_perp("cme", "BTC", 81400.0, i * 0.1)                       # futures ~49bp over spot
    gap, age = n.venue_gap("cme", "BTC", 3.0, 81000.0)
    assert gap == pytest.approx(0.0, abs=1e-6) and age == pytest.approx(0.1)
    n.on_perp("cme", "BTC", 81400.0 * math.exp(2e-4), 3.0)               # CME +2bp, spot not yet
    gap, _ = n.venue_gap("cme", "BTC", 3.0, 81000.0)
    assert gap == pytest.approx(2.0 * 29 / 30, rel=0.05)
    assert n.nowcast("BTC", 3.0, 81000.0) == (81000.0, None, 0)          # no PERPS data: spot unchanged


def _cme(n, px, t, half_spread=2.5):
    n.on_perp("cme", "BTC", px, t, px - half_spread, px + half_spread)


def _leads_setup():
    n = pn.PerpNowcast()
    n.leads_fn = lambda: ("cme", "binance_perp")
    _cme(n, 81400.0, 0.0)
    n.on_perp("binance_perp", "BTC", 81050.0, 0.0)
    n.on_spot("BTC", 81000.0)                                   # both leads referenced at this spot move
    return n


def test_cme_prices_half_its_whole_touch_shift_and_takes_all_of_it():
    n = _leads_setup()
    _cme(n, 81400.0 * math.exp(1e-4), 0.5)                      # bid and ask both +1bp
    n.on_perp("binance_perp", "BTC", 81050.0, 0.5)
    _, inc = n.increment("BTC", 0.6, 81000.0)
    assert inc == pytest.approx(0.5, rel=1e-3) and n.lead_used["BTC"] == "cme"
    _, full = n.increment("BTC", 0.6, 81000.0, leads=("cme",), full=True)
    assert full == pytest.approx(1.0, rel=1e-3) and n.lead_used["BTC"] == "cme"


def test_one_sided_cme_change_is_no_move():
    n = _leads_setup()
    n.on_perp("cme", "BTC", 81402.5, 0.5, 81397.5, 81407.5)     # only the ask ticked up
    assert n.increment("BTC", 0.6, 81000.0)[1] == 0.0
    n.on_perp("cme", "BTC", 81405.0, 0.6, 81402.5, 81407.5)     # both up: the smaller side counts
    _, full = n.increment("BTC", 0.7, 81000.0, full=True)
    assert full == pytest.approx(math.log(81407.5 / 81402.5) * 1e4, rel=1e-3)


def test_increment_falls_back_to_binance_when_cme_is_stale():
    n = _leads_setup()
    n.on_perp("binance_perp", "BTC", 81050.0, 0.5 + pn.FRESH_S)
    _, inc = n.increment("BTC", 0.6 + pn.FRESH_S, 81000.0)      # CME book now stale: Binance leads
    assert inc == pytest.approx(0.0) and n.lead_used["BTC"] == "binance_perp"


def test_cme_increment_resets_on_a_spot_move():
    n = _leads_setup()
    _cme(n, 81400.0 * math.exp(1e-4), 0.5)
    n.on_spot("BTC", 81000.0 * math.exp(1e-4))                  # spot catches up
    assert n.increment("BTC", 0.6, 81000.0 * math.exp(1e-4))[1] == pytest.approx(0.0)


def test_explicit_leads_do_not_overwrite_the_recorded_lead():
    n = _leads_setup()
    n.increment("BTC", 0.1, 81000.0)
    n.increment("BTC", 0.1, 81000.0, leads=("binance_perp",))   # logging call
    assert n.lead_used["BTC"] == "cme"


def test_touch_shift_holds_through_spot_ticks():
    n = _leads_setup()
    _cme(n, 81400.0 * math.exp(1e-4), 0.10)                     # whole touch +1bp
    n.on_spot("BTC", 81001.0)                                   # spot ticks: increment resets...
    assert n.increment("BTC", 0.11, 81001.0, leads=("cme",))[1] == 0.0
    assert n.touch_shift("cme", "BTC", 0.11, 0.3) == pytest.approx(1.0, rel=1e-3)   # ...the shift doesn't
    n.on_perp("cme", "BTC", 81400.0 * math.exp(1e-4), 0.12, 81397.5 * math.exp(1e-4), 81402.5 * math.exp(2e-4))
    assert n.touch_shift("cme", "BTC", 0.12, 0.3) == pytest.approx(1.0, rel=1e-3)   # smaller side counts
    assert n.touch_shift("cme", "BTC", 0.12 + pn.FRESH_S + 0.1, 0.3) == 0.0           # stale book: nothing
