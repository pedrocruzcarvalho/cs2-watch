"""
Paper bankroll on top of the prediction ledger: what would a fixed staking plan have done, bet by bet?

Plan (start $300, paper only, nothing is placed):
  - When a match is first flagged, bet STAKES[level] on the flagged side at the price the scanner saw then.
  - When the level later rises (LOW -> MEDIUM -> HIGH), top up to ESCALATION x STAKES[new level] at the new
    price. Escalation is the signal: money keeps arriving on one side, so the plan bets more than for a match
    that was at that level from the start. LOW -> HIGH = $10 + $50 = $60, versus $40 for a straight HIGH.
  - A downgrade keeps what's in. If the flagged side flips to the other team, nothing more goes in.
  - Bets are only placed before kickoff, only on matches listed on Polymarket, and only after the account opened.
  - Every tranche pays the price + SPREAD and Polymarket's sports taker fee (0.05 x p x (1 - p) per share).
    Its stake leaves the balance when placed and comes back as shares x $1 if the side wins.
  - A tranche the balance can't cover is skipped.
"""

from explain import explain

STAKES = {"HIGH": 40.0, "MEDIUM": 20.0, "LOW": 10.0}
RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
ESCALATION = 1.5
START_BALANCE = 300.0
SPREAD, FEE = 0.01, 0.05


def cost_per_share(price):
    p = min(price + SPREAD, 0.99)
    return p + FEE * p * (1 - p)


def tranches(r, opened_at, now):
    """The bets the plan places on one match, from its level history. Returns (tranches, path, escalated, flipped)."""
    if r.get("mode") != "live" or "poly" not in r.get("refs", {}):
        return [], [], False, False
    out, side, top, staked, flipped, escalated = [], None, 0, 0.0, False, False
    path = []
    for h in r.get("history", []):
        if h["t"] >= r["start"] or h["t"] > now:
            break
        if h["level"] in RANK and (side is None or h["side"] == side) and (not path or path[-1][0] != h["level"]):
            path.append((h["level"], h["t"]))
        if h["level"] not in STAKES or h.get("price") is None or h["t"] < opened_at:
            continue
        if side is None:
            side, top = h["side"], RANK[h["level"]]
            out.append(dict(t=h["t"], level=h["level"], price=h["price"], stake=STAKES[h["level"]],
                            snap=h.get("snap")))
            staked = STAKES[h["level"]]
        elif h["side"] != side:
            flipped = True
            break
        elif RANK[h["level"]] > top:
            top, escalated = RANK[h["level"]], True
            add = ESCALATION * STAKES[h["level"]] - staked
            if add > 0:
                out.append(dict(t=h["t"], level=h["level"], price=h["price"], stake=add, snap=h.get("snap")))
                staked += add
    for x in out:
        x["side"] = side
    return out, path, escalated, flipped


def run(ledger, opened_at, now):
    """Replays every tranche and settlement in time order. Returns (bets newest first, stats, balance series)."""
    events, bets = [], {}
    for r in ledger.values():
        tr, path, esc, flipped = tranches(r, opened_at, now)
        if not tr:
            continue
        key = (r["match"], r["start"])
        bets[key] = dict(match=r["match"], start=r["start"], side=tr[0]["side"], level=max(
            (t["level"] for t in tr), key=RANK.get), first_level=tr[0]["level"], path=path, escalated=esc,
            flipped=flipped, tranches=[], stake=0.0, shares=0.0, status="open", pnl=0.0, winner=None,
            settled_at=None, url=r.get("urls", {}).get("poly"), final_level=r.get("level"), teams=r.get("teams", []),
            snap=next((t["snap"] for t in reversed(tr) if t.get("snap")), None))
        for t in tr:
            events.append((t["t"], 0, key, t))
        if r.get("winner"):
            events.append((max(r.get("resolved_at") or r["start"], r["start"]), 1, key, r))
    events.sort(key=lambda e: (e[0], e[1]))

    balance, series = START_BALANCE, [(opened_at, START_BALANCE)]
    for t, kind, key, x in events:
        b = bets[key]
        if kind == 0:
            cost = cost_per_share(x["price"])
            tr = dict(t=t, level=x["level"], price=x["price"], stake=x["stake"], shares=round(x["stake"] / cost, 2))
            # (the snapshot stays on the bet, not on each tranche, to keep the stored data small)
            if balance + 1e-9 < x["stake"]:
                tr.update(skipped=True, stake=0.0, shares=0.0)
            else:
                balance -= x["stake"]
                b["stake"] += x["stake"]
                b["shares"] += tr["shares"]
            b["tranches"].append(tr)
        else:
            if b["stake"] <= 0:
                b["status"] = "skipped"
                continue
            b["winner"], b["settled_at"] = x["winner"], t
            if x["winner"] not in x.get("teams", [x["winner"]]):
                b.update(status="void")                              # a result we can't map: refund
                balance += b["stake"]
            elif x["winner"] == b["side"]:
                b.update(status="won", pnl=round(b["shares"] - b["stake"], 2))
                balance += b["shares"]
            else:
                b.update(status="lost", pnl=-b["stake"])
        b["balance_after"] = round(balance, 2)
        series.append((t, round(balance + sum(y["stake"] for y in bets.values() if y["status"] == "open"), 2)))

    for b in bets.values():
        if b["status"] == "open" and b["stake"] <= 0:
            b["status"] = "skipped"
        paid = sum(tr["stake"] for tr in b["tranches"])
        b["price"] = round(sum(tr["price"] * tr["stake"] for tr in b["tranches"]) / paid, 3) if paid else \
            b["tranches"][0]["price"]
        b["why"] = explain(b["side"], b["teams"], b["start"], b["path"], b["tranches"], b["snap"],
                           escalation=ESCALATION, stakes=STAKES, status=b["status"], winner=b["winner"], pnl=b["pnl"],
                           stake=b["stake"], shares=b["shares"], flipped=b["flipped"], now=now)
        b.pop("snap")
    out = sorted(bets.values(), key=lambda b: b["start"], reverse=True)
    return out, stats(out, balance), series


