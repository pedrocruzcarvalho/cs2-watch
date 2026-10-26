import bankroll as B
import informed as I
import radar_account as RA

T0 = 1_800_000_000


def _info(start):
    return dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=start, market="Match Winner", cid="c1",
                ref="slug", url="u", teams=["A", "B"], prices={"A": 0.4, "B": 0.6}, tokens={"A": "ta", "B": "tb"})


def _group(items, start, vt="T3"):
    return dict(match="Counter-Strike: A vs B (BO3) - Cup", market="Match Winner", start=start, vtier=vt,
                items=[dict(wallet=w, name="", outcome=o, usd=u, avg=0.4, tags=[dict(kind="big", label="big bet (LOW)")],
                            who="record: normal luck") for w, o, u in items])


def test_radar_side_is_bet_once_before_kickoff():
    st = {}
    sigs = RA.signals([_group([("0xa", "A", 5000), ("0xb", "B", 1000)], T0 + 3600)], [_info(T0 + 3600)], T0)
    assert sigs[0]["active"] and sigs[0]["side"] == "A" and sigs[0]["money"] == 5000
    I.place_bets(st, sigs, T0, lambda tok: [[0.42, 1000]], B.fill, log=lambda *a: None)
    I.place_bets(st, sigs, T0 + 600, lambda tok: [[0.42, 1000]], B.fill, log=lambda *a: None)
    assert len(st["account"]["bets"]) == 1 and st["account"]["bets"]["c1"]["side"] == "A"


def test_split_money_tier1_and_kalshi_only_are_not_bet():
    split = RA.signals([_group([("0xa", "A", 5000), ("0xb", "B", 3000)], T0 + 3600)], [_info(T0 + 3600)], T0)[0]
    t1 = RA.signals([_group([("0xa", "A", 5000)], T0 + 3600, "T1")], [_info(T0 + 3600)], T0, skip_tiers=("T1",))[0]
    kalshi = RA.signals([_group([("", "A", 9000)], T0 + 3600)], [_info(T0 + 3600)], T0)
    assert not split["active"] and not t1["active"] and kalshi == []


def test_kalshi_pre_match_order_maps_to_polymarket_and_is_bet():
    import cloud_run as CR
    import cs2_watch as C
    import sizing as Z
    C.TEAMS = {}
    poly = dict(venue="poly", match="Counter-Strike: Alpha vs Beta Esports (BO3) - Cup", start=T0 + 3600, market="Match Winner",
                cid="c1", ref="slug", url="u", teams=["Alpha", "Beta Esports"], prices={"Alpha": 0.45, "Beta Esports": 0.55},
                tokens={"Alpha": "ta", "Beta Esports": "tb"})
    krow = dict(venue="kalshi", match="Alpha vs. Beta  [Cup]", market="Match Winner", start=T0 + 3600, outcome="Alpha",
                usd=2500, avg_price=0.45)
    small = dict(krow, usd=600)
    groups = CR.kalshi_pre([krow, small], [poly], T0)
    assert len(groups) == 1 and groups[0]["items"][0]["outcome"] == "Alpha" and len(groups[0]["items"]) == 1
    sigs = RA.signals(groups, [poly], T0, pre_match_only_list=True)
    assert sigs[0]["active"] and sigs[0]["side"] == "Alpha"
    assert Z.radar_pct(sigs[0]) == 0.04
    st = {}
    I.place_bets(st, sigs, T0, lambda tok: [[0.46, 1000]], B.fill, log=lambda *a: None, stake_fn=Z.RADAR)
    assert abs(st["account"]["bets"]["c1"]["stake"] - 12.0) < 0.01         # 4% of $300
