import dataclasses
import json
import time
from types import SimpleNamespace

from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM import farm
from kalshi_strategies.LIPFARM.farm import parse_book, plan_market, remove_ours

P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
CFG = farm.Cfg(max_capital=5000, num_markets=5, lots=1000, wide_spread=0.20, decided_min=0.97, decided_max=0.98,
               min_ahead=0.0, max_position=1e9, scan_markets=1, refresh_s=30,
               programs_refresh_s=600)


def test_decided_quotes_only_the_expensive_side():
    others = {"yes": {0.97: 200.0}, "no": {0.02: 5000.0}}
    pl = plan_market("T", others, P, CFG)
    assert pl.mode == "decided" and pl.legs == (("yes", 0.97),) and pl.capital == 970.0
    # yes side: our 1000 of 1200; no side: none of ours -> half of 1000/1200 per snapshot.
    assert abs(pl.rate - (1000 / 1200) / 2 * 0.001) < 1e-12


def test_decided_needs_the_other_side_to_qualify_on_others_alone():
    assert plan_market("T", {"yes": {0.97: 200.0}, "no": {0.02: 999.0}}, P, CFG) is None


def test_no_plan_at_99_or_in_between():
    assert plan_market("T", {"yes": {0.99: 200.0}, "no": {0.005: 5000.0}}, P, CFG) is None
    assert plan_market("T", {"yes": {0.60: 5000.0}, "no": {0.30: 5000.0}}, P, CFG) is None   # 10c spread


def test_wide_quotes_both_touches():
    others = {"yes": {0.11: 50.0}, "no": {0.37: 20.0}}
    pl = plan_market("T", others, P, CFG)
    assert pl.mode == "wide" and set(pl.legs) == {("yes", 0.11), ("no", 0.37)}
    assert abs(pl.capital - 480.0) < 1e-9


def test_wide_drops_a_side_at_its_position_cap():
    others = {"yes": {0.11: 50.0, 0.05: 2000.0}, "no": {0.37: 5000.0}}   # yes qualifies without us
    cfg = dataclasses.replace(CFG, max_position=1000)
    pl = plan_market("T", others, P, cfg, held={"yes": 1000.0})
    assert pl.legs == (("no", 0.37),)


def test_min_ahead_needs_others_at_our_price():
    others = {"yes": {0.97: 200.0}, "no": {0.02: 5000.0}}
    assert plan_market("T", others, P, dataclasses.replace(CFG, min_ahead=200)) is not None
    assert plan_market("T", others, P, dataclasses.replace(CFG, min_ahead=201)) is None


def test_subcent_prices_survive_parsing():
    yes, no = parse_book({"yes_dollars": [["0.9850", "10"]], "no_dollars": [["0.0040", "5"]]})
    assert yes == {0.985: 10.0} and no == {0.004: 5.0}
    assert remove_ours(no, 0.004, 5.0) == {}


def _farm(tmp_path, monkeypatch, plans, orders=None):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    f = farm.Farm()
    far = "2099-01-01T00:00:00Z"
    f.programs = {t: {"id": "p" + t, "end_date": far} for t in plans}
    f.plans = plans
    f.state["orders"] = orders or {}
    return f


def test_choose_by_lip_per_dollar_within_capital(tmp_path, monkeypatch):
    a = farm.Plan("A-1-X", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002)
    b = farm.Plan("B-1-X", "wide", (("yes", 0.1), ("no", 0.4)), 1e-3, 500.0, 0.002)   # best per dollar
    c = farm.Plan("C-1-X", "decided", (("no", 0.98),), 5e-4, 980.0, 0.002)
    f = _farm(tmp_path, monkeypatch, {"A-1-X": a, "B-1-X": b, "C-1-X": c})
    assert list(f.choose(dataclasses.replace(CFG, max_capital=1500))) == ["B-1-X", "A-1-X"]
    assert list(f.choose(dataclasses.replace(CFG, max_capital=1400))) == ["B-1-X"]


