from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM.farm import best_pick, parse_book, remove_ours

P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)


def test_cheap_side_only_and_joins_touch():
    # yes decided: yes bids deep at 0.97, no side thin at 0.01.
    others = {"yes": {0.97: 5000.0}, "no": {0.01: 200.0}}
    pk = best_pick("T", others, P, lots=1000, max_price=0.05)
    assert pk.side == "no" and pk.price == 0.01
    # no side: 1000/(1200) share, yes side 0 -> half of that per snapshot.
    assert abs(pk.rate - (1000 / 1200) / 2 * 0.001) < 1e-12


def test_no_pick_when_cheap_side_too_expensive():
    others = {"yes": {0.60: 5000.0}, "no": {0.30: 5000.0}}
    assert best_pick("T", others, P, lots=1000, max_price=0.05) is None


def test_empty_side_bids_one_cent_unless_it_crosses():
    assert best_pick("T", {"yes": {0.97: 5000.0}, "no": {}}, P, 1000, 0.05).price == 0.01
    assert best_pick("T", {"yes": {0.99: 5000.0}, "no": {}}, P, 1000, 0.05) is None


def test_subcent_prices_survive_parsing():
    yes, no = parse_book({"yes_dollars": [["0.9850", "10"]], "no_dollars": [["0.0040", "5"]]})
    assert yes == {0.985: 10.0} and no == {0.004: 5.0}
    assert remove_ours(no, 0.004, 5.0) == {}
