import bankroll as B

T0 = 1_800_000_000


def rec(i, level, price, winner=None, side="A", poly=True, start=None, resolved=None):
    s = start if start is not None else T0 + i * 3600
    return dict(mode="live", match=f"Counter-Strike: A vs B #{i}", start=s, level=level, tracked=side, price=price,
                teams=["A", "B"], refs={"poly": f"m{i}"} if poly else {"kalshi": f"k{i}"}, urls={}, conflict=False,
                winner=winner, resolved_at=resolved if resolved is not None else (s + 7200 if winner else None))


def test_win_pays_shares_after_spread_and_fee_and_loss_costs_stake():
    led = {"1": rec(1, "HIGH", 0.50, winner="A"), "2": rec(2, "MEDIUM", 0.40, winner="B")}
    bets, st, _ = B.run(led, T0, T0 + 10 * 86400)
    won = next(b for b in bets if b["level"] == "HIGH")
    cost = 0.51 + 0.05 * 0.51 * 0.49
    assert abs(won["pnl"] - round(40 / cost - 40, 2)) < 0.01
    assert st["wins"] == 1 and st["losses"] == 1
    assert abs(st["balance"] - (300 + won["pnl"] - 20)) < 0.01


def test_open_bet_is_reserved_and_bets_it_cannot_afford_are_skipped():
    led = {str(i): rec(i, "HIGH", 0.5, start=T0 + i) for i in range(9)}            # 9 x $40 at once, nothing settled
    bets, st, _ = B.run(led, T0, T0 + 86400)
    assert st["open"] == 7 and st["skipped"] == 2 and abs(st["balance"] - 20) < 1e-9
    assert abs(st["equity"] - 300) < 1e-9


def test_only_polymarket_flags_after_opening_and_after_kickoff_are_bet():
    led = {"old": rec(1, "LOW", 0.5, start=T0 - 10), "kalshi": rec(2, "LOW", 0.5, poly=False),
           "none": rec(3, "", 0.5), "future": rec(4, "LOW", 0.5, start=T0 + 10 * 86400), "ok": rec(5, "LOW", 0.5)}
    bets, _, _ = B.run(led, T0, T0 + 86400)
    assert [b["match"] for b in bets] == ["Counter-Strike: A vs B #5"]
