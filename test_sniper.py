import bankroll as B
import informed as I
import sniper as SN

T0 = 1_800_000_000
GOOD = dict(n=12, won=0.75, edge=0.2, size=900, share=0.9, pnl=5000, cs2_pnl=5000, at=T0)


def _info(start, allpos=(), livepos=(), winner=None):
    return dict(venue="poly", match="Counter-Strike: A vs B (BO3) - Cup", start=start, market="Match Winner", cid="c1",
                ref="slug", url="u", teams=["A", "B"], prices={"A": 0.4, "B": 0.6}, tokens={"A": "ta", "B": "tb"},
                winner=winner, allpos=list(allpos), livepos=list(livepos))


def test_profile_rules():
    assert SN.is_sniper(GOOD)
    assert not SN.is_sniper(dict(GOOD, n=80))                     # too many bets: a regular, not a sniper
    assert not SN.is_sniper(dict(GOOD, share=0.2))                # mostly bets other things
    assert not SN.is_sniper(dict(GOOD, edge=0.0))
    h = dict(rows=[["2026-10-01", "cs2-x", "t", 0.5, True, 1000]] * 3 + [["2026-10-02", "cs2-y", "t", 0.4, False, 500]],
             n_closed=4, pnl=1, cs2_pnl=1)
    p = SN.profile_of(h)
    assert p["n"] == 4 and p["won"] == 0.75 and p["share"] == 1.0


def test_sniper_bet_pre_and_live_once():
    st = dict(profiles={"0xs": GOOD})
    pre = SN.signals(st, [_info(T0 + 3600, allpos=[["0xs", "c1", "A", 1500, 0.4, "s"]])], lambda m: "T3", T0)
    assert pre[0]["active"] and pre[0]["side"] == "A" and not pre[0]["live"]
    I.place_bets(st, pre, T0, lambda tok: [[0.42, 1000]], B.fill, log=lambda *a: None, tag="sniper")
    live = SN.signals(st, [_info(T0 - 600, livepos=[["0xs", "c1", "A", 2000, 0.5, "s", T0]])], lambda m: "T3", T0)
    I.place_bets(st, live, T0, lambda tok: [[0.5, 1000]], B.fill, log=lambda *a: None, live=True, tag="sniper")
    assert len(st["account"]["bets"]) == 1                        # once per market


def test_small_unknown_split_or_tier1_not_bet():
    st = dict(profiles={"0xs": GOOD, "0xt": GOOD})
    small = SN.signals(st, [_info(T0 + 3600, allpos=[["0xs", "c1", "A", 600, 0.4, ""]])], lambda m: "T3", T0)[0]
    unknown = SN.signals(st, [_info(T0 + 3600, allpos=[["0xz", "c1", "A", 9000, 0.4, ""]])], lambda m: "T3", T0)
    split = SN.signals(st, [_info(T0 + 3600, allpos=[["0xs", "c1", "A", 2000, 0.4, ""], ["0xt", "c1", "B", 1500, 0.6, ""]])],
                       lambda m: "T3", T0)[0]
    t1 = SN.signals(st, [_info(T0 + 3600, allpos=[["0xs", "c1", "A", 2000, 0.4, ""]])], lambda m: "T1", T0)[0]
    hand = SN.signals({}, [_info(T0 + 3600, allpos=[["0xh", "c1", "B", 1200, 0.6, ""]])], lambda m: "T3", T0, manual=["0xH"])[0]
    assert not small["active"] and unknown == [] and not split["active"] and not t1["active"] and hand["active"]
