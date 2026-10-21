"""
Snipers: copy wallets that look like the ones shyux calls out (YourBookmaker: few CS2 bets, mostly CS2, mostly right)
when they put real money on a side, before or during a match.

Found by sniper_research.py (Oct 2026, 1,506 wallets' full histories, walk-forward over 60 days): wallets with only
3-40 CS2 bets that won 60%+ and beat their prices kept doing it. Of 576 settings, 542 were positive on the first
half and 424 of those on the second; the median setting returned about +10%. The setting fixed below (a middle one,
not the best) returned +22.9% over 124 bets (90% range +12..+34): pre-match +27.2% (61 bets, halves +30/+22,
favourite control +12%), in-play +18.8% (63 bets, halves +21/+16, favourite control +14%).

Rules (fixed on 2026-10-21 before any live result):
  sniper   a wallet whose whole Polymarket history (losing bets never cashed in included) has 3-40 CS2 bets,
           won 60%+ of them, at least 5 points above what its prices implied, average CS2 bet $100+, and at
           least half of all its positions on CS2. Wallets on Pedro's hand-picked list always count.
  signal   on a non-tier-1 CS2 match, a sniper holds $1,000+ net on one side of a market (before kickoff, or bought
           in-play), and snipers have less than half of that on the other side
  bet      $20 on that side at the next 10-minute scan, filled against the real order book (fee included), once per
           market, skipped at 0.97+. $300 paper bankroll.
Profiles are looked up when a wallet first bets $1,000+ and refreshed every 3 days. State: "account", "profiles".
KV bundle part state:sniper.
"""

from collections import defaultdict

import informed as I

MIN_N, MAX_N, MIN_WON, MIN_EDGE, MIN_SIZE, MIN_SHARE = 3, 40, 0.60, 0.05, 100, 0.5
MIN_USD = 1000
REFRESH = 3 * 86400


def profile_of(h):
    rows = [r for r in h["rows"] if 0.03 < r[3] < 0.97]
    n = len(rows)
    if not n:
        return dict(n=0)
    wins = sum(1 for r in rows if r[4])
    return dict(n=n, won=round(wins / n, 3), edge=round((wins - sum(r[3] for r in rows)) / n, 3),
                size=round(sum(r[5] for r in rows) / n), share=round(len(h["rows"]) / max(h["n_closed"], len(h["rows"]), 1), 3),
                pnl=h["pnl"], cs2_pnl=h["cs2_pnl"])


def is_sniper(p):
    return bool(p and p.get("n", 0) and MIN_N <= p["n"] <= MAX_N and p["won"] >= MIN_WON and p["edge"] >= MIN_EDGE
                and p["size"] >= MIN_SIZE and p["share"] >= MIN_SHARE)


def candidates(infos, now):
    """Wallets holding $1,000+ on one side of a non-finished market: (wallet, usd), biggest first."""
    out = defaultdict(float)
    for i in infos:
        if i.get("venue") != "poly" or i.get("winner"):
            continue
        for x in (i.get("allpos") or []) + (i.get("livepos") or []):
            if x[3] >= MIN_USD:
                out[x[0]] = max(out[x[0]], x[3])
    return sorted(out.items(), key=lambda kv: -kv[1])


def refresh(state, infos, now, fetch, manual=(), log=print, per_tick=8):
    prof = state.setdefault("profiles", {})
    todo = [w for w, _ in candidates(infos, now) if now - prof.get(w, {}).get("at", 0) > REFRESH][:per_tick]
    for w in todo:
        try:
            prof[w] = dict(profile_of(fetch(w)), at=now)
        except Exception as e:
            log(f"  sniper profile {w[:10]} failed: {e}")
    for w in [w for w, v in prof.items() if now - v["at"] > 30 * 86400]:
        del prof[w]
    return len(todo)


