"""
Informed wallets: follow Polymarket wallets whose recent CS2 record is too good for luck, when they bet with conviction.

Rules (fixed on 2026-10-20 before any live result, from the 60-day walk-forward test in mine_analyze.py):
  record     every pre-kickoff CS2 position of $20+ (net) on a market that has resolved, last 21 days, price 0.03-0.97
  informed   5+ such bets, luck score >= 2.5 (wins vs what the prices predicted), edge >= 10 points
  signal     on a T3-T6 match (stronger team below Valve #50 or unranked), informed wallets have $200+ net on one
             side of a market and less than half of that on the other side
  bet        $20 on that side, in the last 30 minutes before kickoff, filled against the real order book
             (fee included), one bet per market; $300 paper bankroll
State: "records" (wallet -> recent resolved positions), "counted" (markets already added), "names",
"account" (bets). Stored in KV as state:informed.
"""

import json
import math
import time
from collections import defaultdict

WINDOW = 21 * 86400
MIN_BETS, MIN_Z, MIN_EDGE = 5, 2.5, 0.10
MIN_MONEY = 200
STAKE, START_BALANCE = 20.0, 300.0
BET_WINDOW = 30 * 60
LOW_TIERS = {"T3", "T4", "T5", "T6"}


# ------------------------------------------------------------------------------------------------ records

def add_resolved(state, infos, now):
    """Add every position of every newly resolved Polymarket market to the wallets' records."""
    rec, counted, names = state.setdefault("records", {}), state.setdefault("counted", {}), state.setdefault("names", {})
    added = 0
    for i in infos:
        cid = i.get("cid")
        if i.get("venue") != "poly" or not cid or not i.get("winner") or cid in counted or i["start"] > now:
            continue
        counted[cid] = i["start"]
        for w, _, o, usd, avg, name in i.get("allpos") or []:
            rec.setdefault(w, []).append([round(i["start"]), 1 if o == i["winner"] else 0, avg, usd])
            if name:
                names[w] = name
            added += 1
    cut = now - WINDOW - 86400
    for w in list(rec):
        rec[w] = [x for x in rec[w] if x[0] >= cut]
        if not rec[w]:
            del rec[w]
    state["counted"] = {c: t for c, t in counted.items() if t >= cut}
    return added


def record_of(rows, now):
    rows = [r for r in rows if now - WINDOW <= r[0] <= now - 3 * 3600]
    n = len(rows)
    if not n:
        return dict(n=0)
    wins = sum(r[1] for r in rows)
    exp = sum(r[2] for r in rows)
    var = sum(r[2] * (1 - r[2]) for r in rows)
    return dict(n=n, wins=wins, losses=n - wins, win_rate=round(wins / n, 3), avg_price=round(exp / n, 3),
                edge=round((wins - exp) / n, 3), z=round((wins - exp) / math.sqrt(var), 2) if var > 0 else 0.0,
                usd=round(sum(r[3] for r in rows)))


def qualifying(state, now):
    out = {}
    for w, rows in state.get("records", {}).items():
        r = record_of(rows, now)
        if r.get("n", 0) >= MIN_BETS and r["z"] >= MIN_Z and r["edge"] >= MIN_EDGE:
            out[w] = dict(r, name=state.get("names", {}).get(w, ""))
    return out


def rising(state, now, qual, limit=25):
    """Wallets close to the bar (not bet on): luck score 2.0-2.5 with 5+ bets, or a 2.5+ luck score from only 3-4 bets.
    In the 60-day test, wallets in the 2.0-2.5 band added about break-even bets, so they are watched, not followed;
    they move to the informed list by themselves once their record crosses the bar."""
    out = []
    for w, rows in state.get("records", {}).items():
        if w in qual:
            continue
        r = record_of(rows, now)
        if r.get("n", 0) < 3 or r["edge"] < MIN_EDGE:
            continue
        if r["n"] >= MIN_BETS and 2.0 <= r["z"] < MIN_Z:
            need = f"luck score {r['z']:.2f}, needs {MIN_Z}"
        elif r["n"] < MIN_BETS and r["z"] >= MIN_Z:
            need = f"{MIN_BETS - r['n']} more bet{'s' if MIN_BETS - r['n'] != 1 else ''} with a record like this"
        else:
            continue
        out.append(dict(wallet=w, name=state.get("names", {}).get(w, ""), record=r, needs=need))
    return sorted(out, key=lambda x: -x["record"]["z"])[:limit]


