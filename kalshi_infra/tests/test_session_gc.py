import gc
import weakref

from kalshi_infra.runtime import session


class _Node:
    pass


def test_freeze_heap_reclaims_cycles_frozen_by_the_last_event():
    try:
        a, b = _Node(), _Node()
        a.other, b.other = b, a
        dead = weakref.ref(a)
        session._freeze_heap()          # last event: the cycle is alive and gets frozen
        assert gc.get_freeze_count() > 0
        del a, b
        gc.collect()
        assert dead() is not None       # frozen: an ordinary collection skips it
        session._freeze_heap()          # next event
        assert dead() is None
    finally:
        gc.unfreeze()
