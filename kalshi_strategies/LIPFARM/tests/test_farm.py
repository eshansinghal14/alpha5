import dataclasses
import json
import time
from types import SimpleNamespace

import pytest

from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM import farm
from kalshi_strategies.LIPFARM.farm import parse_book, plan_market, remove_ours

P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
CFG = farm.Cfg(max_capital=5000, num_markets=5, min_lots_frac=0.0, sentinel_keep=2.0, dump_slip_ticks=2, chase_ticks=2, max_chase_ticks=5,
               adverse_lots=300, exit_depth_frac=10.0, exit_depth_slip=0.03, wide_spread=0.20, min_leg_price=0.0, decided_min=0.97,
               min_ahead=0.0, max_position=1e9, write_tokens_per_s=5.0, scan_markets=1, scan_rotate=0, refresh_s=30,
               programs_refresh_s=600, flow_days=4.0, max_sweeps_day=0.5, max_trades_day=20.0,
               skip_categories="Sports,Mentions")


@pytest.fixture(autouse=True)
def _quiet_flow(monkeypatch):
    """No trade reads in tests: every market reads as having no flow."""
    monkeypatch.setattr(farm, "fetch_flow", lambda t, days: {"trades_day": 0.0, "sweeps_day": 0.0, "days": days,
                                                             "at": time.time()})


def test_decided_quotes_only_the_expensive_side():
    others = {"yes": {0.97: 200.0}, "no": {0.02: 5000.0}}
    pl = plan_market("T", others, P, CFG)
    assert pl.mode == "decided" and pl.legs == (("yes", 0.97),) and pl.capital == 970.0
    # yes side: our 1000 of 1200; no side: none of ours -> half of 1000/1200 per snapshot.
    assert abs(pl.rate - (1000 / 1200) / 2 * 0.001) < 1e-12


def test_decided_needs_the_other_side_to_qualify_on_others_alone():
    assert plan_market("T", {"yes": {0.97: 200.0}, "no": {0.02: 999.0}}, P, CFG) is None


def test_no_plan_at_99_or_in_between():
    assert plan_market("T", {"yes": {0.99: 200.0}, "no": {0.005: 5000.0}}, P, CFG) is None   # top price
    tapered = dataclasses.replace(P, price_ranges=((0.0, 0.1, 0.001), (0.1, 0.9, 0.01), (0.9, 1.0, 0.001)))
    assert plan_market("T", {"yes": {0.99: 200.0}, "no": {0.005: 5000.0}}, tapered, CFG).legs == (("yes", 0.99),)
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


def test_choose_ranks_per_dollar_and_caps_by_exposure_not_resting_bids(tmp_path, monkeypatch):
    a = farm.Plan("A-1-X", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002, 1000)
    b = farm.Plan("B-1-X", "wide", (("yes", 0.1), ("no", 0.4)), 1e-3, 500.0, 0.002, 1000)   # best per dollar
    c = farm.Plan("C-1-X", "decided", (("no", 0.98),), 5e-4, 980.0, 0.002, 1000)
    f = _farm(tmp_path, monkeypatch, {"A-1-X": a, "B-1-X": b, "C-1-X": c})
    # Resting bids hold no cash: all three fit $1000, each one's worst fill being under it.
    assert list(f.choose(dataclasses.replace(CFG, max_capital=1000))) == ["B-1-X", "A-1-X", "C-1-X"]
    # $500 already held: only a market whose biggest fill keeps us under $1000.
    monkeypatch.setattr(f, "exposure", lambda: 500.0)
    assert list(f.choose(dataclasses.replace(CFG, max_capital=1000))) == ["B-1-X"]


def test_quoted_past_one_dollar_first_then_under_then_new(tmp_path, monkeypatch):
    def pl(t, rate):
        return farm.Plan(t, "decided", (("yes", 0.97),), rate, 1000.0, 0.002, 1000)
    order = {"oid": "o", "side": "yes", "price": 0.97, "size": 1000, "remaining": 1000, "placed": 0}
    f = _farm(tmp_path, monkeypatch, {"PAST-1-A": pl("PAST-1-A", 1e-4), "UNDER-1-A": pl("UNDER-1-A", 2e-4),
                                      "NEW-1-A": pl("NEW-1-A", 9e-4)},
              {"PAST-1-A|yes": dict(order), "UNDER-1-A|yes": dict(order)})
    f.state["period_accrued"] = {"pPAST-1-A": 1.5, "pUNDER-1-A": 0.4}
    assert list(f.choose(dataclasses.replace(CFG, num_markets=3))) == ["PAST-1-A", "UNDER-1-A", "NEW-1-A"]
    assert list(f.choose(dataclasses.replace(CFG, num_markets=1))) == ["PAST-1-A"]


