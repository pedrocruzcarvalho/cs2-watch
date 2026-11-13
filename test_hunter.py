import hunter as H

NOW = 1_800_000_000.0


def sig(**kw):
    u = dict(game="cs2", event="e1", slug="s", title="Counter-Strike: A vs B (BO3) - Cup", start=NOW + 3600, url="u",
             cid="c1", market="Match Winner", kind="match", w="0xw", name="n", o="A", other="B", phase="pre",
             usd=5000, px=0.6, vol=50000, share=0.1, last=NOW - 60, first=NOW - 600, prices={"A": 0.6, "B": 0.4},
             tokens={"A": "ta", "B": "tb"}, age=2.0, score=70, why=["x"], no=None, rule="A", side="A",
             exited=False, sold_frac=0.0, sold_usd=0.0, sold_at=None, hedge=False)
    u.update(kw)
    return u


def book(_):
    return [[0.6, 1000]]


def fill(amount, asks):
    p = asks[0][0]
    return amount / p, amount, p, 1


def test_one_bet_per_match_never_both_teams():
    st = {}
    H.place(st, [sig(), sig(o="B", other="A", side="B"), sig(cid="c2", market="Map 1 Winner")], NOW, log=lambda *_: None,
            book_asks=book, fill=fill)
    bets = st["account"]["bets"]
    assert list(bets) == ["e1|A"]
    assert bets["e1|A"]["stake"] == round(H.START * H.stake_pct(sig()), 2)


def test_rejected_and_stale_signals_are_not_bet():
    st = {}
    H.place(st, [sig(no="long shot"), sig(event="e2", rule=None), sig(event="e3", prices={"A": 0.95, "B": 0.05})],
            NOW, log=lambda *_: None, book_asks=book, fill=fill)
    assert st["account"]["bets"] == {}


def test_settle_and_balance():
    st = {}
    H.place(st, [sig()], NOW, log=lambda *_: None, book_asks=book, fill=fill)
    b = st["account"]["bets"]["e1|A"]
    H.settle(st, [dict(cid="c1", winner="A")], NOW + 7200)
    assert b["status"] == "won" and b["pnl"] > 0
    assert H.balance(st["account"]) == round(H.START + b["pnl"], 2)
    v = H.view(st, [], NOW + 7200)
    assert v["stats"]["wins"] == 1 and v["bets"][0]["match"] == "A vs B (BO3)" and v["bets"][0]["event"] == "Cup"


def test_cancelled_match_refunds():
    st = {}
    H.place(st, [sig()], NOW, log=lambda *_: None, book_asks=book, fill=fill)
    H.settle(st, [dict(cid="c1", winner=H.C.CANCELLED)], NOW + 7200)
    assert H.balance(st["account"]) == H.START


def u(**kw):
    return dict(dict(game="cs2", phase="live", kind="match", px=0.6, vol=50000, share=0.05, age=None, usd=2000), **kw)


def test_rules():
    assert H.classify(u())[0] is None                            # CS2 in-play, small share: no edge
    assert H.classify(u(game="dota"))[0] == "B"
    assert H.classify(u(team_live=9000, tier="T4", top=True))[0] is None   # tier 1/2: never
    assert H.classify(u(team_live=6000, tier="T4"))[0] == "A"      # 59% of a T4 match's normal money
    assert H.classify(u(team_live=2500, tier="T4"))[0] == "G"      # 25%
    assert H.classify(u(vol=300000))[0] is None
    assert H.classify(u(px=0.3))[0] is None
    assert H.classify(u(age=1))[0] is None                       # fresh CS2 accounts lose in-play
    assert H.classify(u(phase="pre"))[0] is None                 # CS2 pre-match money: no edge
    assert H.classify(u(phase="pre", age=5))[0] == "D"           # fresh CS2 account pre-match: bet against
    assert H.classify(u(phase="pre", game="dota", vol=80000))[0] == "C"
    assert H.classify(u(kind="map"))[0] is None                  # map, small share
    assert H.classify(u(kind="map", team_live=6000, tier="T4"))[0] == "A"
    assert H.classify(u(kind="map", share=0.2, phase="pre"))[0] is None


def test_fade_bets_the_other_team():
    st = {}
    H.place(st, [sig(rule="D", side="B", phase="pre")], NOW, log=lambda *_: None, book_asks=book, fill=fill)
    b = st["account"]["bets"]["e1|B"]
    assert b["side"] == "B" and b["their_side"] == "A"


def test_lol_rules():
    assert H.classify(u(game="lol"))[0] is None                                  # LoL in-play: no edge
    assert H.classify(u(game="lol", phase="pre", share=0.15))[0] == "E"
    assert H.classify(u(game="lol", phase="pre", share=0.05))[0] is None


def test_every_low_tier_signal_is_tracked_and_resolved():
    st = {}
    sigs = [sig(rule=None, no="pré-jogo sem vantagem no teste", tier="T4", top=False),
            sig(cid="c9", tier="T1", top=True)]
    H.track(st, sigs, [], NOW)
    assert list(st["signals"]) == ["c1|A|pre"]                    # tier 1 left out
    x = st["signals"]["c1|A|pre"]
    assert x["price"] == 0.6 and x["no"] and x["match"] == "A vs B (BO3)"
    H.track(st, [], [dict(cid="c1", winner="B")], NOW + 9000)
    assert x["status"] == "lost" and x["winner"] == "B"


def test_follow_the_wallet_out():
    st = {}
    H.place(st, [sig()], NOW, log=lambda *_: None, book_asks=book, fill=fill)
    H.follow(st, [sig(exited=True)], NOW + 600, log=lambda *_: None, book_bids=lambda _: [[0.7, 1000]],
             sell=lambda n, bids: (n, n * 0.7, 0.7, 1))
    b = st["account"]["bets"]["e1|A"]
    assert b["status"] == "sold" and b["pnl"] > 0 and b["exit_reason"] == "the wallet sold"
    assert H.balance(st["account"]) == round(H.START + b["pnl"], 2)
    import notify as N
    assert "EXIT A" in N.events([("Caçador", st)], NOW + 600)[0][0]


def test_several_small_bets_add_up():
    mk = {"m": dict(cid="m", kind="match", label="Match Winner", outcomes=["A", "B"], prices={"A": 0.6, "B": 0.4},
                    tokens={}, vol=40000),
          "k1": dict(cid="k1", kind="map", label="Map 1 Winner", outcomes=["A", "B"], prices={}, tokens={}, vol=5000)}
    trades = [dict(side="BUY", conditionId=c, proxyWallet="0xW", outcome="A", size=usd / 0.5, price=0.5, timestamp=ts,
                   name="w", pseudonym="") for c, usd, ts in (("m", 400, 10), ("k1", 300, 20), ("m", 350, 200))]
    pos = H.positions(trades, mk, start=100)
    e = dict(id="e1", slug="s", title="Counter-Strike: A vs B (BO3) - Cup")
    out = H.combos("cs2", e, mk, pos, [], 100, "u", 40000, "T4")
    assert len(out) == 1
    c = out[0]
    assert c["usd"] == 1050 and c["cid"] == "m" and c["phase"] == "live" and c["combo"]
    assert c["market"].endswith("(3)") and "soma de 3 apostas" in c["extra"][0]