def test_quoted_past_one_dollar_first_then_under_then_new(tmp_path, monkeypatch):
    def pl(t, rate):
        return farm.Plan(t, "decided", (("yes", 0.97),), rate, 1000.0, 0.002)
    order = {"oid": "o", "side": "yes", "price": 0.97, "size": 1000, "remaining": 1000, "placed": 0}
    f = _farm(tmp_path, monkeypatch, {"PAST-1-A": pl("PAST-1-A", 1e-4), "UNDER-1-A": pl("UNDER-1-A", 2e-4),
                                      "NEW-1-A": pl("NEW-1-A", 9e-4)},
              {"PAST-1-A|yes": dict(order), "UNDER-1-A|yes": dict(order)})
    f.state["period_accrued"] = {"pPAST-1-A": 1.5, "pUNDER-1-A": 0.4}
    assert list(f.choose(dataclasses.replace(CFG, num_markets=3))) == ["PAST-1-A", "UNDER-1-A", "NEW-1-A"]
    assert list(f.choose(dataclasses.replace(CFG, num_markets=1))) == ["PAST-1-A"]


class _Bucket:
    tokens_per_s = 300.0
    COST = {"order": 10, "amend": 10, "cancel": 2}

    def __init__(self, tokens=300.0, ahead=0.0):
        self.tokens, self.ahead, self.sent, self.cancelled = tokens, ahead, [], []

    def can_afford(self, kind, reserve=0.0):
        return self.tokens >= self.COST[kind] + reserve

    def allow_now(self, kind, force=False, reason=""):
        if force or self.tokens >= self.COST[kind]:
            self.tokens -= self.COST[kind]
            return True
        return False

    def _resting(self, prefix):
        return [("old", "X-1-A", "lipf-old"), ("arbs", "Y-1-A", "someone-else")]

    def place_resting(self, order):
        self.sent.append(order)
        return SimpleNamespace(exchange_order_id=f"oid{len(self.sent)}", fill=None)

    def cancel(self, oid, ticker):
        self.cancelled.append(oid)
        return True


def _live(tmp_path, monkeypatch, bucket, placed_ago=120.0, lots=1000):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    monkeypatch.setattr(farm, "CLAIMS_PATH", str(tmp_path / "live" / "claimed.json"))
    f = farm.Farm()
    f.live = True
    monkeypatch.setattr(f, "client", lambda shard: bucket)
    monkeypatch.setattr(f, "shard", lambda t: 0)
    f.programs = {"Z-1-A": {"id": "p", "end_date": "2099-01-01T00:00:00Z", "target_size_fp": "1000",
                            "discount_factor_bps": 5000, "period_reward": 1800000,
                            "start_date": "2026-11-04T00:00:00Z"}}
    f.state["orders"] = {"Z-1-A|yes": {"oid": "oid0", "side": "yes", "price": 0.97, "size": lots, "remaining": lots,
                                       "placed": time.time() - placed_ago}}
    return f


def _guard(f, monkeypatch, bucket, yes_book, no_book, cfg=CFG):
    ob = {"yes_dollars": [[f"{p:.4f}", str(q)] for p, q in yes_book],
          "no_dollars": [[f"{p:.4f}", str(q)] for p, q in no_book]}
    monkeypatch.setattr(farm, "fetch_book", lambda t: ob)
    monkeypatch.setattr(farm, "queue_ahead", lambda c, ts: {"oid0": bucket.ahead})
    f.guard(cfg)


def test_guard_moves_back_only_with_size_behind_and_spare_budget(tmp_path, monkeypatch):
    # Book at 0.97 shows 4000 incl. our 1000 -> 3000 others; the no side qualifies on its own.
    for tokens, ahead, ago, moved in ((300.0, 1500.0, 120.0, True), (300.0, 2900.0, 120.0, False),
                                      (200.0, 0.0, 120.0, False), (300.0, 0.0, 1.0, False)):
        b = _Bucket(tokens, ahead)
        f = _live(tmp_path, monkeypatch, b, placed_ago=ago)
        _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 5000)])
        assert (b.cancelled == ["oid0"] and len(b.sent) == 1) is moved
        assert "Z-1-A|yes" in f.state["orders"]


def test_guard_pulls_a_bid_its_plan_dropped_and_cools_down(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 500)])     # no side under Target: excluded
    assert b.cancelled == ["oid0"] and not f.state["orders"]
    f.plans = {"Z-1-A": farm.Plan("Z-1-A", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002)}
    assert f.choose(CFG) == {}                                   # cooling down


