import asyncio

from kalshi_infra.runtime.session import coalesce_latest


def test_one_pass_per_key_with_the_newest_item():
    seen = []

    async def main():
        push = coalesce_latest(seen.append)
        for i in range(5):
            push("A", ("A", i))
        push("B", ("B", 0))
        push("A", ("A", 9))
        assert seen == []                      # nothing runs inside the burst
        await asyncio.sleep(0)
        assert sorted(seen) == [("A", 9), ("B", 0)]
        push("A", ("A", 10))                   # a later burst schedules again
        await asyncio.sleep(0)
        assert seen[-1] == ("A", 10)
    asyncio.run(main())


def test_inline_without_a_loop():
    seen = []
    coalesce_latest(seen.append)("A", 1)
    assert seen == [1]
