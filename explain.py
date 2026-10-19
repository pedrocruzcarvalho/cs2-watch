"""
Plain-English explanation of one bet, from templates (no LLM). Used for both placed bets and upcoming ones.

explain(...) returns {"headline": str, "sections": [{"title": str, "bullets": [str, ...]}, ...]}.
Everything comes from the evidence snapshot taken when the bet was placed, so a past bet explains what
was known *then*, not what the flags look like now.
"""

from datetime import datetime, timezone

from cs2_watch import market_kind

KIND_CONTEXT = {
    "map": "This is a single-map market. Fixing one map is easier and cheaper than fixing a match: a team only "
           "has to lose that map, and it can still win the series.",
    "maps": "This is a total-maps or map-handicap market. These can be fixed without changing who wins (for "
            "example by dropping one map), which is why fixers like them.",
    "rounds": "This is a round-level market (total rounds or round handicap on one map). These markets are tiny, "
              "so a big bet here is very visible, and throwing rounds doesn't require losing the match.",
}

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


def adjust_text(a, side):
    """One adjustment to the level, in plain words."""
    if a["kind"] == "bookies":
        if a["effect"] == "up":
            return (f"+1 level, bookmakers agree: at {a['book']}, {side}'s chance of winning went from "
                    f"{a['start'] * 100:.0f}% to {a['now'] * 100:.0f}% since {hhmm(a['since'])}, a rise of "
                    f"{a['move'] * 100:.0f} percentage points (bookmaker margin removed). "
                    f"Bookmakers take sharp money; when their price moves the same way as the flagged Polymarket "
                    f"money, the information looks like it is spreading beyond one wallet.")
        return (f"-1 level, bookmakers disagree: at {a['book']}, {side}'s chance went from {a['start'] * 100:.0f}% "
                f"to {a['now'] * 100:.0f}% since {hhmm(a['since'])}, the opposite way to the flagged money.")
    if a["kind"] == "line":
        return (f"+1 level, line move: on Polymarket the price of {side} climbed from {a['low']:.2f} to "
                f"{a['now']:.2f} since {hhmm(a['since'])} ({a['move'] * 100:.0f} cents), more than the 12 cents in "
                f"6 hours that counts as a strong move. Money spread over many accounts moves the price even when "
                f"no single wallet stands out.")
    if a["kind"] == "volume":
        return (f"Unusual volume (doesn't change the level): {money(a['flow'])} is already on this match "
                f"{a['hours_left']:.0f} h before kickoff, about {a['ratio']:.0f}x what a {a['vtier']} match normally "
                f"has by then (a whole typical {a['vtier']} match gets {money(a['median'])} before kickoff). In the "
                f"30-day backtest this added nothing to the level, so it only decides timing: at 10x or more, a "
                f"MEDIUM/HIGH flag is bet straight away.")
    if a["kind"] == "throw":
        if a["effect"] == "drop":
            return (f"Flag dropped: backing {side} means {a['team']} would have to lose on purpose, and {a['team']} is "
                    f"#{a['rank']} in Valve's ranking, inside the top 40 and not on the list of teams that could still "
                    f"fix. Established teams have salaries, sponsors and careers at stake, so no money is bet.")
        return (f"-1 level: {a['team']} would have to lose on purpose and is #{a['rank']} in Valve's ranking "
                f"({a['tier']}), a team with something to lose, so the flag counts for less.")
    if a["kind"] == "conflict":
        return (f"-1 level, money on both sides: {money(a['other_money'])} of flagged money also went on "
                f"{a['other']}, which muddies the signal.")
    return ""


def cause_text(c, side):
    """Why a step escalated, for the 'Why this stake' bullets."""
    if not c:
        return ""
    if c["kind"] in ("window", "early"):
        return ""
    if c["kind"] == "bets":
        return (f"The flagged bets themselves got stronger: on their own they went from {c.get('prev') or 'nothing'} to "
                f"{c.get('base')}, from new or bigger suspicious positions.")
    if c["kind"] == "unknown":
        return "The flagged money got stronger (this step was recorded before the cause was saved with each step)."
    if c["kind"].startswith("cleared_"):
        return "A reason that had lowered the level no longer applies (for example the money on the other side faded)."
    return "It rose because of this: " + adjust_text(c, side)


