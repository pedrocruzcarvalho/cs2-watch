from datetime import datetime, timezone

import oddspapi as O

T0 = 1_800_000_000


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _hist(prices1, prices2, book="pinnacle"):
    mk = lambda ps: {"players": {"0": [{"createdAt": _iso(t), "price": p} for t, p in ps]}}
    return {"fixtureId": "f", "bookmakers": {book: {"markets": {"171": {"outcomes": {"171": mk(prices1),
                                                                                  "172": mk(prices2)}}}}}}


def test_series_removes_margin_and_pairs_both_sides():
    pts = O.series(_hist([(T0, 1.80), (T0 + 600, 1.60)], [(T0, 2.00), (T0 + 600, 2.40)]))["pinnacle"]
    assert abs(pts[0][1] - (1 / 1.8) / (1 / 1.8 + 1 / 2.0)) < 1e-3          # 52.6%, not the raw 55.6%
    assert abs(pts[-1][1] - 0.6) < 1e-3                                        # 1.60 vs 2.40 -> 60%


def _match(level="MEDIUM", side="B"):
    return dict(match="Counter-Strike: A vs B (BO3) - Cup", start=T0 + 7200, teams=["A", "B"], kind="match",
                fix_side=side, tracked=side, level=level, pts=7)


def test_bookmakers_agreeing_raise_and_disagreeing_lower_the_level():
    m = _match()                                                               # p1 = A; B goes 40% -> 50%
    O.judge(m, "A", {"pinnacle": [[T0 - 20 * 3600, 0.60], [T0 - 3600, 0.50]]}, T0)
    assert m["level"] == "HIGH" and m["sharp"]["agree"] and abs(m["sharp"]["move"] - 0.10) < 1e-9
    m = _match()
    O.judge(m, "A", {"pinnacle": [[T0 - 20 * 3600, 0.50], [T0 - 3600, 0.60]]}, T0)
    assert m["level"] == "LOW" and not m["sharp"]["agree"]
    m = _match(level="LOW")
    O.judge(m, "A", {"1xbet": [[T0 - 20 * 3600, 0.50], [T0 - 3600, 0.70]]}, T0)
    assert m["level"] == "LOW"                                                # never below LOW
    m = _match()
    O.judge(m, "A", {"1xbet": [[T0 - 20 * 3600, 0.59], [T0 - 3600, 0.58]]}, T0)
    assert m["level"] == "MEDIUM" and "sharp" not in m and m["books"]["1xbet"]["now"] == 0.42


def test_link_finds_the_match_with_different_name_spellings():
    fx = [dict(id="f1", p1="Fire Flux eSports", p2="Ex-6666", start=T0, tournament="NODWIN")]
    m = dict(match="Counter-Strike: Fire Flux Esports vs ex-6666 (BO3) - NODWIN", start=T0 + 600,
             teams=["Fire Flux Esports", "ex-6666"], kind="match")
    assert O.link(fx, [m])["f1"][1] == "Fire Flux Esports"


def test_budget_never_exceeds_cap_and_skips_without_key(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "key", lambda: "k")
    monkeypatch.setattr(O, "call", lambda path, **q: calls.append(path) or [])
    st = {"month": "2000-01", "used": O.MONTH_CAP, "fixtures_at": 0}
    O.tick(st, [], T0, log=lambda *a: None)                                   # new month: counter resets
    assert calls == ["fixtures"] and st["used"] == 1
    calls.clear()
    O.tick(st, [], T0 + 3600, log=lambda *a: None)                             # within 12 h: no new list
    assert calls == []
    st.update(used=O.MONTH_CAP, fixtures_at=0)
    O.tick(st, [], T0 + 7200, log=lambda *a: None)                             # cap reached: nothing billable
    assert calls == []
    monkeypatch.setattr(O, "key", lambda: None)
    assert O.tick({}, [], T0) == {}