# ------------------------------------------------------------------------------------------------ signals

def signals(infos, qual, vtier_of, now):
    """Upcoming markets where informed wallets have money. Returns every such market (signal or not)."""
    out = []
    for i in infos:
        if i.get("venue") != "poly" or i["start"] <= now or not i.get("allpos"):
            continue
        money, who = defaultdict(float), defaultdict(list)
        for w, _, o, usd, avg, name in i["allpos"]:
            if w in qual:
                money[o] += usd
                who[o].append(dict(wallet=w, name=name or qual[w].get("name", ""), usd=usd, avg=avg, record=qual[w]))
        if not money:
            continue
        side = max(money, key=money.get)
        other = sum(v for o, v in money.items() if o != side)
        vt = vtier_of(i["match"])
        reasons = []
        if vt not in LOW_TIERS:
            reasons.append(f"match tier {vt} (only T3-T6 count)")
        if money[side] < MIN_MONEY:
            reasons.append(f"only ${money[side]:,.0f} of informed money (needs ${MIN_MONEY})")
        if other >= 0.5 * money[side]:
            reasons.append("informed wallets are split between both sides")
        out.append(dict(match=i["match"], start=i["start"], market=i.get("market", "Match Winner"), cid=i.get("cid"),
                        slug=i.get("ref"), url=i.get("url"), teams=i.get("teams"), prices=i.get("prices") or {},
                        tokens=i.get("tokens") or {}, side=side, money=round(money[side]), other_money=round(other),
                        wallets=sorted(who[side], key=lambda x: -x["usd"]),
                        against=sorted((x for o, xs in who.items() if o != side for x in xs), key=lambda x: -x["usd"]),
                        vtier=vt, active=not reasons, why_not=reasons))
    return sorted(out, key=lambda s: (not s["active"], s["start"]))


# ------------------------------------------------------------------------------------------------ account

def place_bets(state, sigs, now, book_asks, fill, log=print):
    """In the last 30 minutes before kickoff, bet $20 on every active signal not yet bet."""
    acct = state.setdefault("account", dict(opened_at=now, bets={}))
    for s in sigs:
        key = s["cid"]
        if not s["active"] or key in acct["bets"] or not 0 < s["start"] - now <= BET_WINDOW:
            continue
        bal = balance(acct)
        if bal < STAKE:
            acct["bets"][key] = dict(sig_summary(s), placed_at=now, status="skipped", reason="balance", stake=0.0,
                                     shares=0.0, price=s["prices"].get(s["side"]))
            continue
        asks = book_asks(s["tokens"].get(s["side"]))
        if asks:
            sh, spent, avg, levels = fill(STAKE, asks)
        else:
            sh = spent = avg = levels = 0
        if not sh:
            acct["bets"][key] = dict(sig_summary(s), placed_at=now, status="skipped", reason="no sellers", stake=0.0,
                                     shares=0.0, price=s["prices"].get(s["side"]))
            continue
        acct["bets"][key] = dict(sig_summary(s), placed_at=now, status="open", stake=round(spent, 2),
                                 shares=round(sh, 2), price=round(avg, 4), levels=levels)
        log(f"informed bet: ${spent:.2f} on {s['side']} ({s['match'][16:60]}, {s['market']}) at {avg:.3f}")


def sig_summary(s):
    return {k: s[k] for k in ("match", "start", "market", "cid", "slug", "url", "teams", "side", "money", "other_money",
                              "wallets", "vtier")}


def settle(state, infos, now, fetch_winner, log=print):
    acct = state.get("account", {})
    by_cid = {i.get("cid"): i for i in infos if i.get("winner")}
    for b in acct.get("bets", {}).values():
        if b["status"] != "open" or now < b["start"]:
            continue
        w = (by_cid.get(b["cid"]) or {}).get("winner")
        if not w and now > b["start"] + 2 * 3600:
            try:
                w = fetch_winner(b["slug"], b["cid"])
            except Exception as err:
                log(f"  informed settle failed: {err}")
        if not w:
            continue
        b["winner"], b["settled_at"] = w, now
        if w == b["side"]:
            b.update(status="won", pnl=round(b["shares"] - b["stake"], 2))
        else:
            b.update(status="lost", pnl=-b["stake"])


