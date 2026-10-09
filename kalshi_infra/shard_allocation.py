"""
Show or set Kalshi's per-shard balance allocation.

Each matching-engine shard checks collateral on its own, so an API order on
a shard with $0 is rejected `insufficient_shard_balance` even when the
account has money elsewhere. Kalshi's auto-rebalancer sweeps balance toward
the target split here every ~10s.

Usage:
    python -m kalshi_infra.shard_allocation                  # show
    python -m kalshi_infra.shard_allocation 2=100            # crypto only
    python -m kalshi_infra.shard_allocation 0=50 2=50        # SPX + crypto
    python -m kalshi_infra.shard_allocation --event KXCRYPTOLEAD15M-26OCT090000
    python -m kalshi_infra.shard_allocation --off            # stop rebalancing
"""
import argparse

from kalshi_infra.kalshi.orders import BALANCE_PATH, BASE_URL, KalshiOrderClient


def show(client: KalshiOrderClient) -> None:
    target = client.target_allocation()
    alloc = target["allocations"]
    print("target allocation:",
          ", ".join(f"shard {s}: {p}%" for s, p in sorted(alloc.items())) or "none (rebalancing off)",
          f"| resting reservation: {target['resting_margin_reservation']}")
    resp = client._session.get(BASE_URL + BALANCE_PATH,
                               headers=client._headers("GET", BALANCE_PATH),
                               timeout=client.timeout_s)
    resp.raise_for_status()
    for row in resp.json().get("balance_breakdown", []):
        print(f"  shard {row['exchange_index']}: ${float(row['balance']):.2f}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("split", nargs="*", help="SHARD=PERCENT pairs, totalling 100")
    p.add_argument("--event", action="append", default=[],
                   help="print the shard an event trades on (repeatable)")
    p.add_argument("--off", action="store_true", help="clear the target (disables rebalancing)")
    args = p.parse_args()

    client = KalshiOrderClient.from_account()
    for ev in args.event:
        print(f"{ev} trades on shard {client.exchange_index(ev)}")
    if args.off:
        client.set_target_allocation({})
        print("rebalancing disabled")
    elif args.split:
        alloc = {}
        for item in args.split:
            shard, _, pct = item.partition("=")
            alloc[int(shard)] = int(pct)
        client.set_target_allocation(alloc)
        print(f"target set: {alloc}  (Kalshi rebalances within ~10s)")
    show(client)


if __name__ == "__main__":
    main()
