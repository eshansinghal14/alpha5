"""
Coinbase _ProductBook self-healing tests.

The 2026-10-06 bug: a dropped `new_quantity=0` removal (seq gap) leaves a
stale level on the wrong side of the touch. Because level2 sizes are
ABSOLUTE, the phantom never clears on its own and crosses the book,
blanking the composite for minutes. `to_venue_book` must prune it on
read so the emitted VenueBook is always uncrossed.
"""
import os
import sys

import pytest

from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.coinbase import (CoinbaseAdapter, _ProductBook,
                                                                  MAX_BOOK_LEVELS)


def _emit(pb):
    return pb.to_venue_book("coinbase", "BTC-USD", "BTC")


def test_clean_book_passes_through():
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.0, 5), (99.9, 3)], asks=[(100.1, 4), (100.2, 2)])
    pb.last_receive_ts = 1.0
    vb = _emit(pb)
    assert vb.best_bid() == 100.0
    assert vb.best_ask() == 100.1
    assert not vb.is_crossed()


def test_phantom_bid_above_ask_is_pruned():
    """Market moved up; a stale bid at 100.30 should have been removed
    but its removal was dropped. Best ask is 100.10. The phantom bid
    crosses -- prune it so the emitted book is clean."""
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.00, 5), (99.95, 3)],
                      asks=[(100.10, 4), (100.20, 2)])
    # Inject the phantom (simulating a missed removal).
    pb.apply_change("bid", 100.30, 2.0)   # 100.30 > best ask 100.10
    pb.last_receive_ts = 1.0
    vb = _emit(pb)
    assert not vb.is_crossed()
    assert vb.best_bid() == 100.00       # phantom gone, real bid restored
    assert vb.best_ask() == 100.10


def test_phantom_ask_below_bid_is_pruned():
    """Symmetric: a stale ask below the best bid."""
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.00, 5), (99.95, 3)],
                      asks=[(100.10, 4), (100.20, 2)])
    pb.apply_change("ask", 99.90, 2.0)    # 99.90 < best bid 100.00
    pb.last_receive_ts = 1.0
    vb = _emit(pb)
    assert not vb.is_crossed()
    assert vb.best_ask() == 100.10
    assert vb.best_bid() == 100.00


def test_raw_dict_retains_pruned_level_for_future_update():
    """Pruning is read-time only; the raw dict keeps the level so a
    genuine later update at that price still lands (and would then be
    emitted if it's no longer crossing)."""
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.00, 5)], asks=[(100.10, 4)])
    pb.apply_change("bid", 100.30, 2.0)   # phantom
    pb.last_receive_ts = 1.0
    _emit(pb)                             # prunes in the emitted view
    # Raw dict still has it...
    assert 100.30 in pb.bids
    # ...and when the market genuinely moves asks up past it, it becomes
    # a legitimate best bid and is emitted.
    pb.apply_change("ask", 100.10, 0.0)   # remove old ask
    pb.apply_change("ask", 100.40, 4.0)   # new ask above the 100.30 bid
    vb = _emit(pb)
    assert vb.best_bid() == 100.30
    assert vb.best_ask() == 100.40
    assert not vb.is_crossed()


def test_emitted_book_is_depth_capped_but_keeps_the_touch():
    """Coinbase ships the whole book (24k levels on BTC); emitting and
    depth-weighting all of it per message pegged the event loop (~180 ms
    on a 42k-level book at ~13 msg/s), which was the real cause of the
    DOWN-flapping 'no decisions' bug. to_venue_book caps the emitted view
    at MAX_BOOK_LEVELS per side -- keeping the BEST levels (the touch and
    the CF utilized-depth region), dropping only deep levels that never
    enter the composite. The raw dict is untouched."""
    pb = _ProductBook()
    n = MAX_BOOK_LEVELS + 500
    # Descending bids from 100.00, ascending asks from 100.10.
    bids = [(100.00 - 0.01 * i, 1.0) for i in range(n)]
    asks = [(100.10 + 0.01 * i, 1.0) for i in range(n)]
    pb.apply_snapshot(bids=bids, asks=asks)
    pb.last_receive_ts = 1.0
    vb = _emit(pb)
    assert len(vb.bids) == MAX_BOOK_LEVELS
    assert len(vb.asks) == MAX_BOOK_LEVELS
    # The TOUCH is kept (the cap drops the deep tail, not the top).
    assert vb.best_bid() == 100.00
    assert vb.best_ask() == 100.10
    # Levels are sorted (bids descending, asks ascending).
    assert vb.bids[0].price > vb.bids[-1].price
    assert vb.asks[0].price < vb.asks[-1].price
    # Raw dict keeps every level for future updates.
    assert len(pb.bids) == n and len(pb.asks) == n


