import bankroll as B
import informed as I

T0 = 1_800_000_000


def _state():
    st = dict(records={}, names={})
    st["records"]["0xgood"] = [[T0 - 86400 * d, 1, 0.45, 300] for d in range(1, 9)] + [[T0 - 86400 * 10, 0, 0.5, 300]]
    st["records"]["0xmeh"] = [[T0 - 86400 * d, d % 2, 0.5, 300] for d in range(1, 12)]
    return st


def _info(start, allpos, winner=None):
    return dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=start, market="Match Winner", cid="c1",
                ref="slug", url="u", teams=["A", "B"], prices={"A": 0.45, "B": 0.55}, tokens={"A": "tok"}, winner=winner,
                allpos=allpos)


def test_record_and_qualification():
    q = I.qualifying(_state(), T0)
    assert "0xgood" in q and "0xmeh" not in q and q["0xgood"]["n"] == 9


def test_signal_is_copied_at_once_and_only_once():
    st = _state()
    q = I.qualifying(st, T0)
    info = _info(T0 + 5 * 3600, [["0xgood", "c1", "A", 250, 0.44, "g"], ["0xmeh", "c1", "B", 900, 0.55, "m"]])
    sigs = I.signals([info], q, lambda m: "T4", T0)
    assert sigs[0]["active"] and sigs[0]["side"] == "A" and sigs[0]["money"] == 250
    I.place_bets(st, sigs, T0, lambda tok: [[0.46, 1000]], B.fill, log=lambda *a: None)
    I.place_bets(st, sigs, T0 + 600, lambda tok: [[0.47, 1000]], B.fill, log=lambda *a: None)
    bets = st["account"]["bets"]
    assert len(bets) == 1 and bets["c1"]["status"] == "open" and abs(bets["c1"]["stake"] - 20) < 1e-6
    I.settle(st, [_info(T0 + 5 * 3600, [], winner="A")], T0 + 9 * 3600, lambda s, c: None)
    assert bets["c1"]["status"] == "won" and bets["c1"]["pnl"] > 0


def test_no_signal_on_t2_small_or_split_money():
    q = I.qualifying(_state(), T0)
    small = I.signals([_info(T0 + 3600, [["0xgood", "c1", "A", 150, 0.44, ""]])], q, lambda m: "T4", T0)[0]
    t2 = I.signals([_info(T0 + 3600, [["0xgood", "c1", "A", 500, 0.44, ""]])], q, lambda m: "T2", T0)[0]
    assert not small["active"] and not t2["active"]
