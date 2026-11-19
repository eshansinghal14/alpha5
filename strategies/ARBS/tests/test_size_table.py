"""size_table.py: stats from a print row, the table fit, and the runtime cut."""
import json
import os

from strategies.ARBS import size_table as st


def _row(depth_b=30.0, sweep=0.30, vol=60.0, after_b=None):
    """A two-leg complete set: a print sells our leg A YES down to `sweep`; leg B's
    YES ask is 0.60 with `depth_b` (its NO bids), `after_b` left 0.1 s later."""
    pre = {"X-A": [[lag, 0.38, 100.0, 0.40, 100.0] for lag in (0.02, 0.05, 0.1, 0.25)],
           "X-B": [[lag, 0.58, 50.0, 0.60, depth_b] for lag in (0.02, 0.05, 0.1, 0.25)]}
    books = {"X-A": {"yes": [[0.38, 100.0]], "no": [[0.60, 100.0], [0.59, 1000.0]]},
             "X-B": {"yes": [[0.58, 50.0]], "no": [[0.40, depth_b], [0.30, 1000.0]]}}
    left = depth_b if after_b is None else after_b
    after = {"0.1": {"X-A": books["X-A"], "X-B": {"yes": [[0.58, 50.0]], "no": [[0.40, left], [0.30, 1000.0]]}}}
    return {"ticker": "X-A", "side": "yes", "prints": [[0.0, sweep, vol]], "pre": pre, "books": books,
            "after": after, "baskets": [["complete_set:X:buy_all_yes", 1.0, True]]}


def test_a_fill_scores_every_size_and_thin_completion_costs_the_big_ones():
    deep = list(st.events(_row(depth_b=1000.0)))
    thin = list(st.events(_row(depth_b=10.0, after_b=0.0)))
    assert deep and thin
    live, dmin, dist, stable, pnl = deep[0]
    assert live and stable and dmin == 1000.0 and dist is not None and dist > 0
    assert len(pnl) == len(st.SIZES)
    # the thin book's 50-lot exit walks to the 0.30 level (unwind cheaper or not, it loses vs deep)
    i = st.SIZES.index(50)
    assert thin[0][4][i] < deep[0][4][i]


def test_table_cuts_to_the_best_size_and_keeps_the_size_when_it_is_best():
    cells = {st.cell(i, True, 10.0, 6.0, True): [10.0 if k == 5 else -10.0, 100] for i, k in enumerate(st.SIZES)}
    t = st.Table({"sizes": list(st.SIZES), "cells": cells})
    assert t.size(200, True, 10.0, 6.0, True) == 5.0
    assert t.size(5, True, 10.0, 6.0, True) == 5          # already the best
    assert t.size(7, True, 10.0, 6.0, True) == 7          # 5 is the largest grid size under 7: kept as is
    assert st.Table({"sizes": [1, 2], "cells": cells}).size(200, True, 10.0, 6.0, True) == 200   # stale layout


def test_day_stats_and_table_round_trip(tmp_path):
    import gzip
    root = tmp_path / "ARBS"
    (root / "prints").mkdir(parents=True)
    with gzip.open(root / "prints" / "2026-11-18.jsonl.gz", "wt") as fh:
        for _ in range(3):
            fh.write(json.dumps(_row()) + "\n")
    assert st.write_day(str(root), "2026-11-18")
    st.write_table([str(root)], str(root), "2026-11-19")
    d = json.load(open(root / st.TABLE_FILE))
    assert d["trained_on"] == ["ARBS:2026-11-18"] and d["sizes"] == list(st.SIZES)
    # the 5c quote fills; the 10c one rests under the 0.30 print
    assert sum(n for _, n in d["cells"].values()) == 3 * len(st.SIZES)
