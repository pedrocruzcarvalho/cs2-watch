import notify as N

NOW = 1000.0


def bet(**kw):
    return dict(dict(status="open", placed_at=NOW, stake=6.0, side="Alpha", price=0.42, match="Counter-Strike: Alpha vs Beta",
                     market="Match Winner", url="https://polymarket.com/event/a"), **kw)


def acct(**bets):
    return dict(account=dict(bets=bets))


def test_only_this_ticks_bets_count():
    evs = N.events([("Radar", acct(a=bet(), old=bet(placed_at=NOW - 600), won=bet(status="won"), skip=bet(status="skipped")))], NOW)
    assert len(evs) == 1
    assert evs[0][0] == "Radar: APOSTA $6.00 em Alpha a 0.42 (odd 2.38)\nCS2 · Alpha vs Beta (Match Winner)"


def test_exit_counts():
    sold = bet(status="sold", placed_at=NOW - 600, settled_at=NOW, proceeds=7.5)
    assert "SAI de Alpha por $7.50 (pagou $6.00)" in N.events([("Radar", acct(a=sold))], NOW)[0][0]


def test_accounts_without_bets():
    assert N.events([("Live", {}), ("Tennis", dict(account=dict(opened_at=1)))], NOW) == []


def test_one_event_links_to_market():
    p = N.payload("t", [("x", "https://u")])
    assert p["title"] == "1 movimento" and p["click"] == "https://u"


def test_many_events_no_link_and_trimmed():
    p = N.payload("t", [("y" * 100, "https://u")] * 100)
    assert p["title"] == "100 movimentos" and "click" not in p
    assert len(p["message"]) < N.MAX_CHARS + 50 and p["message"].endswith("(more on the dashboard)")


def test_no_topic_sends_nothing(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(N.urllib.request, "urlopen", lambda *a, **k: 1 / 0)
    N.send([("Radar", acct(a=bet()))], NOW, log=lambda m: 1 / 0)


def test_failure_never_raises_or_logs_detail(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "secret-topic")

    def boom(*a, **k):
        raise OSError("secret-topic")

    monkeypatch.setattr(N.urllib.request, "urlopen", boom)
    logs = []
    N.send([("Radar", acct(a=bet()))], NOW, log=logs.append)
    assert logs == ["alert failed: OSError"]