def balance(acct):
    bal = START_BALANCE
    for b in acct.get("bets", {}).values():
        if b["status"] in ("open", "won", "lost"):
            bal -= b["stake"]
        if b["status"] == "won":
            bal += b["shares"]
    return round(bal, 2)


# ------------------------------------------------------------------------------------------------ dashboard

def money(v):
    return f"${v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"${v:,.2f}"


def wallet_line(x, side):
    r = x["record"]
    who = f"\"{x['name']}\"" if x.get("name") and not x["name"].startswith("0x") else x["wallet"][:6] + "…" + x["wallet"][-4:]
    return (f"{who}: {r['n']} resolved CS2 bets in the last 21 days, won {r['wins']} ({r['win_rate']:.0%}) at prices "
            f"that predicted {r['avg_price']:.0%}, so an edge of {r['edge'] * 100:+.0f} points and a luck score of "
            f"{r['z']:.1f}. Put {money(x['usd'])} on {side} at {x['avg']:.2f} before kickoff.")


def explain(b, now):
    other = next((t for t in b.get("teams") or [] if t != b["side"]), "the other side")
    head = (f"{money(b['stake'])} on {b['side']}" + (" to beat " + other if b["market"] == "Match Winner" else f" ({b['market']})")
            + f", because {len(b['wallets'])} informed wallet{'s' if len(b['wallets']) != 1 else ''} put "
            f"{money(b['money'])} on {b['side']} before kickoff.")
    secs = [dict(title="The informed wallets", bullets=[wallet_line(x, b["side"]) for x in b["wallets"][:6]] or ["-"])]
    why = [f"A wallet counts as informed when its last 21 days of CS2 bets (5+, $20+ each, before kickoff) won far "
           f"more often than its prices predicted: luck score 2.5+ (pure luck gets there about 0.6% of the time) and "
           f"an edge of 10+ points.",
           f"This is a {b.get('vtier')} match, where the 60-day test found the edge, and informed money on {b['side']} was "
           f"{money(b['money'])} vs {money(b.get('other_money', 0))} on the other side (needs $200+ and one-sided)."]
    if b["status"] != "skipped":
        why.append(f"Placed in the last 30 minutes before kickoff: {money(b['stake'])} at an average {b['price']:.3f} "
                   f"({b['price'] * 100:.0f}% implied), filled against the real order book, fee included. If {b['side']} "
                   f"wins it pays {money(b['shares'])}.")
    secs.append(dict(title="Why this bet", bullets=why))
    secs.append(dict(title="Why it could be wrong", bullets=[
        "With about 1,100 active CS2 wallets, a handful reach a 2.5 luck score by chance; part of any informed list is "
        "just lucky.",
        "Informed can mean fixes, but also lineup news, sick players or simply very good bettors. The 60-day test "
        "returned +17.5% over 55 bets; these live bets are what confirm or reject it."]))
    res = {"won": f"{b['side']} won: paid {money(b.get('shares', 0))}, profit {money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {money(b['stake'])} stake was lost.",
           "skipped": f"Not placed: {'no one was selling on the order book' if b.get('reason') == 'no sellers' else 'the balance could not cover it'}.",
           "open": "Not settled yet."}[b["status"]]
    secs.append(dict(title="Result", bullets=[res]))
    return dict(headline=head, sections=secs)


