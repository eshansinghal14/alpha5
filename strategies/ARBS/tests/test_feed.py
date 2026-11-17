import asyncio
import json

from strategies.ARBS import feed as F


def test_the_feed_reads_while_it_subscribes_and_is_up_once_subscribed(monkeypatch):
    monkeypatch.setattr(F, "SUBSCRIBE_PACING_S", 0.0)
    monkeypatch.setattr(F.auth, "ws_headers", lambda: {})
    sent, up_when_read = [], []
    f = F.MultiFeed([f"T-{i}" for i in range(500)], lambda *a: None, lambda *a: None, label="t")

    async def go():
        gate = asyncio.Event()

        class WS:
            async def send(self, m):
                sent.append(json.loads(m))
                if len(sent) == 10:
                    # Halfway through subscribing: only a reader running beside it opens this.
                    await asyncio.wait_for(gate.wait(), 2.0)

            async def recv(self):
                await asyncio.sleep(0)
                if len(sent) >= 10 and not gate.is_set():
                    up_when_read.append(f.up)
                    gate.set()
                return json.dumps({"type": "noop"})

        class Conn:
            async def __aenter__(self):
                return WS()

            async def __aexit__(self, *a):
                return False
        monkeypatch.setattr(F.websockets, "connect", lambda *a, **k: Conn())
        task = asyncio.create_task(f.run())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if f.up:
                break
        task.cancel()
    asyncio.run(go())
    assert up_when_read == [False]                       # read mid-subscribe, before the feed counted as up
    assert f.up and len([m for m in sent if m["cmd"] == "subscribe"]) == 20     # 10 batches x 2 channels
    assert len({m["id"] for m in sent}) == len(sent)