def group(name, g):
    st = sum(b["stake"] for b in g)
    pnl = sum(b["pnl"] for b in g)
    return dict(name=name, bets=len(g), won=sum(b["status"] == "won" for b in g),
                lost=sum(b["status"] == "lost" for b in g), staked=st, pnl=round(pnl, 2),
                roi=round(pnl / st, 4) if st else None)


def stats(bets, balance):
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    open_ = [b for b in bets if b["status"] == "open"]
    won = [b for b in settled if b["status"] == "won"]
    staked = sum(b["stake"] for b in settled)
    pnl = sum(b["pnl"] for b in settled)
    equity = balance + sum(b["stake"] for b in open_)
    peak, dd, eq = START_BALANCE, 0.0, START_BALANCE
    for b in sorted(settled, key=lambda b: b["settled_at"] or b["start"]):
        eq += b["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    streak, kind = 0, None
    for b in sorted(settled, key=lambda b: b["settled_at"] or b["start"], reverse=True):
        kind = kind or b["status"]
        if b["status"] != kind:
            break
        streak += 1
    by_level = [group(lvl, [b for b in settled if b["level"] == lvl]) | dict(
        level=lvl, stake=STAKES[lvl], open=sum(b["level"] == lvl for b in open_)) for lvl in STAKES]
    by_path = [group("escalated", [b for b in settled if b["escalated"]]),
               group("flagged once, never rose", [b for b in settled if not b["escalated"]])]
    return dict(start=START_BALANCE, balance=round(balance, 2), equity=round(equity, 2),
                profit=round(equity - START_BALANCE, 2), settled_pnl=round(pnl, 2),
                roi=round(pnl / staked, 4) if staked else None, staked=staked,
                bets=len(settled), wins=len(won), losses=len(settled) - len(won),
                win_rate=round(len(won) / len(settled), 4) if settled else None,
                open=len(open_), at_risk=sum(b["stake"] for b in open_),
                skipped=sum(b["status"] == "skipped" for b in bets),
                escalated=sum(b["escalated"] for b in bets),
                best=max((b["pnl"] for b in settled), default=0), worst=min((b["pnl"] for b in settled), default=0),
                max_drawdown=round(dd, 2), streak=f"{streak} {kind}" if kind else "", by_level=by_level,
                by_path=by_path, escalation=ESCALATION)


def planned(matches, ledger, opened_at, now):
    """Flagged matches that haven't started: level path so far, money already in, and what the next step adds."""
    held = {}
    bets, _, _ = run(ledger, opened_at, now)
    for b in bets:
        held[(b["match"], b["start"])] = b
    hist = {(r["match"], r["start"]): r.get("history", []) for r in ledger.values() if r.get("mode") == "live"}
    out = []
    for m in matches:
        if m["start"] <= now or m["level"] not in STAKES or not m.get("fix_side"):
            continue
        b = held.get((m["match"], m["start"]))
        path = [(h["level"], h["t"]) for h in hist.get((m["match"], m["start"]), []) if h["level"] in RANK]
        path = [p for i, p in enumerate(path) if i == 0 or p[0] != path[i - 1][0]]
        price = m["prices"].get(m["fix_side"])
        snap = dict(flags=m["flags"], money=m["money"], flow=m["flow"], tier=m["tier"], conflict=m["conflict"])
        why = explain(b["side"] if b else m["fix_side"], m["teams"], m["start"], path, b["tranches"] if b else [], snap,
                      escalation=ESCALATION, stakes=STAKES, stake=b["stake"] if b else 0.0,
                      shares=b["shares"] if b else 0.0, flipped=bool(b and b["flipped"]), now=now)
        out.append(dict(why=why, match=m["match"], start=m["start"], level=m["level"], pts=m["pts"], side=m["fix_side"],
                        price=price, bettable="poly" in m["refs"] and price is not None,
                        in_stake=b["stake"] if b else 0.0, in_shares=b["shares"] if b else 0.0,
                        bet_side=b["side"] if b else None, escalated=bool(b and b["escalated"]),
                        flipped=bool(b and b["flipped"]), path=path, conflict=m["conflict"], tier=m["tier"],
                        money=m["money"], flow=m["flow"], urls=m["urls"], flags=m["flags"]))
    return sorted(out, key=lambda b: b["start"])
