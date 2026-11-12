import json

from infra.polymarket.recorder import Recorder


def _book(slug, bids, offers):
    lv = lambda L: [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in L]
    return json.dumps({"marketData": {"marketSlug": slug, "bids": lv(bids), "offers": lv(offers),
                                      "transactTime": "2026-11-12T18:00:00Z"}})


def _replay(rows):
    book = {}
    for r in rows:
        if r["snap"]:
            book = {}
        for side, px, q in json.loads(r["chg"]):
            if q:
                book[(side, px)] = q
            else:
                book.pop((side, px), None)
    return book


def test_changes_replay_to_the_last_book_and_repeats_are_dropped():
    r = Recorder()
    r.on_message(_book("m", [(0.40, 10), (0.39, 5)], [(0.42, 7)]), 1)
    r.on_message(_book("m", [(0.40, 10), (0.39, 5)], [(0.42, 7)]), 2)     # stats-only resend
    r.on_message(_book("m", [(0.40, 4)], [(0.42, 7), (0.45, 1)]), 3)
    assert [x["snap"] for x in r.books] == [True, False]
    assert _replay(r.books) == {("b", 0.40): 4.0, ("a", 0.42): 7.0, ("a", 0.45): 1.0}


def test_trade_row():
    r = Recorder()
    r.on_message(json.dumps({"trade": {"marketSlug": "m", "price": {"value": "0.77"}, "quantity": {"value": "31.96"},
                                       "tradeTime": "2026-11-12T18:18:03.299566514Z", "id": "X",
                                       "taker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG",
                                                 "outcomeSide": "OUTCOME_SIDE_YES"}}}), 5)
    t = r.trades[0]
    assert (t["slug"], t["px"], t["qty"], t["taker_outcome"]) == ("m", 0.77, 31.96, "OUTCOME_SIDE_YES")
