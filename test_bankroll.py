import bankroll as B
import cs2_watch as C

T0 = 1_800_000_000


import pytest


@pytest.fixture(autouse=True)
def old_rule(monkeypatch):
    """Most tests describe the first-flag rule, which still governs every bet placed before the switch."""
    monkeypatch.setattr(B, "SWITCH_TS", 10 ** 12)
    monkeypatch.setattr(C, "TEAMS", {})


def rec(i, history, winner=None, poly=True, start=None, resolved=None):
    s = start if start is not None else T0 + 86400 + i * 3600
    return dict(mode="live", match=f"Counter-Strike: A vs B #{i}", start=s, level=history[-1][1] if history else "",
                tracked="A", price=0.5, teams=["A", "B"], refs={"poly": f"m{i}"} if poly else {"kalshi": f"k{i}"},
                urls={}, conflict=False, winner=winner,
                resolved_at=resolved if resolved is not None else (s + 7200 if winner else None),
                history=[dict(t=t, level=lvl, side=side, price=px, pts=0) for t, lvl, side, px in history])


def test_straight_high_bets_40_and_win_pays_shares_after_spread_and_fee():
    led = {"1": rec(1, [(T0, "HIGH", "A", 0.50)], winner="A")}
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    cost = 0.51 + 0.05 * 0.51 * 0.49
    assert bets[0]["stake"] == 40 and abs(bets[0]["pnl"] - round(40 / cost - 40, 2)) < 0.02
    assert st["wins"] == 1 and not bets[0]["escalated"]


def test_low_to_high_tops_up_to_one_and_a_half_times_high_at_the_new_price():
    led = {"1": rec(1, [(T0, "LOW", "A", 0.30), (T0 + 600, "MEDIUM", "A", 0.35), (T0 + 1200, "HIGH", "A", 0.45)],
                    winner="B")}
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    b = bets[0]
    assert [round(t["stake"], 2) for t in b["tranches"]] == [10, 20, 30]      # 10, then to 30 (1.5x20), then to 60
    assert [t["price"] for t in b["tranches"]] == [0.30, 0.35, 0.45]
    assert b["escalated"] and b["stake"] == 60 and b["pnl"] == -60 and st["balance"] == 240
    assert [p[0] for p in b["path"]] == ["LOW", "MEDIUM", "HIGH"]


def test_downgrade_keeps_money_in_and_side_flip_stops_new_money():
    led = {"1": rec(1, [(T0, "MEDIUM", "A", 0.4), (T0 + 600, "LOW", "A", 0.4), (T0 + 900, "HIGH", "B", 0.6)])}
    bets, st, _ = B.run(led, T0, T0 + 86400 + 7200)
    assert bets[0]["stake"] == 20 and bets[0]["flipped"] and bets[0]["side"] == "A"


def test_cannot_afford_is_skipped_and_only_polymarket_after_opening_before_kickoff():
    led = {str(i): rec(i, [(T0 + i, "HIGH", "A", 0.5)]) for i in range(9)}       # 9 x $40, none settled
    bets, st, _ = B.run(led, T0, T0 + 86400)
    assert st["open"] == 7 and st["skipped"] == 2 and abs(st["equity"] - 300) < 1e-9
    led = {"old": rec(1, [(T0 - 10, "LOW", "A", 0.5)]), "kalshi": rec(2, [(T0, "LOW", "A", 0.5)], poly=False),
           "late": rec(3, [(T0 + 86400 + 4 * 3600, "LOW", "A", 0.5)]), "ok": rec(4, [(T0, "LOW", "A", 0.5)])}
    bets, _, _ = B.run(led, T0, T0 + 3 * 86400)
    assert [b["match"] for b in bets] == ["Counter-Strike: A vs B #4"]


