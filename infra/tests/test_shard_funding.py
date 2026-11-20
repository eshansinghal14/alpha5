from infra.runtime.shard_funding import plan

SHARDS = {0: (4000.0, 8000.0), 2: (1500.0, 2500.0), 3: (3000.0, 6000.0)}


def test_nothing_moves_when_every_shard_is_above_its_floor():
    assert plan({0: 14000, 2: 2400, 3: 9000}, SHARDS) == ([], [])


def test_short_shard_is_filled_to_target_from_the_richest():
    moves, unfunded = plan({0: 14000, 2: 7.16, 3: 9000}, SHARDS)
    assert moves == [(0, 2, 2492.84)] and unfunded == []


def test_donor_never_goes_below_its_target():
    moves, unfunded = plan({0: 8500, 2: 100, 3: 6100}, SHARDS)
    # Shard 0 has $500 above target, shard 3 $100: the partial top-up leaves shard 2 short.
    assert moves == [(0, 2, 500.0)] and unfunded == [2]


def test_cooling_and_daily_cap_limit_transfers():
    assert plan({0: 14000, 2: 100, 3: 9000}, SHARDS, cooling={2}) == ([], [])
    moves, unfunded = plan({0: 14000, 2: 100, 3: 9000}, SHARDS, day_left=0)
    assert moves == [] and unfunded == [2]


def test_neediest_shard_first_and_balances_update_between_moves():
    moves, _ = plan({0: 9000, 2: 0, 3: 1000}, SHARDS, max_transfer=5000)
    # Shard 3 is 2000 under its floor, shard 2 1500 under: shard 3 first.
    assert moves[0][1] == 3
    assert sum(a for s, _, a in moves if s == 0) <= 9000 - 8000
