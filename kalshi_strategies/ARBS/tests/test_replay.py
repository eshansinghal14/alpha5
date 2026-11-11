import pytest

from kalshi_strategies.ARBS import replay

NO_FEES = lambda t: (0.0, 0.0)


def row(b_no_bids_after, pre_b=None, prints=((0.0, 0.40, 50),)):
    """X-A YES printed at 0.40; basket buys YES A + YES B for $1. B's YES ask
    is 1 - its NO bid: 0.50 at the print."""
    books = {"X-A": {"yes": [[0.40, 100]], "no": [[0.55, 100]]},
             "X-B": {"yes": [[0.45, 100]], "no": [[0.50, 100]]}}
    after = {"X-A": books["X-A"], "X-B": {"yes": [[0.45, 100]], "no": b_no_bids_after}}
    r = {"t": 0.0, "ticker": "X-A", "side": "yes", "prints": [list(p) for p in prints],
         "baskets": [["complete_set:X:buy_all_yes", 1.0, True]], "books": books,
         "after": {"0.03": after, "0.1": after, "1.0": after}}
    if pre_b is not None:
        r["pre"] = {"X-B": [[0.02, *pre_b]]}
    return r


def test_quote_fills_through_its_price_and_completes():
    s = replay.score(row([[0.50, 100]]), NO_FEES, [0.02, 0.12], 10)
    # Edge 2c: quote 0.48, the 0.40 print fills 10, B bought at 0.50.
    assert s["fills"] == [10, 0.0] and s["pnl"][0] == pytest.approx(10 * (1 - 0.48 - 0.50))


def test_completion_pays_the_move_after_the_fill():
    s = replay.score(row([[0.47, 100]]), NO_FEES, [0.02], 10)     # B's ask up to 0.53 by completion
    assert s["pnl"][0] == pytest.approx(10 * (1 - 0.48 - 0.53))


def test_a_lagged_quote_is_priced_on_the_book_before_the_move():
    # 20 ms before the print B's ask was 0.45: a quote resting then sat at 0.53
    # and the print took it; B has since moved to 0.50.
    s = replay.score(row([[0.50, 100]], pre_b=(0.45, 100, 0.45, 100)), NO_FEES, [0.02], 10, lag=0.02)
    assert s["fills"] == [10] and s["pnl"][0] == pytest.approx(10 * (1 - 0.53 - 0.50))
    assert replay.score(row([[0.50, 100]]), NO_FEES, [0.12], 10)["fills"] == [0.0]


def test_size_capped_at_what_the_other_legs_complete_at_the_edge():
    r = row([[0.50, 100]])
    r["books"]["X-B"]["no"] = [[0.50, 3], [0.40, 100]]           # 3 at 0.50, then 0.60
    s = replay.score(r, NO_FEES, [0.02], 50)
    assert s["fills"] == [3]


def test_live_only_skips_non_live_baskets():
    r = row([[0.50, 100]])
    r["baskets"][0][2] = False
    assert replay.score(r, NO_FEES, [0.02], 10, live_only=True) is None


def test_exit_unwinds_when_that_is_cheaper_than_completing():
    s = replay.score(row([[0.30, 100]]), NO_FEES, [0.02], 10)     # B's ask jumps to 0.70
    # Completing loses 10 * 0.18; buying A's NO at 0.60 ($1 a pair) loses 10 * 0.08.
    assert s["pnl"][0] == pytest.approx(10 * (1 - 0.48 - 0.60))
