from kalshi_strategies.LIPFARM.levels import NEG_WAIT_S, Levels


def test_joins_go_to_the_back_and_fills_come_off_the_front():
    lv = Levels()
    lv.snapshot("T", "yes", [(0.40, 300.0)], 0.0)
    lv.delta("T", "yes", 0.40, 200.0, 1.0)
    lv.delta("T", "yes", 0.40, 1000.0, 2.0)
    assert lv.view("T", "yes", 0.40)["sizes"] == [300.0, 200.0, 1000.0]
    lv.trade("T", "yes", 0.40, 3.0)
    lv.delta("T", "yes", 0.40, -350.0, 3.0)
    lv.flush(3.01 + NEG_WAIT_S)
    assert lv.view("T", "yes", 0.40)["sizes"] == [150.0, 1000.0] and lv.n["fill"] == 1


def test_a_cancel_removes_the_chunk_of_its_size_else_the_newest():
    lv = Levels()
    lv.delta("T", "no", 0.30, 500.0, 0.0)
    lv.delta("T", "no", 0.30, 1000.0, 1.0)
    lv.delta("T", "no", 0.30, 200.0, 2.0)
    lv.delta("T", "no", 0.30, -500.0, 3.0)                 # no trade: a cancel, exactly the first order
    lv.flush(4.0)
    assert lv.view("T", "no", 0.30)["sizes"] == [1000.0, 200.0]
    lv.delta("T", "no", 0.30, -50.0, 5.0)                  # no exact match: off the newest
    lv.flush(6.0)
    assert lv.view("T", "no", 0.30)["sizes"] == [1000.0, 150.0]


def test_our_order_is_tagged_and_the_size_ahead_of_it_read():
    lv = Levels()
    lv.snapshot("T", "yes", [(0.40, 700.0)], 0.0)
    lv.delta("T", "yes", 0.40, 1000.0, 1.0)                # ours lands in the book...
    lv.ours("T", "yes", 0.40, 1000.0, "oid1", 1.05)        # ...before its create is answered
    lv.delta("T", "yes", 0.40, 300.0, 2.0)
    assert lv.ahead_of("T", "yes", 0.40, "oid1") == 700.0
    lv.ours("T", "yes", 0.40, 50.0, "oid2", 3.0)           # answered first, lands after
    lv.delta("T", "yes", 0.40, 50.0, 3.1)
    assert lv.ahead_of("T", "yes", 0.40, "oid2") == 2000.0


def test_a_snapshot_reconciles_the_difference_as_an_unknown_block():
    lv = Levels()
    lv.delta("T", "yes", 0.40, 100.0, 0.0)
    lv.snapshot("T", "yes", [(0.40, 400.0), (0.39, 50.0)], 1.0)
    assert lv.view("T", "yes", 0.40) == {"n": 2, "sizes": [100.0, 300.0], "snap": 1, "ours": 0}
    lv.snapshot("T", "yes", [(0.40, 400.0)], 2.0)
    assert lv.view("T", "yes", 0.39)["n"] == 0
