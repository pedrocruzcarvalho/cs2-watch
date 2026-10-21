"""
Live big money: a second paper account that follows big bets placed DURING low-tier CS2 matches.

Why this and not the pre-match "fishy bet" flags (fishy_research.py, live_research.py, 60 days, Oct 2026):
  - Before kickoff, big bets only pick the favourite: following them did no better than backing the favourite at
    the same moment, and big bets on underdogs lost 15-23%. No pre-match setting held up on days it hadn't seen
    (of the settings positive on the first 30 days, 30% stayed positive on the next 30).
  - In-play, one wallet putting $3k+ on one side of a T3-T6 market did beat the price: +16.7% over 140 bets copied
    10 minutes later (+21.7% and +11.0% in the two halves), +11.4% copied after 1 minute, while backing the
    favourite at those same moments returned -6.4%. Positive at every size from $1.5k to $5k, 6 of 9 weeks.
    Most of it comes from T3; T4-T6 on their own were slightly negative. The rule keeps T3-T6 as defined before
    the tier split was looked at.

Rules (fixed on 2026-10-20 before any live result):
  signal   during a T3-T6 match (Valve ranking), one wallet's net in-play position on one side of a market reaches
           $4,000 (raised from $3,000 by Pedro on Oct 21 2026; at $4k the test gave +18.9% over 83 bets, halves +17/+22),
           and big wallets ($2,000+) have less than half of that on the other side
  bet      $20 on that side at the next 10-minute scan, filled against the real order book (fee included), one bet
           per market, skipped if the side already costs 0.97+; $300 paper bankroll.
State: "account" (bets). Stored in KV as state:livemoney.
"""

from collections import defaultdict

import informed as I

MIN_WALLET = 4000                                     # $3,000 until Oct 21 2026 (Pedro raised it)
OTHER_MIN = 2000
LIVE_HOURS = 5
LOW_TIERS = {"T2*", "T3", "T4", "T5", "T6"}          # T2* = tier 2 with a fix-prone team


def signals(infos, vtier_of, now):
    """Markets in play where some wallet has $1k+ in-play. Returns every such market (signal or not)."""
    out = []
    for i in infos:
        if i.get("venue") != "poly" or not i["start"] <= now < i["start"] + LIVE_HOURS * 3600 or i.get("winner"):
            continue
        pos = i.get("livepos") or []
        if not pos:
            continue
        top = max(pos, key=lambda x: x[3])
        side = top[2]
        backers = sorted((x for x in pos if x[2] == side), key=lambda x: -x[3])
        against = sorted((x for x in pos if x[2] != side and x[3] >= OTHER_MIN), key=lambda x: -x[3])
        other = sum(x[3] for x in against)
        vt = vtier_of(i["match"])
        reasons = []
        if vt not in LOW_TIERS:
            reasons.append(f"match tier {vt} (only T3-T6, or T2 with a fix-prone team)")
        if top[3] < MIN_WALLET:
            reasons.append(f"biggest in-play position is ${top[3]:,.0f} (needs ${MIN_WALLET:,})")
        if other >= 0.5 * top[3]:
            reasons.append(f"${other:,.0f} of big money on the other side")
        card = lambda x: dict(wallet=x[0], name=x[5], usd=x[3], avg=x[4], at=x[6])
        out.append(dict(match=i["match"], start=i["start"], market=i.get("market", "Match Winner"), cid=i.get("cid"),
                        slug=i.get("ref"), url=i.get("url"), teams=i.get("teams"), prices=i.get("prices") or {},
                        tokens=i.get("tokens") or {}, side=side, money=round(top[3]), other_money=round(other),
                        wallets=[card(x) for x in backers[:6]], against=[card(x) for x in against[:6]],
                        vtier=vt, active=not reasons, why_not=reasons))
    return sorted(out, key=lambda s: (not s["active"], -s["money"]))


def wallet_line(x, side):
    who = f"\"{x['name']}\"" if x.get("name") and not x["name"].startswith("0x") else x["wallet"][:6] + "…" + x["wallet"][-4:]
    return f"{who} has {I.money(x['usd'])} on {side} bought during the match, at an average {x['avg']:.2f}."


def explain(b, now):
    other = next((t for t in b.get("teams") or [] if t != b["side"]), "the other side")
    mins = (b.get("placed_at", b["start"]) - b["start"]) / 60
    head = (f"{I.money(b['stake'])} on {b['side']}" + (" to beat " + other if b["market"] == "Match Winner" else f" ({b['market']})")
            + f", because one wallet put {I.money(b['money'])} on it during the match.")
    secs = [dict(title="The money", bullets=[wallet_line(x, b["side"]) for x in b["wallets"][:6]] or ["-"])]
    why = [f"Rule: on a T3-T6 match, one wallet with $4,000+ on one side bought in-play, and less than half of that "
           f"from big wallets on the other side. This was {I.money(b['money'])} vs {I.money(b.get('other_money', 0))}, "
           f"on a {b.get('vtier')} match.",
           "In 60 days of past matches, copying these in-play bets 10 minutes later returned +16.7% over 140 bets, while "
           "backing the favourite at the same moments lost 6%. Before kickoff, big bets showed no edge at all."]
    if b["status"] != "skipped":
        why.append(f"Copied {mins:.0f} min into the match: {I.money(b['stake'])} at an average {b['price']:.3f} "
                   f"({b['price'] * 100:.0f}% implied), filled against the real order book, fee included. "
                   f"If {b['side']} wins it pays {I.money(b['shares'])}.")
    secs.append(dict(title="Why this bet", bullets=why))
    secs.append(dict(title="Why it could be wrong", bullets=[
        "In-play money often just follows the score: someone watching the stream sees a round or map swing before "
        "the price does. That is still information, but it fades fast, and we copy up to 10 minutes late.",
        "The past test is 140 bets and most of the edge came from T3 matches; it may be smaller than it looked."]))
    res = {"won": f"{b['side']} won: paid {I.money(b.get('shares', 0))}, profit {I.money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {I.money(b['stake'])} stake was lost.",
           "skipped": f"Not placed: {'no one was selling on the order book' if b.get('reason') == 'no sellers' else 'the balance could not cover it'}.",
           "open": "Not settled yet."}[b["status"]]
    secs.append(dict(title="Result", bullets=[res]))
    return dict(headline=head, sections=secs)


def dashboard(state, sigs, now):
    view = I.account_view(state, sigs, now, explain, "LIVE", ("T2*", "T3", "T4", "T5", "T6"),
                          ("1 big wallet", "2+ big wallets"))
    return dict(view, rules=dict(min_wallet=MIN_WALLET, other_min=OTHER_MIN, stake=I.STAKE, start=I.START_BALANCE))
