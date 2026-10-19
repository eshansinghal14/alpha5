"""The quote ladder through kalshi_backtest.Backtest: the same OrderManager,
metered on simulated time, with creates and cancels carrying send latency."""
import json

import pandas as pd

from kalshi_backtest import engine
from kalshi_backtest.core import Quote, Trade
from kalshi_infra.kalshi.fees import schedule
from kalshi_infra.trading.fair import Fair, FairUpdate
from kalshi_infra.trading.quoter import Quoter
from kalshi_infra.tests.test_quoter import Model, cfg

FREE = schedule("quadratic", 0)
T0 = pd.Timestamp("2026-10-10 00:00:00", tz="UTC")


class _Row:
    def __init__(self, time, side, levels):
        self.time, self.side, self.msg_type = time, side, "snapshot"
        self.levels_json, self.price, self.delta = json.dumps(levels), None, None


def _data(trades=()):
    """A one-market l2 day: 0.40 bid / 0.60 ask (no side 0.40), 100 deep."""
    raw = [_Row(T0, "yes", [[0.40, 100.0]]), _Row(T0, "no", [[0.40, 100.0]])]
    q = Quote(ticker="A", time=T0, bid=0.40, ask=0.60, bid_depth=100.0, ask_depth=100.0,
              source="kalshi_ws", fidelity="l2")
    return engine.EventData(event_ticker="E", source="kalshi_ws", fidelity="l2",
                            quotes={"A": [q]}, trades={"A": list(trades)}, raw_ws={"A": raw},
                            results={"A": "no"})


def _run(reserve, fairs, trades=()):
    c = cfg(quote_levels_behind=2, cancels_token=reserve)
    q = Quoter(c, Model(), FREE, close_time=None)
    bt = engine.Backtest(fee_multiplier=0.0, maker_fee_multiplier=0.0, max_position=10.0,
                         cancels_token=reserve)
    ext = [(t, FairUpdate({"A": Fair(p, p - 0.01, p + 0.01)})) for t, p in fairs]
    res = bt.run(q.__call__, "E", event_data=_data(trades), external_events=ext)
    return res, bt, q


def test_ladder_rests_and_fills_off_prints():
    t1 = T0 + pd.Timedelta(seconds=1)
    res, bt, _ = _run(0.0, [(t1, 0.50)],
                      trades=[Trade(ticker="A", time=t1 + pd.Timedelta(seconds=2), price=0.41, size=1.0,
                                    taker_side="no")])
    quotes = [o for o in res.orders if o.intent == "quote"]
    assert sorted({(o.side, o.price) for o in quotes}) == [("no", 0.41), ("yes", 0.41)]
    assert res.portfolio.position("A").yes_qty == 1.0


def test_bucket_is_metered_on_sim_time():
    """The modelled bucket (80/s) minus a 70-token reserve leaves one create's
    worth per simulated second: 3 fair updates 1s apart give ~3 creates."""
    fairs = [(T0 + pd.Timedelta(seconds=s), 0.50) for s in (1, 2, 3)]
    res, _, _ = _run(70.0, fairs)
    n = len([o for o in res.orders if o.intent == "quote"])
    assert 2 <= n <= 4


def test_fair_drop_moves_the_inside_quote_down():
    fairs = [(T0 + pd.Timedelta(seconds=1), 0.50), (T0 + pd.Timedelta(seconds=2), 0.42)]
    res, bt, q = _run(0.0, fairs)
    yes = [o for o in res.orders if o.intent == "quote" and o.side == "yes"]
    assert len(yes) == 1 and yes[0].order_id not in res.cancelled_order_ids
    assert yes[0].price < 0.41                       # amended down, not re-created
    assert bt._om.counts.get("move_down:inside_edge", 0) == 1