def test_ledger_history_records_only_changes():
    m = dict(id="x", match="Counter-Strike: A vs B", tier="low", start=T0 + 9999, teams=["A", "B"], level="LOW",
             pts=4, conflict=False, fix_side="A", tracked="A", tracked_is="flagged", price=0.4,
             prices={"A": 0.4, "B": 0.6}, refs={"poly": "s"}, urls={}, money={}, winner=None, flags=[], flow=0)
    led = {}
    C.record(led, [m], "live", T0)
    C.record(led, [dict(m, price=0.41, prices={"A": 0.41, "B": 0.59})], "live", T0 + 600)
    C.record(led, [dict(m, level="HIGH", pts=10, prices={"A": 0.45, "B": 0.55})], "live", T0 + 1200)
    h = led["live|x"]["history"]
    assert [(x["level"], x["price"]) for x in h] == [("LOW", 0.4), ("HIGH", 0.45)]


def test_explanation_reads_like_the_evidence_and_the_stake_steps():
    flag = dict(venue="poly", outcome="A", usd=18000, avg_price=0.35, share=0.69, level="HIGH", pts=11, why=[],
                name="fresh1", first=T0 - 3600, pre_match=True, move=0.12, x_p90=40,
                wallet_info=dict(created=T0 - 2 * 86400, traded=3, first_trade=T0 - 7200))
    snap = dict(flags=[flag], money={"A": 18000, "B": 0}, flow=26000, tier="low", conflict=False)
    led = {"1": rec(1, [(T0, "LOW", "A", 0.30), (T0 + 600, "HIGH", "A", 0.35)], winner="A")}
    for h in led["1"]["history"]:
        h["snap"] = snap
    bets, _, _ = B.run(led, T0, T0 + 10 * 86400)
    w = bets[0]["why"]
    text = w["headline"] + " ".join(b for s in w["sections"] for b in s["bullets"])
    assert w["headline"].startswith("$60 on A to beat B")
    assert "brand-new" in text and "only 3 markets" in text and "69%" in text
    assert "escalated to HIGH" in text and "1.5x" in text and "underdog" in text and "A won" in text
    assert [s["title"] for s in w["sections"]] == ["Why it was flagged", "Why this stake", "Context",
                                                   "Why it could be wrong", "Result"]


def test_bets_without_saved_evidence_get_it_backfilled_with_a_note():
    flag = dict(venue="kalshi", outcome="A", usd=26005, avg_price=0.40, share=0.53, level="HIGH", pts=9, why=[],
                first=T0, pre_match=True)
    led = {"live|x": rec(1, [(T0, "HIGH", "A", 0.42)])}
    m = dict(id="x", flags=[flag], money={"A": 26005, "B": 0}, flow=49000, tier="mid", conflict=False)
    assert C.backfill_snapshots(led, [m], T0 + 3600) == 1
    bets, _, _ = B.run(led, T0, T0 + 3600)
    text = " ".join(b for s in bets[0]["why"]["sections"] for b in s["bullets"])
    assert "anonymous order on Kalshi bought $26,005" in text and "not at the moment of the bet" in text


def _live_row(**kw):
    r = dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=T0, tier="low", outcome="A", usd=5000,
             avg_price=0.40, share=0.30, level="", pts=3, why=[], name="n", wallet="0x1", first=T0 + 600,
             pre_match=False, move=0, x_p90=0, wallet_info=dict(created=T0 - 2 * 86400, traded=40, first_trade=T0))
    r.update(kw)
    return r


def test_live_flag_needs_every_strict_condition():
    assert C.live_check(_live_row())                                            # fresh wallet, $5k, 30%, open price
    assert not C.live_check(_live_row(pre_match=True))                          # placed before kickoff
    assert not C.live_check(_live_row(usd=2500))                                # too small
    assert not C.live_check(_live_row(share=0.20))                              # not dominant
    assert not C.live_check(_live_row(avg_price=0.92))                          # result already decided
    assert not C.live_check(_live_row(wallet_info=dict(created=T0 - 400 * 86400, traded=40)))   # old, active
    assert C.live_check(_live_row(wallet_info=dict(created=T0 - 400 * 86400, traded=4)))        # old but 4 markets
    assert not C.live_check(_live_row(wallet_info=dict(created=T0 - 86400, traded=900)))        # busy trader
    assert not C.live_check(_live_row(venue="kalshi", wallet_info=None))                       # Kalshi needs $15k
    assert C.live_check(_live_row(venue="kalshi", wallet_info=None, usd=16000, share=0.55))


