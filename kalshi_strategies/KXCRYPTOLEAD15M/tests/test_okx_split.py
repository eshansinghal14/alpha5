import asyncio
import json

from kalshi_strategies.KXCRYPTOLEAD15M.spot_feed.venues import OkxPerpAdapter, _OkxTrades


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, m):
        self.sent.append(json.loads(m) if m != "ping" else m)


def _subs(ad):
    ws = _WS()

    async def go():
        await ad._subscribe(ws)
        await asyncio.sleep(0)
    asyncio.run(go())
    return {a["channel"] for a in ws.sent[0]["args"]}


def test_book_and_trades_on_separate_sockets_and_prints_reach_the_parent():
    ad = OkxPerpAdapter(["BTC"])
    tr = _OkxTrades(ad)
    assert _subs(ad) == {"bbo-tbt"} and _subs(tr) == {"trades"}
    seen = []
    ad.on_print = lambda coin, t, as_of, exch_ts, kind: seen.append((coin, t["trade_id"], kind))
    msg = {"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
           "data": [{"px": "80000", "sz": "3", "tradeId": "42", "side": "buy", "ts": "1000"}]}
    asyncio.run(tr._handle(None, msg, 1.0, lambda vb: None))
    assert seen == [("BTC", "42", "okx_perp")]
    assert not tr._books["BTC"].last_receive_ts and not ad._books["BTC"].last_receive_ts   # no book touched
