"""watch.py families: each basket pays at least its payout on every outcome
(checked by brute force over scores), and the rules checks hold."""
import itertools

from strategies.ARBS import watch

TAIL = "originally scheduled for Nov 22, 2026, then the market resolves to Yes."


def mk(ticker, sub="", strike=None, rules="", typ="greater", mid=0.5):
    m = {"ticker": ticker, "status": "active", "market_type": "binary", "yes_sub_title": sub,
         "rules_primary": rules, "yes_bid_dollars": f"{mid - 0.01:.2f}", "yes_ask_dollars": f"{mid + 0.01:.2f}",
         "occurrence_datetime": "2026-11-23T03:00:00Z"}
    if strike is not None:
        m.update(strike_type=typ, floor_strike=strike)
    return m


def ev(et, markets, crt="", shard=3, category="Sports"):
    return {"event_ticker": et, "series_ticker": et.split("-")[0], "collateral_return_type": crt,
            "exchange_index": shard, "category": category, "markets": markets}


def pays(b, outcome):
    """outcome: ticker -> bool (YES resolves)."""
    return sum(1.0 if outcome[l["ticker"]] == (l["side"] == "yes") else 0.0 for l in b["legs"])


def check_all(rels, truth, worlds):
    """Every basket pays at least its payout in every world; returns how many baskets."""
    worlds = list(worlds)
    n = 0
    for _, r, _ in rels:
        for b in r["baskets"]:
            for w in worlds:
                got = pays(b, {l["ticker"]: truth(l["ticker"], w) for l in b["legs"]})
                assert got >= b["payout"] - 1e-9, (b["basket_id"], w)
            n += 1
    return n


def total(et, strikes, scope="the A vs B Pro Basketball game "):
    return ev(et, [mk(f"{et}-{int(k + .5)}", f"Over {k} points", k,
                      f"If over {k} total points are scored in {scope}{TAIL}") for k in strikes])


def test_same_quantity_ladders_across_events_and_equal_strikes_both_ways():
    a = total("KXNBATOTAL-26NOV22AB", [199.5, 201.5])
    b = total("KXNBATOTALPRECISE-26NOV22AB", [200.5, 201.5])
    rels = watch.build([a, b], {3})
    ids = {x["basket_id"] for _, r, _ in rels for x in r["baskets"]}
    assert any(i.endswith("KXNBATOTAL-26NOV22AB-202=>KXNBATOTALPRECISE-26NOV22AB-202") for i in ids)
    assert any(i.endswith("KXNBATOTALPRECISE-26NOV22AB-202=>KXNBATOTAL-26NOV22AB-202") for i in ids)
    strikes = {m["ticker"]: m["floor_strike"] for e in (a, b) for m in e["markets"]}
    assert check_all(rels, lambda t, w: w > strikes[t], range(150, 260)) > 0
    # Another day's game with the same rules text isn't the same quantity.
    c = total("KXNBATOTAL-26NOV29AB", [200.5])
    c["markets"][0]["occurrence_datetime"] = "2026-11-30T03:00:00Z"
    assert not any("26NOV29" in l["ticker"] for _, r, _ in watch.build([a, c], {3})
                   for x in r["baskets"] for l in x["legs"])


def test_unnetted_ladders_hold_on_every_count_for_listed_series_only():
    et = "KXGTA6UNITS-01MAR27"
    e = ev(et, [mk(f"{et}-T{k}", f"At least {k} million", k, typ="greater_or_equal") for k in (33, 36, 39, 42, 45)],
           shard=0, category="Entertainment")
    assert not watch.build([e], {0})
    rels = watch.build([e], {0}, ladder_series={"KXGTA6UNITS"})
    ids = {x["basket_id"] for _, r, _ in rels for x in r["baskets"]}
    assert f"watch:{et}:ladder:T42=>T33" in ids and f"watch:{et}:ladder:T45=>T33" not in ids   # SPAN 3
    strikes = {m["ticker"]: m["floor_strike"] for m in e["markets"]}
    assert check_all(rels, lambda t, w: w >= strikes[t], range(30, 50)) == 3 + 3 + 2 + 1
    assert not watch.build([{**e, "collateral_return_type": "DIRECNET"}], {0}, ladder_series={"KXGTA6UNITS"})