def dashboard(state, sigs, qual, now):
    acct = state.get("account", dict(opened_at=now, bets={}))
    bets = sorted(acct.get("bets", {}).values(), key=lambda b: b["start"], reverse=True)
    out_bets, bal, series = [], START_BALANCE, [(acct.get("opened_at", now), START_BALANCE)]
    events = []
    for b in bets:
        if b["status"] in ("open", "won", "lost"):
            events.append((b["placed_at"], -b["stake"]))
            if b["status"] == "won":
                events.append((b["settled_at"], b["shares"]))
    open_stake = 0.0
    for t, d in sorted(events):
        bal += d
        series.append((t, round(bal + sum(x["stake"] for x in bets if x["status"] == "open" and x["placed_at"] <= t
                                          and (x.get("settled_at") or 9e18) > t), 2)))
    for b in bets:
        out_bets.append(dict(b, level="INFORMED", path=[], escalated=False, flipped=False, tranches=[],
                             kind="match" if b["market"] == "Match Winner" else "side", why=explain(b, now)))
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    open_ = [b for b in bets if b["status"] == "open"]
    staked = sum(b["stake"] for b in settled)
    pnl = sum(b.get("pnl", 0) for b in settled)
    cash = balance(acct)
    peak, dd, eq = START_BALANCE, 0.0, START_BALANCE
    for b in sorted(settled, key=lambda b: b.get("settled_at") or b["start"]):
        eq += b.get("pnl", 0)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)

    def group(name, g):
        st = sum(b["stake"] for b in g)
        p = sum(b.get("pnl", 0) for b in g)
        return dict(name=name, bets=len(g), won=sum(b["status"] == "won" for b in g),
                    lost=sum(b["status"] == "lost" for b in g), staked=st, pnl=round(p, 2),
                    roi=round(p / st, 4) if st else None)
    stats = dict(start=START_BALANCE, balance=cash, equity=round(cash + sum(b["stake"] for b in open_), 2),
                 profit=round(cash + sum(b["stake"] for b in open_) - START_BALANCE, 2), roi=round(pnl / staked, 4) if staked else None,
                 bets=len(settled), wins=sum(b["status"] == "won" for b in settled),
                 losses=sum(b["status"] == "lost" for b in settled),
                 win_rate=round(sum(b["status"] == "won" for b in settled) / len(settled), 4) if settled else None,
                 open=len(open_), at_risk=round(sum(b["stake"] for b in open_), 2),
                 skipped=sum(b["status"] == "skipped" for b in bets), escalated=0,
                 best=max((b.get("pnl", 0) for b in settled), default=0),
                 worst=min((b.get("pnl", 0) for b in settled), default=0), max_drawdown=round(dd, 2),
                 streak="", escalation=1,
                 by_market=[group("Match winner", [b for b in settled if b["market"] == "Match Winner"]),
                            group("Maps, totals, rounds", [b for b in settled if b["market"] != "Match Winner"])],
                 by_tier=[group(t, [b for b in settled if b.get("vtier") == t]) for t in ("T3", "T4", "T5", "T6")],
                 by_wallets=[group("1 informed wallet", [b for b in settled if len(b["wallets"]) == 1]),
                             group("2+ informed wallets", [b for b in settled if len(b["wallets"]) >= 2])])
    if settled:
        last = sorted(settled, key=lambda b: b.get("settled_at") or 0, reverse=True)
        k, n = last[0]["status"], 0
        for b in last:
            if b["status"] != k:
                break
            n += 1
        stats["streak"] = f"{n} {k}"
    active_bets = {}
    for s in sigs:
        for x in s["wallets"] + s["against"]:
            active_bets.setdefault(x["wallet"], []).append(dict(match=s["match"], market=s["market"], usd=x["usd"],
                                                                start=s["start"], side=s["side"] if x in s["wallets"] else "other"))
    wallets = []
    for w, r in sorted(qual.items(), key=lambda kv: -kv[1]["z"]):
        wallets.append(dict(wallet=w, name=r.get("name", ""), record=r, betting=active_bets.get(w, [])))
    sig_out = []
    for s in sigs:
        b = acct.get("bets", {}).get(s["cid"])
        sig_out.append(dict(s, placed=b and {k: b[k] for k in ("status", "stake", "price", "shares")}))
    return dict(stats=stats, series=series, bets=out_bets[:500], signals=sig_out[:60], wallets=wallets,
                rising=[dict(x, betting=active_bets.get(x["wallet"], [])) for x in rising(state, now, qual)],
                opened_at=acct.get("opened_at", now), rules=dict(window_days=21, min_bets=MIN_BETS, min_z=MIN_Z,
                                                                 min_edge=MIN_EDGE, min_money=MIN_MONEY, stake=STAKE))