class _Bucket:
    tokens_per_s = 300.0
    ioc_fill = None                                              # contracts an IOC fills (None = all)
    COST = {"order": 10, "amend": 10, "cancel": 2}

    def __init__(self, tokens=300.0, ahead=0.0):
        self.tokens, self.ahead, self.sent, self.cancelled = tokens, ahead, [], []

    def can_afford(self, kind, reserve=0.0):
        return self.tokens >= self.COST[kind] + reserve

    def tokens_now(self):
        return self.tokens

    def allow_now(self, kind, force=False, reason=""):
        if force or self.tokens >= self.COST[kind]:
            self.tokens -= self.COST[kind]
            return True
        return False

    def _resting(self, prefix):
        return [("old", "X-1-A", "lipf-old"), ("arbs", "Y-1-A", "someone-else")]

    groups_made = 0

    def place_resting(self, order, order_group_id=None):
        self.sent.append(order)
        order.group = order_group_id
        return SimpleNamespace(exchange_order_id=f"oid{len(self.sent)}", fill=None)

    def create_order_group(self, limit):
        self.groups_made += 1
        return f"g{self.groups_made}"

    def reset_order_group(self, gid):
        self.resets = getattr(self, "resets", []) + [gid]

    def cancel(self, oid, ticker):
        self.cancelled.append(oid)
        return True

    def place_ioc(self, order):
        self.sent.append(order)
        q = order.size if self.ioc_fill is None else self.ioc_fill
        return SimpleNamespace(price=order.price, size=q, fee=0.0, exchange_order_id=f"ioc{len(self.sent)}")


def _live(tmp_path, monkeypatch, bucket, placed_ago=120.0, lots=1000):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    monkeypatch.setattr(farm, "CLAIMS_PATH", str(tmp_path / "live" / "claimed.json"))
    f = farm.Farm()
    f.live = True
    f.meta = {"Z-1-A": {"volume_24h_fp": "100000"}, "Z-1-B": {"volume_24h_fp": "100000"}}
    f.taker_rates = {"Z-1": 0.07}
    # Never a real socket in tests.
    f.ws = SimpleNamespace(up=False, ensure=lambda tickers: None, bids=lambda t: None,
                           take=lambda: (set(), []), wake=SimpleNamespace(set=lambda: None))
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
        _guard(f, monkeypatch, b, [(0.97, 4000), (0.95, 2000)], [(0.02, 5000)])
        assert (b.cancelled == ["oid0"] and len(b.sent) == 1) is moved
        assert "Z-1-A|yes" in f.state["orders"]


def test_guard_pulls_a_bid_its_plan_dropped_and_cools_down(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 500)])     # no side under Target: excluded
    assert b.cancelled == ["oid0"] and not f.state["orders"]
    f.plans = {"Z-1-A": farm.Plan("Z-1-A", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002, 1000)}
    assert f.choose(CFG) == {}                                   # cooling down


def test_changed_lots_replaces_every_bid(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, lots=500, placed_ago=0.0)
    f.plans = {"Z-1-A": farm.Plan("Z-1-A", "decided", (("yes", 0.97),), 1e-3, 970.0, 0.002, 1000)}
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
    f.plans = {"W-1-A": farm.Plan("W-1-A", "wide", (("yes", 0.11), ("no", 0.37)), 1e-3, 480.0, 0.002, 1000)}
    f.reconcile(CFG)
    assert not b.sent and set(f.pending) == {"W-1-A|yes", "W-1-A|no"}     # waits for ARBS, without blocking
    f.state["claims"]["W-1-A"] -= farm.CLAIM_SETTLE_S
    f.place_pending()
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
    # Bought at 0.91: the yes bid never falls 2 ticks under it here, so no chase.
    f.state["fills"] = [{"ticker": "Z-1-A", "side": "yes", "price": 0.91, "size": 600.0, "fee": 0.0,
                         "order_id": "oid0"}]
    f.books = {"Z-1-A": {"yes": {0.97: 3000.0}, "no": {0.02: 5000.0}}}
    f.hedge(CFG)
    assert not b.sent                                  # oid0 predates hedging: not ours to hedge
    f.state["hedged_oids"] = ["oid0"]
    f.hedge(CFG)
    assert [(o.side, o.price, o.size) for o in b.sent] == [("no", 0.02, 600.0)]
    assert "Z-1-A|no|h" in f.state["orders"]
    # The no touch moves to 0.05: the exit goes a tick above it, whatever the pair then costs.
    f.books = {"Z-1-A": {"yes": {0.90: 3000.0}, "no": {0.05: 5000.0}}}     # no offer 0.10
    f.state["orders"]["Z-1-A|no|h"]["placed"] -= farm.EXIT_REPRICE_S         # outbid, and old enough
    f.hedge(CFG)
    assert b.cancelled == ["oid1"] and f.state["orders"]["Z-1-A|no|h"]["price"] == 0.06
    # It never crosses: an offer 2c above stops it one tick under (step-ups only).
    f.books = {"Z-1-A": {"yes": {0.93: 3000.0}, "no": {0.06: 5000.0}}}     # no offer 0.07
    f.state["orders"]["Z-1-A|no|h"]["placed"] -= farm.EXIT_REPRICE_S
    f.hedge(CFG)
    assert f.state["orders"]["Z-1-A|no|h"]["price"] == 0.06
    # What we hold counts against max_capital: 600 x 0.91 = $546.
    assert abs(f.exposure() - 546.0) < 1e-9