def flag_sentence(f, start):
    share = f.get("share") or 0
    where = "of all the money traded on this match"
    timing = " before kickoff" if f.get("pre_match") else " during the match"
    if f.get("watched"):
        lead = f"Watched wallet ({f['watched']}). "
    else:
        lead = ""
    if f.get("cluster"):
        names = ", ".join(f"{x['name']} ({money(x['usd'])})" for x in f.get("members", [])[:5])
        s = (f"On Polymarket, {f['cluster']} separate wallets, all brand-new or with almost no history, put a "
             f"combined {money(f['usd'])} on {f['outcome']} at an average {f['avg_price']:.2f}{timing}, "
             f"{share:.0%} {where}. Each piece is small on its own; splitting one big bet across fresh wallets is a "
             f"known way to hide it. Wallets: {names}.")
        return s + f" ({f['level']}, {f['pts']} pts)"
    if f.get("venue") == "kalshi":
        s = (f"An anonymous order on Kalshi bought {money(f['usd'])} of {f['outcome']} at {f['avg_price']:.2f}{timing}, "
             f"{share:.0%} {where}.")
    else:
        rec = f.get("record")
        rec_txt = (f" Track record: {rec['text']}. Verdict: {rec['label']} ({rec['why']})." if rec else "")
        if f.get("hours_left") and f["hours_left"] > 0 and abs(f.get("share_now", share) - share) > 0.02:
            where_txt = (f"about {share:.0%} of the {money(f.get('expected_flow') or 0)} this match is expected to have by "
                         f"kickoff ({f['share_now']:.0%} of what's traded so far; money on these matches mostly "
                         f"arrives in the last hours, and kickoff was {f['hours_left']:.0f} h away)")
        else:
            where_txt = f"{share:.0%} {where}"
        s = lead + (f"On Polymarket, {describe_wallet(f, start)} put {money(f['usd'])} on {f['outcome']} at "
                    f"{f['avg_price']:.2f}{timing}. That is {where_txt}.")
    extra = []
    if (f.get("x_p90") or 0) >= 25:
        extra.append(f"about {f['x_p90']:.0f}x bigger than a typical large trade on this match")
    if (f.get("move") or 0) >= 0.10:
        extra.append(f"the price then moved {f['move'] * 100:.0f}c in that direction")
    if any("also hit on" in w for w in f.get("why", [])):
        extra.append("the same side was also hit hard on the other venue")
    if extra:
        s += " Also: " + "; ".join(extra) + "."
    if f.get("venue") != "kalshi" and not f.get("cluster"):
        s += rec_txt
    return s + f" ({f['level']}, {f['pts']} pts)"


