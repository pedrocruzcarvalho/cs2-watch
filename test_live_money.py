import bankroll as B
import informed as I
import live_money as LM

T0 = 1_800_000_000


def _info(livepos, start=T0 - 1800, winner=None):
    return dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=start, market="Match Winner", cid="c1",
                ref="slug", url="u", teams=["A", "B"], prices={"A": 0.6, "B": 0.4}, tokens={"A": "ta", "B": "tb"},
                winner=winner, livepos=livepos)


def test_big_inplay_bet_is_copied_once():
    st = {}
    sigs = LM.signals([_info([["0xw", "c1", "A", 3500, 0.58, "w", T0 - 60], ["0xs", "c1", "B", 1200, 0.4, "", T0]])],
                      lambda m: "T3", T0)
    assert sigs[0]["active"] and sigs[0]["side"] == "A" and sigs[0]["money"] == 3500
    I.place_bets(st, sigs, T0, lambda tok: [[0.62, 1000]], B.fill, log=lambda *a: None, live=True)
    I.place_bets(st, sigs, T0 + 600, lambda tok: [[0.63, 1000]], B.fill, log=lambda *a: None, live=True)
    bets = st["account"]["bets"]
    assert len(bets) == 1 and bets["c1"]["status"] == "open"
    I.settle(st, [_info([], winner="B")], T0 + 3 * 3600, lambda s, c: None)
    assert bets["c1"]["status"] == "lost" and bets["c1"]["pnl"] == -bets["c1"]["stake"]


def test_no_live_bet_before_kickoff_small_split_or_top_tier():
    pre = LM.signals([_info([["0xw", "c1", "A", 5000, 0.5, "", T0]], start=T0 + 600)], lambda m: "T3", T0)
    small = LM.signals([_info([["0xw", "c1", "A", 2500, 0.5, "", T0]])], lambda m: "T3", T0)[0]
    split = LM.signals([_info([["0xw", "c1", "A", 4000, 0.5, "", T0], ["0xv", "c1", "B", 2500, 0.5, "", T0]])],
                       lambda m: "T4", T0)[0]
    t2 = LM.signals([_info([["0xw", "c1", "A", 9000, 0.5, "", T0]])], lambda m: "T2", T0)[0]
    assert pre == [] and not small["active"] and not split["active"] and not t2["active"]


def test_decided_market_is_not_bought():
    st = {}
    sigs = LM.signals([_info([["0xw", "c1", "A", 6000, 0.95, "", T0]])], lambda m: "T5", T0)
    I.place_bets(st, sigs, T0, lambda tok: [[0.98, 1000]], B.fill, log=lambda *a: None, live=True)
    assert st["account"]["bets"] == {}


def test_pre_match_account_ignores_live_signals():
    st = {}
    sigs = LM.signals([_info([["0xw", "c1", "A", 6000, 0.6, "", T0]])], lambda m: "T3", T0)
    I.place_bets(st, sigs, T0, lambda tok: [[0.6, 1000]], B.fill, log=lambda *a: None)
    assert st["account"]["bets"] == {}
