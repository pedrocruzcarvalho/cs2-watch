from datetime import datetime, timezone

import cs2_watch as C

EVENT = {"startTime": "2026-10-18T14:00:00Z", "markets": [
    {"conditionId": "m1", "groupItemTitle": "Match Winner", "closed": True,
     "outcomes": '["A", "B"]', "outcomePrices": '["0", "1"]'}]}
T0 = datetime(2026, 10, 18, 14, tzinfo=timezone.utc).timestamp()


def trade(w, side, outcome, size, price, ts):
    return dict(proxyWallet=w, side=side, outcome=outcome, size=size, price=price, timestamp=ts, conditionId="m1")


def test_sweeps_are_ignored_and_net_position_is_buys_minus_sells():
    trades = [trade("w1", "BUY", "B", 5000, 0.40, T0 - 3600),          # $2,000 pre-match on the underdog
              trade("w1", "SELL", "B", 1000, 0.50, T0 - 1800),         # sells $500 back
              trade("w2", "BUY", "B", 100000, 0.99, T0 + 9000),        # post-result sweep: ignored
              *[trade(f"f{i}", "BUY", "A", 50, 0.6, T0 - 7200 + i) for i in range(20)]]
    pos, stats = C.poly_positions(EVENT, trades)
    assert [p["wallet"] for p in pos] == ["w1"]
    p = pos[0]
    assert abs(p["net"] - 1500) < 1e-6 and p["pre_match"] and p["won"] is True
    assert abs(p["avg_price"] - 0.40) < 1e-9
    assert abs(stats["flow"] - 3100) < 1e-6                                        # sweep not counted as match money


def test_fresh_wallet_needs_real_size_to_be_high():
    fresh = dict(created=T0 - 86400, traded=2, first_trade=T0 - 3600)
    base = dict(share=0.5, x_p90=40, pre_match=True, avg_price=0.35, move=0.15, first=T0 - 3600)
    pts_small, _ = C.score(dict(base, net=800), "low", fresh, T0)
    pts_big, _ = C.score(dict(base, net=20000), "low", fresh, T0)
    assert C.level(pts_small, 800, 0.5) == ""
    assert C.level(pts_big, 20000, 0.5) == "HIGH"


def test_busy_trader_is_not_flagged():
    pro = dict(created=T0 - 400 * 86400, traded=3000, first_trade=T0 - 400 * 86400)
    p = dict(net=20000, share=0.1, x_p90=5, pre_match=True, avg_price=0.55, move=0.0, first=T0 - 3600)
    pts, _ = C.score(p, "mid", pro, T0)
    assert C.level(pts, 20000, 0.1) == ""


def test_tier_and_kalshi_ticker_time():
    assert C.tier("Counter-Strike: Vitality vs 9z (BO3) - PGL Masters Bucharest Group Stage") == "top"
    assert C.tier("Counter-Strike: Betclic Apogee Esports vs BBL (BO3) - PGL Bucharest Closed Qualifier") == "low"
    assert C.tier("BetBoom Team vs. FURIA") == "top"
    assert C.kalshi_start("KXCS2GAME-26OCT190400FORZERG1") == datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    assert C.match_key("Counter-Strike: FORZE Reload vs GenOne (BO3) - X") == C.match_key("FORZE Reload vs. GenOne: Map 1 Winner")


def _info(venue, title, start, prices, winner=None):
    return dict(venue=venue, match=title, tier="low", start=start, teams=["Alpha", "Bravo"], prices=prices,
                winner=winner, flow=10000, ref="x", url="u")


def _row(outcome, usd, lvl, pts, start):
    return dict(venue="poly", match="Counter-Strike: Alpha vs Bravo (BO3) - Cup", start=start, outcome=outcome,
                usd=usd, level=lvl, pts=pts, market="Match Winner", avg_price=0.3, share=0.4, why=[], name="",
                wallet="0x1", first=start - 3600)


def test_fix_side_is_where_flagged_money_went_and_conflict_downgrades():
    infos = [_info("poly", "Counter-Strike: Alpha vs Bravo (BO3) - Cup", T0, {"Alpha": 0.7, "Bravo": 0.3}),
             _info("kalshi", "Alpha vs. Bravo", T0 + 600, {"Alpha": 0.69, "Bravo": 0.31})]
    m = C.build_matches(infos, [_row("Bravo", 8000, "HIGH", 10, T0), _row("Alpha", 1000, "", 2, T0)])
    assert len(m) == 1 and m[0]["fix_side"] == "Bravo" and m[0]["level"] == "HIGH" and m[0]["price"] == 0.3
    m = C.build_matches(infos[:1], [_row("Bravo", 8000, "HIGH", 10, T0), _row("Alpha", 5000, "MEDIUM", 7, T0)])
    assert m[0]["conflict"] and m[0]["level"] == "MEDIUM"
    m = C.build_matches(infos[:1], [])
    assert m[0]["fix_side"] is None and m[0]["tracked"] == "Alpha" and m[0]["tracked_is"] == "favorite"


def test_live_prediction_freezes_at_kickoff_and_validation_scores_it():
    led = {}
    info = _info("poly", "Counter-Strike: Alpha vs Bravo (BO3) - Cup", T0, {"Alpha": 0.7, "Bravo": 0.3})
    C.record(led, C.build_matches([info], [_row("Bravo", 8000, "HIGH", 10, T0)]), "live", T0 - 3600)
    C.record(led, C.build_matches([dict(info, prices={"Alpha": 0.5, "Bravo": 0.5})], []), "live", T0 + 60)
    (r,) = led.values()
    assert r["fix_side"] == "Bravo" and r["price"] == 0.3                # after kickoff nothing changes
    r["winner"] = "Bravo"
    v = C.validation(led)["live"][0]
    assert v["level"] == "HIGH" and v["n"] == 1 and abs(v["edge"] - 0.7) < 1e-9


def test_kalshi_title_with_tournament_suffix_matches_polymarket():
    assert C.match_key("UNiTY esports vs. 9INE  [Stake Ranked Episode 6: Closed Qualifier Playoffs]") == \
        C.match_key("Counter-Strike: 9INE vs UNiTY esports (BO3) - Stake Ranked Episode 6: Closed Qualifier Playoffs")


def test_team_name_variants_across_venues_are_one_match():
    assert C.same_match(C.match_key("Counter-Strike: WBT vs Nexus (BO1) - DraculaN"), C.match_key("Nexus vs. WBT Academy"))
    assert not C.same_match(C.match_key("WBT vs Nexus"), C.match_key("WBT vs Nemiga"))
