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
    assert len(bets) == 2 and {b["side"] for b in bets.values()} == {"A"}   # one per market, never both teams of one


def test_rejected_and_stale_signals_are_not_bet():
    st = {}
    H.place(st, [sig(no="long shot"), sig(event="e2", rule=None), sig(event="e3", prices={"A": 0.95, "B": 0.05})],
            NOW, log=lambda *_: None, book_asks=book, fill=fill)
    assert st["account"]["bets"] == {}


def test_settle_and_balance():
    st = {}
    H.place(st, [sig()], NOW, log=lambda *_: None, book_asks=book, fill=fill)
    b = st["account"]["bets"]["e1|c1|A"]
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
    b = st["account"]["bets"]["e1|c1|B"]
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
    b = st["account"]["bets"]["e1|c1|A"]
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


def test_shyux_rule():
    s = dict(shyux="swiftfoxx", team_pre=800, team_live=400)
    assert H.classify(u(**s))[0] == "S"                                   # $1.2k on one CS2 team, all markets
    assert H.classify(u(**dict(s, team_live=0)))[0] != "S"                # $800: too small
    assert H.classify(u(**dict(s, shyux_opp=True)))[0] != "S"             # another shyux wallet on the other team
    assert H.classify(u(**dict(s, px=0.9)))[0] != "S"                     # odds too short
    assert H.classify(u(**dict(s, game="dota")))[0] != "S"               # CS2 only


def test_dota_tier1_teams_are_top():
    assert H.tier_of("dota", "Dota 2: Team Liquid vs LGD Gaming (BO3) - 1win Essence Group C") == "T1-2"
    assert H.tier_of("dota", "Dota 2: PlayTime vs Yellow Submarine (BO3) - 1win Essence Play-in") == "T3+"
    assert H.tier_of("dota", "Dota 2: PuckChamp vs Spirit Academy (BO3) - Asgard Championship") == "T3+"


def test_push_only_strong_one_sided_flags():
    import cloud_run as CR
    f = lambda **k: dict(dict(side="A", rule="A", odds=1.6, late=None, score=80, kind="match", market="M", live=True,
                              usd=5000, name="n", why="w"), **k)
    m = lambda *fl, ev="e1": dict(event_id=ev, flags=list(fl), match="A vs B", event="Cup", tier="T4", url="u")
    assert CR.pushable(m(f()))["rule"] == "A"
    assert CR.pushable(m(f(rule="F"))) is None                            # weak rule: page only
    assert CR.pushable(m(f(odds=1.05))) is None                           # 0.95: nothing to win
    assert CR.pushable(m(f(), f(side="B", rule="S"))) is None             # both teams flagged: no push
    st = {}
    assert CR.new_fixes(st, dict(upcoming=[m(f())]), NOW)
    assert CR.new_fixes(st, dict(upcoming=[m(f(side="B"))]), NOW + 60) is None   # one push per match, ever


def test_fishy_score():
    H.SHYUX.setdefault("0xs", "shy")
    pos = lambda w, o, usd, first=0: dict(w=w, o=o, pre=usd, live=0, first=first, cid="c")
    big = [pos("0xa", "A", 4000), pos("0xb", "A", 700), pos("0xc", "A", 700), pos("0xs", "A", 700), pos("0xz", "B", 300)]
    f = H.fishy("cs2", "T5", big, 0, {w: dict(t=None, checked=1) for w in "0xa 0xb 0xc 0xs".split()}, 1)
    sc, parts, top = f["A"]
    assert top == "0xa" and sc == 2 + 1 + 1 + 1 + 1          # 1.2x normal, 94% one-sided, one huge wallet, 4 big, shyux
    fresh = {w: dict(t=None, checked=1) for w in "0xb 0xc 0xs".split()} | {"0xa": dict(t=-86400, checked=1)}
    assert H.fishy("cs2", "T5", big, 0, fresh, 1)["A"][0] == 3     # a fresh account among them: -3
    assert H.fishy("dota", "T3+", big, 0, {}, 1) == {}
    shy_fresh = {w: dict(t=None, checked=1) for w in "0xa 0xb 0xc".split()} | {"0xs": dict(t=-86400, checked=1)}
    assert H.fishy("cs2", "T5", big, 0, shy_fresh, 1)["A"][0] == 6   # a fresh shyux wallet is not penalised
    assert H.classify(u(phase="pre", tier="T5", fishy=3))[0] == "M"
    assert H.classify(u(phase="pre", tier="T5", fishy=2))[0] != "M"
    assert H.classify(u(phase="live", tier="T5", fishy=5))[0] != "M"
