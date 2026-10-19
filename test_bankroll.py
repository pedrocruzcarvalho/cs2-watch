import bankroll as B
import cs2_watch as C

T0 = 1_800_000_000


def rec(i, history, winner=None, poly=True, start=None, resolved=None):
    s = start if start is not None else T0 + 86400 + i * 3600
    return dict(mode="live", match=f"Counter-Strike: A vs B #{i}", start=s, level=history[-1][1] if history else "",
                tracked="A", price=0.5, teams=["A", "B"], refs={"poly": f"m{i}"} if poly else {"kalshi": f"k{i}"},
                urls={}, conflict=False, winner=winner,
                resolved_at=resolved if resolved is not None else (s + 7200 if winner else None),
                history=[dict(t=t, level=lvl, side=side, price=px, pts=0) for t, lvl, side, px in history])


def test_straight_high_bets_40_and_win_pays_shares_after_spread_and_fee():
    led = {"1": rec(1, [(T0, "HIGH", "A", 0.50)], winner="A")}
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    cost = 0.51 + 0.05 * 0.51 * 0.49
    assert bets[0]["stake"] == 40 and abs(bets[0]["pnl"] - round(40 / cost - 40, 2)) < 0.02
    assert st["wins"] == 1 and not bets[0]["escalated"]


def test_low_to_high_tops_up_to_one_and_a_half_times_high_at_the_new_price():
    led = {"1": rec(1, [(T0, "LOW", "A", 0.30), (T0 + 600, "MEDIUM", "A", 0.35), (T0 + 1200, "HIGH", "A", 0.45)],
                    winner="B")}
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    b = bets[0]
    assert [round(t["stake"], 2) for t in b["tranches"]] == [10, 20, 30]      # 10, then to 30 (1.5x20), then to 60
    assert [t["price"] for t in b["tranches"]] == [0.30, 0.35, 0.45]
    assert b["escalated"] and b["stake"] == 60 and b["pnl"] == -60 and st["balance"] == 240
    assert [p[0] for p in b["path"]] == ["LOW", "MEDIUM", "HIGH"]


def test_downgrade_keeps_money_in_and_side_flip_stops_new_money():
    led = {"1": rec(1, [(T0, "MEDIUM", "A", 0.4), (T0 + 600, "LOW", "A", 0.4), (T0 + 900, "HIGH", "B", 0.6)])}
    bets, st, _ = B.run(led, T0, T0 + 86400 + 7200)
    assert bets[0]["stake"] == 20 and bets[0]["flipped"] and bets[0]["side"] == "A"


def test_cannot_afford_is_skipped_and_only_polymarket_after_opening_before_kickoff():
    led = {str(i): rec(i, [(T0 + i, "HIGH", "A", 0.5)]) for i in range(9)}       # 9 x $40, none settled
    bets, st, _ = B.run(led, T0, T0 + 86400)
    assert st["open"] == 7 and st["skipped"] == 2 and abs(st["equity"] - 300) < 1e-9
    led = {"old": rec(1, [(T0 - 10, "LOW", "A", 0.5)]), "kalshi": rec(2, [(T0, "LOW", "A", 0.5)], poly=False),
           "late": rec(3, [(T0 + 86400 + 4 * 3600, "LOW", "A", 0.5)]), "ok": rec(4, [(T0, "LOW", "A", 0.5)])}
    bets, _, _ = B.run(led, T0, T0 + 3 * 86400)
    assert [b["match"] for b in bets] == ["Counter-Strike: A vs B #4"]


def test_ledger_history_records_only_changes():
    m = dict(id="x", match="Counter-Strike: A vs B", tier="low", start=T0 + 9999, teams=["A", "B"], level="LOW",
             pts=4, conflict=False, fix_side="A", tracked="A", tracked_is="flagged", price=0.4,
             prices={"A": 0.4, "B": 0.6}, refs={"poly": "s"}, urls={}, money={}, winner=None)
    led = {}
    C.record(led, [m], "live", T0)
    C.record(led, [dict(m, price=0.41, prices={"A": 0.41, "B": 0.59})], "live", T0 + 600)
    C.record(led, [dict(m, level="HIGH", pts=10, prices={"A": 0.45, "B": 0.55})], "live", T0 + 1200)
    h = led["live|x"]["history"]
    assert [(x["level"], x["price"]) for x in h] == [("LOW", 0.4), ("HIGH", 0.45)]