def test_live_flag_adds_20_once_on_the_same_side_never_against():
    led = {"1": rec(1, [(T0, "MEDIUM", "A", 0.40)], winner="A")}
    s = led["1"]["start"]
    led["1"]["history"] += [dict(t=s + 900, level="LIVE", side="A", price=0.30, pts=0, snap=None)]
    bets, st, _ = B.run(led, T0, s + 86400)
    assert bets[0]["stake"] == 40 and bets[0]["live"] and [p[0] for p in bets[0]["path"]] == ["MEDIUM", "LIVE"]
    led["1"]["history"][-1]["side"] = "B"                                        # live money on the other side
    bets, _, _ = B.run(led, T0, s + 86400)
    assert bets[0]["stake"] == 20 and not bets[0]["live"]
    only = {"2": rec(2, [])}
    only["2"]["history"] = [dict(t=only["2"]["start"] + 600, level="LIVE", side="B", price=0.35, pts=0, snap=None)]
    bets, _, _ = B.run(only, T0, only["2"]["start"] + 3600)
    assert bets[0]["level"] == "LIVE" and bets[0]["stake"] == 20 and bets[0]["side"] == "B"


def _minfo(market, outcomes, prices, venue="poly", winner=None):
    return dict(venue=venue, match="Counter-Strike: A vs B (BO3) - Cup", tier="low", start=T0, market=market,
                teams=outcomes, prices=prices, winner=winner, flow=1000, cid="c-" + market, ref="slug", url="u")


def _mrow(market, outcome, usd, lvl, pts):
    return dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=T0, tier="low", market=market,
                outcome=outcome, usd=usd, level=lvl, pts=pts, avg_price=0.3, share=0.2, mshare=0.7, why=[], name="",
                wallet="0x1", first=T0 - 600, pre_match=True)


def test_each_flagged_market_becomes_its_own_prediction_at_its_own_price():
    infos = [_minfo("Match Winner", ["A", "B"], {"A": 0.7, "B": 0.3}),
             _minfo("Map 1 Winner", ["A", "B"], {"A": 0.65, "B": 0.35}),
             _minfo("Map 1 Total Rounds: Over/Under 21.5", ["Over", "Under"], {"Over": 0.5, "Under": 0.5}),
             _minfo("Total Maps O/U 2.5", ["Over", "Under"], {"Over": 0.4, "Under": 0.6}, venue="kalshi")]
    ms = C.build_matches(infos, [_mrow("Map 1 Winner", "B", 6000, "HIGH", 10)])
    assert [m["market"] for m in ms] == ["Match Winner", "Map 1 Winner"]       # unflagged side markets skipped
    mw, m1 = ms
    assert mw["fix_side"] is None and mw["tracked_is"] == "favorite"            # map money doesn't leak into it
    assert m1["fix_side"] == "B" and m1["price"] == 0.35 and m1["level"] == "HIGH" and m1["kind"] == "map"
    assert m1["id"] == mw["id"] + "|Map 1 Winner" and m1["refs"]["poly_cid"] == "c-Map 1 Winner"


def test_market_labels_and_side_market_dominance_point():
    assert C.poly_market_label({"sportsMarketType": "totals", "groupItemTitle": "O/U 2.5 Games"}) == "Total Maps O/U 2.5"
    assert C.poly_market_label({"sportsMarketType": "moneyline", "groupItemTitle": "Match Winner"}) == "Match Winner"
    assert C.event_label({"event_ticker": "KXCS2TOTALMAPS-26OCT18X", "title": "x"}) == "Total Maps O/U 2.5"
    assert C.market_kind("Map 2 Winner") == "map" and C.market_kind("Map 1 Rounds Handicap: A (-3.5)") == "rounds"
    base = dict(net=1500, share=0.05, x_p90=1, pre_match=True, avg_price=0.5, move=0, first=T0)
    side, _ = C.score(dict(base, market="Map 1 Total Rounds: Over/Under 21.5", mshare=0.8), "mid", None, T0)
    main, _ = C.score(dict(base, market="Match Winner", mshare=0.8), "mid", None, T0)
    assert side == main + 1


