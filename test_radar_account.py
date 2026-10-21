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
