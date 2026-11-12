"""Basket spec loader (`Kalshi arb baskets spec.json`): every basket as a
list of (ticker, side) legs and the payout it is guaranteed."""
from __future__ import annotations

import dataclasses
import json

import pandas as pd


@dataclasses.dataclass(frozen=True)
class Leg:
    ticker: str
    side: str          # "yes" | "no": the contract this leg BUYS


@dataclasses.dataclass(frozen=True)
class Basket:
    basket_id: str
    relationship_id: str
    type: str
    cluster: str
    legs: tuple[Leg, ...]
    # Worst-case payout per bundle. For `exact: false` baskets this is the
    # lower of payout and payout_in_tie_or_rounding_case: GOVPARTYFL buy_all_yes
    # pays 0 on a tie, so it is never an arb and must not be traded as one.
    payout: float
    quote_until: pd.Timestamp | None
    resume_quoting: pd.Timestamp | None
    settles: pd.Timestamp | None = None
    live_from: pd.Timestamp | None = None      # a game's start: its quotes are picked off on each score
    exact: bool = True                         # False: payout is the tie/rounding case, other outcomes pay more

    @property
    def tickers(self) -> list[str]:
        return [l.ticker for l in self.legs]

    def quoting_allowed(self, now: pd.Timestamp) -> bool:
        """The spec's quote window: quoting stops at quote_until (election
        night: legs reprice faster than a completion can follow) and comes back
        only at resume_quoting. Takes are allowed throughout."""
        if self.quote_until is None or now < self.quote_until:
            return True
        return self.resume_quoting is not None and now >= self.resume_quoting

    def is_live(self, now: pd.Timestamp) -> bool:
        return self.live_from is not None and now >= self.live_from


def _ts(v) -> pd.Timestamp | None:
    return None if not v else pd.Timestamp(v)


def basket_to_dict(b: Basket) -> dict:
    return {"basket_id": b.basket_id, "relationship_id": b.relationship_id, "type": b.type, "cluster": b.cluster,
            "legs": [[l.ticker, l.side] for l in b.legs], "payout": b.payout,
            "settles": None if b.settles is None else b.settles.isoformat(),
            "live_from": None if b.live_from is None else b.live_from.isoformat(), "exact": b.exact}


def basket_from_dict(d: dict) -> Basket:
    """A held basket's saved definition. Its quote window isn't kept: it is
    only completed and settled, never quoted, once the spec drops it."""
    return Basket(basket_id=d["basket_id"], relationship_id=d["relationship_id"], type=d["type"],
                  cluster=d["cluster"], legs=tuple(Leg(t, s) for t, s in d["legs"]), payout=float(d["payout"]),
                  quote_until=None, resume_quoting=None, settles=_ts(d.get("settles")),
                  live_from=_ts(d.get("live_from")), exact=d.get("exact", True))


def _settles(rel: dict) -> pd.Timestamp | None:
    if rel.get("expected_settlement_utc"):
        return pd.Timestamp(rel["expected_settlement_utc"])
    if rel.get("expected_settlement_month"):
        return pd.Period(rel["expected_settlement_month"], freq="M").end_time.tz_localize("UTC")
    return None


def load_spec(path: str, exclude: list[str] | None = None) -> list[Basket]:
    """Baskets in file order. `exclude` drops any basket whose basket_id,
    relationship_id or cluster equals one of its entries."""
    with open(path, encoding="utf-8") as fh:
        return parse_spec(json.load(fh), exclude)


def parse_spec(doc: dict, exclude: list[str] | None = None) -> list[Basket]:
    ex = set(exclude or [])
    out, seen = [], set()
    for rel in doc["relationships"]:
        for b in rel["baskets"]:
            if {b["basket_id"], rel["relationship_id"], rel.get("cluster")} & ex:
                continue
            if b["basket_id"] in seen:
                raise ValueError(f"basket {b['basket_id']} listed twice")
            seen.add(b["basket_id"])
            legs = tuple(Leg(l["ticker"], l["side"]) for l in b["legs"])
            if any(l.side not in ("yes", "no") for l in legs):
                raise ValueError(f"{b['basket_id']}: leg side must be yes/no")
            if len({l.ticker for l in legs}) != len(legs):
                raise ValueError(f"{b['basket_id']}: a ticker appears twice")
            payout = float(b["payout"])
            if b.get("exact") is False and b.get("payout_in_tie_or_rounding_case") is not None:
                payout = min(payout, float(b["payout_in_tie_or_rounding_case"]))
            out.append(Basket(basket_id=b["basket_id"], relationship_id=rel["relationship_id"],
                              type=rel["type"], cluster=rel.get("cluster") or "", legs=legs, payout=payout,
                              quote_until=_ts(rel.get("quote_until_utc")),
                              resume_quoting=_ts(rel.get("resume_quoting_utc")), settles=_settles(rel),
                              live_from=_ts(rel.get("live_from_utc")), exact=b.get("exact") is not False))
    return out


def merge(first: list[Basket], *more: list[Basket]) -> list[Basket]:
    """Baskets of `first`, then those of `more` not already listed under the
    same basket_id or the same legs (the scan re-finds hand-spec baskets)."""
    out = list(first)
    ids = {b.basket_id for b in out}
    legs = {frozenset(b.legs) for b in out}
    for bs in more:
        for b in bs:
            if b.basket_id not in ids and frozenset(b.legs) not in legs:
                out.append(b)
                ids.add(b.basket_id)
                legs.add(frozenset(b.legs))
    return out