def test_explanation_names_the_market():
    led = {"1": rec(1, [(T0, "MEDIUM", "B", 0.35)])}
    led["1"].update(market="Map 1 Winner", kind="map")
    bets, _, _ = B.run(led, T0, T0 + 3600)
    w = bets[0]["why"]
    assert w["headline"].startswith("$20 on B to win Map 1") and bets[0]["kind"] == "map"
    assert "single-map market" in " ".join(w["sections"][2]["bullets"])


def test_fill_walks_the_real_asks_and_handles_thin_or_empty_books():
    sh, spent, avg, used = B.fill(40, [[0.40, 50], [0.42, 1000]])
    per1, per2 = 0.40 + 0.05 * 0.4 * 0.6, 0.42 + 0.05 * 0.42 * 0.58
    assert used == 2 and abs(spent - 40) < 1e-9 and abs(sh - (50 + (40 - 50 * per1) / per2)) < 1e-9
    assert 0.40 < avg < 0.42
    sh, spent, avg, used = B.fill(40, [[0.40, 10]])                              # thin: only 10 shares for sale
    assert abs(sh - 10) < 1e-9 and spent < 5
    assert B.fill(40, []) == (0.0, 0.0, None, 0)


def test_book_fill_spends_only_what_filled_and_empty_book_skips():
    led = {"1": rec(1, [(T0, "HIGH", "A", 0.40)], winner="A"), "2": rec(2, [(T0, "LOW", "A", 0.40)])}
    led["1"]["history"][0]["asks"] = [[0.41, 20]]                                # thin book: ~$8.6 fills
    led["2"]["history"][0]["asks"] = []                                          # nobody selling
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    thin = next(b for b in bets if b["match"].endswith("#1"))
    empty = next(b for b in bets if b["match"].endswith("#2"))
    assert thin["stake"] < 9 and thin["tranches"][0]["book"] and abs(thin["shares"] - 20) < 1e-6
    assert empty["status"] == "skipped" and empty["tranches"][0]["reason"] == "no sellers"
    assert abs(st["balance"] - (300 - thin["stake"] + thin["shares"])) < 0.01
    text = " ".join(x for s in thin["why"]["sections"] for x in s["bullets"])
    assert "real order book" in text and "not enough sellers" in text


def _small(w, usd, created_days, traded, outcome="A", market="Match Winner"):
    return dict(match="Counter-Strike: A vs B (BO3) - Cup", tier="low", start=T0, market=market, outcome=outcome,
                wallet=w, name=w, usd=usd, avg_price=0.4, first=T0 - 3600, pre=usd, buy=usd,
                wallet_info=dict(created=T0 - created_days * 86400, traded=traded), match_flow=20000,
                market_flow=12000, url="u")


def test_split_bets_across_fresh_wallets_become_one_cluster_flag():
    small = [_small(f"w{i}", 1500, 2, 3) for i in range(4)] + [_small("pro", 1500, 400, 3000)]
    rows = C.find_clusters(small, [])
    assert len(rows) == 1
    c = rows[0]
    assert c["cluster"] == 4 and c["usd"] == 6000 and c["level"] and "pro" not in c["members_wallets"]
    assert not C.find_clusters(small[:2], [])                                    # 2 wallets is not a cluster
    m = C.judge_market([c, dict(_small("w0", 1500, 2, 3), level="LOW", pts=4, why=[], venue="poly", wallet="w0")],
                       ["A", "B"])
    assert m["money"]["A"] == 6000                                               # member not counted twice


