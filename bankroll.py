"""
Paper bankroll on top of the prediction ledger: what would a fixed staking plan have done, bet by bet?

Plan: start with $300. When a flagged match kicks off, put STAKES[level] on the flagged side, on Polymarket,
at the last price the scanner saw before kickoff + SPREAD, paying Polymarket's sports taker fee
(0.05 x p x (1 - p) per share). The stake leaves the balance at kickoff and comes back (as shares x $1) when
the match resolves. A bet the balance cannot cover is recorded as skipped. Paper only: nothing is placed.

Only matches listed on Polymarket are bet (Kalshi-only flags still count as flags, but there's no
Polymarket price to bet at), and only matches that kick off after the account was opened.
"""

STAKES = {"HIGH": 40.0, "MEDIUM": 20.0, "LOW": 10.0}
START_BALANCE = 300.0
SPREAD, FEE = 0.01, 0.05


def cost_per_share(price):
    p = min(price + SPREAD, 0.99)
    return p + FEE * p * (1 - p)


def eligible(r, opened_at):
    return (r.get("mode") == "live" and r.get("level") in STAKES and "poly" in r.get("refs", {})
            and r.get("price") is not None and r["start"] >= opened_at)


def run(ledger, opened_at, now):
    """Replays every bet in time order. Returns (bets newest first, stats, balance series)."""
    events = []
    for r in ledger.values():
        if not eligible(r, opened_at) or r["start"] > now:
            continue
        events.append((r["start"], 0, r))
        if r.get("winner"):
            events.append((max(r.get("resolved_at") or r["start"], r["start"]), 1, r))
    events.sort(key=lambda e: (e[0], e[1]))

    balance, bets, series = START_BALANCE, {}, [(opened_at, START_BALANCE)]
    for t, kind, r in events:
        key = (r["match"], r["start"])
        if kind == 0:
            stake = STAKES[r["level"]]
            cost = cost_per_share(r["price"])
            b = dict(match=r["match"], start=r["start"], level=r["level"], side=r["tracked"], price=r["price"],
                     cost=round(cost, 4), stake=stake, shares=round(stake / cost, 2), status="open", pnl=0.0,
                     url=r.get("urls", {}).get("poly"), conflict=r.get("conflict", False), winner=None,
                     settled_at=None)
            if balance + 1e-9 < stake:
                b.update(status="skipped", stake=0.0, shares=0.0)
            else:
                balance -= stake
            bets[key] = b
        else:
            b = bets[key]
            if b["status"] != "open":
                continue
            b["winner"], b["settled_at"] = r["winner"], t
            if r["winner"] not in r.get("teams", [r["winner"]]):
                b.update(status="void", pnl=0.0)                       # e.g. a result we can't map: refund
                balance += b["stake"]
            elif r["winner"] == b["side"]:
                b.update(status="won", pnl=round(b["shares"] - b["stake"], 2))
                balance += b["shares"]
            else:
                b.update(status="lost", pnl=-b["stake"])
        bets[key]["balance_after"] = round(balance, 2)
        series.append((t, round(balance + sum(x["stake"] for x in bets.values() if x["status"] == "open"), 2)))

    out = sorted(bets.values(), key=lambda b: b["start"], reverse=True)
    return out, stats(out, balance), series


def stats(bets, balance):
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    open_ = [b for b in bets if b["status"] == "open"]
    won = [b for b in settled if b["status"] == "won"]
    staked = sum(b["stake"] for b in settled)
    pnl = sum(b["pnl"] for b in settled)
    equity = balance + sum(b["stake"] for b in open_)
    # max drawdown on equity (open bets valued at cost), replayed in settlement order
    peak, dd, eq = START_BALANCE, 0.0, START_BALANCE
    for b in sorted(settled, key=lambda b: b["settled_at"] or b["start"]):
        eq += b["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    streak, kind = 0, None
    for b in sorted(settled, key=lambda b: b["settled_at"] or b["start"], reverse=True):
        if kind is None:
            kind = b["status"]
        if b["status"] != kind:
            break
        streak += 1
    by_level = []
    for lvl in STAKES:
        g = [b for b in settled if b["level"] == lvl]
        st = sum(b["stake"] for b in g)
        by_level.append(dict(level=lvl, stake=STAKES[lvl], bets=len(g), won=sum(b["status"] == "won" for b in g),
                             lost=sum(b["status"] == "lost" for b in g), staked=st,
                             pnl=round(sum(b["pnl"] for b in g), 2),
                             roi=round(sum(b["pnl"] for b in g) / st, 4) if st else None,
                             open=sum(b["level"] == lvl for b in open_)))
    return dict(start=START_BALANCE, balance=round(balance, 2), equity=round(equity, 2),
                profit=round(equity - START_BALANCE, 2), settled_pnl=round(pnl, 2),
                roi=round(pnl / staked, 4) if staked else None, staked=staked,
                bets=len(settled), wins=len(won), losses=len(settled) - len(won),
                win_rate=round(len(won) / len(settled), 4) if settled else None,
                open=len(open_), at_risk=sum(b["stake"] for b in open_),
                skipped=sum(b["status"] == "skipped" for b in bets),
                avg_price=round(sum(b["price"] for b in settled) / len(settled), 3) if settled else None,
                best=max((b["pnl"] for b in settled), default=0), worst=min((b["pnl"] for b in settled), default=0),
                max_drawdown=round(dd, 2), streak=f"{streak} {kind}" if kind else "", by_level=by_level)


def planned(matches, now):
    """Upcoming flagged matches and the bet the plan would place at kickoff if the flag still stands."""
    out = []
    for m in matches:
        if m["start"] <= now or m["level"] not in STAKES or not m.get("fix_side"):
            continue
        price = m["prices"].get(m["fix_side"])
        stake = STAKES[m["level"]]
        bettable = "poly" in m["refs"] and price is not None
        out.append(dict(match=m["match"], start=m["start"], level=m["level"], pts=m["pts"], side=m["fix_side"],
                        price=price, stake=stake if bettable else 0.0,
                        to_win=round(stake / cost_per_share(price) - stake, 2) if bettable else None,
                        bettable=bettable, conflict=m["conflict"], tier=m["tier"], money=m["money"],
                        flow=m["flow"], urls=m["urls"], flags=m["flags"]))
    return sorted(out, key=lambda b: b["start"])
