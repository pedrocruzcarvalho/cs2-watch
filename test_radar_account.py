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


def _held(info, rows):
    return dict(info, heldpos=[[w, "c1", o, sh, avg, last] for w, o, sh, avg, last in rows])


def test_follows_listed_wallet_out_and_onto_its_new_side():
    st, log = {}, []
    g = _group([("0xfix", "B", 2600)], T0 - 600)
    g["live"] = True
    info = _info(T0 - 3600)
    sigs = RA.signals([g], [info], T0)
    I.place_bets(st, sigs, T0, lambda tok: [[0.75, 1000]], B.fill, log=log.append, live=True)
    assert st["account"]["bets"]["c1"]["side"] == "B"
    # still holding: nothing happens
    RA.follow(st, sigs, [_held(info, [("0xfix", "B", 10000, 0.26, 0)])], {"0xfix"}, T0 + 60,
              lambda tok: [[0.4, 1000]], B.sell, log=log.append)
    assert st["account"]["bets"]["c1"]["status"] == "open"
    # sold out after our bet: we sell too, at the bids
    RA.follow(st, sigs, [_held(info, [("0xfix", "B", 0, 0.26, T0 + 120)])], {"0xfix"}, T0 + 180,
              lambda tok: [[0.4, 1000]], B.sell, log=log.append)
    bets = st["account"]["bets"]
    assert "c1" not in bets
    sold = next(iter(bets.values()))
    assert sold["status"] == "sold" and sold["pnl"] < 0 and abs(sold["proceeds"] - sold["shares"] * (0.4 - 0.05 * 0.4 * 0.6)) < 0.02
    assert abs(I.balance(st["account"]) - (I.START_BALANCE + sold["pnl"])) < 0.01
    # the old side is not re-entered; the wallet's new side is bet
    same = RA.signals([g], [info], T0 + 600)
    RA.follow(st, same, [info], {"0xfix"}, T0 + 600, lambda tok: [], B.sell, log=log.append)
    assert not same[0]["active"]
    g2 = _group([("0xfix", "A", 4400)], T0 - 600)
    g2["live"] = True
    new = RA.signals([g2], [info], T0 + 900)
    RA.follow(st, new, [info], {"0xfix"}, T0 + 900, lambda tok: [], B.sell, log=log.append)
    I.place_bets(st, new, T0 + 900, lambda tok: [[0.5, 1000]], B.fill, log=log.append, live=True)
    assert st["account"]["bets"]["c1"]["side"] == "A" and st["account"]["bets"]["c1"]["status"] == "open"


def test_switching_sides_without_selling_also_exits_and_unlisted_wallets_are_not_followed():
    st = {}
    g = _group([("0xfix", "B", 2600)], T0 - 600)
    g["live"] = True
    info = _info(T0 - 3600)
    sigs = RA.signals([g], [info], T0)
    I.place_bets(st, sigs, T0, lambda tok: [[0.5, 1000]], B.fill, log=lambda *a: None, live=True)
    flipped = _held(info, [("0xfix", "B", 1000, 0.5, 0), ("0xfix", "A", 3000, 0.5, 0)])
    RA.follow(st, sigs, [flipped], set(), T0 + 60, lambda tok: [[0.5, 1000]], B.sell, log=lambda *a: None)
    assert st["account"]["bets"]["c1"]["status"] == "open"            # not a hand-picked wallet: not followed
    RA.follow(st, sigs, [flipped], {"0xfix"}, T0 + 60, lambda tok: [[0.5, 1000]], B.sell, log=lambda *a: None)
    assert "c1" not in st["account"]["bets"]