def test_guard_never_pulls_a_hedge(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    f.state["orders"]["Z-1-A|no|h"] = {"oid": "h1", "side": "no", "price": 0.02, "size": 600, "remaining": 600,
                                       "placed": time.time()}
    f.state["fills"] = [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 600.0, "fee": 0.0,
                         "order_id": "oid0"}]
    f.state["hedged_oids"] = ["oid0"]
    monkeypatch.setattr(f, "poll_fills", lambda: None)
    _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 1100)])    # 500 others + our 600 hedge: plan gone

    assert b.cancelled == ["oid0"] and list(f.state["orders"]) == ["Z-1-A|no|h"]


def test_new_fill_is_exited_within_the_second(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.state["hedged_oids"] = ["oid0"]

    def poll():
        f.state["fills"] = [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 250.0, "fee": 0.0,
                             "order_id": "oid0"}]
        return {"Z-1-A"}
    monkeypatch.setattr(f, "poll_fills", poll)
    monkeypatch.setattr(farm, "fetch_book", lambda t: {"yes_dollars": [["0.9700", "4000"], ["0.9500", "2000"]],
                                                       "no_dollars": [["0.0200", "5000"]]})
    f.exit_now(CFG)
    assert [(o.side, o.price, o.size) for o in b.sent] == [("no", 0.02, 250.0)]
    assert "Z-1-A|yes" in f.state["orders"]          # the LIP bid keeps running beside the exit