def test_any_message_freshens_every_synced_book():
    """A quiet product's book is current as long as the shared connection
    is delivering; the view the composite holds must see that too."""
    ad = CoinbaseAdapter(coins=["BTC", "HYPE"])
    for c in ("BTC", "HYPE"):
        ad._books[c].apply_snapshot(bids=[(1.0, 1)], asks=[(1.1, 1)])
        ad._books[c].last_receive_ts = 1.0
    held = ad.latest("HYPE")
    ad._books["BTC"].is_resyncing = True
    ad._mark_fresh(9.0)
    assert ad._books["HYPE"].last_receive_ts == 9.0 and held.receive_ts == 9.0
    assert ad._books["BTC"].last_receive_ts == 1.0   # resyncing: not vouched for


def test_print_through_the_touch_moves_mid_but_not_book_mid():
    """The held print is a pricing signal only: bars (and so the settlement
    averages) are built from book_mid."""
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.0, 5)], asks=[(100.1, 4)])
    pb.last_receive_ts = 1.0
    pb.apply_trade(100.5, now=1.0)
    vb = _emit(pb)
    assert vb.mid() == 100.5
    assert abs(vb.book_mid() - 100.05) < 1e-12


def test_held_print_clears_when_book_catches_up():
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.0, 5)], asks=[(100.1, 4)])
    pb.apply_trade(100.5, now=1.0)
    pb.apply_change("ask", 100.1, 0.0)
    pb.apply_change("ask", 100.6, 4.0)
    pb.settle_trade(now=1.1)
    assert pb.trade_px is None


def test_every_live_print_reaches_on_print():
    import asyncio
    ad = CoinbaseAdapter(coins=["BTC"])
    ad._books["BTC"].apply_snapshot(bids=[(100.0, 1)], asks=[(100.1, 1)])
    ad._books["BTC"].last_receive_ts = 1.0
    seen = []
    ad.on_print = lambda coin, t, as_of, ts: seen.append((coin, t["trade_id"]))
    ev = {"type": "update", "trades": [{"product_id": "BTC-USD", "trade_id": i, "price": "100.05",
                                        "size": "1", "time": f"2026-10-10T00:00:0{i}Z"} for i in range(3)]}
    asyncio.run(ad._handle_trades(ev, 2.0, lambda vb: None))
    assert seen == [("BTC", 0), ("BTC", 1), ("BTC", 2)]
    asyncio.run(ad._handle_trades({**ev, "type": "snapshot"}, 2.0, lambda vb: None))
    assert len(seen) == 3


def test_exchange_feed_prints_drive_the_held_print_and_at_duplicates_are_dropped():
    """Exchange `match` is the same trade ~55ms sooner: it reaches on_print
    and sets the held print; the Advanced Trade copy of it is ignored."""
    import asyncio
    ad = CoinbaseAdapter(coins=["SOL"])
    pb = ad._books["SOL"]
    pb.apply_snapshot(bids=[(100.0, 1)], asks=[(100.1, 1)])
    pb.last_receive_ts = 1.0
    seen, views = [], []
    ad.on_print = lambda coin, t, as_of, ts: seen.append((coin, str(t["trade_id"]), t["side"]))
    m = {"type": "match", "product_id": "SOL-USD", "trade_id": 7, "price": "100.3", "size": "2",
         "side": "buy", "time": "2026-10-20T02:30:00.000000Z"}
    asyncio.run(ad._ex_match("SOL", m, 2.0, None, views.append))
    assert seen == [("SOL", "7", "BUY")]
    assert views and views[-1].mid() == 100.3                   # held through-the-touch print
    at = {"type": "update", "trades": [{"product_id": "SOL-USD", "trade_id": "7", "price": "100.3",
                                        "size": "2", "side": "BUY", "time": "2026-10-20T02:30:00Z"}]}
    asyncio.run(ad._handle_trades(at, 2.05, views.append))       # same trade, later: dropped
    assert len(seen) == 1
    ad.ex_live = True                                            # while connected, AT prints are ignored
    at2 = {"type": "update", "trades": [{**at["trades"][0], "trade_id": "8"}]}
    asyncio.run(ad._handle_trades(at2, 2.1, views.append))
    assert len(seen) == 1
    ad.ex_live = False                                           # Exchange feed down: AT carries prints again
    asyncio.run(ad._handle_trades(at2, 2.2, views.append))
    assert seen[-1][:2] == ("SOL", "8")


