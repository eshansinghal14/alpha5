import json

from kalshi_infra.trading import lip
from kalshi_strategies.LIPFARM.farm import best_pick, parse_book, remove_ours

P = lip.LipParams(target_size=1000, discount_factor=0.5, period_reward_usd=86.4, snapshots_per_period=86400)


def test_cheap_side_only_and_joins_touch():
    # yes decided: yes bids deep at 0.97, no side thin at 0.01.
    others = {"yes": {0.97: 5000.0}, "no": {0.01: 200.0}}
    pk = best_pick("T", others, P, lots=1000, max_price=0.05)
    assert pk.side == "no" and pk.price == 0.01
    # no side: 1000/(1200) share, yes side 0 -> half of that per snapshot.
    assert abs(pk.rate - (1000 / 1200) / 2 * 0.001) < 1e-12


def test_no_pick_when_cheap_side_too_expensive():
    others = {"yes": {0.60: 5000.0}, "no": {0.30: 5000.0}}
    assert best_pick("T", others, P, lots=1000, max_price=0.05) is None


def test_empty_side_bids_one_cent_unless_it_crosses():
    assert best_pick("T", {"yes": {0.97: 5000.0}, "no": {}}, P, 1000, 0.05).price == 0.01
    assert best_pick("T", {"yes": {0.99: 5000.0}, "no": {}}, P, 1000, 0.05) is None


def test_subcent_prices_survive_parsing():
    yes, no = parse_book({"yes_dollars": [["0.9850", "10"]], "no_dollars": [["0.0040", "5"]]})
    assert yes == {0.985: 10.0} and no == {0.004: 5.0}
    assert remove_ours(no, 0.004, 5.0) == {}