def test_line_move_raises_the_level_one_step():
    m = dict(id="x", start=T0 + 5 * 3600, fix_side="B", teams=["A", "B"], prices={"A": 0.40, "B": 0.60},
             level="LOW", pts=5)
    led = {"live|x": dict(line=[[T0 - 3 * 3600, 0.55], [T0 - 3600, 0.50]])}     # B was 0.45, now 0.60
    C.apply_line_moves(led, [m], T0)
    assert m["level"] == "MEDIUM" and m["steam"]["move"] == 0.15 and m["steam"]["raised_from"] == "LOW"
    m2 = dict(m, level="LOW", prices={"A": 0.52, "B": 0.48}, steam=None)
    C.apply_line_moves(led, [m2], T0)
    assert m2["level"] == "LOW"                                                  # only 3c: no change



def test_size_floors_reflect_polymarket_stakes():
    assert C.level(12, 2500, 0.9) == ""                                          # $2.5k is ordinary money now
    assert C.level(12, 3500, 0.35) == "MEDIUM" and C.level(12, 3500, 0.1) == "LOW"
    assert C.level(12, 6000, 0.35) == "HIGH" and C.level(12, 6000, 0.1) == "MEDIUM"
    assert C.level(12, 12000, 0.05) == "HIGH"


def test_watched_wallets_get_a_level_floor():
    assert C.watch_level("", 600) == "MEDIUM" and C.watch_level("", 3500) == "HIGH"
    assert C.watch_level("HIGH", 600) == "HIGH" and C.watch_level("", 80) == ""


def _flag(wallet, match, won, usd=8000, level="HIGH"):
    return dict(venue="poly", wallet=wallet, name=wallet, level=level, match=match, start=T0, market="Match Winner",
                outcome="A", usd=usd, won=won)


def test_auto_watch_after_a_flagged_win_or_two_flagged_matches():
    auto = {}
    w = C.update_watch(auto, [_flag("x", "Counter-Strike: A vs B (BO3) - Cup", None)], T0)
    assert w == {}                                                               # one unresolved flag: not yet
    w = C.update_watch(auto, [_flag("x", "Counter-Strike: A vs B (BO3) - Cup", True)], T0 + 9000)
    assert "x" in w and "won" in w["x"]["reason"] and w["x"]["won"] == 1
    w = C.update_watch(auto, [_flag("y", "Counter-Strike: A vs B (BO3) - Cup", False),
                              _flag("y", "Counter-Strike: C vs D (BO3) - Cup", None)], T0)
    assert "flagged on 2 different matches" == w["y"]["reason"]
    w = C.update_watch(auto, [_flag("z", "Counter-Strike: E vs F (BO3) - Cup", True, level="LOW")], T0)
    assert "z" not in w                                                          # LOW flags don't count
    assert C.update_watch(auto, [], T0 + 61 * 86400) == {}                       # forgotten after 60 quiet days


def test_escalation_names_its_cause_and_level_adds_up():
    led = {"1": rec(1, [(T0, "LOW", "A", 0.49), (T0 + 600, "MEDIUM", "A", 0.51)])}
    h0, h1 = led["1"]["history"]
    h0["snap"] = dict(flags=[], base_level="LOW", base_pts=4, adjust=[], tier="low")
    bk = dict(kind="bookies", effect="up", book="Stake", start=0.44, now=0.51, since=T0 - 3600, move=0.07)
    h1["snap"] = dict(flags=[], base_level="LOW", base_pts=4, adjust=[bk], tier="low", sharp=dict(bk, agree=True))
    bets, _, _ = B.run(led, T0, T0 + 3600)
    w = bets[0]["why"]
    lvl = next(s for s in w["sections"] if s["title"] == "Why this level")["bullets"]
    assert lvl[0].startswith("The bets alone make this LOW (4 points")
    assert "bookmakers agree: at Stake" in lvl[1] and "44% to 51%" in lvl[1] and lvl[-1] == "Level now: MEDIUM."
    stake = next(s for s in w["sections"] if s["title"] == "Why this stake")["bullets"]
    assert "escalated to MEDIUM. It rose because of this: +1 level, bookmakers agree" in stake[1]


