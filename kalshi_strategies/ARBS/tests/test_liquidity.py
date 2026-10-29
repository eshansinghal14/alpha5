"""Liquidity log accounting on hand-fed book updates."""
import gzip
import json

from kalshi_strategies.ARBS import liquidity
from kalshi_strategies.ARBS.liquidity import Liquidity


def test_worsened_ask_refill_and_unrefilled(tmp_path):
    lq = Liquidity(str(tmp_path))
    lq.on_book("A", 0.0, 0.40, 10, 0.35, 20)      # yes ask .40, no ask .65
    lq.on_book("A", 1.0, 0.42, 3, 0.35, 20)       # yes ask lifted
    lq.on_book("A", 1.5, 0.41, 5, 0.35, 20)       # better but not back
    lq.on_book("A", 3.0, 0.40, 8, 0.30, 20)       # back at .40; no ask worsens to .70
    lq.on_trade("A", 7)
    rows = lq.flush(3.0 + liquidity.REFILL_CAP_S + 1, 1000.0)
    r = rows[0]
    assert (r["yes_worsened"], r["yes_refills"], r["yes_refill_p50_s"], r["yes_unrefilled"]) == (1, 1, 2.0, 0)
    assert (r["no_worsened"], r["no_refills"], r["no_unrefilled"]) == (1, 0, 1)
    assert r["yes_min_depth"] == 3 and r["spread"] == 0.1 and (r["prints"], r["contracts"]) == (1, 7)
    r2 = lq.flush(1000.0, 1300.0)[0]
    assert (r2["updates"], r2["yes_worsened"], r2["yes_min_depth"]) == (0, 0, 8)
    lq.write(rows, "2026-10-29")
    with gzip.open(tmp_path / "liquidity" / "2026-10-29.jsonl.gz", "rt") as fh:
        assert json.loads(fh.readline())["ticker"] == "A"


def test_emptied_side_counts_as_worsened(tmp_path):
    lq = Liquidity(str(tmp_path))
    lq.on_book("B", 0.0, 0.30, 4, None, None)
    lq.on_book("B", 1.0, None, None, None, None)
    lq.on_book("B", 4.0, 0.30, 2, None, None)
    r = lq.flush(5.0, 0.0)[0]
    assert (r["yes_worsened"], r["yes_refills"], r["yes_refill_max_s"], r["no_ask"]) == (1, 1, 3.0, None)