def test_same_quantity_ladders_are_sports_only():
    a = total("KXNBATOTAL-26NOV22AB", [199.5, 201.5])
    b = total("KXNBATOTALPRECISE-26NOV22AB", [200.5, 201.5])
    for e in (a, b):
        e["category"] = "Economics"
    assert not watch.build([a, b], {3})


def test_protected_props_are_not_paired_with_plain_ones():
    def reb(series, secondary):
        et = f"{series}-26NOV22AB"
        e = ev(et, [mk(f"{et}-LUKA-{k}", f"Luka: {k}+", k - .5,
                       f"If Luka records {k}+ rebounds in the A vs B Pro Basketball game {TAIL}") for k in (8, 10)])
        for m in e["markets"]:
            m["rules_secondary"] = secondary
        return e
    plain = reb("KXNBAREB", "If a player is inactive, the market settles to a fair price.")
    prot = reb("KXNBAREBPROT", "This Contract is designated as protected: a player out after the 1st half "
                               "settles at the fair price unless he got there.")
    series = [{l["ticker"].split("-")[0] for l in b["legs"]}
              for _, r, _ in watch.build([plain, prot], {3}) for b in r["baskets"]]
    assert series and all(len(s) == 1 for s in series)


def test_a_strike_missing_from_the_rules_is_not_matched():
    e = ev("KXX-1", [mk("KXX-1-A", "", 5.5, "If it rains a lot."), mk("KXX-1-B", "", 7.5, "If it rains a lot.")])
    assert not watch.build([e], {3})


def _game(scope="the Denver vs Pittsburgh Pro Football game "):
    sp = ev("KXNFLSPREAD-26NOV22DENPIT", [
        mk(f"KXNFLSPREAD-26NOV22DENPIT-DEN{k}", f"Denver wins by over {k - .5} points", k - .5,
           f"If Denver wins by more than {k - .5} points in {scope}{TAIL}") for k in (1, 7, 15)]
        + [mk("KXNFLSPREAD-26NOV22DENPIT-PIT7", "Pittsburgh wins by over 6.5 points", 6.5,
              f"If Pittsburgh wins by more than 6.5 points in {scope}{TAIL}")], shard=0)
    buckets = []
    for lo, hi in ((1, 6), (7, 14), (15, None)):
        code = f"DEN{lo}PLUS" if hi is None else f"DEN{lo}TO{hi}"
        sub = f"Denver by {lo} or more" if hi is None else f"Denver by {lo} to {hi}"
        buckets.append(mk(f"KXNFLWINMARGIN-26NOV22DENPIT-{code}", sub, rules=f"If Denver wins by that in {scope}{TAIL}"))
    mg = ev("KXNFLWINMARGIN-26NOV22DENPIT", buckets, crt="MECNET", shard=0)
    gm = ev("KXNFLGAME-26NOV22DENPIT", [mk(f"KXNFLGAME-26NOV22DENPIT-{c}", n, rules=f"If {n} wins {scope}{TAIL}")
                                       for c, n in (("DEN", "Denver"), ("PIT", "Pittsburgh"))], crt="MECNET", shard=0)
    return sp, mg, gm


def _football_truth(t, margin):
    """margin: Denver's points less Pittsburgh's."""
    sfx = t.rsplit("-", 1)[-1]
    if t.startswith("KXNFLSPREAD"):
        k = int(sfx[3:])
        return (margin if sfx.startswith("DEN") else -margin) >= k
    if t.startswith("KXNFLGAME"):
        return margin > 0 if sfx == "DEN" else margin < 0
    if sfx.endswith("PLUS"):
        return margin >= int(sfx[3:-4])
    lo, hi = sfx[3:].split("TO")
    return int(lo) <= margin <= int(hi)


def test_spread_margin_and_winner_baskets_hold_on_every_margin():
    sp, mg, gm = _game()
    rels = watch.build([sp, mg, gm], {0})
    kinds = {r["relationship_id"].rsplit(":", 1)[-1] for _, r, _ in rels}
    assert {"margin", "winner"} <= kinds
    assert check_all(rels, _football_truth, range(-60, 61)) > 0
    ids = {x["basket_id"]: x for _, r, _ in rels for x in r["baskets"]}
    assert ids["watch:KXNFLSPREAD-26NOV22DENPIT:margin:yes:DEN7"]["payout"] == 2      # 7-14 and 15+