def test_our_bucket_limits_lip_bids_but_never_exits(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.bucket = farm.TokenBucket(rate=0.0, capacity=25.0)        # room for two orders, no refill
    assert f.place("A-1-X", "yes", 0.1, 1000) and f.place("B-1-X", "yes", 0.1, 1000)
    assert not f.place("C-1-X", "yes", 0.1, 1000)                # our budget is spent: ARBS's is untouched
    assert f.place("D-1-X", "no", 0.9, 300, hedge=True)          # an exit goes anyway
    assert [o.ticker for o in b.sent] == ["A-1-X", "B-1-X", "D-1-X"] and f.bucket.tokens < 0


class _Ws:
    def __init__(self, books, fills=()):
        self._books, self._fills, self.up = books, list(fills), True
        self.wake = SimpleNamespace(set=lambda: None, wait=lambda timeout=None: None, clear=lambda: None)

    def bids(self, t):
        return self._books.get(t)

    def take(self):
        d, f, self._fills = set(self._books), self._fills, []
        return d, f

    def ensure(self, tickers):
        pass


def test_socket_fill_is_exited_from_the_socket_book(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.state["hedged_oids"] = ["oid0"]
    monkeypatch.setattr(farm, "fetch_book", lambda t: (_ for _ in ()).throw(AssertionError("no REST read")))
    f.ws = _Ws({"Z-1-A": ({0.97: 4000.0, 0.95: 2000.0}, {0.02: 5000.0})},
               [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 250.0, "fee": 0.0,
                 "order_id": "oid0", "trade_id": "t1", "time": None}])
    f.react(CFG)
    assert [(o.side, o.price, o.size) for o in b.sent] == [("no", 0.02, 250.0)]
    assert f.state["orders"]["Z-1-A|yes"]["remaining"] == 750.0
    f.ws._fills = [{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 250.0, "fee": 0.0,
                    "order_id": "oid0", "trade_id": "t1", "time": None}]
    f.react(CFG)                                                 # same trade again: not booked twice
    assert len(f.state["fills"]) == 1 and len(b.sent) == 1


def test_socket_book_change_pulls_a_bid_its_plan_dropped(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    f.ws = _Ws({"Z-1-A": ({0.97: 4000.0}, {0.02: 500.0})})      # no side under Target now
    f.react(CFG)
    assert b.cancelled == ["oid0"] and not f.state["orders"]


def _held(f, side="yes", price=0.97, size=600.0):
    f.state["fills"] = [{"ticker": "Z-1-A", "side": side, "price": price, "size": size, "fee": 0.0,
                         "order_id": "oid0", "trade_id": "t0"}]
    f.state["hedged_oids"] = ["oid0"]


def test_fill_locked_by_taking_the_pair_when_it_costs_under_a_dollar(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    _held(f)                                                     # long 600 yes at 0.97
    f.books = {"Z-1-A": {"yes": {0.985: 400.0}, "no": {0.01: 5000.0}}}   # no offer 0.015 x 400
    f.hedge(CFG, takes=True)
    ioc = [o for o in b.sent if o.intent == "take"]
    assert [(o.side, o.price, o.size) for o in ioc] == [("no", 0.015, 400)]
    assert not f.state.get("locked")                             # a pair nets at Kalshi; nothing to track


def test_fill_locked_through_an_arbs_basket(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    _held(f, price=0.60)                                         # long 600 yes A at 0.60
    f.books = {"Z-1-A": {"yes": {0.55: 3000.0}, "no": {0.40: 3000.0}},   # the pair would cost 0.60 + 0.45
               "Z-1-B": {"yes": {0.20: 3000.0}, "no": {0.70: 900.0}}}   # yes B offer 0.30 x 900
    f.baskets = {("Z-1-A", "yes"): [(1.0, (("Z-1-A", "yes"), ("Z-1-B", "yes")))]}
    f.hedge(CFG, takes=True)
    ioc = [o for o in b.sent if o.intent == "take"]
    assert [(o.ticker, o.side, o.price, o.size) for o in ioc] == [("Z-1-B", "yes", 0.3, 600)]
    assert f.state["locked"] == {"Z-1-A": 600.0, "Z-1-B": 600.0}
    assert f.free_positions() == {}                              # both legs locked, B booked from the IOC
    assert not any(farm.is_hedge(k) for k in f.state["orders"])
    # B's fill arriving later on the socket isn't booked a second time.
    assert f.book_fills([{"ticker": "Z-1-B", "side": "yes", "price": 0.3, "size": 600.0, "fee": 0.0,
                          "order_id": "ioc1", "trade_id": "real-1"}]) == set()


def test_a_fill_pulls_bids_that_could_take_us_past_max_capital(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)          # resting yes 0.97 x1000 = $970
    _held(f)                                                     # holding $582
    monkeypatch.setattr(f, "poll_fills", lambda: set())
    _guard(f, monkeypatch, b, [(0.97, 4000)], [(0.02, 5000)], cfg=dataclasses.replace(CFG, max_capital=1500))
    assert "oid0" in b.cancelled and "Z-1-A|yes" not in f.state["orders"]
    assert "Z-1-A|no|h" in f.state["orders"]                     # the exit stays


def test_exit_depth_counts_only_bids_below_ours(tmp_path, monkeypatch):
    cfg = dataclasses.replace(CFG, exit_depth_frac=1.0, exit_depth_slip=0.03)
    # no touch 0.05 with 1500 there and only 1c bids below: no exit depth on no.
    o = {"yes": {0.80: 1500.0, 0.78: 400.0}, "no": {0.05: 1500.0, 0.01: 9000.0}}
    assert farm.exit_lots(o, cfg, 1000) == {"yes": 400, "no": 0}
    P1 = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)
    pl = plan_market("T", o, P1, dataclasses.replace(cfg, wide_spread=0.15), {}, farm.exit_lots(o, cfg, 1000))
    assert pl.mode == "wide" and pl.legs == (("yes", 0.80),) and pl.lots == 400      # the 5c no leg isn't quoted


def test_sync_keeps_a_resting_order_the_list_missed_and_cancels_orphans(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)                          # journal: Z-1-A|yes = oid0
    statuses = {"oid0": "resting"}
    b._resting = lambda prefix: [("orph", "Z-1-A", "lipf-orph"), ("arbs1", "Z-1-A", "arbs-x")]
    b.order = lambda oid: {"status": statuses[oid]}
    f.sync_live_orders()
    assert "Z-1-A|yes" in f.state["orders"]                      # missed by the list, still resting: kept
    assert b.cancelled == ["orph"]                               # ours, unjournaled: cancelled; ARBS's untouched
    statuses["oid0"] = "canceled"
    f.sync_live_orders()
    assert "Z-1-A|yes" not in f.state["orders"]


def test_candidates_rank_by_what_we_earned_and_rotate_through_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    f = farm.Farm()
    prog = {"id": "p", "end_date": "2099-01-01T00:00:00Z", "target_size_fp": "1000", "discount_factor_bps": 5000,
            "start_date": "2026-11-04T00:00:00Z"}
    # BIG has the largest pool but paid us nothing when read; RT is small and pays.
    f.programs = {"BIG-1-A": dict(prog, period_reward=9000000), "RT-1-A": dict(prog, period_reward=100000),
                  "C-1-A": dict(prog, period_reward=50000), "D-1-A": dict(prog, period_reward=40000),
                  "E-1-A": dict(prog, period_reward=30000)}
    f.est = {"BIG-1-A": 0.0, "RT-1-A": 2e-6}
    cfg = dataclasses.replace(CFG, scan_markets=1, scan_rotate=2)
    first = f.candidates(cfg)
    assert first[0] == "RT-1-A" and len(first) == 3
    second = f.candidates(cfg)
    assert second[0] == "RT-1-A" and set(first[1:]).isdisjoint(second[1:])     # the slice moves on


def test_full_fill_is_informed_and_sold_into_the_book_at_once(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)          # LIP yes bid 0.97 x1000 = oid0
    f.state["lip_oid_size"] = {"oid0": 1000}
    f.state["hedged_oids"] = ["oid0"]
    f.books = {"Z-1-A": {"yes": {0.95: 300.0, 0.90: 2000.0}, "no": {0.02: 5000.0}}}
    f.book_fills([{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 1000.0, "fee": 0.0,
                   "order_id": "oid0", "trade_id": "t1"}])
    assert f.dumping == {"Z-1-A"}
    f.hedge(CFG)
    ioc = [o for o in b.sent if o.intent == "take"]
    # Filled at 0.97: sell at once down to 2 ticks under it (300 at 0.95); rest the other 700.
    assert [(o.side, o.price, o.size) for o in ioc] == [("no", 0.05, 300)]
    assert "oid0" in b.cancelled and f.state["cooldown"]["Z-1-A"] > time.time()
    assert f.state["cooldown"]["Z-1-A"] <= time.time() + farm.TRIP_COOLDOWN_S
    assert abs(f.free_positions()["Z-1-A"] - 700.0) < 1e-9 and "Z-1-A|no|h" in f.state["orders"]
    # The book keeps falling: best bid 0.94 is 3 ticks under our fill -> sell down to 0.92.
    f.books = {"Z-1-A": {"yes": {0.94: 400.0, 0.92: 500.0, 0.85: 9000.0}, "no": {0.02: 5000.0}}}
    f.state["chased"]["Z-1-A"] -= farm.EXIT_REPRICE_S
    f.hedge(CFG)
    ioc = [o for o in b.sent if o.intent == "take"]
    assert [(o.side, o.price, o.size) for o in ioc][-1] == ("no", 0.08, 700)
    # Then the book empties: best bid 0.80 is past max_chase_ticks (5) under our fill. No sale; the exit rests.
    f.books = {"Z-1-A": {"yes": {0.80: 50.0, 0.01: 9000.0}, "no": {0.02: 5000.0}}}
    f.state["chased"]["Z-1-A"] -= farm.EXIT_REPRICE_S
    n = len([o for o in b.sent if o.intent == "take"])
    f.hedge(CFG)
    assert len([o for o in b.sent if o.intent == "take"]) == n


def test_fill_under_adverse_lots_keeps_the_passive_exit(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    f.state["lip_oid_size"] = {"oid0": 1000}
    f.state["hedged_oids"] = ["oid0"]
    f.books = {"Z-1-A": {"yes": {0.96: 300.0}, "no": {0.02: 5000.0}}}      # 1 tick under: no chase
    f.book_fills([{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 299.0, "fee": 0.0,
                   "order_id": "oid0", "trade_id": "t1"}])
    assert not f.dumping
    f.hedge(CFG)
    assert not [o for o in b.sent if o.intent == "take"] and "Z-1-A|no|h" in f.state["orders"]


def test_exit_steps_up_only_when_outbid_and_not_too_often(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    _held(f, price=0.90, size=600.0)                             # bought at the 0.90 yes touch: no chase
    f.books = {"Z-1-A": {"yes": {0.90: 3000.0}, "no": {0.05: 5000.0}}}     # no offer 0.10
    f.hedge(CFG)
    assert f.state["orders"]["Z-1-A|no|h"]["price"] == 0.06
    f.books = {"Z-1-A": {"yes": {0.90: 3000.0}, "no": {0.03: 5000.0}}}     # touch drops: stay at 0.06
    f.hedge(CFG)
    f.books = {"Z-1-A": {"yes": {0.90: 3000.0}, "no": {0.07: 5000.0}}}     # outbid, but just placed: wait
    f.hedge(CFG)
    assert len(b.sent) == 1 and f.state["orders"]["Z-1-A|no|h"]["price"] == 0.06
    f.state["orders"]["Z-1-A|no|h"]["placed"] -= farm.EXIT_REPRICE_S
    f.hedge(CFG)
    assert len(b.sent) == 2 and f.state["orders"]["Z-1-A|no|h"]["price"] == 0.08


def test_dust_is_sold_into_the_book(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    _held(f, size=11.0)
    f.books = {"Z-1-A": {"yes": {0.95: 300.0}, "no": {0.02: 5000.0}}}
    f.hedge(CFG)
    assert [(o.side, o.price, o.size) for o in b.sent if o.intent == "take"] == [("no", 0.05, 11)]
    assert "Z-1-A|yes" in f.state["orders"]                     # dust isn't informed: LIP bids stay


def test_forced_spends_borrow_at_most_one_burst():
    bk = farm.TokenBucket(rate=0.0, capacity=60.0)
    for _ in range(50):
        bk.take(10.0, force=True)
    assert bk.tokens == -60.0


def test_fractional_dust_is_sold_exactly_and_never_churns(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    _held(f, size=10.69)
    f.books = {"Z-1-A": {"yes": {0.95: 300.0}, "no": {0.02: 5000.0}}}
    f.hedge(CFG)
    assert [(o.side, o.size) for o in b.sent if o.intent == "take"] == [("no", 10.69)]
    assert f.free_positions() == {}
    n = len(b.sent)
    for _ in range(5):
        f.hedge(CFG)
    assert len(b.sent) == n and not b.cancelled                 # nothing left: no more orders
    # Dust with nobody to sell to keeps one exit and doesn't re-place it.
    b2 = _Bucket()
    f2 = _live(tmp_path, monkeypatch, b2, placed_ago=1.0)
    _held(f2, size=0.69)
    f2.books = {"Z-1-A": {"yes": {}, "no": {0.02: 5000.0}}}
    for _ in range(5):
        f2.hedge(CFG)
    assert len([o for o in b2.sent if o.ticker == "Z-1-A" and o.side == "no"]) == 1


def test_wide_skips_legs_under_min_leg_price():
    others = {"yes": {0.70: 50.0, 0.68: 2000.0}, "no": {0.06: 20.0, 0.05: 5000.0}}
    cfg = dataclasses.replace(CFG, min_leg_price=0.10, exit_depth_frac=10.0)
    pl = plan_market("T", others, P, cfg, {}, farm.exit_lots(others, cfg, 1000))
    assert pl.mode == "wide" and pl.legs == (("yes", 0.70),)    # the 6c no leg isn't quoted


def test_moves_come_first_but_are_capped_per_pass(tmp_path, monkeypatch):
    b = _Bucket(tokens=300.0, ahead=1500.0)
    f = _live(tmp_path, monkeypatch, b)
    f.bucket = farm.TokenBucket(rate=0.0, capacity=120.0)
    f.bucket.tokens = 20.0                                       # enough for one move: it goes
    _guard(f, monkeypatch, b, [(0.97, 4000), (0.95, 2000)], [(0.02, 5000)])
    assert b.cancelled == ["oid0"]
    assert farm.MAX_MOVES * (farm.ORDER_TOKENS + farm.CANCEL_TOKENS) / farm.GUARD_S <= 12.0

def test_sentinel_layout_on_a_thin_touch_touch_layout_on_a_crowded_one():
    cfg = dataclasses.replace(CFG, sentinel_keep=0.8)
    thin = {"yes": {0.40: 50.0, 0.39: 300.0, 0.37: 2000.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    pl = plan_market("T", thin, P, cfg, {}, 1000)
    assert pl.layout["yes"] == "sentinel" and pl.big["yes"] == 0.39
    orders = farm.desired_orders(pl)
    assert orders["T|yes|s"] == (0.40, farm.SENTINEL_LOTS, True) and orders["T|yes"] == (0.39, 1000, True)
    crowded = {"yes": {0.40: 3000.0, 0.39: 300.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    pl = plan_market("T", crowded, P, cfg, {}, 1000)
    assert pl.layout["yes"] == "touch" and farm.desired_orders(pl)["T|yes"] == (0.40, 1000, False)


def test_sentinel_and_bid_go_in_the_sides_group_and_a_sentinel_fill_trips_it(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.state["orders"] = {}
    pl = farm.Plan("Z-1-A", "wide", (("yes", 0.40),), 1e-3, 390.0, 0.002, 1000, {"yes": "sentinel"}, {"yes": 0.39})
    f.plans = {"Z-1-A": pl}
    f.state["claims"] = {"Z-1-A": 0.0}
    f.reconcile(CFG)
    assert [(o.price, o.size, o.group) for o in b.sent] == [(0.39, 1000, "g1"), (0.40, farm.SENTINEL_LOTS, "g1")]
    sent_oid = f.state["orders"]["Z-1-A|yes|s"]["oid"]
    f.book_fills([{"ticker": "Z-1-A", "side": "yes", "price": 0.40, "size": 1.1, "fee": 0.0, "order_id": sent_oid,
                   "trade_id": "t1"}])
    assert f.state["groups"]["Z-1-A|yes"]["tripped"] and "Z-1-A" in f.dumping
    assert not f.keys_of("Z-1-A", hedges=False) and f.state["cooldown"]["Z-1-A"] > time.time()
    f.group("Z-1-A", "yes")                                      # re-placing later resets the group first
    assert b.resets == ["g1"] and not f.state["groups"]["Z-1-A|yes"]["tripped"]


def test_layout_has_hysteresis():
    cfg = dataclasses.replace(CFG, sentinel_keep=0.8)
    thin = {"yes": {0.40: 50.0, 0.39: 300.0, 0.37: 2000.0}, "no": {0.30: 3000.0, 0.28: 2000.0}}
    base = plan_market("T", thin, P, dataclasses.replace(cfg, sentinel_keep=0.0), {}, 1000)
    pl = plan_market("T", thin, P, cfg, {}, 1000)
    # Find a sentinel_keep just above what the sentinel layout keeps: a touch side stays touch,
    # a sentinel side stays sentinel.
    ratio = pl.rate / plan_market("T", thin, P, dataclasses.replace(cfg, sentinel_keep=2.0), {}, 1000).rate
    near = dataclasses.replace(cfg, sentinel_keep=ratio + 0.05)
    assert plan_market("T", thin, P, near, {}, 1000, current={"yes": "touch"}).layout["yes"] == "touch"
    assert plan_market("T", thin, P, near, {}, 1000, current={"yes": "sentinel"}).layout["yes"] == "sentinel"
    assert base.layout["yes"] == "sentinel"


def test_sentinel_main_bid_never_goes_out_alone(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b)
    f.state["orders"] = {}
    pl = farm.Plan("Z-1-A", "wide", (("yes", 0.40),), 1e-3, 390.0, 0.002, 1000, {"yes": "sentinel"}, {"yes": 0.39})
    f.plans = {"Z-1-A": pl}
    f.state["claims"] = {"Z-1-A": 0.0}
    f.bucket = farm.TokenBucket(rate=0.0, capacity=120.0)
    f.bucket.tokens = 25.0                                       # one order's worth after the group: neither goes
    f.reconcile(CFG)
    assert not b.sent and not f.state["orders"]
    f.bucket.tokens = 40.0
    f.reconcile(CFG)
    assert [(o.price, o.size) for o in b.sent] == [(0.39, 1000), (0.40, farm.SENTINEL_LOTS)]


def test_sides_sized_under_target_fifth_are_not_quoted():
    cfg = dataclasses.replace(CFG, min_lots_frac=0.2)
    others = {"yes": {0.97: 900.0}, "no": {0.02: 5000.0}}          # the yes side reaches Target with us
    assert plan_market("T", others, P, cfg, {}, 199) is None        # Target 1000: under 200 lots
    assert plan_market("T", others, P, cfg, {}, 200).lots == 200


def test_fill_of_adverse_lots_dumps_even_on_a_small_bid(tmp_path, monkeypatch):
    b = _Bucket()
    f = _live(tmp_path, monkeypatch, b, placed_ago=1.0)
    f.state["lip_oid_size"] = {"oid0": 5000}
    f.state["hedged_oids"] = ["oid0"]
    f.books = {"Z-1-A": {"yes": {0.96: 300.0}, "no": {0.02: 5000.0}}}
    f.book_fills([{"ticker": "Z-1-A", "side": "yes", "price": 0.97, "size": 300.0, "fee": 0.0,
                   "order_id": "oid0", "trade_id": "t1"}])
    assert f.dumping == {"Z-1-A"}                                       # 300 of 5000: 6%, still a dump


def test_bid_size_follows_the_programs_target():
    deep = {"yes": {0.40: 50.0, 0.39: 9000.0}, "no": {0.35: 50.0, 0.34: 9000.0}}
    for target in (1000, 2500):
        Pt = dataclasses.replace(P, target_size=target)
        assert plan_market("T", deep, Pt, CFG, {}, farm.exit_lots(deep, CFG, target)).lots == target


def _tr(ts, side, yes_px):
    return {"created_time": f"2026-11-05T12:00:{ts:06.3f}Z", "taker_side": side, "yes_price_dollars": f"{yes_px:.2f}"}


def test_count_sweeps_counts_bursts_that_cleared_a_level():
    trades = [_tr(0.0, "yes", 0.88), _tr(0.2, "yes", 0.89), _tr(0.4, "yes", 0.90),   # through 3 levels: 1
              _tr(5.0, "no", 0.50), _tr(5.5, "no", 0.50),                           # one level only: 0
              _tr(9.0, "no", 0.50), _tr(9.5, "no", 0.49),                           # 1
              _tr(20.0, "yes", 0.50), _tr(22.0, "yes", 0.60)]                       # > 1 s apart: 0
    assert farm.count_sweeps(trades) == 2


def test_narrow_market_needs_exit_liquidity_and_is_sized_by_it():
    others = {"yes": {0.45: 500.0, 0.44: 300.0}, "no": {0.50: 500.0, 0.49: 600.0}}
    assert plan_market("T", others, P, CFG) is None                     # 5c spread: needs exit
    pl = plan_market("T", others, P, dataclasses.replace(CFG, exit_depth_frac=1.0), {},
                     exit=farm.exit_lots(others, dataclasses.replace(CFG, exit_depth_frac=1.0), 1000))
    assert pl.mode == "narrow" and set(pl.legs) == {("yes", 0.45), ("no", 0.50)} and pl.lots == 300
    # No depth under a touch: that side isn't backed, so it isn't quoted.
    thin = {"yes": {0.45: 1500.0}, "no": {0.50: 500.0, 0.49: 600.0}}
    pl = plan_market("T", thin, P, CFG, {}, exit=farm.exit_lots(thin, CFG, 1000))
    assert pl.legs == (("no", 0.50),)


def test_wide_and_decided_quote_target_whatever_the_exit_depth():
    others = {"yes": {0.11: 2000.0}, "no": {0.37: 2000.0}}              # nothing under either touch
    pl = plan_market("T", others, P, CFG, {}, exit=farm.exit_lots(others, CFG, 1000))
    assert pl.mode == "wide" and pl.lots == 1000
    dec = {"yes": {0.97: 2000.0}, "no": {0.02: 5000.0}}
    assert plan_market("T", dec, P, CFG, {}, exit=farm.exit_lots(dec, CFG, 1000)).lots == 1000


def test_flow_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    f = farm.Farm()
    f.meta = {"S-1-A": {"event_ticker": "S-1"}, "Q-1-A": {"event_ticker": "Q-1"}}
    f.categories = {"S": "Sports", "Q": "Politics"}
    assert f.flow_ok("S-1-A", CFG) is False and not f.flow_futs       # skipped category, no read
    assert f.flow_ok("Q-1-A", CFG) is None                            # unread: read queued
    f.flow_futs["Q-1-A"].result()
    assert f.flow_ok("Q-1-A", CFG) is True and "Q-1-A" in f.state["flow"]
    f.state["flow"]["Q-1-A"].update(sweeps_day=0.75)                  # MT1's 0.75/day
    assert f.flow_ok("Q-1-A", CFG) is False
    f.state["flow"]["Q-1-A"].update(sweeps_day=0.0, trades_day=21.0)
    assert f.flow_ok("Q-1-A", CFG) is False


def test_scan_places_only_after_a_passing_flow_read(tmp_path, monkeypatch):
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    monkeypatch.setattr(farm, "arbs_held_events", lambda: set())
    f = farm.Farm()
    f.programs = {"Q-1-A": {"id": "p", "end_date": "2099-01-01T00:00:00Z", "target_size": 1000,
                            "discount_factor_bps": 5000, "period_reward": 8640000, "period_duration": 86400}}
    monkeypatch.setattr(f, "params", lambda t: P)
    ob = {"yes_dollars": [["0.97", "2000"]], "no_dollars": [["0.02", "5000"]]}
    f.scan_finish(CFG, [("Q-1-A", ob)])
    assert f.plans == {} and f.est["Q-1-A"] > 0                       # unread: not placed, keeps its rank
    f.flow_futs["Q-1-A"].result()
    f.scan_finish(CFG, [("Q-1-A", ob)])
    assert list(f.plans) == ["Q-1-A"]