def _ex_ready(ad, coin="SOL", bid=100.0, ask=100.1):
    import time as _t
    ad.ex_book_live = True
    ad._ex_snapshot(coin, {"bids": [[str(bid), "5"]], "asks": [[str(ask), "5"]]}, _t.monotonic())


def test_views_come_from_the_exchange_book_while_synced_then_fall_back():
    import asyncio, time as _t
    ad = CoinbaseAdapter(coins=["SOL"])
    at = ad._books["SOL"]
    at.apply_snapshot(bids=[(99.0, 1)], asks=[(99.2, 1)])
    at.last_receive_ts = _t.monotonic()
    views = []
    _ex_ready(ad)
    asyncio.run(ad._ex_l2update("SOL", {"changes": [["buy", "100.05", "2"]]}, _t.monotonic(), None, views.append))
    assert views and views[-1].best_bid() == 100.05 and views[-1].best_ask() == 100.1
    n = len(views)
    ev = {"product_id": "SOL-USD", "type": "update", "updates": [{"side": "bid", "price_level": "99.1", "new_quantity": "1"}]}
    asyncio.run(ad._handle_event(ev, _t.monotonic(), None, 1, views.append))
    assert len(views) == n                                     # AT kept current, not emitted
    assert at.bids.peekitem(-1)[0] == 99.1
    ad.ex_book_live = False                                    # Exchange feed down: AT is the source again
    asyncio.run(ad._handle_event(ev, _t.monotonic(), None, 2, views.append))
    assert views[-1].best_bid() == 99.1


def test_ticker_touch_outside_the_book_is_held_until_the_book_reaches_it():
    import asyncio, time as _t
    ad = CoinbaseAdapter(coins=["SOL"])
    views = []
    _ex_ready(ad)
    asyncio.run(ad._ex_ticker("SOL", {"best_bid": "100.20", "best_ask": "100.30"}, _t.monotonic(), views.append))
    assert views and views[-1].mid() == pytest.approx(100.25)  # above the book's ask: held
    asyncio.run(ad._ex_l2update("SOL", {"changes": [["sell", "100.1", "0"], ["sell", "100.3", "5"],
                                                    ["buy", "100.2", "5"]]}, _t.monotonic(), None, views.append))
    assert views[-1].trade_px is None and views[-1].mid() == pytest.approx(100.25)


def test_exchange_feed_offsets_dont_age_the_book_feed():
    ad = CoinbaseAdapter(coins=["SOL"])
    import time as _t
    now = _t.time()
    ad._data_age(now - 0.010, 1.0, "ex")
    assert ad._data_age(now - 0.070, 1.0, "at") == 0.0           # its own minimum, not the Exchange one


def test_custom_max_levels_param():
    pb = _ProductBook()
    pb.apply_snapshot(bids=[(100.0 - 0.01 * i, 1.0) for i in range(20)],
                      asks=[(100.1 + 0.01 * i, 1.0) for i in range(20)])
    pb.last_receive_ts = 1.0
    vb = pb.to_venue_book("coinbase", "BTC-USD", "BTC", max_levels=5)
    assert len(vb.bids) == 5 and len(vb.asks) == 5
    assert vb.best_bid() == 100.0 and vb.best_ask() == 100.1
