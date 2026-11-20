"""Keeps each trading shard's cash above its floor by moving money from the
shard with the most to spare (alpha5-shard-funding.timer, every minute).

Kalshi checks collateral per exchange shard, so a shard can be empty while the
account has plenty: on 2026-11-20 the race's shard 2 was emptied to $7 and
every race order was refused until cash was moved back by hand.

    python -m infra.runtime.shard_funding            # check and fund
    python -m infra.runtime.shard_funding --dry-run  # print what it would move
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid

import pandas as pd

from infra.config import atomic_write_json
from infra.kalshi.orders import BASE_URL, KalshiOrderClient
from infra.runtime.persist import LIVE_ROOT

# shard -> (floor, target) in dollars of available cash (Kalshi's balance,
# which resting orders don't reduce). Below floor, a shard is filled to target.
# A donor never goes below its own target. Levels as of 11-20: shard 0 ~$14k
# (ARBS, ARBS0B, ARBS0C), shard 2 ~$2.5k (the race), shard 3 ~$9k (ARBS3, ARBS3B).
SHARDS = {
    0: (4000.0, 8000.0),
    2: (1500.0, 2500.0),
    3: (3000.0, 6000.0),
}
MAX_TRANSFER = 5000.0
MAX_PER_DAY = 10000.0
# A transfer completes asynchronously; a shard just funded isn't funded again
# until the balance has had time to show it.
COOLDOWN_S = 600
TRANSFER_PATH = "/trade-api/v2/portfolio/intra_exchange_instance_transfer"
STATE_PATH = os.path.join(LIVE_ROOT, "shard_funding.json")
LOG_PATH = os.path.join(LIVE_ROOT, "shard_funding.log")
REGION = "us-east-2"
TOPIC_PATH = "/etc/alpha5/report-topic-arn"


def plan(balances: dict[int, float], shards: dict = SHARDS, max_transfer: float = MAX_TRANSFER,
         day_left: float = MAX_PER_DAY, cooling: set[int] = frozenset()) -> tuple[list, list]:
    """(transfers as (src, dst, dollars), shards that are short but couldn't be
    funded). Neediest shard first; each takes from whichever shard has the most
    above its target at that point."""
    bal = dict(balances)
    moves, unfunded = [], []
    short = sorted((s for s in shards if s in bal and bal[s] < shards[s][0] and s not in cooling),
                   key=lambda s: bal[s] - shards[s][0])
    for dst in short:
        want = min(shards[dst][1] - bal[dst], max_transfer, day_left)
        spare = {s: bal[s] - shards[s][1] for s in shards if s != dst and s in bal}
        src = max(spare, key=spare.get, default=None)
        amount = round(min(want, spare[src]), 2) if src is not None else 0.0
        # Floor rather than target: a partial top-up that still leaves the shard
        # short is reported, not silently accepted.
        if amount < 1 or bal[dst] + amount < shards[dst][0]:
            unfunded.append(dst)
            if amount < 1:
                continue
        moves.append((src, dst, amount))
        bal[src] -= amount
        bal[dst] += amount
        day_left -= amount
    return moves, unfunded


def log(msg: str):
    line = f"{pd.Timestamp.now(tz='UTC').isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def alert(subject: str, body: str):
    try:
        topic = open(TOPIC_PATH).read().strip()
        subprocess.run(["aws", "sns", "publish", "--region", REGION, "--topic-arn", topic,
                        "--subject", subject[:99], "--message", body, "--output", "text"],
                       capture_output=True, timeout=30, check=True)
    except Exception as e:
        log(f"alert failed: {e!r}")


def read_balance(client: KalshiOrderClient, shard: int, tries: int = 8) -> float | None:
    """The shard's balance, or None. ARBS keeps the account's read bucket near
    empty, so a single read 429s often (11-20)."""
    for i in range(tries):
        try:
            return client.balance(exchange_index=shard)
        except Exception as e:
            if i == tries - 1:
                log(f"shard {shard} balance read failed: {e!r}")
            time.sleep(1.0 + i)
    return None


def transfer(client: KalshiOrderClient, src: int, dst: int, dollars: float) -> str:
    body = {"client_transfer_id": str(uuid.uuid4()), "source": "event_contract",
            "destination": "event_contract", "amount": int(round(dollars * 10000)),  # centicents
            "source_exchange_shard": src, "destination_exchange_shard": dst}
    resp = client._session.post(BASE_URL + TRANSFER_PATH, data=json.dumps(body), timeout=20,
                                headers={**client._headers("POST", TRANSFER_PATH),
                                         "Content-Type": "application/json"})
    resp.raise_for_status()
    return resp.json().get("transfer_id", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = KalshiOrderClient(tokens_per_s=1)
    balances = {}
    for s in SHARDS:
        balances[s] = read_balance(client, s)
        if balances[s] is None:
            # Unreadable shard: skip the run rather than fund on a guess.
            log(f"shard {s} balance unreadable; skipped this run")
            return

    try:
        state = json.load(open(STATE_PATH))
    except (OSError, ValueError):
        state = {"transfers": []}
    now = time.time()
    recent = [t for t in state["transfers"] if now - t["time"] < 86400]
    cooling = {t["dst"] for t in recent if now - t["time"] < COOLDOWN_S}
    day_left = MAX_PER_DAY - sum(t["amount"] for t in recent)

    moves, unfunded = plan(balances, day_left=day_left, cooling=cooling)
    shown = ", ".join(f"shard {s} ${b:,.2f}" for s, b in sorted(balances.items()))
    if args.dry_run:
        print(shown, "->", moves or "nothing to move", "unfunded:", unfunded)
        return
    for src, dst, amount in moves:
        try:
            tid = transfer(client, src, dst, amount)
        except Exception as e:
            log(f"transfer ${amount:,.2f} shard {src} -> {dst} FAILED: {e!r}")
            alert(f"alpha5: shard {dst} funding failed", f"{shown}\nTransfer ${amount:,.2f} from shard {src} failed: {e!r}")
            continue
        recent.append({"time": now, "src": src, "dst": dst, "amount": amount, "transfer_id": tid})
        log(f"moved ${amount:,.2f} shard {src} -> {dst} ({tid}); before: {shown}")
        alert(f"alpha5: moved ${amount:,.0f} to shard {dst}",
              f"Shard {dst} was below its ${SHARDS[dst][0]:,.0f} floor.\nMoved ${amount:,.2f} from shard {src} "
              f"(transfer {tid}).\nBalances before: {shown}")
    for dst in unfunded:
        last = state.get("unfunded_alert", {}).get(str(dst), 0)
        if now - last > 3600:              # once an hour while it stays short
            log(f"shard {dst} below floor and can't be fully funded: {shown}")
            alert(f"alpha5: shard {dst} short of cash",
                  f"Shard {dst} is below its ${SHARDS[dst][0]:,.0f} floor and no shard has enough to spare "
                  f"(or the ${MAX_PER_DAY:,.0f}/day cap is used).\nBalances: {shown}")
            state.setdefault("unfunded_alert", {})[str(dst)] = now
    state["transfers"] = recent
    state["last"] = {"time": now, "balances": balances}
    atomic_write_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