def test_cross_event_pairs_need_the_same_scope():
    sp, mg, gm = _game()
    for m in gm["markets"]:
        m["rules_primary"] = m["rules_primary"].replace(TAIL, "originally scheduled for Nov 22, 2026 in regulation only.")
    assert not any(r["relationship_id"].endswith(":winner") for _, r, _ in watch.build([sp, mg, gm], {0}))


def test_half_and_team_totals_add_up_on_every_score():
    g = "26NOV22AB"
    game = "the A vs B Pro Basketball game "
    tot = total(f"KXNBATOTAL-{g}", [x + .5 for x in range(190, 231, 3)], scope=game)
    h1 = ev(f"KXNBA1HTOTAL-{g}", [mk(f"KXNBA1HTOTAL-{g}-{k}", "", k - .5,
                                     f"If over {k - .5} total points are scored in the 1st half of {game}{TAIL}")
                                  for k in range(100, 116, 2)])
    h2 = ev(f"KXNBA2HTOTAL-{g}", [mk(f"KXNBA2HTOTAL-{g}-{k}", "", k - .5,
                                     f"If over {k - .5} total points are scored in the 2nd half (including overtime) of {game}{TAIL}")
                                  for k in range(95, 111, 2)])
    rels = [x for x in watch.build([tot, h1, h2], {3}) if x[1]["relationship_id"].endswith(":sum")]
    assert rels
    strikes = {m["ticker"]: m["floor_strike"] for e in (tot, h1, h2) for m in e["markets"]}

    def truth(t, w):
        a, b = w
        v = a if "1HTOTAL" in t else b if "2HTOTAL" in t else a + b
        return v > strikes[t]
    assert check_all(rels, truth, itertools.product(range(80, 131), range(80, 131))) > 0
    # Without overtime in the 2nd half, the halves don't make the game.
    for m in h2["markets"]:
        m["rules_primary"] = m["rules_primary"].replace(" (including overtime)", "")
    assert not [x for x in watch.build([tot, h1, h2], {3}) if x[1]["relationship_id"].endswith(":sum")]


def test_props_hold_on_every_stat_line():
    g = "26NOV22AB"

    def rules(k, what):
        return f"If Luka records {k}+ {what} in the A vs B Pro Basketball game {TAIL}"
    pts = ev(f"KXNBAPTS-{g}", [mk(f"KXNBAPTS-{g}-LUKA-{k}", f"Luka: {k}+", k - .5, rules(k, "points")) for k in (20, 25, 30)])
    pra = ev(f"KXNBAPRA-{g}", [mk(f"KXNBAPRA-{g}-LUKA-{k}", f"Luka: {k}+", k - .5, rules(k, "combined")) for k in (25, 30, 35)])
    thr = ev(f"KXNBA3PT-{g}", [mk(f"KXNBA3PT-{g}-LUKA-{k}", f"Luka: {k}+", k - .5, rules(k, "threes")) for k in (3, 4, 5)])
    rels = [x for x in watch.build([pts, pra, thr], {3}) if x[1]["relationship_id"].endswith(":props")]
    assert rels
    strikes = {m["ticker"]: m["floor_strike"] for e in (pts, pra, thr) for m in e["markets"]}

    def truth(t, w):
        threes, other_pts, extra = w
        p = 3 * threes + other_pts
        v = p if "-KXNBAPTS" in "-" + t else p + extra if "PRA" in t else threes
        return v > strikes[t]
    assert check_all(rels, truth, itertools.product(range(0, 9), range(0, 40), range(0, 25, 2))) > 0


def test_deadlines_hold_whenever_it_happens():
    e = ev("KXREL-27", [mk(f"KXREL-27-{d}", f"Before {d}", rules=f"If it happens before {d}.") for d in ("JAN", "MAR", "JUN")])
    for m, c in zip(e["markets"], ("2027-01-01", "2027-03-01", "2027-06-01")):
        m["close_time"] = m["expected_expiration_time"] = c + "T00:00:00Z"
    rels = watch.build([e], {3})
    order = {"JAN": 1, "MAR": 3, "JUN": 6}
    assert check_all(rels, lambda t, month: month < order[t.rsplit("-", 1)[-1]], range(0, 9)) == 3