def test_arm_quotes_for_real_and_disarm_pulls(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kalshi_strategies.LIPFARM import farm

    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    monkeypatch.setattr(farm, "CLAIMS_PATH", str(tmp_path / "live" / "claimed.json"))
    sent, cancelled = [], []

    class FakeClient:
        tokens_per_s = 300.0
        COST = {"order": 10, "amend": 10, "cancel": 2}

        def can_afford(self, kind, reserve=0.0):
            return True

        def allow_now(self, kind, force=False, reason=""):
            return True

        def queue_positions(self, event):
            return {}

        def _resting(self, prefix):
            return [("old", "X-1-A", "lipf-old"), ("arbs", "Y-1-A", "someone-else")]

        def place_resting(self, order):
            sent.append(order)
            return SimpleNamespace(exchange_order_id=f"oid{len(sent)}", fill=None)

        def cancel(self, oid, ticker):
            cancelled.append(oid)
            return True

    f = farm.Farm()
    monkeypatch.setattr(f, "client", lambda shard: FakeClient())
    monkeypatch.setattr(f, "shard", lambda t: 0)
    f.set_live(True, "test")
    assert f.live and cancelled == ["old"]            # only our stray, never another strategy's
    cfg = farm.Cfg(num_markets=1, lots=1000, max_price=0.05, switch_margin=0.5, scan_markets=1, refresh_s=30,
                   programs_refresh_s=600)
    f.programs = {"Z-1-A": {"id": "p", "end_date": "2099-01-01T00:00:00Z"}}
    f.picks = {"Z-1-A": farm.Pick("Z-1-A", "no", 0.01, 0.001, 0.002)}
    monkeypatch.setattr(farm, "CLAIM_SETTLE_S", 0.0)
    monkeypatch.setattr(farm.time, "sleep", lambda s: None)
    f.reconcile(cfg)
    # A new claim is written (with positions) before the order goes out.
    with open(farm.CLAIMS_PATH) as fh:
        assert json.load(fh) == {"written_at": f.state["claims"]["Z-1-A"], "tickers": ["Z-1-A"], "positions": {}}
    assert [(o.ticker, o.side, o.price, o.size) for o in sent] == [("Z-1-A", "no", 0.01, 1000)]
    assert sent[0].client_order_id.startswith(farm.CID_PREFIX)
    f.set_live(False, "test")
    assert not f.live and cancelled == ["old", "oid1"]


def test_sticky_until_one_dollar_then_switch_margin(tmp_path, monkeypatch):
    from kalshi_strategies.LIPFARM import farm

    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    f = farm.Farm()
    cfg = farm.Cfg(num_markets=1, lots=1000, max_price=0.05, switch_margin=0.5, scan_markets=1, refresh_s=30,
                   programs_refresh_s=600)
    far = "2099-01-01T00:00:00Z"
    f.programs = {"OLD-1-A": {"id": "po", "end_date": far}, "NEW-1-A": {"id": "pn", "end_date": far},
                  "SOON-1-A": {"id": "ps", "end_date": "2000-01-01T00:00:00Z"}}
    f.state["orders"] = {"OLD-1-A": {"side": "no", "price": 0.01, "remaining": 1000}}
    f.picks = {"OLD-1-A": farm.Pick("OLD-1-A", "no", 0.01, 1e-4, 0.002),
               "NEW-1-A": farm.Pick("NEW-1-A", "no", 0.01, 9e-4, 0.002),
               "SOON-1-A": farm.Pick("SOON-1-A", "no", 0.01, 1.0, 0.002)}
    assert list(f.choose(cfg)) == ["OLD-1-A"]          # under $1 this period: kept; SOON can't reach $1
    f.state["period_accrued"] = {"po": 1.5}
    f.picks["NEW-1-A"] = farm.Pick("NEW-1-A", "no", 0.01, 1.4e-4, 0.002)
    assert list(f.choose(cfg)) == ["OLD-1-A"]          # past $1, newcomer only 40% better
    f.picks["NEW-1-A"] = farm.Pick("NEW-1-A", "no", 0.01, 1.6e-4, 0.002)
    assert list(f.choose(cfg)) == ["NEW-1-A"]


class _Bucket:
    tokens_per_s = 300.0
    COST = {"order": 10, "amend": 10, "cancel": 2}

    def __init__(self, tokens, ahead):
        self.tokens, self.ahead, self.sent, self.cancelled = tokens, ahead, [], []

    def can_afford(self, kind, reserve=0.0):
        return self.tokens >= self.COST[kind] + reserve

    def allow_now(self, kind, force=False, reason=""):
        if force or self.tokens >= self.COST[kind]:
            self.tokens -= self.COST[kind]
            return True
        return False

    def queue_positions(self, event):
        return {"oid0": self.ahead}

    def place_resting(self, order):
        from types import SimpleNamespace
        self.sent.append(order)
        return SimpleNamespace(exchange_order_id=f"oid{len(self.sent)}", fill=None)

    def cancel(self, oid, ticker):
        self.cancelled.append(oid)
        return True


def _live_farm(tmp_path, monkeypatch, bucket, lots=1000, placed_ago=120.0):
    from kalshi_strategies.LIPFARM import farm
    monkeypatch.setattr(farm, "ROOT", str(tmp_path))
    f = farm.Farm()
    f.live = True
    monkeypatch.setattr(f, "client", lambda shard: bucket)
    monkeypatch.setattr(f, "shard", lambda t: 0)
    f.state["orders"] = {"Z-1-A": {"oid": "oid0", "side": "no", "price": 0.01, "size": lots, "remaining": lots,
                                   "placed": __import__("time").time() - placed_ago}}
    f.books = {"Z-1-A": {"yes": {0.98: 5000.0}, "no": {0.01: 3000.0}}}
    cfg = farm.Cfg(num_markets=1, lots=1000, max_price=0.05, switch_margin=0.5, scan_markets=1, refresh_s=30,
                   programs_refresh_s=600)
    return farm, f, cfg, {"Z-1-A": farm.Pick("Z-1-A", "no", 0.01, 0.001, 0.002)}


def test_move_back_only_with_size_behind_and_spare_budget(tmp_path, monkeypatch):
    b = _Bucket(tokens=300.0, ahead=1500.0)           # 3000 others at 0.01, 1500 ahead -> 1500 behind
    farm, f, cfg, want = _live_farm(tmp_path, monkeypatch, b)
    f.move_back(cfg, want)
    assert b.cancelled == ["oid0"] and len(b.sent) == 1 and f.state["orders"]["Z-1-A"]["oid"] == "oid1"
    b = _Bucket(tokens=300.0, ahead=2500.0)           # only 500 behind: not worth a move
    farm, f, cfg, want = _live_farm(tmp_path, monkeypatch, b)
    f.move_back(cfg, want)
    assert not b.cancelled
    b = _Bucket(tokens=200.0, ahead=0.0)              # ARBS is using the bucket: wait
    farm, f, cfg, want = _live_farm(tmp_path, monkeypatch, b)
    f.move_back(cfg, want)
    assert not b.cancelled
    b = _Bucket(tokens=300.0, ahead=0.0)              # moved under a minute ago
    farm, f, cfg, want = _live_farm(tmp_path, monkeypatch, b, placed_ago=10.0)
    f.move_back(cfg, want)
    assert not b.cancelled


def test_changed_lots_replaces_every_quote(tmp_path, monkeypatch):
    b = _Bucket(tokens=300.0, ahead=0.0)
    farm, f, cfg, want = _live_farm(tmp_path, monkeypatch, b, lots=500, placed_ago=0.0)
    f.picks = want
    f.programs = {"Z-1-A": {"id": "p", "end_date": "2099-01-01T00:00:00Z"}}
    f.state["claims"] = {"Z-1-A": 0.0}
    monkeypatch.setattr(farm, "CLAIMS_PATH", str(tmp_path / "live" / "claimed.json"))
    f.reconcile(cfg)                                  # cfg.lots = 1000, quote rests 500
    assert b.cancelled == ["oid0"] and [o.size for o in b.sent] == [1000]