def test_changed_lots_replaces_every_bid(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, lots=500, placed_ago=0.0)
    f.plans = {"Z-1-A": farm.Plan("Z-1-A", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002)}
    f.state["claims"] = {"Z-1-A": 0.0}
    f.reconcile(CFG)                                             # CFG.lots = 1000, bid rests 500
    assert b.cancelled == ["oid0"] and [o.size for o in b.sent] == [1000]


def test_arm_wide_plan_places_both_sides_and_disarm_pulls(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.live = False
    f.state["orders"] = {}
    f.set_live(True, "test")
    assert f.live and b.cancelled == ["old"]                     # only our stray, never another strategy's
    f.programs = {"W-1-A": {"id": "pw", "end_date": "2099-01-01T00:00:00Z"}}
    f.plans = {"W-1-A": farm.Plan("W-1-A", "wide", (("yes", 0.11), ("no", 0.37)), 1e-3, 480.0, 0.002)}
    monkeypatch.setattr(farm, "CLAIM_SETTLE_S", 0.0)
    monkeypatch.setattr(farm.time, "sleep", lambda s: None)
    f.reconcile(CFG)
    assert sorted((o.side, o.price, o.size) for o in b.sent) == [("no", 0.37, 1000), ("yes", 0.11, 1000)]
    assert all(o.client_order_id.startswith(farm.CID_PREFIX) for o in b.sent)
    with open(farm.CLAIMS_PATH) as fh:
        assert json.load(fh)["tickers"] == ["W-1-A"]
    f.set_live(False, "test")
    assert not f.live and sorted(b.cancelled) == ["oid1", "oid2", "old"]


def test_old_journal_keys_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    (tmp_path / "shadow").mkdir()
    (tmp_path / "shadow" / "state.json").write_text(json.dumps(
        {"t0": 0, "orders": {"Q-1-A": {"oid": "x", "side": "no", "price": 0.01, "size": 1, "remaining": 1,
                                       "placed": 0}}, "fills": [], "trade_ids": [], "accrued": {}}))
    assert list(farm.Farm().state["orders"]) == ["Q-1-A|no"]


def test_fill_is_hedged_on_the_other_side_with_the_pair_under_one_dollar(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.meta = {"Z-1-A": {}}
    f.state["fills"] = [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 600.0, "fee": 0.0,
                         "order_id": "oid0"}]
    f.books = {"Z-1-A": {"yes": {0.97: 3000.0}, "no": {0.02: 5000.0}}}
    f.hedge(CFG)
    assert not b.sent                                  # oid0 predates hedging: not ours to hedge
    f.state["hedged_oids"] = ["oid0"]
    f.hedge(CFG)
    assert [(o.side, o.price, o.size) for o in b.sent] == [("no", 0.02, 600.0)]
    assert "Z-1-A|no|h" in f.state["orders"]
    # No touch is 0.05 now: capped so the pair costs 0.99.
    f.books = {"Z-1-A": {"yes": {0.97: 3000.0}, "no": {0.05: 5000.0}}}
    f.hedge(CFG)
    assert not b.cancelled and len(b.sent) == 1 and f.state["orders"]["Z-1-A|no|h"]["price"] == 0.02
    # Hedge capital comes off max_capital.
    f.plans = {"Z-1-A": farm.Plan("Z-1-A", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002)}
    assert f.choose(dataclasses.replace(CFG, max_capital=975)) == {}
    assert list(f.choose(dataclasses.replace(CFG, max_capital=982))) == ["Z-1-A"]


def test_guard_never_pulls_a_hedge(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    f.meta = {"Z-1-A": {}}
    f.state["orders"]["Z-1-A|no|h"] = {"oid": "h1", "side": "no", "price": 0.02, "size": 600, "remaining": 600,
                                       "placed": time.time()}
    f.state["fills"] = [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 600.0, "fee": 0.0,
                         "order_id": "oid0"}]
    f.state["hedged_oids"] = ["oid0"]
    monkeypatch.setattr(f, "poll_fills", lambda: None)
    _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 1100)])    # 500 others + our 600 hedge: plan gone

    assert b.cancelled == ["oid0"] and list(f.state["orders"]) == ["Z-1-A|no|h"]