def signals(state, infos, vtier_of, now, manual=()):
    prof = state.get("profiles", {})
    manual = {w.lower() for w in manual}
    out = []
    for i in infos:
        if i.get("venue") != "poly" or i.get("winner"):
            continue
        live = i["start"] <= now
        pos = (i.get("livepos") or []) if live else (i.get("allpos") or [])
        money, who = defaultdict(float), defaultdict(list)
        for x in pos:
            w = x[0]
            p = prof.get(w)
            hand = w in manual
            if not (hand or is_sniper(p)):
                continue
            money[x[2]] += x[3]
            who[x[2]].append(dict(wallet=w, name=x[5], usd=x[3], avg=x[4], profile=p, hand=hand))
        if not money:
            continue
        side = max(money, key=money.get)
        other = sum(v for o, v in money.items() if o != side)
        top = max(x["usd"] for x in who[side])
        vt = vtier_of(i["match"])
        reasons = []
        if vt == "T1":
            reasons.append("tier-1 match")
        if top < MIN_USD:
            reasons.append(f"biggest sniper position is ${top:,.0f} (needs ${MIN_USD:,})")
        if other >= 0.5 * money[side]:
            reasons.append("snipers are split between both sides")
        out.append(dict(match=i["match"], start=i["start"], market=i.get("market", "Match Winner"), cid=i.get("cid"),
                        slug=i.get("ref"), url=i.get("url"), teams=i.get("teams"), prices=i.get("prices") or {},
                        tokens=i.get("tokens") or {}, side=side, money=round(money[side]), other_money=round(other),
                        wallets=sorted(who[side], key=lambda x: -x["usd"]),
                        against=sorted((x for o, xs in who.items() if o != side for x in xs), key=lambda x: -x["usd"]),
                        vtier=vt, live=live, active=not reasons, why_not=reasons))
    return sorted(out, key=lambda s: (not s["active"], s["start"]))


def wallet_line(x, side):
    p = x.get("profile") or {}
    who = f"\"{x['name']}\"" if x.get("name") and not str(x["name"]).startswith("0x") else x["wallet"][:6] + "…" + x["wallet"][-4:]
    rec = (f"{p['n']} CS2 bets, won {p['won']:.0%} ({p['edge'] * 100:+.0f} pts over its prices), "
           f"{p['share']:.0%} of its bets are CS2, CS2 profit {I.money(p.get('cs2_pnl', 0))}") if p.get("n") else "no history read yet"
    return f"{who}{' (your list)' if x.get('hand') else ''}: {I.money(x['usd'])} on {side} at {x['avg']:.2f}. Record: {rec}."


def explain(b, now):
    other = next((t for t in b.get("teams") or [] if t != b["side"]), "the other side")
    when = "during the match" if b.get("placed_at", 0) >= b["start"] else "before kickoff"
    head = (f"{I.money(b['stake'])} on {b['side']}" + (" to beat " + other if b["market"] == "Match Winner" else f" ({b['market']})")
            + f", copying sniper money ({I.money(b['money'])}) {when}.")
    why = ["A sniper is a wallet with only 3-40 CS2 bets in its whole history that won 60%+ of them, beat its prices "
           "by 5+ points and bets mostly CS2: a few big, well-aimed bets, like the wallets shyux calls out.",
           "In the 60-day walk-forward test, copying them made +22.9% over 124 bets (both halves positive)."]
    if b["status"] != "skipped":
        why.append(f"Bet {I.money(b['stake'])} at {b['price']:.3f}, filled against the real order book, fee included. "
                   f"If {b['side']} wins it pays {I.money(b['shares'])}.")
    res = {"won": f"{b['side']} won: paid {I.money(b.get('shares', 0))}, profit {I.money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {I.money(b['stake'])} stake was lost.",
           "skipped": "Not placed: no one was selling on the order book.", "open": "Not settled yet."}[b["status"]]
    return dict(headline=head, sections=[
        dict(title="The snipers", bullets=[wallet_line(x, b["side"]) for x in b.get("wallets", [])[:6]] or ["-"]),
        dict(title="Why this bet", bullets=why),
        dict(title="Why it could be wrong", bullets=[
            "A wallet with 3-40 bets can look brilliant by luck; the test says the group keeps winning, not each one.",
            "We copy up to 10 minutes late; in-play the price may already have moved."]),
        dict(title="Result", bullets=[res])])


def dashboard(state, sigs, now):
    view = I.account_view(state, sigs, now, explain, "SNIPER", ("T2", "T2*", "T3", "T4", "T5", "T6"),
                          ("1 sniper", "2+ snipers"))
    prof = state.get("profiles", {})
    snipers = sorted(({"wallet": w, **p} for w, p in prof.items() if is_sniper(p)), key=lambda x: -x["edge"])
    return dict(view, snipers=snipers[:60], checked=len(prof),
                rules=dict(n=[MIN_N, MAX_N], won=MIN_WON, edge=MIN_EDGE, share=MIN_SHARE, usd=MIN_USD, stake=I.STAKE))