def explain(side, teams, start, path, tranches, snap, *, escalation, stakes, status="open", winner=None, pnl=0.0,
            stake=0.0, shares=0.0, flipped=False, now=None, market="Match Winner"):
    snap = snap or {}
    kind = market_kind(market)
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
    what = (f"{side} to beat {other}" if kind == "match" else
            f"{side} to win {market.replace(' Winner', '')}" if kind == "map" else f"{side} in {market}")
    if stake:
        head = f"{money(stake)} on {what}" + (f" at an average {avg:.2f}" if avg else "") + f", because {because}."
    else:
        head = f"Flagged: {what}, because {because}."

    sections = []
    if snap.get("base_level"):
        lvl_b = [f"The bets alone make this {snap['base_level']} ({snap.get('base_pts', '?')} points, from the "
                 f"strongest flagged position below)."]
        for a in snap.get("adjust") or []:
            lvl_b.append(adjust_text(a, side))
        if len(lvl_b) > 1:
            final = path[-1][0] if path else snap["base_level"]
            lvl_b.append(f"Level now: {final}.")
        sections.append(dict(title="Why this level", bullets=lvl_b))
    why_flagged = [flag_sentence(f, start) for f in mine[:4]] or [
        "The flagged-money details weren't captured for this bet, and the match is no longer in the scan window."]
    if snap.get("backfilled_at"):
        why_flagged.append(f"Note: this bet was placed before evidence was saved with each bet, so this is the "
                           f"evidence as the scanner saw it at {hhmm(snap['backfilled_at'])}, not at the moment "
                           f"of the bet.")
    sections.append(dict(title="Why it was flagged", bullets=why_flagged))

    stake_b = []
    total = 0.0
    for i, t in enumerate(tranches):
        implied = f"{t['price'] * 100:.0f}% implied"
        if t.get("skipped"):
            why_skip = (f"nobody was selling {side} on the order book at that moment" if t.get("reason") == "no sellers"
                        else "the balance couldn't cover it")
            stake_b.append(f"{hhmm(t['t'])}: the plan wanted to add {money(stakes.get(t['level'], 0))} "
                           f"({t['level']}) but {why_skip}, so it was skipped.")
            continue
        if t.get("book"):
            note = (f" Filled against the real order book: average {t['price']:.3f} across {t.get('levels', 1)} "
                    f"price level{'s' if t.get('levels', 1) != 1 else ''}, fee included.")
            if t.get("wanted") and t["stake"] < t["wanted"] - 0.01:
                note += f" Only {money(t['stake'])} of the {money(t['wanted'])} could be filled: not enough sellers."
        else:
            note = ""
        total += t["stake"]
        if (t.get("cause") or {}).get("kind") == "early":
            c, v = t["cause"], t["cause"].get("volume") or {}
            stake_b.append(f"{hhmm(t['t'])}: bet placed early, without waiting for the last 30 minutes, because this "
                           f"{c['level']} flag came with unusual volume: {money(v.get('flow', 0))} already traded "
                           f"{v.get('hours_left', 0):.0f} h before kickoff, about {v.get('ratio', 0):.0f}x what a "
                           f"{v.get('vtier', '')} match normally has by then. {money(t['stake'])} went on {side} at "
                           f"{t['price']:.2f} ({implied})." + note)
        elif (t.get("cause") or {}).get("kind") == "window":
            c = t["cause"]
            why_now = (f"The flag first appeared as {c['first']} and had risen to {c['level']}, so it gets "
                       f"{escalation}x the normal {c['level']} stake ({money(stakes[c['level']])})."
                       if c.get("rose") else f"{c['level']} = {money(stakes[c['level']])} in the plan.")
            stake_b.append(f"{hhmm(t['t'])}: bet placed in the last 30 minutes before kickoff, at the level the match "
                           f"had then. Money on these matches mostly arrives in the final hour, so the flag is judged "
                           f"on the near-final picture. {money(t['stake'])} went on {side} at {t['price']:.2f} "
                           f"({implied}). {why_now}" + note)
        elif t["level"] == "LIVE":
            stake_b.append(f"{hhmm(t['t'])}: during the match, a bet passed the strict LIVE test, so the plan "
                           f"{'added' if i else 'placed'} {money(t['stake'])} on {side} at {t['price']:.2f} "
                           f"({implied}){', bringing the total to ' + money(total) if i else ''}. The price is the "
                           f"one at that scan; live prices move fast, so it can be up to 10 minutes old." + note)
        elif i == 0:
            stake_b.append(f"{hhmm(t['t'])}: first flagged {t['level']}, so {money(t['stake'])} went on {side} at "
                           f"{t['price']:.2f} ({implied}). {t['level']} = {money(stakes[t['level']])} in the plan." + note)
        else:
            stake_b.append(f"{hhmm(t['t'])}: escalated to {t['level']}. {cause_text(t.get('cause'), side)} So the plan "
                           f"topped up {money(t['stake'])} at {t['price']:.2f} ({implied}), bringing the total to "
                           f"{money(total)}: an escalated market gets {escalation}x the normal {t['level']} stake "
                           f"({money(stakes[t['level']])})." + note)
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
    if kind in KIND_CONTEXT:
        ctx.append(KIND_CONTEXT[kind])
    sh, books = snap.get("sharp"), snap.get("books") or {}
    if snap.get("adjust") is not None and sh:
        pass                                                           # already explained in "Why this level"
    elif sh and sh.get("agree"):
        ctx.append(f"Bookmakers agree: at {sh['book']} the chance of {side} rose from {sh['start'] * 100:.0f}% to "
                   f"{sh['now'] * 100:.0f}% since {hhmm(sh['since'])}. Bookmakers moving the same way as the flagged "
                   f"money is strong corroboration, so the level went up one step (from {sh['raised_from']}).")
    elif sh:
        ctx.append(f"Bookmakers disagree: at {sh['book']} the chance of {side} fell from {sh['start'] * 100:.0f}% to "
                   f"{sh['now'] * 100:.0f}% since {hhmm(sh['since'])}, against the flagged money, so the level went "
                   f"down one step (from {sh['lowered_from']}).")
    elif books:
        v = next(iter(sorted(books.values(), key=lambda v: v["book"] != "Pinnacle")))
        if abs(v["move"]) < 0.02:
            ctx.append(f"At {v['book']}, {side} is at {v['now'] * 100:.0f}% and has barely moved over the last 12 "
                       f"hours: the wider betting market isn't reacting to this money.")
        else:
            ctx.append(f"At {v['book']}, {side} is at {v['now'] * 100:.0f}% and moved {v['move'] * 100:+.0f} points over "
                       f"the last 12 hours, short of the 6-point bar that would change the level.")
    st = snap.get("steam")
    if st and snap.get("adjust") is None:
        ctx.append(f"Line move: the price of {side} climbed from {st['low']:.2f} to {st['now']:.2f} since "
                   f"{hhmm(st['since'])} ({st['move'] * 100:.0f}c). Money spread over many accounts moves the price "
                   f"even when no single wallet stands out, so the level was raised from {st['raised_from']} one step.")
    if snap.get("live"):
        ctx.append("LIVE flags are much stricter than pre-match ones, because big in-play bets on the score are "
                   "normal. Every one of these had to hold: placed during the match; $3,000+ and 25%+ of all money "
                   "on the match; a wallet under 7 days old or with 5 or fewer markets ever (Kalshi: one $15,000+ "
                   "order worth 50%+ of the match); price still open (0.10 to 0.85); not a tier-1 match.")
    ranks = {k: v for k, v in (snap.get("ranks") or {}).items()}
    if ranks:
        ctx.append("Valve ranking: " + ", ".join(
            f"{k} #{v}" if v else f"{k} unranked" for k, v in ranks.items()) +
            (f" (match tier {snap.get('vtier')}, from the stronger team)." if snap.get("vtier") else "."))
    tier = snap.get("tier")
    if tier == "low":
        ctx.append("Lower-tier match: thin market where one bettor can dominate, and where most fixing cases happen.")
    elif tier == "mid":
        ctx.append("One well-known team is playing, so bigger bets are more normal here. The flag had to clear "
                   "a higher bar.")
    if avg is not None and kind in ("match", "map"):
        if avg < 0.5:
            ctx.append(f"{side} was the underdog ({avg * 100:.0f}% implied): the flagged money is betting on an "
                       f"upset, the classic fix pattern.")
        else:
            ctx.append(f"{side} was already favoured ({avg * 100:.0f}% implied). The flag means the money is "
                       f"unusually confident, not that it expects an upset.")
    m = snap.get("money") or {}
    if m:
        ctx.append(f"Flagged money in this market: {money(m.get(side, 0))} on {side} vs "
                   f"{money(sum(v for k, v in m.items() if k != side))} on {other}, out of "
                   f"{money(snap.get('flow', 0))} traded in it.")
    elif avg is not None and kind not in ("match", "map"):
        ctx.append(f"{side} was priced at {avg:.2f} ({avg * 100:.0f}% implied) when the bet went in.")
    if (snap.get("conflict") or against) and not snap.get("live"):
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
