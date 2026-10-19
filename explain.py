"""
Plain-English explanation of one bet, from templates (no LLM). Used for both placed bets and upcoming ones.

explain(...) returns {"headline": str, "sections": [{"title": str, "bullets": [str, ...]}, ...]}.
Everything comes from the evidence snapshot taken when the bet was placed, so a past bet explains what
was known *then*, not what the flags look like now.
"""

from datetime import datetime, timezone

LEVEL_WORD = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}


def money(v):
    return f"${v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"${v:,.2f}"


def hhmm(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M UTC")


def span(seconds):
    d = seconds / 86400
    if d < 1:
        h = max(seconds / 3600, 0)
        return "less than an hour" if h < 1 else f"{h:.0f} hours"
    if d < 60:
        return f"{d:.0f} day{'s' if round(d) != 1 else ''}"
    return f"{d / 30:.0f} months"


def age_adj(seconds):
    """'3-day-old', '5-hour-old', '2-month-old'."""
    word = span(seconds)
    if word == "less than an hour":
        return "brand-new"
    n, unit = word.split()
    return f"{n}-{unit.rstrip('s')}-old"


def describe_wallet(f, start):
    """'wallet "x" (brand-new: 2 days old at kickoff; only 3 markets ever traded)' and similar."""
    w = f.get("wallet_info") or {}
    bits = []
    if w.get("created"):
        age = start - w["created"]
        if age < 7 * 86400:
            bits.append(f"brand-new: {span(age)} old at kickoff")
        elif age < 30 * 86400:
            bits.append(f"young: {span(age)} old")
        else:
            bits.append(f"{span(age)} old")
    traded = w.get("traded")
    if traded is not None:
        if traded <= 5:
            bits.append(f"only {traded} market{'s' if traded != 1 else ''} ever traded")
        elif traded <= 20:
            bits.append(f"just {traded} markets ever traded")
        elif traded >= 500:
            bits.append(f"a very active trader with {traded:,} markets, more likely a pro than an insider")
    if w.get("first_trade") and 0 <= f.get("first", 0) - w["first_trade"] < 2 * 86400:
        bits.append("its first trade ever was in the 48 hours before this bet")
    who = f"wallet \"{f['name']}\"" if f.get("name") and not str(f["name"]).startswith("0x") else "a wallet"
    return who + (f" ({'; '.join(bits)})" if bits else "")


def flag_sentence(f, start):
    share = f.get("share") or 0
    where = "of all the money traded on this match"
    timing = " before kickoff" if f.get("pre_match") else ""
    if f.get("venue") == "kalshi":
        s = (f"An anonymous order on Kalshi bought {money(f['usd'])} of {f['outcome']} at {f['avg_price']:.2f}{timing}, "
             f"{share:.0%} {where}.")
    else:
        s = (f"On Polymarket, {describe_wallet(f, start)} put {money(f['usd'])} on {f['outcome']} at "
             f"{f['avg_price']:.2f}{timing}. That is {share:.0%} {where}.")
    extra = []
    if (f.get("x_p90") or 0) >= 25:
        extra.append(f"about {f['x_p90']:.0f}x bigger than a typical large trade on this match")
    if (f.get("move") or 0) >= 0.10:
        extra.append(f"the price then moved {f['move'] * 100:.0f}c in that direction")
    if any("also hit on" in w for w in f.get("why", [])):
        extra.append("the same side was also hit hard on the other venue")
    if extra:
        s += " Also: " + "; ".join(extra) + "."
    return s + f" ({f['level']}, {f['pts']} pts)"


def explain(side, teams, start, path, tranches, snap, *, escalation, stakes, status="open", winner=None, pnl=0.0,
            stake=0.0, shares=0.0, flipped=False, now=None):
    snap = snap or {}
    flags = snap.get("flags") or []
    other = next((t for t in teams if t != side), "the other team")
    mine = [f for f in flags if f.get("outcome") == side] or flags
    against = [f for f in flags if f.get("outcome") == other]
    paid = [t for t in tranches if t.get("stake")]
    avg = sum(t["price"] * t["stake"] for t in paid) / stake if stake and paid else (tranches[0]["price"] if tranches
                                                                                      else None)

    # headline
    lead = mine[0] if mine else None
    if lead and lead.get("venue") == "poly":
        w = lead.get("wallet_info") or {}
        young = w.get("created") and start - w["created"] < 30 * 86400
        because = (f"{'a ' + age_adj(start - w['created']) if young else 'one'} wallet put "
                   f"{money(lead['usd'])} on them ({lead['share']:.0%} of the match's money)")
    elif lead:
        because = f"a {money(lead['usd'])} anonymous Kalshi order backed them ({lead['share']:.0%} of the match's money)"
    else:
        because = "flagged money backed them"
    if stake:
        head = f"{money(stake)} on {side} to beat {other}" + (f" at an average {avg:.2f}" if avg else "") + \
               f", because {because}."
    else:
        head = f"Flagged: {side} vs {other}, because {because}."

    sections = []
    sections.append(dict(title="Why it was flagged", bullets=[flag_sentence(f, start) for f in mine[:4]] or [
        "The flagged-money details weren't captured for this bet."]))

    stake_b = []
    total = 0.0
    for i, t in enumerate(tranches):
        implied = f"{t['price'] * 100:.0f}% implied"
        if t.get("skipped"):
            stake_b.append(f"{hhmm(t['t'])}: the plan wanted to add {money(stakes.get(t['level'], 0))} "
                           f"({t['level']}) but the balance couldn't cover it, so it was skipped.")
            continue
        total += t["stake"]
        if i == 0:
            stake_b.append(f"{hhmm(t['t'])}: first flagged {t['level']}, so {money(t['stake'])} went on {side} at "
                           f"{t['price']:.2f} ({implied}). {t['level']} = {money(stakes[t['level']])} in the plan.")
        else:
            stake_b.append(f"{hhmm(t['t'])}: escalated to {t['level']}, so the plan topped up {money(t['stake'])} at "
                           f"{t['price']:.2f} ({implied}), bringing the total to {money(total)}. Escalations get "
                           f"{escalation}x the normal {t['level']} stake ({money(stakes[t['level']])}), because a "
                           f"flag that keeps getting stronger is the signal this plan bets on.")
    if not tranches:
        stake_b.append("No money is in yet. It goes in on the next scan if the flag still stands.")
    if len(path) > 1:
        stake_b.append("Level path: " + " → ".join(f"{lvl} ({hhmm(t)})" for lvl, t in path) + ".")
    if flipped:
        stake_b.append(f"Later the flagged money switched to {other}. The plan stopped adding to this bet "
                       f"(it can't take money back).")
    if stake and status == "open":
        stake_b.append(f"If {side} wins, it pays back {money(shares)} (profit {money(shares - stake)}). "
                       f"If not, the {money(stake)} is lost.")
    sections.append(dict(title="Why this stake", bullets=stake_b))

    ctx = []
    tier = snap.get("tier")
    if tier == "low":
        ctx.append("Lower-tier match: thin market where one bettor can dominate, and where most fixing cases happen.")
    elif tier == "mid":
        ctx.append("One well-known team is playing, so bigger bets are more normal here. The flag had to clear "
                   "a higher bar.")
    if avg is not None:
        if avg < 0.5:
            ctx.append(f"{side} was the underdog ({avg * 100:.0f}% implied): the flagged money is betting on an "
                       f"upset, the classic fix pattern.")
        else:
            ctx.append(f"{side} was already favoured ({avg * 100:.0f}% implied). The flag means the money is "
                       f"unusually confident, not that it expects an upset.")
    m = snap.get("money") or {}
    if m:
        ctx.append(f"Flagged money: {money(m.get(side, 0))} on {side} vs {money(sum(v for k, v in m.items() if k != side))} "
                   f"on {other}, out of {money(snap.get('flow', 0))} traded on the match.")
    if snap.get("conflict") or against:
        ctx.append(f"There was also suspicious money on {other}, which weakens the signal (the level was lowered "
                   f"one step for that).")
    sections.append(dict(title="Context", bullets=ctx))

    sections.append(dict(title="Why it could be wrong", bullets=[
        "A flag is evidence, not proof. Sharp bettors, people with legit early news (stand-ins, illness, "
        "visa problems) and market makers can leave the same footprint.",
        "Over the 30-day backtest, flagged sides returned only about +2 to +4% per bet, which is not "
        "statistically proven. Treat every bet here as part of the experiment."]))

    if status == "won":
        sections.append(dict(title="Result", bullets=[f"{side} won. The bet paid {money(shares)}: "
                                                       f"profit {money(pnl)}."]))
    elif status == "lost":
        sections.append(dict(title="Result", bullets=[f"{winner} won. The {money(stake)} stake was lost."]))
    elif status == "void":
        sections.append(dict(title="Result", bullets=["The result couldn't be matched to a team, so the stake "
                                                       "was refunded."]))
    elif status == "skipped":
        sections.append(dict(title="Result", bullets=["No money went in: the balance couldn't cover it."]))
    elif now is not None:
        sections.append(dict(title="Result", bullets=[f"Not settled yet: the match {'starts in ' + span(start - now) if start > now else 'has started; result pending'}."]))
    return dict(headline=head, sections=sections)