def test_early_bets_are_judged_against_the_money_expected_by_kickoff():
    assert C.traded_by_now(0) == 1.0 and abs(C.traded_by_now(1) - 0.526) < 1e-9
    assert abs(C.traded_by_now(12) - 0.003) < 1e-9 and 0.098 < C.traded_by_now(5) < 0.215
    # $5k is the only money in a mid-tier market 16 h before kickoff: judged against the typical $24k, not 100%
    assert C.expected_total(5000, 5000, 16, floor=24438) == 24438
    # 6 h out, $2k from others so far -> others scaled x10 (only ~10% has usually arrived)
    assert abs(C.expected_total(7000, 5000, 6, floor=0) - (5000 + 2000 / 0.10)) < 1e-6
    # after kickoff: real totals
    assert C.expected_total(7000, 5000, -1, floor=99999) == 7000



def test_new_rule_waits_for_the_last_30_minutes_and_keeps_earlier_bets(monkeypatch):
    monkeypatch.setattr(B, "SWITCH_TS", T0 + 1000)
    r = rec(1, [(T0, "LOW", "A", 0.40)], start=T0 + 20 * 3600)                 # placed before the switch: kept
    r["history"] += [dict(t=T0 + 5 * 3600, level="HIGH", side="A", price=0.45, pts=9, snap=None),   # outside window
                     dict(t=T0 + 20 * 3600 - 1200, level="HIGH", side="A", price=0.50, pts=9, snap=None, window=True)]
    bets, _, _ = B.run({"1": r}, T0, T0 + 30 * 3600)
    assert [round(t["stake"]) for t in bets[0]["tranches"]] == [10, 50]         # $10 kept, top-up only in the window
    assert bets[0]["tranches"][1]["price"] == 0.50
    r2 = rec(2, [(T0 + 2000, "LOW", "A", 0.40), (T0 + 3 * 3600, "MEDIUM", "A", 0.42)], start=T0 + 20 * 3600)
    assert B.run({"2": r2}, T0, T0 + 10 * 3600)[0] == []                      # nothing before the window
    r2["history"].append(dict(t=T0 + 20 * 3600 - 900, level="MEDIUM", side="A", price=0.44, pts=7, snap=None,
                              window=True))
    b = B.run({"2": r2}, T0, T0 + 30 * 3600)[0][0]
    assert b["stake"] == 30 and b["tranches"][0]["cause"]["rose"]              # rose LOW->MEDIUM: 1.5 x $20
    assert "last 30 minutes before kickoff" in " ".join(b["why"]["sections"][0]["bullets"] +
                                                         b["why"]["sections"][1]["bullets"])


def test_flags_that_need_an_established_team_to_throw_are_dropped(monkeypatch):
    monkeypatch.setattr(C, "TEAMS", {"teams": {"big": (25, 1500, "BIG"), "private": (95, 900, "PRIVATE"),
                                               "nobodies": (250, 500, "Nobodies")}})
    row = lambda out: dict(_mrow("Match Winner", out, 12000, "HIGH", 11), match="Counter-Strike: BIG vs PRIVATE (BO3) - Cup")
    m = C.judge_market([row("PRIVATE")], ["BIG", "PRIVATE"])                   # BIG (#25) would have to throw
    assert m["level"] == "" and m["dropped"]["team"] == "BIG"
    m = C.judge_market([row("BIG")], ["BIG", "PRIVATE"])                       # PRIVATE (#95, T3) would throw
    assert m["level"] == "MEDIUM" and m["adjust"][-1]["kind"] == "throw"
    m = C.judge_market([row("BIG")], ["BIG", "Nobodies"])                      # #250 would throw: unchanged
    assert m["level"] == "HIGH"
    assert C.vtier("Counter-Strike: BIG vs Nobodies (BO3) - Cup") == "T2" and C.tier("Counter-Strike: Nobodies vs Zed (BO3)") == "low"
