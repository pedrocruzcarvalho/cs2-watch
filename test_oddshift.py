import bankroll as B
import informed as I
import oddshift as OS

T0 = 1_800_000_000


def _info(start, home_px, score=None, live=True, winner=None):
    return dict(venue="poly", match="Counter-Strike: Damajuana vs your end (BO3) - Cup", start=start, market="Match Winner",
                cid="c1", ref="slug", url="u", teams=["Damajuana", "your end"],
                prices={"Damajuana": home_px, "your end": 1 - home_px}, tokens={"Damajuana": "ta", "your end": "tb"},
                winner=winner, score=score, live=live)


def test_parse_and_model():
    assert OS.parse_score("13-8|1-0|Bo3") == (13, 8, 1, 0, 3)
    assert OS.parse_score("000-000|1-0|Bo3") is None                   # maps only, no rounds
    assert OS.parse_score("garbage") is None
    assert OS.parse_score("000-000|0-0|Bo3") is None                   # feed has no live score
    assert OS.parse_score("6-1|0-0|Bo3") == (6, 1, 0, 0, 3)
    assert abs(OS.model((0, 0, 0, 0, 3), 0.7) - 0.7) < 0.01           # no score yet: the pre-match price
    assert OS.model((1, 0, 1, 0, 3), 0.5) > 0.75                       # a map up and leading the next
    assert 0.6 < OS.model((14, 12, 1, 1, 3), 0.5) < 0.95              # overtime lead, not a finished map


def test_price_against_scoreboard_bets_the_market_side_once():
    st = {}
    OS.signals(st, [_info(T0 + 600, 0.5, live=False)], lambda m: "T4", T0)        # remembers the pre-match price
    sigs = OS.signals(st, [_info(T0 - 3600, 0.35, "1-0|1-0|Bo3")], lambda m: "T4", T0)
    s = sigs[0]
    assert s["active"] and s["leader"] == "Damajuana" and s["side"] == "your end" and s["gap"] > 0.35
    I.place_bets(st, sigs, T0, lambda tok: [[0.66, 1000]], B.fill, log=lambda *a: None, live=True)
    I.place_bets(st, sigs, T0 + 600, lambda tok: [[0.66, 1000]], B.fill, log=lambda *a: None, live=True)
    bets = st["account"]["bets"]
    assert len(bets) == 1 and bets["c1"]["side"] == "your end" and bets["c1"]["score"]["maps"] == [1, 0]
    assert st["snaps"] and st["snaps"][0][2] == "1-0|1-0|Bo3"


def test_no_signal_when_price_matches_score_or_top_tier_or_no_prematch_price():
    st = {}
    OS.signals(st, [_info(T0 + 600, 0.5, live=False)], lambda m: "T4", T0)
    fair = OS.signals(st, [_info(T0 - 3600, 0.75, "1-0|1-0|Bo3")], lambda m: "T4", T0)[0]
    top = OS.signals(st, [_info(T0 - 3600, 0.2, "1-0|1-0|Bo3")], lambda m: "T1", T0)[0]
    assert not fair["active"] and not top["active"]
    assert OS.signals({}, [_info(T0 - 3600, 0.2, "1-0|1-0|Bo3")], lambda m: "T4", T0) == []
