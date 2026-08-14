"""Decode full WebSocket message format with an open market."""
import sys, os, asyncio, json, base64, datetime
sys.path.insert(0, os.path.dirname(__file__))
import api_functions, importlib; importlib.reload(api_functions)
from api_functions import kalshi_get, event_ticker_from_date
from keys import KALSHI_API_KEY as API_KEY

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import websockets

WS_KEY   = os.path.join(os.path.dirname(__file__), "..", "kalshi_btc_hedge", "eshan-ws-key.txt")
URL      = "wss://api.elections.kalshi.com/trade-api/ws/v2"

def ws_headers():
    with open(WS_KEY, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = (ts + "GET" + "/trade-api/ws/v2").encode()
    sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return [("KALSHI-ACCESS-KEY", API_KEY), ("KALSHI-ACCESS-SIGNATURE", base64.b64encode(sig).decode()), ("KALSHI-ACCESS-TIMESTAMP", ts)]

# Find open markets
today = datetime.date.today()
tickers = []
for d in [today, today + datetime.timedelta(days=1)]:
    ev = event_ticker_from_date(d)
    try:
        data = kalshi_get("/trade-api/v2/markets", params={"event_ticker": ev, "status": "open"})
        mkts = data.get("markets", [])
        tickers += [m["ticker"] for m in mkts[:3]]  # take up to 3
        if tickers:
            print(f"Found {len(mkts)} open markets for {ev}: {[m['ticker'] for m in mkts]}")
            break
    except Exception as e:
        print(f"  {ev}: {e}")

if not tickers:
    print("No open markets found"); sys.exit(1)

async def probe():
    async with websockets.connect(URL, additional_headers=ws_headers(), open_timeout=10) as ws:
        print(f"\nSubscribing to {tickers} ...")
        await ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers": tickers}}))
        for i in range(10):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                d = json.loads(raw)
                print(f"\n[{i}] type={d.get('type')} sid={d.get('sid')} seq={d.get('seq')}")
                print(f"     msg keys: {list(d.get('msg', {}).keys())}")
                print(f"     full: {raw[:500]}")
            except asyncio.TimeoutError:
                print(f"[{i}] timeout - no more messages")
                break

asyncio.run(probe())
