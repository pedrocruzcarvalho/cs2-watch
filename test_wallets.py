import wallets as W


def test_record_compares_wins_with_what_the_prices_predicted():
    r = W.record([(0.5, True), (0.5, False)] * 10)
    assert r["n"] == 20 and r["win_rate"] == 0.5 and r["edge"] == 0.0 and r["z"] == 0.0
    r = W.record([(0.4, True)] * 16 + [(0.4, False)] * 4)                     # 80% won at 0.40
    assert r["edge"] == 0.4 and r["z"] > 3


def test_losers_left_in_open_positions_are_counted(monkeypatch):
    closed = [dict(avgPrice=0.5, curPrice=1, slug="cs2-a", totalBought=100)] * 10
    open_ = [dict(avgPrice=0.5, curPrice=0, slug="cs2-b", initialValue=50, redeemable=False)] * 10
    open_ += [dict(avgPrice=0.5, curPrice=0.55, slug="cs2-c", initialValue=50)]          # still live: ignored
    monkeypatch.setattr(W, "get", lambda url: closed if "closed-positions" in url else open_)
    prof = W.build("0xabc")
    assert prof["all"]["n"] == 20 and prof["all"]["wins"] == 10 and prof["verdict"] == "lucky"


def _prof(all_, cs2=None, flagged=None, traded=50):
    return dict(all=all_, cs2=cs2 or all_, flagged=flagged or dict(n=0), traded=traded)


def test_verdicts():
    luck = W.record([(0.5, True)] * 11 + [(0.5, False)] * 9)
    skill = W.record([(0.4, True)] * 16 + [(0.4, False)] * 4)
    fl_good = W.record([(0.35, True)] * 3)
    assert W.verdict(_prof(luck))[0] == "lucky"
    assert W.verdict(_prof(skill, traded=50))[0] == "suspect"                  # beats prices, CS2-only, few markets
    assert W.verdict(_prof(skill, cs2=dict(n=2), traded=900))[0] == "sharp"   # beats prices across many markets
    assert W.verdict(_prof(luck, flagged=fl_good))[0] == "suspect"            # 3/3 flagged wins at 0.35
    assert W.verdict(_prof(W.record([(0.5, True)] * 3)))[0] == "unclear"      # too few bets
